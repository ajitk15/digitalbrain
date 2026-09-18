"""Emptying an organization between demonstrations.

Nothing else in this platform deletes an application. Every record that matters
points at one with `on_delete=PROTECT` - audit events, AI spend, documents, runs
- because destroying an application would destroy the account of what was done
in it and what it cost. Disabling is the supported answer, and it is the right
one for a real deployment.

A demonstration instance is the case that argument does not cover. The same
story is told a dozen times, and each telling needs the application back in its
opening state: no sources, no graph, no runs. Disabling leaves the old one on
screen beside the new, and "CarePath Demo 7" in front of a customer is worse
than either.

So this exists, and it is fenced accordingly:

* `allow_demo_reset` in `config/local.toml`, off unless somebody writes it down,
  and **refused outright** by `load_config` under `mode = "production"` - the
  same treatment `claude_use_host_login` and `allow_self_approval` get.
* Platform administrators only.
* The organization's name has to be typed. A button that empties a workspace
  should cost more than a click that could be a misclick.
* The organization, its membership and every user account survive. What goes is
  what a demonstration fills: the portfolios, products and applications beneath
  it, and everything those own.

It is audited like anything else, and the audit rows for the deleted
applications go too - they name resources that no longer exist, and leaving
them behind would fill the log with references nobody can follow.
"""

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import (
    AIConfiguration,
    AIUsage,
    ApiToken,
    Application,
    ApplicationFeature,
    ApplicationGrant,
    AuditEvent,
    ChangePlan,
    ChatConversation,
    ChatMessage,
    CodeFile,
    CodeRelationship,
    CodeRepository,
    CodeSnapshot,
    Connector,
    Document,
    FactoryRun,
    GraphRevision,
    KnowledgeEntry,
    KnowledgeGraph,
    KnowledgeSource,
    ManagedCredential,
    PlanItem,
    Portfolio,
    Product,
    ProposedChange,
    RunEvent,
    RunPhase,
)
from .policy import require_platform_admin
from .services import audit

#: Deepest first. Every one of these is PROTECT or CASCADE onto something above
#: it, so the order is not a preference - a delete out of order is refused by
#: the database, which is the protection working.
ORDER = (
    (ProposedChange, "run__application_id__in"),
    (RunEvent, "run__application_id__in"),
    (RunPhase, "run__application_id__in"),
    (FactoryRun, "application_id__in"),
    (PlanItem, "plan__application_id__in"),
    (ChangePlan, "application_id__in"),
    (ChatMessage, "conversation__application_id__in"),
    (ChatConversation, "application_id__in"),
    (CodeRelationship, "snapshot__repository__application_id__in"),
    (CodeFile, "snapshot__repository__application_id__in"),
    (CodeSnapshot, "repository__application_id__in"),
    (CodeRepository, "application_id__in"),
    (GraphRevision, "application_id__in"),
    (KnowledgeGraph, "application_id__in"),
    (KnowledgeEntry, "application_id__in"),
    (Document, "application_id__in"),
    (KnowledgeSource, "application_id__in"),
    (Connector, "application_id__in"),
    (AIUsage, "application_id__in"),
    (AIConfiguration, "application_id__in"),
    (ApiToken, "application_id__in"),
    (ManagedCredential, "application_id__in"),
    (ApplicationFeature, "application_id__in"),
    (ApplicationGrant, "application_id__in"),
)


def allowed():
    """Whether this deployment offers the reset at all."""
    return bool(getattr(settings, "ALLOW_DEMO_RESET", False))


def summary(organization):
    """What a reset would remove, for the screen to say before it is pressed."""
    applications = Application.objects.filter(
        product__portfolio__organization=organization
    )
    ids = list(applications.values_list("pk", flat=True))
    counts = {
        "applications": len(ids),
        "products": Product.objects.filter(portfolio__organization=organization).count(),
        "portfolios": Portfolio.objects.filter(organization=organization).count(),
    }
    if ids:
        counts["documents"] = Document.objects.filter(application_id__in=ids).count()
        counts["runs"] = FactoryRun.objects.filter(application_id__in=ids).count()
        counts["spend records"] = AIUsage.objects.filter(application_id__in=ids).count()
    return counts


@transaction.atomic
def reset(user, organization, typed_name):
    """Empty one organization, keeping the organization and its people.

    The name is typed rather than confirmed, because the cost of getting this
    wrong is everything the workspace holds and there is no undo. Compared
    case-sensitively and whole: "carepath" is not "CarePath", and a reset is not
    a place to be helpful about typing.
    """
    require_platform_admin(user)
    if not allowed():
        raise PermissionDenied(
            "Demonstration reset is not enabled on this deployment."
        )
    if (typed_name or "") != organization.name:
        raise ValidationError(
            f"Type the organization's name exactly - {organization.name} - to "
            "confirm. Nothing was removed."
        )
    applications = Application.objects.filter(
        product__portfolio__organization=organization
    )
    ids = list(applications.values_list("pk", flat=True))
    removed = {}
    for model, lookup in ORDER:
        count, _ = model.objects.filter(**{lookup: ids}).delete()
        if count:
            removed[model.__name__] = count
    # Audit rows name a resource by id. Those ids are about to stop existing,
    # so the rows go with them rather than becoming references nobody can follow.
    stale, _ = AuditEvent.objects.filter(
        resource_id__in=[str(app_id) for app_id in ids]
    ).delete()
    if stale:
        removed["AuditEvent"] = stale
    # The hierarchy itself, innermost first: a portfolio cannot go while a
    # product hangs off it, and a product cannot go while an application does.
    for label, queryset in (
        ("Application", Application.objects.filter(pk__in=ids)),
        ("Product", Product.objects.filter(portfolio__organization=organization)),
        ("Portfolio", Portfolio.objects.filter(organization=organization)),
    ):
        count, _ = queryset.delete()
        if count:
            removed[label] = count
    audit(
        user,
        "organization.reset",
        organization.pk,
        organization,
        details={"removed": removed},
    )
    return removed
