"""An authenticator app as an optional second step after the password.

Optional per person and never forced: the user ID and password sign-in stays
the way in that works without anything else, and a person who wants more adds
an app on their Authenticator screen. Microsoft sign-in skips this step - the
directory runs its own.

**The codes** are RFC 6238 time-based one-time passwords: HMAC-SHA1, 30-second
steps, six digits, one step either side for clock drift - what every
authenticator app produces. A code is accepted once: `last_step` remembers the
newest step used, and a code for that step or an earlier one is refused.

**The seed** is the shared secret both sides compute codes from, so it is
sealed (`seal`) before it is stored: Fernet, under a key derived with HKDF
from the mounted Django secret key (and `SECRET_KEY_FALLBACKS` - the mounted
`django_secret_key_previous` - to open, so a rotation keeps working seeds). A
copy of the database alone yields no codes. Replacing the secret key without
mounting the old one beside it makes every seed unreadable; those people sign
in with a recovery code, or an administrator removes the app.

**Recovery codes** are ten one-time codes shown once when the app is added or
when the person asks for new ones; only a SHA-256 digest of each is stored,
as an API token's is. They are high-entropy and random, so a plain digest is
enough.

**The sign-in step** (`LoginView.form_valid`, `sign_in_code`): a correct
password for someone with an app does not sign them in. It puts the account
in this browser's session for `PENDING_SECONDS`, and only a correct code
calls `login`. A wrong code counts as a failed sign-in for django-axes, so the
same lockout holds as for a wrong password, and `MAX_TRIES` (under the
lockout's five) sends the person back to the password first.

**Losing the phone.** A recovery code signs them in; failing that, a platform
administrator removes the app on the person's page (not for a platform
administrator, as with reset links), or the host runs
`manage.py remove_authenticator <user ID>`. Every change is audited.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

from axes.handlers.proxy import AxesProxyHandler
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, user_logged_in, user_login_failed
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .models import Authenticator, RecoveryCode, User
from .services import audit

ISSUER = "Digital Brain"
STEP_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1
SEED_BYTES = 20
RECOVERY_CODES = 10
#: A recovery code: two groups of five from an alphabet with no look-alikes.
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
RECOVERY_LENGTH = 10
#: How long a correct password waits for its code, and how many codes it may try.
PENDING_SECONDS = 300
MAX_TRIES = 3
PENDING_KEY = "second_step"
HKDF_INFO = b"digitalbrain.authenticator.seed"


def _key(secret):
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(
        secret.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def _sealer():
    keys = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return MultiFernet([_key(key) for key in keys])


def seal(seed):
    return _sealer().encrypt(seed).decode("ascii")


def unseal(sealed):
    try:
        return _sealer().decrypt(sealed.encode("ascii"))
    except InvalidToken:
        return None


def code_at(seed, step):
    digest = hmac.new(seed, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def current_step(now=None):
    return int((time.time() if now is None else now) // STEP_SECONDS)


def readable_key(seed):
    """The seed as the base32 text an app accepts when typed in."""
    return base64.b32encode(seed).decode("ascii").rstrip("=")


def provisioning_uri(user, seed):
    label = urllib.parse.quote(f"{ISSUER}:{user.username}")
    query = urllib.parse.urlencode(
        {"secret": readable_key(seed), "issuer": ISSUER, "digits": DIGITS, "period": STEP_SECONDS}
    )
    return f"otpauth://totp/{label}?{query}"


def enrolled(user):
    return Authenticator.objects.filter(user=user, confirmed_at__isnull=False).exists()


def _matching_step(authenticator, code):
    """The step `code` was made for, if it is new and within drift; else None."""
    code = "".join(code.split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    seed = unseal(authenticator.sealed_seed)
    if seed is None:
        return None
    now = current_step()
    for step in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 1):
        if step > authenticator.last_step and hmac.compare_digest(code_at(seed, step), code):
            return step
    return None


def _recovery_digest(code):
    normal = "".join(code.lower().split()).replace("-", "")
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()


@transaction.atomic
def start(user):
    """A new, unconfirmed seed for `user`. Returns the seed; refused once confirmed."""
    current = Authenticator.objects.select_for_update().filter(user=user).first()
    if current is not None and current.confirmed_at is not None:
        raise ValueError("An authenticator app is already added.")
    seed = secrets.token_bytes(SEED_BYTES)
    Authenticator.objects.update_or_create(
        user=user, defaults={"sealed_seed": seal(seed), "confirmed_at": None, "last_step": 0}
    )
    return seed


def pending_seed(user):
    """The unconfirmed seed being set up, or None."""
    current = Authenticator.objects.filter(user=user, confirmed_at__isnull=True).first()
    return unseal(current.sealed_seed) if current else None


@transaction.atomic
def issue_recovery_codes(user):
    """Ten new codes, replacing any before; returned once, stored as digests."""
    RecoveryCode.objects.filter(user=user).delete()
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    RecoveryCode.objects.bulk_create(
        RecoveryCode(user=user, digest=_recovery_digest(code)) for code in codes
    )
    return codes


@sensitive_variables("code")
@transaction.atomic
def confirm(user, code):
    """Turn the app on when `code` comes from it. Returns recovery codes, or None."""
    current = (
        Authenticator.objects.select_for_update()
        .filter(user=user, confirmed_at__isnull=True)
        .first()
    )
    step = _matching_step(current, code) if current else None
    if step is None:
        return None
    current.confirmed_at = timezone.now()
    current.last_step = step
    current.save(update_fields=["confirmed_at", "last_step"])
    codes = issue_recovery_codes(user)
    audit(user, "account.authenticator_added", user.pk)
    return codes


@sensitive_variables("code")
@transaction.atomic
def verify(user, code):
    """'app' or 'recovery' when `code` proves this person, else None. Spends it."""
    current = (
        Authenticator.objects.select_for_update()
        .filter(user=user, confirmed_at__isnull=False)
        .first()
    )
    if current is None:
        return None
    step = _matching_step(current, code)
    if step is not None:
        current.last_step = step
        current.save(update_fields=["last_step"])
        return "app"
    spent = RecoveryCode.objects.filter(
        user=user, digest=_recovery_digest(code), used_at__isnull=True
    ).update(used_at=timezone.now())
    if spent:
        audit(user, "account.recovery_code_used", user.pk, details={"left": codes_left(user)})
        return "recovery"
    return None


def codes_left(user):
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


@transaction.atomic
def remove(user, actor, reason):
    """Take the app and its recovery codes away; the password alone signs in again."""
    removed, _ = Authenticator.objects.filter(user=user).delete()
    RecoveryCode.objects.filter(user=user).delete()
    if removed:
        audit(actor, "account.authenticator_removed", user.pk, details={"reason": reason})
    return bool(removed)


# The sign-in step.


def hold_for_code(request, user, next_url):
    """Keep a correct password waiting for its code instead of signing in."""
    request.session[PENDING_KEY] = {
        "user": str(user.pk),
        "backend": user.backend,
        "next": next_url,
        "at": time.time(),
        "tries": 0,
    }


class CodeForm(forms.Form):
    code = forms.CharField(
        max_length=32,
        label="Code",
        help_text="The six digits your app shows now, or one of your recovery codes.",
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "autofocus": True}),
    )


def _back_to_password(request, text):
    request.session.pop(PENDING_KEY, None)
    messages.error(request, text)
    return redirect("login")


@sensitive_post_parameters("code")
@require_http_methods(["GET", "POST"])
def sign_in_code(request):
    pending = request.session.get(PENDING_KEY)
    if not isinstance(pending, dict) or time.time() - pending.get("at", 0) > PENDING_SECONDS:
        return _back_to_password(request, "Sign in again: the code was not entered in time.")
    user = User.objects.filter(pk=pending["user"], is_active=True).first()
    if user is None:
        return _back_to_password(request, "Sign in again.")
    credentials = {"username": user.username}
    if AxesProxyHandler.is_locked(request, credentials=credentials):
        return _back_to_password(request, "Too many failed sign-ins. Try again later.")
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        used = verify(user, form.cleaned_data["code"])
        if used:
            request.session.pop(PENDING_KEY, None)
            login(request, user, backend=pending["backend"])
            if used == "recovery":
                messages.warning(
                    request,
                    f"You signed in with a recovery code. {codes_left(user)} left - make new "
                    "ones on your Authenticator screen if that is running low.",
                )
            return redirect(pending["next"] or settings.LOGIN_REDIRECT_URL)
        pending["tries"] = pending.get("tries", 0) + 1
        request.session[PENDING_KEY] = pending
        user_login_failed.send(sender=__name__, credentials=credentials, request=request)
        if pending["tries"] >= MAX_TRIES:
            return _back_to_password(request, "Too many wrong codes. Sign in again.")
        form.add_error("code", "That code is not right, or has been used already.")
    return render(request, "registration/code.html", {"form": form})


def _forget_pending(sender, request, user, **kwargs):
    # Any completed sign-in - Microsoft's included - ends a half-finished one.
    if request is not None and hasattr(request, "session"):
        request.session.pop(PENDING_KEY, None)


user_logged_in.connect(_forget_pending, dispatch_uid="authenticator.forget_pending")


# The person's own screen.


@sensitive_post_parameters("code")
@login_required
@require_http_methods(["GET", "POST"])
def manage(request):
    user = request.user
    action = request.POST.get("action") if request.method == "POST" else None
    form = CodeForm(request.POST if action in {"confirm", "codes", "remove"} else None)
    context = {"form": form}
    if action == "start" and not enrolled(user):
        start(user)
    elif action == "confirm" and form.is_valid():
        codes = confirm(user, form.cleaned_data["code"])
        if codes is None:
            form.add_error("code", "That code is not right. Check the time on your phone.")
        else:
            messages.success(request, "Authenticator app added.")
            context["codes"] = codes
    elif action in {"codes", "remove"} and form.is_valid():
        if verify(user, form.cleaned_data["code"]) is None:
            form.add_error("code", "That code is not right, or has been used already.")
        elif action == "codes":
            context["codes"] = issue_recovery_codes(user)
            audit(user, "account.recovery_codes_issued", user.pk)
        else:
            remove(user, user, "removed by its owner")
            messages.success(request, "Authenticator app removed. Your password alone signs in.")
            return redirect("authenticator")
    elif action == "cancel":
        Authenticator.objects.filter(user=user, confirmed_at__isnull=True).delete()
        return redirect("authenticator")
    seed = None if enrolled(user) else pending_seed(user)
    context.update(
        {
            "enrolled": enrolled(user),
            "setting_up": seed is not None,
            "key": readable_key(seed) if seed else "",
            "uri": provisioning_uri(user, seed) if seed else "",
            "left": codes_left(user),
        }
    )
    return render(request, "authenticator.html", context)
