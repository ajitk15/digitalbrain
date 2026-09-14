"""Bearer-token authentication for the machine-facing endpoints.

The contract, and the reason this file is short: a token is not a new kind of
principal. It names a user and an application, and `authenticate` returns those
two so the caller can run exactly the access checks a browser request runs. There
is no token role, no token-specific permission table, and nothing here decides
what a caller may read - `workbench.access` still does.

Consequences worth keeping true:

* Revoking the user's grant, disabling the account or turning off a feature
  switch disables the token immediately, because the check happens per request.
* A token for application A cannot be used on application B: the application is
  bound into the token, and the URL's application must match it.
* Session cookies are never accepted on these endpoints and tokens are never
  accepted on the browser ones. Mixing them is how a token endpoint becomes
  CSRF-able through an authenticated browser.

Secrets are stored only as a SHA-256 digest. The public prefix locates the row;
the digest is then compared in constant time.
"""

import hashlib
import hmac
import secrets
import uuid

from django.utils import timezone

PREFIX = "dbk"
PREFIX_BYTES = 6
SECRET_BYTES = 24

#: A token is not a licence to hammer the graph.
RATE_LIMIT = 120
RATE_WINDOW = 60


class ApiError(Exception):
    """An error safe to return to an API caller."""

    def __init__(self, message, status=400, code="invalid_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def issue():
    """A new token: (public prefix, full secret, digest to store).

    The full secret exists only in this return value and in the response that
    shows it once. It is never written to the database or to a log.
    """
    prefix = f"{PREFIX}_{secrets.token_hex(PREFIX_BYTES)}"
    secret = secrets.token_urlsafe(SECRET_BYTES)
    full = f"{prefix}_{secret}"
    return prefix, full, digest_of(full)


def digest_of(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def presented(request):
    """The bearer token on this request, if one was presented."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def resolve_application(reference):
    """The application an address names, by slug or by id, or None.

    Both forms are accepted so the readable address can be adopted without
    breaking a token, configuration or script that already carries a UUID.
    """
    from .models import Application

    value = str(reference or "").strip()
    if not value:
        return None
    application = Application.objects.filter(slug=value).first()
    if application is not None:
        return application
    try:
        uuid.UUID(value)
    except ValueError:
        return None
    return Application.objects.filter(pk=value).first()


def authenticate(request, reference):
    """Resolve a bearer token to (user, token, application) for one application.

    Raises ApiError with a status the caller can return directly. The messages
    deliberately do not distinguish "no such token" from "wrong application" -
    both are simply unauthorized.

    The address is resolved only after the token has been checked, so an address
    that names nothing is answered exactly like a token that is not valid. A
    readable slug would otherwise tell an unauthenticated caller which
    applications exist, which the opaque id never did.
    """
    from .models import ApiToken

    value = presented(request)
    if not value:
        raise ApiError(
            "Provide an API token as 'Authorization: Bearer <token>'.",
            status=401,
            code="unauthenticated",
        )
    prefix = "_".join(value.split("_")[:2])
    token = ApiToken.objects.filter(prefix=prefix).select_related("user").first()
    # Hash regardless of whether a row was found, so a missing prefix and a wrong
    # secret cost the same.
    candidate = digest_of(value)
    if token is None or not hmac.compare_digest(candidate, token.digest):
        raise ApiError("That API token is not valid.", status=401, code="unauthenticated")
    if not token.active:
        raise ApiError(
            f"That API token is {token.state.lower()}.", status=401, code="unauthenticated"
        )
    application = resolve_application(reference)
    if application is None or token.application_id != application.pk:
        raise ApiError("That API token is not valid.", status=401, code="unauthenticated")
    if not token.user.is_active:
        raise ApiError("That API token is not valid.", status=401, code="unauthenticated")
    throttle(token)
    ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return token.user, token, application


_recent = {}


def throttle(token):
    """A small fixed-window limit per token, held in process memory.

    Process-local like the streaming registry, which is correct for the single
    managed worker process this runs in; several worker processes would each
    allow the window, so a shared counter would be needed before scaling out.
    """
    now = timezone.now().timestamp()
    window_start, count = _recent.get(token.pk, (now, 0))
    if now - window_start >= RATE_WINDOW:
        window_start, count = now, 0
    count += 1
    _recent[token.pk] = (window_start, count)
    if count > RATE_LIMIT:
        raise ApiError(
            f"Rate limit exceeded: {RATE_LIMIT} requests per {RATE_WINDOW} seconds.",
            status=429,
            code="rate_limited",
        )


def reset_throttle():
    _recent.clear()
