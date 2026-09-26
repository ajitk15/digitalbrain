"""Clearing an organization's work between demonstrations.

Nothing else in this platform deletes a run, a graph version or a code
snapshot. Every record that matters is kept because it is the account of what
was done and what it cost; disabling is the supported answer, and it is the
right one for a real deployment.

A demonstration instance is the case that argument does not cover. The same
story is told a dozen times, and each telling needs the *work* back at its
start - no knowledge graph, no code graph, no runs - while the *setup* a
demonstration takes time to build stays: the applications, their connectors,
credentials, sources, AI settings, features and people. This used to delete the
applications and everything beneath them, which meant rebuilding connectors and
re-entering credentials before every telling. It now removes exactly the
things a demonstration re-creates on screen:

* **the knowledge graph** - the working graph and every saved version. It stays
  gone: each application is left with an *idle* graph row carrying its
  sources' current fingerprint, which the background worker leaves alone until
  a source changes. Without it the worker saw sources with no graph and rebuilt
  a draft within a second, so the reset appeared not to have happened. A
  demonstration starts at "Generate graph";
* **the code graph** - registered repositories and their snapshots, files and
  edges, so "Add repository" is a step again;
* **runs** - Code Factory runs with their phases, log, prepared changes, and the
  change plans and gaps they produced, and ServiceOps triage runs;
* **connector imports** - every record a Jira, ServiceNow or GitHub connector
  brought in, superseded revisions included, so "Import" is a step again. The
  connectors themselves stay, configured, with their last-import status
  cleared. A scheduled connector's interval restarts from the reset rather
  than finding itself long overdue and importing on the next worker tick -
  the same trap the graph fell into;
* **link sources** - a GitHub folder or SharePoint library pasted into
  Knowledge, with every document it brought in, their stored files and the
  knowledge read from them, so pasting the link is a step again. A source is
  an address, not a set-up connection: there is nothing to keep but the
  address itself. Documents somebody uploaded by hand are not an import and
  stay.

Everything else is kept as it is, including chat history (a conversation pinned
to a removed graph version is unpinned, not deleted), spend records and the
audit log.

It is fenced accordingly:

* `allow_demo_reset` in `config/local.toml`, off unless somebody writes it down,
  and **refused outright** by `load_config` under `mode = "production"` - the
  same treatment `claude_use_host_login` gets.
* Platform administrators only.
* The organization's name has to be typed. A button that clears a workspace
  should cost more than a click that could be a misclick.
"""

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .connector_kinds import kind_for
from .documents import document_path
from .graphs import fingerprint
from .models import (
    Application,
    ChangePlan,
    ChatConversation,
    CodeFile,
    CodeRelationship,
    CodeRepository,
    CodeSnapshot,
    Connector,
    Document,
    FactoryRun,
    GraphRevision,
    IncidentProfile,
    KnowledgeEntry,
    KnowledgeGraph,
    KnowledgeSource,
    PlanItem,
    ProposedChange,
    RunEvent,
    RunPhase,
    TriageHypothesis,
    TriageRun,
    TriageVerdict,
)
from .policy import require_platform_admin
from .services import audit

#: Deepest first. Each is PROTECT or CASCADE onto something above it, so the
#: order is not a preference - a delete out of order is refused by the
#: database, which is the protection working. Nothing outside this list points
#: at anything in it except `FactoryRun.code_snapshot`, which is SET_NULL and
#: goes with its run first anyway.
ORDER = (
    # Runs, and the plans they produced.
    (ProposedChange, "run__application_id__in"),
    (RunEvent, "run__application_id__in"),
    (RunPhase, "run__application_id__in"),
    (FactoryRun, "application_id__in"),
    (PlanItem, "plan__application_id__in"),
    (ChangePlan, "application_id__in"),
    # ServiceOps triage. A run PROTECTs the imported incident it triaged, so it
    # has to go before the imports below.
    (TriageVerdict, "hypothesis__run__application_id__in"),
    (TriageHypothesis, "run__application_id__in"),
    (TriageRun, "application_id__in"),
    # The code graph.
    (CodeRelationship, "snapshot__repository__application_id__in"),
    (CodeFile, "snapshot__repository__application_id__in"),
    (CodeSnapshot, "repository__application_id__in"),
    (CodeRepository, "application_id__in"),
    # The knowledge graph.
    (GraphRevision, "application_id__in"),
    (KnowledgeGraph, "application_id__in"),
)

#: What the console lists before the button is pressed, in the words it uses.
LABELS = {
    "FactoryRun": "runs",
    "ChangePlan": "plans",
    "CodeRepository": "code repositories",
    "CodeSnapshot": "code snapshots",
    "GraphRevision": "graph versions",
}


def imported(ids):
    """Knowledge a connector brought in, for these applications.

    An import is an ordinary entry with no document behind it, whose source is a
    record URL under its connector's namespace - the same test `connectors.prune`
    uses to decide what a connector owns.
    """
    owned = Q(pk__in=[])
    for connector in Connector.objects.filter(application_id__in=ids):
        try:
            namespace = kind_for(connector.kind).namespace(connector.config)
        except (KeyError, ValidationError):
            continue
        owned |= Q(application_id=connector.application_id, source__startswith=namespace)
    return KnowledgeEntry.objects.filter(owned, document__isnull=True)


def allowed():
    """Whether this deployment offers the reset at all."""
    return bool(getattr(settings, "ALLOW_DEMO_RESET", False))


def application_ids(organization):
    return list(
        Application.objects.filter(product__portfolio__organization=organization).values_list(
            "pk", flat=True
        )
    )


def summary(organization):
    """What a reset would remove, for the screen to say before it is pressed."""
    ids = application_ids(organization)
    counts = {}
    for model, lookup in ORDER:
        label = LABELS.get(model.__name__)
        if label and ids:
            counts[label] = model.objects.filter(**{lookup: ids}).count()
    if ids:
        counts["imported records"] = imported(ids).filter(active=True).count()
        counts["link sources"] = KnowledgeSource.objects.filter(application_id__in=ids).count()
    return counts


@transaction.atomic
def reset(user, organization, typed_name):
    """Clear one organization's graphs and runs, keeping everything it is set up with.

    The name is typed rather than confirmed, because there is no undo. Compared
    case-sensitively and whole: "carepath" is not "CarePath", and a reset is not
    a place to be helpful about typing.
    """
    require_platform_admin(user)
    if not allowed():
        raise PermissionDenied("Demonstration reset is not enabled on this deployment.")
    if (typed_name or "") != organization.name:
        raise ValidationError(
            f"Type the organization's name exactly - {organization.name} - to "
            "confirm. Nothing was removed."
        )
    ids = application_ids(organization)
    removed = {}
    for model, lookup in ORDER:
        count, _ = model.objects.filter(**{lookup: ids}).delete()
        if count:
            removed[model.__name__] = count
    entries = imported(ids)
    IncidentProfile.objects.filter(entry__in=entries).delete()
    count, _ = entries.delete()
    if count:
        removed["KnowledgeEntry imported"] = count
    sources = KnowledgeSource.objects.filter(application_id__in=ids)
    brought = Document.objects.filter(source__in=sources).select_related("application")
    for document in brought:
        # The original bytes first, as deleting a document does: a row removed
        # with its file left behind is storage nothing can find again.
        document_path(document.application, document.pk).unlink(missing_ok=True)
    read = KnowledgeEntry.objects.filter(document__in=brought)
    IncidentProfile.objects.filter(entry__in=read).delete()
    count, _ = read.delete()
    if count:
        removed["KnowledgeEntry from sources"] = count
    count, _ = brought.delete()
    if count:
        removed["Document from sources"] = count
    count, _ = sources.delete()
    if count:
        removed["KnowledgeSource"] = count
    # The connector reads as never imported, and its schedule counts from now.
    Connector.objects.filter(application_id__in=ids).update(
        last_synced_at=None,
        last_attempt_at=timezone.now(),
        last_count=0,
        last_status="",
        last_error="",
        last_duration_ms=0,
    )
    # Idle, not absent: an application with sources and no graph row is exactly
    # what the worker rebuilds. Matching the fingerprint is what holds it off,
    # and only until the sources actually change.
    KnowledgeGraph.objects.bulk_create(
        KnowledgeGraph(application_id=pk, status="idle", fingerprint=fingerprint(pk))
        for pk in ids
    )
    # A conversation can pin a graph version for its graph answers. The version
    # is gone; the conversation is not, so it goes back to the current graph
    # rather than failing to find the one it named.
    unpinned = ChatConversation.objects.filter(
        application_id__in=ids, graph_version__isnull=False
    ).update(graph_version=None)
    if unpinned:
        removed["ChatConversation unpinned"] = unpinned
    audit(
        user,
        "organization.reset",
        organization.pk,
        organization,
        details={"removed": removed},
    )
    return removed
