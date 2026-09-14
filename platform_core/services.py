"""Transactional writes for administration and immutable AI usage receipts."""

import hashlib
from contextvars import ContextVar
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import (
    AIUsage,
    Application,
    ApplicationFeature,
    ApplicationGrant,
    AuditEvent,
    Branding,
    FeatureSwitch,
    OrganizationMember,
)
from .observability import request_id_context
from .policy import application_for, require_platform_admin

FEATURES = {
    "document_uploads": ("Document uploads", not settings.PRODUCTION),
    "usage_reports": ("AI cost reports", True),
    "chat": ("Knowledge chat", True),
    "knowledge": ("Knowledge sources", True),
    "code_factory": ("Code Factory plans", True),
    "connectors": ("External connectors", True),
}


def available_features():
    """(key, label) for every feature that can actually be switched on here.

    Iterating the registry is what makes the create form extensible: adding a
    line to FEATURES puts a new checkbox on it and a new row on the Features
    screen, with nothing else to change. Entries whose availability flag is
    False - document_uploads in production - are not offered at all, because
    all_features would refuse them anyway.
    """
    return [(key, label) for key, (label, available) in FEATURES.items() if available]


def audit(user, action, resource, organization=None, details=None):
    return AuditEvent.objects.create(
        actor=user,
        action=action,
        resource_id=str(resource),
        organization=organization,
        details=details or {},
        request_id=request_id_context.get(),
    )


#: Feature answers memoised for the life of one request.
#:
#: feature_enabled runs two queries and is asked the same questions repeatedly
#: while a page renders - the navigation, the view and several template tags all
#: consult it - which put 16 of a documents page's 28 queries into feature
#: lookups alone. The cache is per request, so a switch still takes effect on the
#: very next one and the "re-checked on every request" rule is untouched. Outside
#: a request (the worker, a shell, a streaming thread) the ContextVar default of
#: None applies and every call reads the database, which is the safe direction.
_feature_cache = ContextVar("feature_cache", default=None)


def begin_feature_cache():
    """Start a fresh memo. Returns a token for reset, mirroring request_id."""
    return _feature_cache.set({})


def end_feature_cache(token):
    _feature_cache.reset(token)


def all_features(application):
    """Every feature answer for one application, in two queries rather than 2n.

    Both tables are small and bounded by FEATURES, so reading them whole costs
    less than asking six separate questions - which is what a page render did.
    """
    switches = {row.key: row.enabled for row in FeatureSwitch.objects.all()}
    local = (
        {
            row.key: row.enabled
            for row in ApplicationFeature.objects.filter(application=application)
        }
        if application is not None
        else {}
    )
    return {
        name: available and switches.get(name, True) and local.get(name, True)
        for name, (_, available) in FEATURES.items()
    }


def feature_enabled(key, application):
    if key not in FEATURES or not FEATURES[key][1]:
        return False
    cache = _feature_cache.get()
    if cache is None:
        # No request scope: read straight through, which is the safe direction.
        global_flag = FeatureSwitch.objects.filter(key=key).first()
        local_flag = ApplicationFeature.objects.filter(application=application, key=key).first()
        return (global_flag is None or global_flag.enabled) and (
            local_flag is None or local_flag.enabled
        )
    app_id = getattr(application, "pk", None)
    answers = cache.get(app_id)
    if answers is None:
        answers = cache[app_id] = all_features(application)
    return answers[key]


@transaction.atomic
def update_branding(user, png):
    require_platform_admin(user)
    branding, _ = Branding.objects.update_or_create(
        pk=1, defaults={"png": png, "digest": hashlib.sha256(png).hexdigest(), "updated_by": user}
    )
    audit(user, "branding.updated", branding.digest)
    return branding


@transaction.atomic
def change_grant(actor, application_id, target, role, can_approve=False, revoke=False):
    # Serialize owner changes, preventing concurrent requests from removing every owner.
    if role not in ApplicationGrant.Role.values:
        raise ValidationError("Invalid application role.")
    Application.objects.select_for_update().get(pk=application_id)
    app, actor_grant = application_for(actor, application_id, owner=True)
    if not OrganizationMember.objects.filter(
        organization_id=app.organization_id, user=target, user__is_active=True
    ).exists():
        raise ValidationError("User must be an active member of this organization.")
    existing = ApplicationGrant.objects.filter(application=app, user=target).first()
    if can_approve and not actor_grant.can_approve:
        raise PermissionDenied("You cannot delegate approval rights you do not hold.")
    if existing and existing.role == "owner" and (revoke or role != "owner"):
        owners = ApplicationGrant.objects.filter(
            application=app,
            role="owner",
            user__is_active=True,
            user__organizationmember__organization_id=app.organization_id,
        )
        if owners.count() <= 1:
            raise ValidationError("Keep at least one active application owner.")
    if revoke:
        if existing:
            existing.delete()
        action = "application.access_revoked"
    else:
        ApplicationGrant.objects.update_or_create(
            application=app, user=target, defaults={"role": role, "can_approve": can_approve}
        )
        action = "application.access_changed"
    audit(
        actor,
        action,
        target.pk,
        app.product.portfolio.organization,
        details={"application_id": str(app.pk), "role": role, "can_approve": can_approve},
    )


@transaction.atomic
def record_ai_usage(
    *,
    actor,
    application_id,
    provider,
    model,
    request_id,
    input_tokens,
    output_tokens,
    amount,
    currency="USD",
    estimated=False,
    purpose="chat",
):
    """Server-side provider adapter boundary; never accepts a browser-supplied bill.

    Called after receiving provider usage. No prompts, outputs, keys or source text are stored.
    Idempotent provider request IDs must have identical accounting on replay.
    Costs are recorded even when the reporting feature is disabled.
    """
    app, _ = application_for(actor, application_id)
    from decimal import InvalidOperation

    try:
        amount = Decimal(str(amount))
    except InvalidOperation:
        raise ValidationError("Cost must be a decimal amount.") from None
    if not amount.is_finite() or amount < 0:
        raise ValidationError("Cost must be finite and nonnegative.")
    if amount != amount.quantize(Decimal("0.00000001")):
        raise ValidationError("Cost supports at most 8 decimal places.")
    if any(type(value) is not int or value < 0 for value in [input_tokens, output_tokens]):
        raise ValidationError("Token counts must be nonnegative integers.")
    if len(currency) != 3 or not currency.isascii() or not currency.isupper():
        raise ValidationError("Currency must be a three-letter uppercase code.")
    values = dict(
        actor=actor,
        purpose=purpose,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        amount=amount,
        currency=currency,
        estimated=estimated,
    )
    candidate = AIUsage(application=app, request_id=request_id, **values)
    candidate.full_clean(validate_unique=False, validate_constraints=False)
    receipt, created = AIUsage.objects.get_or_create(
        application=app,
        provider=provider,
        request_id=request_id,
        defaults={key: value for key, value in values.items() if key != "provider"},
    )
    if not created and any(getattr(receipt, key) != value for key, value in values.items()):
        raise ValidationError("Conflicting accounting for an existing provider request.")
    if created:
        audit(actor, "ai.usage_recorded", receipt.pk, app.product.portfolio.organization)
    return receipt
