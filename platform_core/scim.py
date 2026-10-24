"""Account provisioning from the directory over SCIM 2.0: creates, never grants.

Microsoft Entra ID's provisioning service calls this to keep platform accounts
in step with the directory: a person assigned to the enterprise application
gets an account, a rename or new email is copied over, and a person unassigned
or deleted is deactivated the same hour rather than whenever someone remembers.
Entra *sign-in* (`sso.py`) then links the account by sign-in name on first use,
as it does for an account an administrator made by hand.

**What it does not do is the point.** An account it makes has no password, no
organization membership and no application grant; access is still given on the
Access screens, where it is audited, so the directory cannot widen anyone's
access - `sso.py`'s rule, kept. Groups are not accepted. It never deletes a
user (the audit trail and receipts name them): removal is deactivation, which
signs them out, stops their API tokens and their connector schedules.

**Which accounts it may touch** (`ProvisionedUser`): those it created, and an
existing account it matched by sign-in name - exactly one, username or email -
which is audited `account.directory_linked` when it happens. **Never a platform
administrator**: not matched, not created over, not changed. An operator manages
those, as `link_sso` does for sign-in.

Off unless `scim_provisioning = true`, and then answered only to a bearer token
equal to the mounted `scim_token` (compared in constant time; at least
`MIN_TOKEN` characters). Bearer only - a session cookie means nothing here, so
the endpoint cannot be driven cross-site. Turned off, every address is a 404.

Entra's dialect is accepted as it is sent: an `op` in any case, `"False"` for
false, a path such as `emails[type eq "work"].value`, a value with no path, and
attributes this platform does not keep (title, department, manager...), which
are ignored. Changes are audited as `account.provisioned`, `.updated`,
`.deprovisioned` and `.reprovisioned`, by the non-login `PROVISIONER` account,
with field names and never values.
"""

import hmac
import json
import logging
import re
import uuid

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from digitalbrain.configuration import read_secret

from .api_auth import presented
from .models import ApplicationGrant, OrganizationMember, ProvisionedUser, User
from .services import audit

logger = logging.getLogger(__name__)

SECRET_NAME = "scim_token"
#: Shorter than this and the token is refused as unconfigured: it is the only
#: thing between the internet and creating accounts.
MIN_TOKEN = 32
#: The account audit events from provisioning are recorded as. It cannot sign
#: in: inactive, with no usable password.
PROVISIONER = "entra-provisioning"

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
CONTENT_TYPE = "application/scim+json"

#: `attribute eq "value"`, the one filter Entra sends.
FILTER = re.compile(r'^\s*(userName|externalId)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)
#: Attributes kept, by lower-cased SCIM path.
KEPT = {
    "username": "username",
    "active": "is_active",
    "name.givenname": "first_name",
    "name.familyname": "last_name",
    "externalid": "external_id",
    "emails": "email",
    'emails[type eq "work"].value': "email",
}


class ScimError(Exception):
    def __init__(self, status, detail, scim_type=""):
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


def respond(payload, status=200, headers=None):
    response = JsonResponse(payload, status=status, content_type=CONTENT_TYPE)
    for name, value in (headers or {}).items():
        response[name] = value
    return response


def failure(error):
    payload = {"schemas": [ERROR_SCHEMA], "status": str(error.status), "detail": error.detail}
    if error.scim_type:
        payload["scimType"] = error.scim_type
    return respond(payload, status=error.status)


def authenticate(request):
    """Raise unless SCIM is on and the bearer token is the mounted one."""
    if not settings.SCIM_PROVISIONING:
        raise Http404
    try:
        expected = read_secret(settings.SECRET_DIRECTORY, SECRET_NAME)
    except ImproperlyConfigured:
        expected = ""
    if len(expected) < MIN_TOKEN:
        logger.error("scim_unconfigured", extra={"event": "scim_unconfigured"})
        raise ScimError(503, "Provisioning is switched on but its token is not mounted.")
    value = presented(request)
    # Compared whatever was presented, so a missing token costs the same.
    if not hmac.compare_digest(value.encode(), expected.encode()) or not value:
        logger.warning("scim_refused", extra={"event": "scim_refused"})
        raise ScimError(401, "That provisioning token is not valid.")


def provisioner():
    """The non-login account provisioning's audit events are recorded as."""
    user, created = User.objects.get_or_create(
        username=PROVISIONER, defaults={"is_active": False, "first_name": "Entra provisioning"}
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    if user.is_active or user.is_platform_admin:
        # Somebody made a real account with this name; recording the directory's
        # changes as them would be a lie in the audit trail.
        raise ScimError(500, f"The account '{PROVISIONER}' is in use by a person.")
    return user


def resource(request, user):
    link = user.provisioning
    email = [{"value": user.email, "type": "work", "primary": True}] if user.email else []
    display = f"{user.first_name} {user.last_name}".strip() or user.username
    return {
        "schemas": [USER_SCHEMA],
        "id": str(user.pk),
        "externalId": link.external_id,
        "userName": user.username,
        "active": user.is_active,
        "displayName": display,
        "name": {"givenName": user.first_name, "familyName": user.last_name},
        "emails": email,
        "meta": {
            "resourceType": "User",
            "created": link.created_at.isoformat(),
            "lastModified": link.updated_at.isoformat(),
            "location": request.build_absolute_uri(f"{prefix(request)}/Users/{user.pk}"),
        },
    }


def prefix(request):
    return request.path.split("/Users", 1)[0]


def managed():
    """Users the directory may touch: linked to it, and never an administrator."""
    return User.objects.filter(provisioning__isnull=False, is_platform_admin=False).exclude(
        username=PROVISIONER
    )


def managed_user(pk):
    """A managed user by the id this endpoint gave out; anything else is not found."""
    try:
        pk = uuid.UUID(str(pk))
    except ValueError:
        raise ScimError(404, "No such user.") from None
    user = managed().select_related("provisioning").filter(pk=pk).first()
    if user is None:
        raise ScimError(404, "No such user.")
    return user


def text(value):
    return "" if value is None else str(value)


def truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ScimError(400, "active must be true or false.", "invalidValue")


def email_of(value):
    """The work or primary address of a SCIM `emails` value, or the string itself."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chosen = next(
            (item for item in value if isinstance(item, dict) and item.get("primary")), None
        ) or next(
            (item for item in value if isinstance(item, dict) and item.get("type") == "work"),
            None,
        ) or next((item for item in value if isinstance(item, dict)), None)
        return (chosen or {}).get("value", "")
    return ""


def changes_from(body):
    """{field: value} from a POSTed user, keeping only what this platform holds."""
    found = {}
    if "userName" in body:
        found["username"] = text(body["userName"])
    if "active" in body:
        found["is_active"] = truthy(body["active"])
    if "externalId" in body:
        found["external_id"] = text(body["externalId"])[:255]
    name = body.get("name") if isinstance(body.get("name"), dict) else {}
    if "givenName" in name:
        found["first_name"] = text(name["givenName"])[:150]
    if "familyName" in name:
        found["last_name"] = text(name["familyName"])[:150]
    if "emails" in body:
        found["email"] = text(email_of(body["emails"]))[:254]
    return found


def changes_from_patch(body):
    if not isinstance(body, dict) or not isinstance(body.get("Operations"), list):
        raise ScimError(400, "Send a PatchOp with Operations.", "invalidSyntax")
    found = {}
    for operation in body["Operations"]:
        if not isinstance(operation, dict):
            raise ScimError(400, "Each operation must be an object.", "invalidSyntax")
        op = str(operation.get("op", "")).lower()
        if op not in {"add", "replace", "remove"}:
            raise ScimError(400, f"Unsupported operation: {op or 'none'}.", "invalidSyntax")
        path = operation.get("path")
        value = operation.get("value")
        if not path:
            if op == "remove" or not isinstance(value, dict):
                raise ScimError(400, "An operation without a path needs an object.", "noTarget")
            # Entra writes nested names as dotted keys here: "name.givenName".
            nested = {}
            for key, item in value.items():
                head, _, tail = key.partition(".")
                if tail:
                    nested.setdefault(head, {})[tail] = item
                else:
                    nested[key] = item
            found.update(changes_from(nested))
            continue
        field = KEPT.get(path.lower())
        if field is None:
            continue
        if op == "remove":
            if field in {"username", "is_active"}:
                raise ScimError(400, f"{path} cannot be removed.", "mutability")
            found[field] = ""
        elif field == "is_active":
            found[field] = truthy(value)
        elif field == "email":
            found[field] = text(email_of(value))[:254]
        else:
            found[field] = text(value)[:255 if field == "external_id" else 150]
    return found


def check_username(value, user=None):
    if not isinstance(value, str) or not value.strip():
        raise ScimError(400, "userName is required.", "invalidValue")
    value = value.strip()
    if len(value) > User._meta.get_field("username").max_length:
        raise ScimError(400, "userName is too long.", "invalidValue")
    clash = User.objects.filter(username__iexact=value)
    if user is not None:
        clash = clash.exclude(pk=user.pk)
    if clash.exists():
        raise ScimError(
            409,
            "An account with this userName already exists and is not managed by the "
            "directory. Platform administrators are managed by an operator.",
            "uniqueness",
        )
    return value


def orphaned(user):
    """What a deactivation leaves without an active administrator or owner."""
    organizations = [
        str(member.organization_id)
        for member in OrganizationMember.objects.filter(user=user, is_admin=True)
        if not OrganizationMember.objects.filter(
            organization_id=member.organization_id, is_admin=True, user__is_active=True
        )
        .exclude(user=user)
        .exists()
    ]
    applications = [
        str(grant.application_id)
        for grant in ApplicationGrant.objects.filter(user=user, role="owner")
        if not ApplicationGrant.objects.filter(
            application_id=grant.application_id, role="owner", user__is_active=True
        )
        .exclude(user=user)
        .exists()
    ]
    return {"organizations": organizations, "applications": applications}


def apply(user, found, actor):
    """Write `found` onto a managed user, audited. Returns the user."""
    changed = []
    link = user.provisioning
    if "username" in found:
        found["username"] = check_username(found["username"], user)
    for field, value in found.items():
        target = link if field == "external_id" else user
        if getattr(target, field) != value:
            setattr(target, field, value)
            changed.append(field)
    if not changed:
        return user
    user.save()
    link.save()
    if "is_active" in changed:
        if user.is_active:
            audit(actor, "account.reprovisioned", user.pk)
        else:
            audit(actor, "account.deprovisioned", user.pk, details=orphaned(user))
    rest = [field for field in changed if field != "is_active"]
    if rest:
        audit(actor, "account.updated", user.pk, details={"fields": sorted(rest)})
    return user


def match(value):
    """A managed user for a sign-in name, linking an existing account if exactly one fits."""
    found = managed().filter(username__iexact=value).first()
    if found is not None:
        return found
    candidates = list(
        User.objects.filter(provisioning__isnull=True).filter(
            Q(username__iexact=value) | Q(email__iexact=value)
        )[:2]
    )
    if len(candidates) != 1:
        return None
    user = candidates[0]
    if user.is_platform_admin or user.username == PROVISIONER:
        return None
    ProvisionedUser.objects.create(user=user)
    audit(provisioner(), "account.directory_linked", user.pk)
    return User.objects.select_related("provisioning").get(pk=user.pk)


def body_of(request):
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise ScimError(400, "Send a JSON object.", "invalidSyntax") from None
    if not isinstance(body, dict):
        raise ScimError(400, "Send a JSON object.", "invalidSyntax")
    return body


def listing(request, users):
    try:
        start = max(int(request.GET.get("startIndex", 1)), 1)
        count = max(min(int(request.GET.get("count", 100)), 100), 0)
    except ValueError:
        raise ScimError(400, "startIndex and count must be numbers.", "invalidValue") from None
    page = users[start - 1 : start - 1 + count]
    return respond(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": len(users),
            "startIndex": start,
            "itemsPerPage": len(page),
            "Resources": [resource(request, user) for user in page],
        }
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def users(request):
    try:
        authenticate(request)
        if request.method == "GET":
            text = request.GET.get("filter", "")
            if not text:
                return listing(
                    request, list(managed().select_related("provisioning").order_by("username"))
                )
            parsed = FILTER.match(text)
            if parsed is None:
                raise ScimError(400, "Only userName eq and externalId eq are supported.",
                                "invalidFilter")
            attribute, value = parsed.group(1).lower(), parsed.group(2).replace('\\"', '"')
            with transaction.atomic():
                if attribute == "username":
                    found = match(value)
                else:
                    found = managed().filter(provisioning__external_id=value).first()
            return listing(request, [found] if found else [])
        body = body_of(request)
        found = changes_from(body)
        with transaction.atomic():
            actor = provisioner()
            user = User(
                username=check_username(found.get("username")),
                first_name=found.get("first_name", ""),
                last_name=found.get("last_name", ""),
                email=found.get("email", ""),
                is_active=found.get("is_active", True),
            )
            user.set_unusable_password()
            try:
                user.save()
            except IntegrityError:
                raise ScimError(409, "An account with this userName already exists.",
                                "uniqueness") from None
            ProvisionedUser.objects.create(user=user, external_id=found.get("external_id", ""))
            audit(actor, "account.provisioned", user.pk)
            user = User.objects.select_related("provisioning").get(pk=user.pk)
        payload = resource(request, user)
        return respond(payload, status=201, headers={"Location": payload["meta"]["location"]})
    except ScimError as error:
        return failure(error)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
def user(request, pk):
    try:
        authenticate(request)
        if request.method == "GET":
            return respond(resource(request, managed_user(pk)))
        with transaction.atomic():
            target = managed_user(pk)
            actor = provisioner()
            if request.method == "DELETE":
                # Deleted in the directory: deactivated here, never deleted, and
                # no longer the directory's - a person re-added later is matched
                # again by name, which is audited again.
                apply(target, {"is_active": False}, actor)
                ProvisionedUser.objects.filter(user=target).delete()
                audit(actor, "account.directory_unlinked", target.pk)
                return HttpResponse(status=204)
            apply(target, changes_from_patch(body_of(request)), actor)
        return respond(resource(request, managed_user(pk)))
    except ScimError as error:
        return failure(error)


@csrf_exempt
def unsupported(request, rest=""):
    """Groups and anything else: refused, so the directory cannot grant by group."""
    try:
        authenticate(request)
        raise ScimError(
            404,
            "Only Users are provisioned here. Groups are not accepted: access is given "
            "on the platform's Access screens.",
        )
    except ScimError as error:
        return failure(error)
