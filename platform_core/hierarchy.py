"""Disable, enable and delete a level of the organization tree.

Four levels - organization, portfolio, product, application - and two acts.

**Disabling is reversible.** Each level has its own `active` flag and an
application is live only while it and every level above it are on
(`policy.LIVE`), so turning a parent back on restores exactly what was on before it
was turned off: no child's flag is touched. Disabling refuses new work at once -
`policy.applications_for` reads the whole chain, so every screen, API token,
tool call and paid call (`gateway.authorize`) answers 404 - and fails the work
still *queued* under it, which has cost nothing yet, with a reason. A step
already running is finished and recorded, as a stop between lane steps always
is; its next paid call is refused by the gateway.

**Deleting is not.** It is offered only for a level that is already disabled,
the name must be typed exactly, and it is refused while anything under it is
in flight (`in_flight`). Everything the applications hold is removed, deepest
first (`demo_reset.ORDER`, then `REMAINING`), and every level's row stays as a
tombstone - `deleted_at` set, hidden by `LiveManager` - because two kinds of
record outlive it by design: audit events name the organization (a database
foreign key, kept six years for HIPAA) and AI receipts name the application
(a bill, kept 400 days). Nothing here deletes either. Files go after the commit,
so a rolled-back delete cannot have lost them: originals from the bucket and
the disk, and only the credential files this platform wrote - an operator's
mount and the shared connector folder are theirs.

Only a platform administrator may do either from a screen, and administering
the platform still grants no access to what an application holds: the screens
show names, states and counts, never content.
"""

import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Application, Organization, Portfolio, Product
from .services import audit

logger = logging.getLogger(__name__)

#: The URL word for each level, its model, and the path from an application up
#: to it.
LEVELS = {
    "organization": (Organization, "product__portfolio__organization"),
    "portfolio": (Portfolio, "product__portfolio"),
    "product": (Product, "product"),
    "application": (Application, "pk"),
}

#: Written on queued work a disable stopped. It had not started, so nothing was
#: spent; it can be asked for again once the application is back on.
STOPPED = (
    "Stopped before it started: a platform administrator disabled this application. "
    "Start it again once it is enabled."
)

#: Long enough that no answer can still be streaming (`SESSION_TTL_SECONDS`):
#: a row left "streaming" by a process that died must not block a delete for ever.
STREAM_SECONDS = 600


def level_of(obj):
    for name, (model, _) in LEVELS.items():
        if isinstance(obj, model):
            return name
    raise TypeError(obj)


def organization_of(obj):
    if isinstance(obj, Organization):
        return obj
    if isinstance(obj, Portfolio):
        return obj.organization
    if isinstance(obj, Product):
        return obj.portfolio.organization
    return obj.product.portfolio.organization


def applications_under(obj, manager=None):
    """Every application at or below `obj`, deleted ones excluded."""
    _, path = LEVELS[level_of(obj)]
    return (manager or Application.objects).filter(**{path: obj.pk}, deleted_at__isnull=True)


def is_live(app):
    """Whether this application and every level above it are on."""
    product = app.product
    return (
        app.active
        and product.active
        and product.portfolio.active
        and product.portfolio.organization.active
    )


# ── disable and enable ────────────────────────────────────────────────────────


def queued(ids):
    """(label, queryset, change) for each kind of work waiting for a lane under these applications.

    One list, so what a disable will stop (`waiting`) and what it stops
    (`stop_queued`) cannot disagree.
    """
    from .models import (
        AutomationRun,
        CodeDocumentation,
        CodeRepository,
        CodeReview,
        FactoryRun,
        ServiceOpsTestRun,
        TriageRun,
    )

    now = timezone.now()
    ids = list(ids)
    return [
        (
            "Code Factory runs",
            FactoryRun.objects.filter(
                application_id__in=ids, status__in=["pending", "prepare_queued"]
            ),
            {"status": "failed", "error": STOPPED, "finished_at": now},
        ),
        (
            "triage runs",
            TriageRun.objects.filter(application_id__in=ids, status="queued"),
            {"status": "failed", "error": STOPPED[:300]},
        ),
        (
            "ServiceOps test runs",
            ServiceOpsTestRun.objects.filter(application_id__in=ids, status="queued"),
            {"status": "failed", "finished_at": now},
        ),
        (
            "automation runs",
            AutomationRun.objects.filter(application_id__in=ids, mode="real", status="queued"),
            {"status": "failed", "finished_at": now},
        ),
        (
            "repository indexes",
            CodeRepository.objects.filter(application_id__in=ids, status="queued"),
            {"status": "failed", "error": STOPPED[:500]},
        ),
        (
            "code reviews",
            CodeReview.objects.filter(application_id__in=ids, status="queued"),
            {"status": "failed", "error": STOPPED[:500]},
        ),
        (
            "code reviews",
            CodeReview.objects.filter(application_id__in=ids, ai_status="queued"),
            {"ai_status": "failed", "ai_error": STOPPED[:500]},
        ),
        (
            "code documentation",
            CodeDocumentation.objects.filter(application_id__in=ids, ai_status="queued"),
            {"ai_status": "failed", "ai_error": STOPPED[:500]},
        ),
    ]


def _totals(counted):
    totals = {}
    for label, count in counted:
        totals[label] = totals.get(label, 0) + count
    return {label: count for label, count in totals.items() if count}


def waiting(ids):
    """What a disable of these applications would stop now. {label: count}."""
    return _totals((label, found.count()) for label, found, _ in queued(ids))


def stop_queued(ids):
    """Fail work still waiting for a lane under these applications. {label: count}."""
    return _totals((label, found.update(**change)) for label, found, change in queued(ids))


@transaction.atomic
def set_active(actor, obj, active):
    """Turn one level on or off, audited. Returns what a disable stopped."""
    model = type(obj)
    obj = model.objects.select_for_update().get(pk=obj.pk)
    level = level_of(obj)
    stopped = {}
    if obj.active != active:
        obj.active = active
        obj.save(update_fields=["active"])
    if not active:
        stopped = stop_queued(applications_under(obj).values_list("pk", flat=True))
    details = {"name": obj.name}
    if stopped:
        details["stopped"] = stopped
    audit(
        actor,
        f"{level}.enabled" if active else f"{level}.disabled",
        obj.pk,
        organization_of(obj),
        details=details,
    )
    return stopped


# ── delete ────────────────────────────────────────────────────────────────────


def in_flight(ids):
    """{label: count} of what is running, or about to, under these applications."""
    from datetime import timedelta

    from .models import (
        AIAdmission,
        AutomationRun,
        ChatMessage,
        CodeDocumentation,
        CodeRepository,
        CodeReview,
        Document,
        FactoryRun,
        Job,
        ServiceOpsTestRun,
        TriageRun,
    )

    now = timezone.now()
    ids = list(ids)
    found = {
        "Code Factory runs": FactoryRun.objects.filter(
            application_id__in=ids,
            status__in=["pending", "running", "prepare_queued", "preparing", "delivering"],
        ),
        "triage runs": TriageRun.objects.filter(
            application_id__in=ids, status__in=["queued", "running"]
        ),
        "ServiceOps test runs": ServiceOpsTestRun.objects.filter(
            application_id__in=ids, status__in=["queued", "running"]
        ),
        "automation runs": AutomationRun.objects.filter(
            application_id__in=ids, status__in=["queued", "launching", "running"]
        ),
        "repository indexes": CodeRepository.objects.filter(
            application_id__in=ids, status__in=["queued", "indexing"]
        ),
        "code reviews": CodeReview.objects.filter(
            Q(status__in=["queued", "reading"]) | Q(ai_status__in=["queued", "running"]),
            application_id__in=ids,
        ),
        "code documentation": CodeDocumentation.objects.filter(
            application_id__in=ids, ai_status__in=["queued", "running"]
        ),
        "documents converting": Document.objects.filter(
            application_id__in=ids, status__in=Document.IN_FLIGHT
        ),
        "chat answers": ChatMessage.objects.filter(
            application_id__in=ids,
            status="streaming",
            created_at__gt=now - timedelta(seconds=STREAM_SECONDS),
        ),
        "paid AI calls": AIAdmission.objects.filter(application_id__in=ids, expires_at__gt=now),
        "background jobs": Job.objects.filter(application_id__in=ids, status="running"),
    }
    counts = {label: queryset.count() for label, queryset in found.items()}
    return {label: count for label, count in counts.items() if count}


def remaining():
    """What `demo_reset.ORDER` does not cover, still deepest first.

    Entries go after every run that PROTECTs one - an automation run names its
    automation and incident, both entries - and documents after the entries
    converted from them. `AIUsage` is deliberately absent: a receipt outlives
    what it paid for. `test_hierarchy` fails when a model PROTECTs one of these
    and is in neither list.
    """
    from .models import (
        AIConfiguration,
        AutomationRun,
        ChatConversation,
        ChatMessage,
        Connector,
        Document,
        KnowledgeEntry,
    )

    return (
        AutomationRun,
        ChatMessage,
        ChatConversation,
        Connector,
        AIConfiguration,
        KnowledgeEntry,
        Document,
    )


def purge(app, removed):
    """Remove everything one application holds and leave its row as a tombstone."""
    from .demo_reset import ORDER
    from .models import AIAdmission, AIUsage, Job

    def tally(deleted):
        # Each model's own rows, cascades included under their own name, so a
        # source's passages are never counted as sources.
        for label, count in deleted[1].items():
            if count:
                name = label.rsplit(".", 1)[-1]
                removed[name] = removed.get(name, 0) + count

    for model, lookup in ORDER:
        tally(model.objects.filter(**{lookup: [app.pk]}).delete())
    for model in remaining():
        tally(model.objects.filter(application=app).delete())
    # Whatever else names the application - grants, features, tokens, credential
    # records, the logo, budgets, settings - except the receipts, which stay.
    for relation in Application._meta.related_objects:
        if relation.related_model is AIUsage:
            continue
        tally(relation.related_model._base_manager.filter(**{relation.field.name: app}).delete())
    # Not foreign keys, by design (`Job`, `AIAdmission`). Last, because deleting
    # sources above asked for a graph refresh of an application that is going.
    tally(Job.objects.filter(application_id=app.pk).exclude(status="running").delete())
    tally(AIAdmission.objects.filter(application_id=app.pk).delete())
    Application.everything.filter(pk=app.pk).update(
        active=False,
        deleted_at=timezone.now(),
        # Unique across every row, tombstones included, so the readable address
        # is free for a new application.
        slug=f"deleted-{app.pk}",
        logo_digest="",
        identity_icon="",
    )


def delete(actor, obj, typed_name, require_disabled=True):
    """Delete one level and everything under it. Returns {"removed", "files_left"}.

    `require_disabled` is False only for `manage.py delete_application`, the
    operator's tool, which never asked for it.
    """
    from . import originals
    from .documents import document_path
    from .models import Document, OrganizationMember

    model = type(obj)
    level = level_of(obj)
    with transaction.atomic():
        obj = model.objects.select_for_update().filter(pk=obj.pk).first()
        if obj is None:
            raise ValidationError("It has already been deleted.")
        if (typed_name or "") != obj.name:
            raise ValidationError(
                f"Type the {level}'s name exactly - {obj.name} - to confirm. Nothing was removed."
            )
        if require_disabled and obj.active:
            raise ValidationError(f"Disable the {level} first. Nothing was removed.")
        # Locked before they are checked, so a paid call cannot be admitted
        # between the check and the delete (`gateway.try_admit` locks the same
        # row); `of` so no parent row is locked with them.
        apps = list(
            applications_under(obj, Application.everything)
            .select_related("product__portfolio__organization")
            .select_for_update(of=("self",))
        )
        ids = [app.pk for app in apps]
        busy = in_flight(ids)
        if busy:
            raise ValidationError(
                "Something is still running here: "
                + ", ".join(f"{count} {label}" for label, count in busy.items())
                + ". Wait for it to finish. Nothing was removed."
            )
        organization = organization_of(obj)
        documents = [
            (app, pk)
            for app in apps
            for pk in Document.objects.filter(application=app).values_list("pk", flat=True)
        ]
        removed = {}
        for app in apps:
            purge(app, removed)
        now = timezone.now()
        if level in {"organization", "portfolio"}:
            products = Product.everything.filter(
                **{"portfolio__organization" if level == "organization" else "portfolio": obj.pk},
                deleted_at__isnull=True,
            )
            removed["Product"] = products.update(active=False, deleted_at=now)
        if level == "organization":
            removed["Portfolio"] = Portfolio.everything.filter(
                organization=obj, deleted_at__isnull=True
            ).update(active=False, deleted_at=now)
            removed["OrganizationMember"] = OrganizationMember.objects.filter(
                organization=obj
            ).delete()[0]
        if level != "application":
            model.everything.filter(pk=obj.pk).update(active=False, deleted_at=now)
        removed["Application"] = len(apps)
        removed = {name: count for name, count in removed.items() if count}
        audit(
            actor,
            f"{level}.deleted",
            obj.pk,
            organization,
            details={"name": obj.name, "applications": [str(pk) for pk in ids], "removed": removed},
        )

    # Files after the commit: a rolled-back delete must not have lost them.
    files_left = 0
    store = originals.store()
    for app, pk in documents:
        try:
            store.delete(app, pk)
        except originals.StoreUnavailable:
            files_left += 1
        # And from disk, where it was before a bucket was configured.
        document_path(app, pk).unlink(missing_ok=True)
    for app in apps:
        folder = originals.folder(app)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    cleared = clear_credentials(ids)
    if files_left:
        # The rows are gone, so nothing will ever read these objects; say how
        # many were left where an operator can find them.
        logger.warning(
            "hierarchy_files_left",
            extra={"event": "hierarchy_files_left", "count": files_left},
        )
        audit(
            actor,
            f"{level}.files_left",
            obj.pk,
            organization,
            details={"originals": files_left, "applications": [str(pk) for pk in ids]},
        )
    return {"removed": removed, "files_left": files_left, "credentials": cleared}


def clear_credentials(ids):
    """Remove the credential files this platform wrote for these applications.

    Only the managed directory: an operator's mount is theirs, and the shared
    connector folder belongs to every application.
    """
    from .secrets import managed_directory

    cleared = []
    directory = managed_directory()
    if directory is None or not directory.is_dir():
        return cleared
    for app_id in ids:
        for path in directory.glob(f"*_{app_id}"):
            path.unlink()
            cleared.append(path.name.removesuffix(f"_{app_id}"))
    return sorted(cleared)
