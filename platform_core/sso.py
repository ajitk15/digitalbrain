"""Sign in with Microsoft Entra ID: OpenID Connect, one tenant, nothing granted.

The authorization code flow with PKCE, as a confidential client. The browser is
sent to Microsoft with a fresh `state`, `nonce` and code challenge kept in this
server's session; Microsoft sends it back with a code, which this server trades
for an ID token over the hardened fetcher. The token is accepted only when
every one of these holds:

- signed RS256 by a key the tenant publishes, fetched from Microsoft and never
  taken from the token itself;
- issued by the configured tenant (`iss` and `tid`) for this application
  (`aud`), unexpired, carrying the `nonce` this browser was given;
- the `state` returned matches, and the pending sign-in is used once and is at
  most ten minutes old.

**Who it signs in.** A Microsoft account is matched by its tenant and object
id (`oid`), which Entra never reuses or renames. The first sign-in links it to
the platform user whose username or email equals the token's sign-in name,
unless that user is already linked to another account or is a platform
administrator - an operator links those with `manage.py link_sso`. Nobody
matches: refused, or, with `entra_create_users`, a new account is made.

**What it grants: nothing.** Signing in is authentication only. A new account
belongs to no organization and holds no application grant, so deny by default
reads exactly as it does for a password account. Entra groups and app roles are
not read; access is still given on the Access screens, where it is audited.

Tenant pinning is what makes matching on a name acceptable: `load_config`
refuses `common` and `organizations`, so only the configured directory - whose
administrator decides every sign-in name in it - can present one.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets as tokens
import time
import urllib.parse

import jwt
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView as PasswordLoginView
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET

from digitalbrain.configuration import GUID, read_secret

from .fetching import FetchError, fetch
from .models import SignInIdentity, User
from .services import audit

logger = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com"
SCOPE = "openid profile"
SECRET_NAME = "entra_client_secret"
BACKEND = "django.contrib.auth.backends.ModelBackend"
SESSION_KEY = "sso_pending"
#: How long a started sign-in may wait for Microsoft to send the browser back.
PENDING_SECONDS = 600
#: Clock skew tolerated on exp, iat and nbf.
LEEWAY = 120
#: Signing keys are kept a day; an unknown key id fetches them again, at most
#: once every five minutes, so a forged kid cannot make every sign-in a fetch.
KEYS_SECONDS = 24 * 3600
KEYS_RETRY_SECONDS = 300

#: tenant -> (fetched at, {kid: public key}).
_keys = {}

INVALID = "Microsoft's answer could not be verified, so you were not signed in. Try again."
RETRY = "That sign-in had expired or was not started here. Try again."
UNREACHABLE = (
    "Microsoft could not complete the sign-in. Try again, or tell your administrator "
    "if it keeps happening."
)


class SignInRefused(Exception):
    """A reason to show the person, and a fixed label to log.

    Never carries anything from the request or the token: Microsoft's own
    `error_description` arrives in a URL anybody can craft, so it is not shown.
    """

    def __init__(self, message, kind):
        super().__init__(message)
        self.kind = kind


def enabled():
    return bool(settings.ENTRA_TENANT and settings.ENTRA_CLIENT_ID)


def issuer():
    return f"{AUTHORITY}/{settings.ENTRA_TENANT}/v2.0"


def redirect_uri(request):
    """The address registered in Entra as this application's redirect URI."""
    return request.build_absolute_uri(reverse("sso-callback"))


class LoginView(PasswordLoginView):
    """Django's password sign-in, with a Microsoft button when one is configured."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["sso_enabled"] = enabled()
        return context


def _safe_next(request, target):
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return ""


def _challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@require_GET
def start(request):
    if not enabled():
        raise Http404
    pending = {
        "state": tokens.token_urlsafe(32),
        "nonce": tokens.token_urlsafe(32),
        "verifier": tokens.token_urlsafe(64),
        "next": _safe_next(request, request.GET.get("next", "")),
        "at": time.time(),
    }
    request.session[SESSION_KEY] = pending
    query = urllib.parse.urlencode(
        {
            "client_id": settings.ENTRA_CLIENT_ID,
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": redirect_uri(request),
            "scope": SCOPE,
            "state": pending["state"],
            "nonce": pending["nonce"],
            "code_challenge": _challenge(pending["verifier"]),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return redirect(f"{AUTHORITY}/{settings.ENTRA_TENANT}/oauth2/v2.0/authorize?{query}")


@require_GET
def callback(request):
    if not enabled():
        raise Http404
    # Popped before anything is checked: a pending sign-in is spent by its
    # first answer, right or wrong, so a code cannot be replayed into it.
    pending = request.session.pop(SESSION_KEY, None)
    try:
        user = complete(request, pending)
    except SignInRefused as refusal:
        logger.warning("sso", extra={"event": "sso.refused", "failure_kind": refusal.kind})
        messages.error(request, str(refusal))
        return redirect("login")
    login(request, user, backend=BACKEND)
    return redirect(_safe_next(request, pending.get("next", "")) or settings.LOGIN_REDIRECT_URL)


def complete(request, pending):
    """The signed-in user for this callback, or SignInRefused."""
    error = request.GET.get("error", "")
    if error:
        if error == "access_denied":
            raise SignInRefused(
                "Microsoft did not sign you in: it was cancelled, or your directory has "
                "not allowed you to use this application.",
                "access_denied",
            )
        raise SignInRefused(UNREACHABLE, "provider_error")
    if not isinstance(pending, dict) or time.time() - pending.get("at", 0) > PENDING_SECONDS:
        raise SignInRefused(RETRY, "no_pending")
    if not hmac.compare_digest(request.GET.get("state", ""), pending["state"]):
        raise SignInRefused(RETRY, "state")
    code = request.GET.get("code", "")
    if not code:
        raise SignInRefused(UNREACHABLE, "no_code")
    claims = verify(exchange(request, code, pending["verifier"]), pending["nonce"])
    return account_for(claims)


@sensitive_variables("secret", "body")
def exchange(request, code, verifier):
    """Trade the code for an ID token. The access token is not kept."""
    try:
        secret = read_secret(settings.SECRET_DIRECTORY, SECRET_NAME)
    except ImproperlyConfigured:
        raise SignInRefused(
            "Signing in with Microsoft is not completely set up on this server. "
            "Tell your administrator.",
            "no_secret",
        ) from None
    body = urllib.parse.urlencode(
        {
            "client_id": settings.ENTRA_CLIENT_ID,
            "client_secret": secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(request),
            "code_verifier": verifier,
            "scope": SCOPE,
        }
    ).encode()
    try:
        data, _, _, _ = fetch(
            f"{AUTHORITY}/{settings.ENTRA_TENANT}/oauth2/v2.0/token",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        payload = json.loads(data)
    except (FetchError, ValueError):
        raise SignInRefused(UNREACHABLE, "token_request") from None
    token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise SignInRefused(UNREACHABLE, "no_id_token")
    return token


def verify(token, nonce):
    """The token's claims, once every check in the module docstring has passed."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise SignInRefused(INVALID, "malformed") from None
    # Fixed, not read from the token: "none" and HS256 signed with a public key
    # are the classic ways to make a forged token verify.
    if header.get("alg") != "RS256":
        raise SignInRefused(INVALID, "algorithm")
    key = signing_key(header.get("kid"))
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=settings.ENTRA_CLIENT_ID,
            issuer=issuer(),
            leeway=LEEWAY,
            options={"require": ["exp", "iat", "aud", "iss", "sub", "tid", "oid", "nonce"]},
        )
    except jwt.PyJWTError:
        raise SignInRefused(INVALID, "claims") from None
    if not hmac.compare_digest(str(claims["nonce"]), nonce):
        raise SignInRefused(INVALID, "nonce")
    if str(claims["tid"]).lower() != settings.ENTRA_TENANT:
        raise SignInRefused(INVALID, "tenant")
    if not GUID.fullmatch(str(claims["oid"])):
        raise SignInRefused(INVALID, "subject")
    return claims


def signing_key(kid):
    if not isinstance(kid, str) or not kid:
        raise SignInRefused(INVALID, "key_id")
    tenant = settings.ENTRA_TENANT
    fetched, keys = _keys.get(tenant, (0.0, {}))
    now = time.time()
    if now - fetched > KEYS_SECONDS or (kid not in keys and now - fetched > KEYS_RETRY_SECONDS):
        keys = _load_keys(tenant)
        _keys[tenant] = (now, keys)
    if kid not in keys:
        raise SignInRefused(INVALID, "unknown_key")
    return keys[kid]


def _load_keys(tenant):
    try:
        data, _, _, _ = fetch(
            f"{AUTHORITY}/{tenant}/discovery/v2.0/keys", headers={"Accept": "application/json"}
        )
        found = jwt.PyJWKSet.from_dict(json.loads(data))
    except (FetchError, ValueError, jwt.PyJWTError):
        raise SignInRefused(UNREACHABLE, "keys") from None
    return {key.key_id: key.key for key in found.keys if key.key_id and key.key_type == "RSA"}


def forget_keys():
    """Drop cached signing keys - for tests, and after a tenant change."""
    _keys.clear()


def account_for(claims):
    tenant = settings.ENTRA_TENANT
    subject = str(claims["oid"]).lower()
    name = claims.get("preferred_username")
    name = name.strip().lower()[:254] if isinstance(name, str) else ""
    try:
        with transaction.atomic():
            identity = (
                SignInIdentity.objects.select_related("user")
                .filter(tenant=tenant, subject=subject)
                .first()
            )
            method = None
            if identity is None:
                user, method = _link_or_create(tenant, name, claims)
                identity = SignInIdentity(user=user, tenant=tenant, subject=subject, method=method)
            if not identity.user.is_active:
                raise SignInRefused(
                    "This account has been deactivated. Ask your administrator.", "inactive"
                )
            identity.name = name
            identity.last_signed_in_at = timezone.now()
            identity.save()
            if method:
                audit(
                    identity.user,
                    "sso_user_created" if method == "created" else "sso_linked",
                    identity.user.pk,
                    details={"provider": "entra", "tenant": tenant, "method": method},
                )
    except IntegrityError:
        # Two first sign-ins racing for one account: one won, so try again.
        raise SignInRefused(RETRY, "race") from None
    return identity.user


def _link_or_create(tenant, name, claims):
    if not name:
        raise SignInRefused(
            "Microsoft did not say who you are. Ask your administrator to check that this "
            "application is given the profile scope.",
            "no_name",
        )
    matches = list(User.objects.filter(Q(username__iexact=name) | Q(email__iexact=name))[:2])
    if len(matches) > 1:
        raise SignInRefused(
            "More than one account here matches your Microsoft sign-in name. Ask an "
            "administrator to link yours.",
            "ambiguous",
        )
    if matches:
        user = matches[0]
        if user.is_platform_admin:
            # The strongest account is never handed to whoever the directory
            # names: an operator links it, once, by object id.
            raise SignInRefused(
                "A platform administrator's account is linked to Microsoft by an operator "
                "first. Sign in with your password.",
                "admin_by_name",
            )
        if SignInIdentity.objects.filter(user=user, tenant=tenant).exists():
            raise SignInRefused(
                "Your account here is already linked to a different Microsoft account. "
                "Ask an administrator.",
                "linked_elsewhere",
            )
        return user, "name"
    if not settings.ENTRA_CREATE_USERS:
        raise SignInRefused(
            "There is no account here for your Microsoft sign-in yet. Ask your "
            "administrator to create one.",
            "unknown",
        )
    if len(name) > User._meta.get_field("username").max_length:
        raise SignInRefused(
            "Your Microsoft sign-in name is too long for an account here. Ask your "
            "administrator to create one.",
            "name_length",
        )

    def text(claim):
        value = claims.get(claim)
        return value.strip()[:150] if isinstance(value, str) else ""

    user = User(
        username=name,
        email=name if "@" in name else "",
        first_name=text("given_name"),
        last_name=text("family_name"),
    )
    # Signs in only through Microsoft; nothing a password form posts matches it.
    user.set_unusable_password()
    user.save()
    return user, "created"
