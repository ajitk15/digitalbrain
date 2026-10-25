"""Reproducible, application-scoped structural knowledge graphs with source evidence."""

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict, namedtuple
from dataclasses import dataclass, field
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .graph_ai import EXTRACTION_TIMEOUT, rubric
from .hierarchy import is_live
from .model_catalog import MODEL_CHOICES
from .models import AIConfiguration, Document, GraphRevision, KnowledgeEntry, KnowledgeGraph
from .ontology import CORE, PACKS, for_application
from .services import audit, feature_enabled
from .workbench import access

logger = logging.getLogger(__name__)

#: What one source may add to a graph: nodes, and table rows plus lines of
#: text. Per source, so a graph grows with its library and what a source
#: contributes never depends on what else is in it - these were the whole
#: graph's limits once, with at most `MAX_SOURCES` (100) sources, oldest first.
MAX_NODES_PER_SOURCE = 6000
MAX_RECORDS_PER_SOURCE = 2000
#: Sources whose structure each process keeps, by digest (`fragment`).
FRAGMENT_CACHE = 4096


def sources_for(app_id):
    # Answerable, not just active: a restricted source is never built into a
    # graph, and every citation is verified against this, so restricting one
    # also takes it out of published versions at once.
    #
    # Oldest first. Which sources go to the model first, and which of two
    # sources naming the same value labels its node, follow this order - by id
    # alone that was by UUID, so random, and a source edited (a new entry, so
    # a new UUID) could jump the extraction queue.
    return (
        KnowledgeEntry.objects.filter(application_id=app_id)
        .answerable()
        .order_by("created_at", "id")
    )


def fingerprint(app_id):
    # Sorted by id, not in build order, so a change of order is not a change.
    records = sorted(sources_for(app_id).values_list("id", "digest"))
    config = AIConfiguration.objects.filter(
        application_id=app_id, purpose="graph_generation", enabled=True
    ).first()
    ai_settings = [config.provider, config.model, str(config.configured_by_id)] if config else None
    settings = {
        "generator": "structural-v2",
        "ai": ai_settings,
        "limits": ["per-source", MAX_NODES_PER_SOURCE, MAX_RECORDS_PER_SOURCE],
        "sources": [(str(pk), digest) for pk, digest in records],
    }
    domain = for_application(app_id)
    if domain is not CORE:
        # Only a pack is named, so choosing none leaves every graph's
        # fingerprint as it was and rebuilds nothing.
        settings["ontology"] = rubric(domain)
    return hashlib.sha256(json.dumps(settings, separators=(",", ":")).encode()).hexdigest()


def cells(line):
    return [
        cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
    ]


def is_table_line(line):
    stripped = line.strip()
    return stripped.startswith("|") and "|" in stripped[1:]


def is_separator(values):
    """A Markdown table's `| --- | :--: |` line: structure, not a row."""
    return bool(values) and all(re.fullmatch(r":?-{2,}:?", v.replace(" ", "")) for v in values)


def is_bold_row(lines, index):
    """A table row whose every cell is filled and wholly bold: `| **Step** | **Owner** |`."""
    if index >= len(lines) or not is_table_line(lines[index]):
        return False
    values = cells(lines[index])
    return all(re.fullmatch(r"\*\*[^*]+\*\*", value) for value in values)


Line = namedtuple("Line", "kind index text headers values")


def walk(content):
    """What the graph reads in a source, one `Line` at a time.

    "header" starts a table, "row" is a table row under it, "section" is a
    line of text. Separators, and table lines with no header above them, yield
    nothing. The source check (`source_quality`) reads the same walk, so what it
    says about a table cannot disagree with what `build_graph` made of it.
    """
    lines = content.splitlines()
    headers = None
    promoted = None
    for index, line in enumerate(lines):
        if is_table_line(line):
            values = cells(line)
            if is_separator(values):
                continue
            if index == promoted:
                headers = [value.strip("*").strip() for value in values]
                yield Line("header", index, line, headers, None)
                continue
            if index + 1 < len(lines) and is_separator(cells(lines[index + 1])):
                headers = values
                # MarkItDown writes a Word table whose header row is not marked
                # as one as a blank header, then the real header in bold as the
                # first row. That row is the header: read as a record it named
                # every column "column N" and joined nothing to anything.
                if not any(values) and is_bold_row(lines, index + 2):
                    promoted = index + 2
                    continue
                yield Line("header", index, line, values, None)
            elif headers:
                yield Line("row", index, line, headers, values)
            continue
        headers = None
        if line.strip() and not line.strip().startswith("|"):
            yield Line("section", index, line, None, None)


@dataclass
class Fragment:
    """What one source adds to a graph: its own nodes and edges, and what it counted."""

    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    rows: int = 0
    included_rows: int = 0
    sections: int = 0
    included_sections: int = 0
    empty_cells: int = 0
    duplicate_rows: int = 0
    capped: bool = False


_fragments = OrderedDict()
_fragments_lock = threading.Lock()


def fragment(namespace, entry):
    """`entry`'s structure, built from it alone, and kept by what it was built from.

    A node's id is a digest of the application and what the node is, so a
    value two sources share is one node either way; `build_graph` keeps the
    first source's copy. The cache key is everything the fragment reads - the
    application, the entry, its title and its digest - so a source that did
    not change is not read again by the next rebuild.
    """
    key = (namespace, str(entry.pk), entry.title, entry.digest)
    with _fragments_lock:
        if key in _fragments:
            _fragments.move_to_end(key)
            return _fragments[key]
    made = structure(namespace, entry)
    with _fragments_lock:
        _fragments[key] = made
        while len(_fragments) > FRAGMENT_CACHE:
            _fragments.popitem(last=False)
    return made


def structure(namespace, entry):
    found = Fragment()
    nodes, edges = found.nodes, found.edges

    def node(kind, identity, label, **extra):
        key = hashlib.sha256(f"{namespace}:{kind}:{identity}".encode()).hexdigest()[:24]
        if key not in nodes:
            if len(nodes) >= MAX_NODES_PER_SOURCE:
                return None
            nodes[key] = {"id": key, "kind": kind, "label": label[:120], **extra}
        return key

    def edge(start, end, relation, line, evidence):
        if start and end:
            edges.append(
                {
                    "source": start,
                    "target": end,
                    "relation": relation[:100],
                    "knowledge_id": str(entry.pk),
                    "line": line,
                    "evidence": evidence[:1000],
                    "digest": entry.digest,
                }
            )

    document = node("document", str(entry.pk), entry.title, knowledge_id=str(entry.pk))
    row_seen = set()
    local_included = 0
    for item in walk(entry.content):
        index, line = item.index, item.text
        if item.kind == "row":
            headers, values = item.headers, item.values
            found.rows += 1
            signature = tuple(values)
            if signature in row_seen:
                found.duplicate_rows += 1
            row_seen.add(signature)
            found.empty_cells += sum(not value for value in values)
            if (
                local_included >= MAX_RECORDS_PER_SOURCE
                or len(nodes) + 1 + len(values) > MAX_NODES_PER_SOURCE
            ):
                found.capped = True
                continue
            label = next((value for value in values if value), f"Row {index + 1}")
            record = node(
                "record",
                f"{entry.pk}:{index}",
                label,
                knowledge_id=str(entry.pk),
                line=index + 1,
            )
            edge(document, record, "contains record", index + 1, line)
            for column, value in enumerate(values):
                if not value:
                    continue
                name = headers[column] if column < len(headers) else f"column {column + 1}"
                name = name or f"column {column + 1}"
                value_node = node(
                    "value",
                    json.dumps([name.casefold(), value]),
                    f"{name}: {value}",
                    field=name,
                    value=value[:1000],
                )
                edge(record, value_node, name, index + 1, line)
            found.included_rows += 1
            local_included += 1
        elif item.kind == "section":
            found.sections += 1
            if local_included >= MAX_RECORDS_PER_SOURCE or len(nodes) >= MAX_NODES_PER_SOURCE:
                found.capped = True
                continue
            text = line.lstrip("# ").strip()
            section = node(
                "section",
                f"{entry.pk}:{index}",
                text,
                knowledge_id=str(entry.pk),
                line=index + 1,
            )
            edge(document, section, "contains section", index + 1, line)
            found.included_sections += 1
            local_included += 1
    return found


def build_graph(entries, source_count=None):
    """The structural graph of `entries`, one source at a time.

    Each source's fragment is its own (`fragment`), merged in source order:
    nodes keep the order they first appear in and the first source's copy of a
    shared node, edges keep their order - so the result is exactly what one
    pass over every source made, and a rebuild after one change reads one.
    """
    namespace = str(entries[0].application_id) if entries else ""
    if any(str(entry.application_id) != namespace for entry in entries):
        raise ValueError("Graph sources must belong to one application.")
    nodes, edges, source_refs = {}, [], []
    total_rows = included_rows = missing_values = duplicate_rows = capped_sources = 0
    total_sections = included_sections = 0
    for entry in entries:
        source_refs.append({"id": str(entry.pk), "title": entry.title, "digest": entry.digest})
        found = fragment(namespace, entry)
        # Copies: what is built from the graph may change a node or an edge,
        # and the cached fragment must stay what the source said.
        for key, made in found.nodes.items():
            if key not in nodes:
                nodes[key] = dict(made)
        edges += [dict(made) for made in found.edges]
        total_rows += found.rows
        included_rows += found.included_rows
        total_sections += found.sections
        included_sections += found.included_sections
        missing_values += found.empty_cells
        duplicate_rows += found.duplicate_rows
        capped_sources += found.capped
    source_count = len(source_refs) if source_count is None else source_count

    connected = {endpoint for e in edges for endpoint in [e["source"], e["target"]]}
    isolated = sum(key not in connected for key in nodes)
    coverage = round(
        100 * (included_rows + included_sections) / max(1, total_rows + total_sections), 1
    )
    warnings = []
    if capped_sources:
        warnings.append(f"{capped_sources} source(s) reached the row or node limit for one source.")
    if missing_values:
        warnings.append(f"{missing_values} empty table cells were found.")
    if duplicate_rows:
        warnings.append(f"{duplicate_rows} repeated table rows were found.")
    if isolated:
        warnings.append(f"{isolated} node(s) have no relationships.")
    if not nodes:
        warnings.append("Add a knowledge source or upload documents to generate a graph.")
    quality = {
        "result": "Needs attention" if warnings else "Structural checks passed",
        "source_count": source_count,
        "processed_sources": len(source_refs),
        "nodes": len(nodes),
        "edges": len(edges),
        "table_rows": total_rows,
        "included_rows": included_rows,
        "sections": total_sections,
        "included_sections": included_sections,
        "coverage_percent": coverage,
        "empty_cells": missing_values,
        "duplicate_rows": duplicate_rows,
        "isolated_nodes": isolated,
        "warnings": warnings,
        "provenance_percent": 100 if edges else None,
        "method": "Deterministic Markdown structure v2",
        "semantic_accuracy": "Not measured; no inferred semantic relationships.",
    }
    return {"nodes": list(nodes.values()), "edges": edges, "sources": source_refs}, quality


def set_stage(app_id, stage):
    """Record the step a run is actually in, for the page to display."""
    KnowledgeGraph.objects.filter(application_id=app_id).update(
        stage=stage, updated_at=timezone.now()
    )


def rebuild(app_id):
    before = fingerprint(app_id)
    graph, _ = KnowledgeGraph.objects.get_or_create(application_id=app_id)
    if graph.status == "ready" and graph.fingerprint == before:
        return True
    if graph.status == "building" and graph.fingerprint == before:
        return False
    # Only a run someone explicitly queued may spend money. A rebuild triggered by
    # a source change reaches here with some other status, and must not consume a
    # request left over from an earlier attempt - that would be a paid call nobody
    # asked for, which is exactly what requested_* exists to prevent.
    requested = graph.status == "queued"
    claimed = KnowledgeGraph.objects.filter(
        pk=graph.pk, status=graph.status, fingerprint=graph.fingerprint
    ).update(
        status="building",
        fingerprint=before,
        stage="Reading sources",
        started_at=graph.started_at or timezone.now(),
        updated_at=timezone.now(),
    )
    if not claimed:
        return False
    entries = list(sources_for(app_id))
    if not entries:
        # Nothing to read is not a version. Saving one put an empty "Version 1"
        # on an application with no documents and no tickets, which read as a
        # graph that had been built and found nothing. Idle with the fingerprint
        # of no sources holds the worker off until one arrives; a request made
        # meanwhile is dropped rather than spent later on something unasked.
        KnowledgeGraph.objects.filter(pk=graph.pk, fingerprint=before, status="building").update(
            status="idle",
            data={},
            quality={},
            stage="",
            started_at=None,
            requested_provider="",
            requested_model="",
            updated_at=timezone.now(),
        )
        return True
    data, quality = build_graph(entries)
    # AI enrichment runs only for a run someone asked for, with the model they
    # chose. A background rebuild keeps the structural graph current and never
    # incurs provider charges on its own.
    config = requested_configuration(graph) if requested else None
    if config is None and graph.requested_model and not requested:
        # A stale request from a failed attempt: clear it so no later rebuild
        # picks it up, and leave this one structural.
        KnowledgeGraph.objects.filter(pk=graph.pk).update(requested_provider="", requested_model="")
    if config and entries:
        from .graph_ai import enrich_graph

        set_stage(app_id, f"Extracting relationships with {config.model}")
        try:
            data, quality = enrich_graph(app_id, config, entries, data, quality)
        except Exception as failure:
            # ValidationError messages are already sanitized for display; anything
            # else is an internal fault and must not be echoed to the page.
            reason = (
                " ".join(failure.messages)
                if isinstance(failure, ValidationError)
                else "Graph generation stopped unexpectedly. See the server log."
            )
            KnowledgeGraph.objects.filter(
                pk=graph.pk, fingerprint=before, status="building"
            ).update(status="failed", stage="Generation failed", failure_reason=reason)
            raise
        from . import embeddings

        if embeddings.configuration(app_id):
            # Only in a run someone requested, like extraction: a background
            # rebuild never spends. Unchanged texts are already stored and cost
            # nothing. A failure here costs the version its vectors, never the
            # relationships it has already paid for.
            set_stage(app_id, "Embedding for search")
            try:
                quality["embeddings"] = embeddings.index(
                    graph.requested_by or config.configured_by,
                    entries[0].application,
                    data,
                    entries,
                )
            except Exception as failure:
                logger.warning("embedding_failed", extra={"event": "embedding_failed"})
                quality["warnings"].append(
                    " ".join(failure.messages)
                    if isinstance(failure, ValidationError)
                    else "Embedding stopped unexpectedly. See the server log."
                )
                quality["warnings"].append(
                    "This version has no new search embeddings; questions rank by words "
                    "and by any vectors already stored."
                )
    elif entries:
        from .graph_ai import enrich_graph

        # No model is called: the AI relationships already extracted from text
        # that has not changed are carried into this version. A background
        # rebuild used to drop every one of them, leaving a structural draft
        # as the latest version and the AI one unpublishable once any of its
        # sources moved - so getting them back meant paying again.
        data, quality = enrich_graph(app_id, None, entries, data, quality)
        # A version with no AI says why on its own Quality notes, so nobody has to
        # guess whether it was a choice, a missing credential or the budget. One
        # that carried earlier answers forward names their model and has AI.
        if not quality.get("model"):
            reason = (
                "Structural only was chosen for this run."
                if requested and not graph.requested_model
                else automatic_ai(entries[0].application, graph).reason
                or "No AI run was requested for this version."
            )
            quality.setdefault("warnings", []).append(f"Built without AI: {reason}")
    set_stage(app_id, "Saving version")
    with transaction.atomic():
        locked = KnowledgeGraph.objects.select_for_update().get(pk=graph.pk)
        if fingerprint(app_id) != before:
            return False
        if (
            locked.status == "ready"
            and locked.fingerprint == before
            and GraphRevision.objects.filter(pk=locked.version).exists()
        ):
            locked.status = "ready"
            locked.save(update_fields=["status"])
            return True
        number = (
            GraphRevision.objects.filter(application_id=app_id).aggregate(latest=Max("number"))[
                "latest"
            ]
            or 0
        ) + 1
        revision = GraphRevision.objects.create(
            application_id=app_id,
            number=number,
            fingerprint=before,
            data=data,
            quality=quality,
            # A carried-forward version names the model its relationships came from.
            provider=config.provider if config else quality.get("provider", ""),
            model=config.model if config else quality.get("model", ""),
        )
        # Its elements as rows too, in this transaction, so the two commit together.
        from .graph_store import record

        record(revision)
        # Every saved version is scored against the benchmark questions. After
        # commit, so a failed benchmark can never undo the save, and free: it
        # calls no model.
        from .benchmark import run_after_save

        transaction.on_commit(lambda pk=revision.pk: run_after_save(pk))
        locked.data, locked.quality = data, quality
        locked.fingerprint = before
        locked.version = revision.pk
        locked.status = "ready"
        # The request is spent; a later automatic rebuild must not repeat a paid run.
        locked.requested_provider = ""
        locked.requested_model = ""
        locked.stage = ""
        locked.started_at = None
        locked.failure_reason = ""
        locked.save()
    return True


def requested_configuration(graph):
    """The AI settings for this run, or None when no enrichment was requested.

    Falls back to the application's saved graph model when a run was requested
    without naming one.
    """
    if not graph.requested_model:
        return None
    config = AIConfiguration.objects.filter(
        application_id=graph.application_id, purpose="graph_generation", enabled=True
    ).first()
    if config is None:
        return None
    # A per-run choice overrides the saved default without changing it.
    config.provider = graph.requested_provider or config.provider
    config.model = graph.requested_model
    if graph.requested_by_id:
        config.configured_by_id = graph.requested_by_id
    return config


def published_revision(app_id):
    """The newest published snapshot, or None.

    Without its graph: most callers ask only whether there is one, or which
    sources it read (`graph_store.recorded_sources`), and loading the whole
    graph for that was a read of every node on every chat page. Reading
    `.data` still works and costs the one query it always did.
    """
    return (
        GraphRevision.objects.filter(application_id=app_id, published_at__isnull=False)
        .defer("data")
        .order_by("-published_at", "-number")
        .first()
    )


def publish_revision(user, app_id, number):
    """Make one snapshot the answer for chat and Code Factory."""
    revision = GraphRevision.objects.filter(application_id=app_id, number=number).first()
    if revision is None:
        raise ValidationError("That graph version does not exist.")
    if not revision_readable(revision, app_id):
        raise ValidationError(
            "This version's source evidence has changed, so it cannot be published. "
            "Generate a new version."
        )
    revision.published_at = timezone.now()
    revision.published_by = user
    revision.save(update_fields=["published_at", "published_by"])
    return revision


def refresh_graph(app_id):
    """Bring one application's graph up to date with its sources.

    Returns "rebuilt", "waiting" (documents are still converting) or "current"
    (nothing to do, or nothing this application should have done for it).
    """
    from .models import Application

    app = (
        Application.objects.select_related("product__portfolio__organization")
        .filter(pk=app_id)
        .first()
    )
    if (
        app is None
        or not is_live(app)
        or not feature_enabled("knowledge", app)
    ):
        return "current"
    # Nothing is rebuilt while intake is still running. Waiting for the
    # whole batch rather than for each file is what keeps one import to one
    # version instead of one version per document.
    if Document.objects.filter(application_id=app_id, status__in=Document.IN_FLIGHT).exists():
        return "waiting"
    graph = KnowledgeGraph.objects.filter(application_id=app_id).first()
    current_fingerprint = fingerprint(app_id)
    # Failed or interrupted paid calls are not automatically repeated on every worker
    # tick. "idle" is a graph a demonstration reset cleared: it waits to be asked
    # for, and a source change is what brings the worker back to it.
    if (
        graph
        and graph.fingerprint == current_fingerprint
        and graph.status in {"failed", "building", "idle"}
    ):
        return "current"
    if graph is not None and graph.fingerprint == current_fingerprint and graph.status == "ready":
        return "current"
    # Every version is built with AI unless an owner said otherwise - the first
    # one too, which is why a missing row is made here rather than in rebuild:
    # an upload into a new application used to become a structural v1 nobody
    # had chosen. A placeholder row ("building", never started) and a reset's
    # "idle" row are a first version waiting in the same way.
    if graph is None:
        graph, _ = KnowledgeGraph.objects.get_or_create(application_id=app_id)
    if graph.fingerprint != current_fingerprint and (
        graph.status in {"ready", "failed", "idle"}
        or (graph.status == "building" and graph.started_at is None)
    ):
        arm_auto_refresh(app, graph)
    try:
        rebuild(app_id)
    except Exception:
        KnowledgeGraph.objects.filter(application_id=app_id).update(status="failed")
        raise
    return "rebuilt"


#: How long a refresh waits for converting documents before looking again.
WAIT_FOR_INTAKE_SECONDS = 10


def refresh_job(job):
    """The `graph_refresh` job: one application, asked for by a change to it."""
    from .jobs import Later

    if refresh_graph(job.subject) == "waiting":
        raise Later(WAIT_FOR_INTAKE_SECONDS)


def request_refresh(app_id, delay=None):
    """Ask the graph lane to look at this application. Folds into a waiting request."""
    from .jobs import enqueue

    enqueue("graph_refresh", app_id, application_id=app_id, delay=delay)


def candidate_applications():
    """Every application that has, or has had, something to build a graph from."""
    return set(
        KnowledgeEntry.objects.filter(active=True).values_list("application_id", flat=True)
    ) | set(KnowledgeGraph.objects.values_list("application_id", flat=True))


def process_next_graph():
    """Scan every application and rebuild the first stale graph found.

    The graph lane no longer runs this: it takes `graph_refresh` jobs, written
    when something changes, with `reconcile_if_due` as the safety net. It is
    kept as the one call that brings everything current, which is what tests
    and a one-off catch-up want.
    """
    # Also revisit existing graphs after the final source has been removed.
    for app_id in sorted(candidate_applications(), key=str):
        if refresh_graph(app_id) == "rebuilt":
            return True
    return False


#: How often the graph lane asks about every application anyway. A change
#: writes its own job; this catches what changed without one - a feature
#: switched on for everyone by a command, an organization reactivated, a job
#: lost with a database restore - and the work that waited while the worker
#: was stopped, since the first pass runs as the lane starts.
RECONCILE_INTERVAL = 600
_last_reconciled = None


def reconcile_if_due():
    global _last_reconciled
    now = time.monotonic()
    if _last_reconciled is not None and now - _last_reconciled < RECONCILE_INTERVAL:
        return False
    _last_reconciled = now
    for app_id in candidate_applications():
        request_refresh(app_id, delay=timedelta(0))
    return False


#: How a refusal names a provider: the company, not the SDK `AI_PROVIDERS` labels.
PROVIDER_NAMES = {"claude": "Claude", "openai": "OpenAI"}


@dataclass
class AutomaticAI:
    """What the next version will be built with when nobody presses Generate."""

    on: bool
    provider: str = ""
    model: str = ""
    payer: object = None
    reason: str = ""
    #: The person named on the row lost access and is no longer charged.
    lost: str = ""

    @property
    def label(self):
        wanted = f"{self.provider}:{self.model}"
        for _, group in MODEL_CHOICES:
            for value, text in group:
                if value == wanted:
                    return text
        return self.model

    @property
    def choice(self):
        """The Generate form's value for this model, so it is the one selected."""
        return f"{self.provider}:{self.model}" if self.on else ""


def default_payer(app):
    """The application's first owner still able to open it, or None.

    Who a version is charged to when no owner has named themselves. A receipt
    names a person (`AIUsage.actor`) while the bill is the application's own
    credential and budget, so this decides attribution, never whose money.
    """
    from django.db.models import Q

    from .models import ApplicationGrant

    grant = (
        ApplicationGrant.objects.filter(application=app, role="owner", user__is_active=True)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .select_related("user")
        .order_by("pk")
        .first()
    )
    return grant.user if grant else None


def may_be_charged(user, app):
    from django.http import Http404

    try:
        if user is None or not user.is_active:
            raise PermissionDenied
        access(user, app.pk, "knowledge", write=True)
    except (PermissionDenied, Http404):
        return False
    return True


def automatic_ai(app, graph=None):
    """Whether the next version is built with AI, by which model and as whom - or why not.

    **On unless an owner switched it off.** It used to be off unless an owner
    switched it on, so every version a source change made was structure alone
    and Quality had nothing to compare it with: the owner of the demonstration
    had asked for AI on every version many times, and got it only from the
    Generate button. Traded deliberately, like "Every AI task starts on": the
    spend is bounded the same ways - only new or changed sources go to the
    model, the monthly budget and the gateway apply, every call has a receipt,
    and the result is a draft until someone publishes it.

    The worker reads this before a rebuild and the pages read it before anyone
    uploads, so what a page says will happen is what happens. Each refusal is a
    sentence a person can act on, and is written on the structural version it
    leaves, so a version without AI always says why.
    """
    from django.core.exceptions import ImproperlyConfigured

    from . import budget
    from .ai import provider_credential

    if graph is not None and graph.auto_refresh_declined:
        return AutomaticAI(False, reason="An owner switched automatic AI enrichment off.")
    config = AIConfiguration.objects.filter(
        application=app, purpose="graph_generation", enabled=True
    ).first()
    if config is None or not config.model:
        return AutomaticAI(False, reason="Graph generation is switched off in AI settings.")
    named = graph.auto_refresh_by if graph is not None and graph.auto_refresh_by_id else None
    lost = ""
    payer = named
    if named is not None and not may_be_charged(named, app):
        lost = f"{named.username} may no longer change this application's knowledge."
        payer = None
    if payer is None:
        # Its query is the check: an active owner whose grant has not expired. The
        # rest of `access` - a live tree and Knowledge switched on - is what
        # `refresh_graph` asked before it got here, and what the page needed to draw.
        payer = default_payer(app)
    if payer is None:
        return AutomaticAI(
            False, reason="No owner of this application can be charged for it.", lost=lost
        )
    try:
        provider_credential(config, app)
    except ImproperlyConfigured:
        name = PROVIDER_NAMES.get(config.provider, config.provider)
        return AutomaticAI(
            False,
            reason=f"This application has no {name} credential, so the model cannot be "
            "reached. An owner can set one on Credentials.",
            lost=lost,
        )
    try:
        budget.check(app.pk)
    except ValidationError as refusal:
        return AutomaticAI(False, reason=" ".join(refusal.messages), lost=lost)
    return AutomaticAI(True, config.provider, config.model, payer, lost=lost)


def arm_auto_refresh(app, graph):
    """Turn a source change into an incremental AI run, or record why it cannot.

    As the owner `automatic_ai` names, re-checked here, like a connector's
    schedule: someone who can no longer change this application's knowledge is
    no longer charged, and the reason is kept on the row for the page.
    """
    plan = automatic_ai(app, graph)
    if plan.lost:
        KnowledgeGraph.objects.filter(pk=graph.pk).update(
            auto_refresh_by=None, auto_refresh_note=f"Automatic refresh stopped: {plan.lost}"
        )
    if not plan.on:
        return False
    armed = KnowledgeGraph.objects.filter(pk=graph.pk, status=graph.status).update(
        status="queued",
        fingerprint="",
        stage="Queued (automatic AI enrichment)",
        started_at=timezone.now(),
        failure_reason="",
        requested_provider=plan.provider,
        requested_model=plan.model,
        requested_by=plan.payer,
    )
    if armed:
        audit(
            plan.payer,
            "graph.generation_requested",
            app.pk,
            app.product.portfolio.organization,
            details={"provider": plan.provider, "model": plan.model, "automatic": True},
        )
    return bool(armed)


def set_auto_refresh(request, app, pk):
    """The owner's switch for `arm_auto_refresh`. Off clears who pays."""
    graph, _ = KnowledgeGraph.objects.get_or_create(application=app)
    enabled = request.POST.get("enabled") == "on"
    if (
        enabled
        and not AIConfiguration.objects.filter(
            application=app, purpose="graph_generation", enabled=True
        ).exists()
    ):
        messages.error(request, "Enable Graph generation in AI settings first.")
        return redirect("graph-generate", pk=pk)
    # Off is remembered as a choice, because on is the default: an empty payer
    # alone would read as "nobody chose", and the next change would build with AI.
    KnowledgeGraph.objects.filter(pk=graph.pk).update(
        auto_refresh_by=request.user if enabled else None,
        auto_refresh_declined=not enabled,
        auto_refresh_note="",
    )
    audit(
        request.user,
        "graph.auto_refresh_on" if enabled else "graph.auto_refresh_off",
        app.pk,
        app.product.portfolio.organization,
    )
    messages.success(
        request,
        "AI relationships will be kept current as sources change, charged to you. "
        "New versions are still drafts until published."
        if enabled
        else "Automatic AI enrichment is off. New versions are built from structure alone "
        "until it is switched on again or someone generates with a model.",
    )
    return redirect("graph", pk=pk)


def set_ontology(request, app, pk):
    """The owner's choice of industry domain for AI extraction (`ontology.PACKS`).

    Refused while a run is in flight, so one version is never judged under two
    ontologies. Answers paid for under the previous one are kept in the cache,
    under its rubric, but not carried into this one: the next AI generation
    reads every source again, which is what the page says before anyone
    presses the button.
    """
    key = request.POST.get("ontology", "")
    if key not in PACKS:
        messages.error(request, "Choose one of the listed industry domains.")
        return redirect("graph-generate", pk=pk)
    if run_in_flight(KnowledgeGraph.objects.filter(application=app).first()):
        messages.info(
            request, "A graph generation is running. Change the domain once it finishes."
        )
        return redirect("graph-generate", pk=pk)
    before = app.ontology
    if key == before:
        messages.info(request, f"AI extraction already follows {PACKS[key].label}.")
        return redirect("graph-generate", pk=pk)
    type(app).objects.filter(pk=app.pk).update(ontology=key)
    audit(
        request.user,
        "graph.ontology_changed",
        app.pk,
        app.product.portfolio.organization,
        details={"from": before or "core", "to": key or "core"},
    )
    request_refresh(app.pk)
    messages.success(
        request,
        f"AI extraction now follows {PACKS[key].label}. Relationships extracted under the "
        "previous domain are not carried over: the next AI generation reads every source "
        "again, and provider charges apply.",
    )
    return redirect("graph-generate", pk=pk)


def revision_added(revision, app_id):
    """Sources added since this revision was saved: `revision_drift`'s blind spot."""
    from .graph_store import recorded_sources

    recorded = {source["id"] for source in recorded_sources(revision)}
    return sum(
        1 for pk in sources_for(app_id).values_list("id", flat=True) if str(pk) not in recorded
    )


def revision_drift(revision, app_id):
    """How far a revision has moved from its sources: (changed, recorded).

    Counts the sources this revision recorded whose digest has since changed
    or which are no longer active. It cannot see sources *added* afterwards -
    the same blind spot `publish_revision` has - so it answers "how much of
    what this version read is still true", not "is this version current".

    Not blocking on drift is deliberate: answering from a published snapshot
    is the whole point. Not telling anyone was never decided on purpose, which
    is why this is also read for the revision that is currently answering.
    """
    from .graph_store import recorded_sources

    current = {str(pk): digest for pk, digest in sources_for(app_id).values_list("id", "digest")}
    recorded = recorded_sources(revision)
    # A record with no digest cannot be shown to still read, so it counts as
    # drifted rather than raising - older revisions may predate the field.
    changed = sum(1 for source in recorded if current.get(source["id"]) != source.get("digest"))
    return changed, len(recorded)


def revision_readable(revision, app_id):
    return revision_drift(revision, app_id)[0] == 0


#: How long a "building" run may go quiet before it is treated as interrupted.
#:
#: This MUST exceed the extraction timeout. A legitimate run makes one opaque
#: provider call with no row updates in between, so a window shorter than that
#: call would declare a live run dead, re-offer Retry underneath it and bill the
#: application twice. Derived rather than written as a literal so the two cannot
#: drift apart.
STALL_MINUTES = EXTRACTION_TIMEOUT // 60 + 5


def run_in_flight(graph):
    """True while a generation is queued or actively running.

    A "building" row that has not been touched for STALL_MINUTES is treated as
    interrupted rather than live, so a crashed worker cannot wedge an application
    out of ever generating again.
    """
    if graph is None or graph.status not in {"queued", "building"}:
        return False
    if graph.status == "queued":
        return True
    # A newly created row defaults to "building" without any run behind it;
    # started_at is written when a run is actually claimed, so it distinguishes
    # a live run from a placeholder.
    if graph.started_at is None:
        return False
    return graph.updated_at > timezone.now() - timedelta(minutes=STALL_MINUTES)


def claim_generation(user, app, provider="", model=""):
    """Arm the graph row for one run, or say why not. Returns (claimed, reason).

    Split out of `queue_generation` because there are now two ways to ask for
    the same thing - the Generate form, and a finished Code Factory run offering
    to bring this application's knowledge back in line with the code it just
    changed. Two copies of "is a run already in flight" is exactly the drift
    that would let both finish and bill the application twice.

    It takes a user rather than a request: the second caller has no form.
    """
    graph, _ = KnowledgeGraph.objects.get_or_create(application=app)
    if run_in_flight(graph):
        # Re-arming the row under a live run would let both finish and bill twice.
        return False, (
            "A graph generation is already running for this application. "
            "Watch its progress below; start another once it finishes."
        )
    claimed = KnowledgeGraph.objects.filter(pk=graph.pk, status=graph.status).update(
        status="queued",
        fingerprint="",
        stage="Queued",
        started_at=timezone.now(),
        failure_reason="",
        requested_provider=provider,
        requested_model=model,
        requested_by=user,
    )
    if not claimed:
        # Something else claimed the row between the check and here.
        return False, "A graph generation was just started by someone else."
    request_refresh(app.pk, delay=timedelta(0))
    audit(
        user,
        "graph.generation_requested",
        app.pk,
        app.product.portfolio.organization,
        details={"provider": provider, "model": model or "structural only"},
    )
    return True, ""


def queue_generation(request, app, pk):
    """Queue a graph run, optionally enriched with a chosen model.

    The model is a per-run choice: it does not change the application's saved
    graph settings, and it is recorded on the snapshot it produces.
    """
    choice = request.POST.get("model_choice", "structural")
    if choice == "structural":
        provider, model = "", ""
    else:
        known = {value for _, group in MODEL_CHOICES for value, _ in group if value != "custom"}
        if choice not in known:
            messages.error(request, "Choose an available model.")
            return redirect("graph", pk=pk)
        provider, model = choice.split(":", 1)
        config = AIConfiguration.objects.filter(
            application=app, purpose="graph_generation", enabled=True
        ).first()
        if config is None:
            messages.error(
                request,
                "Enable Graph generation in AI settings before generating an enriched graph.",
            )
            return redirect("graph", pk=pk)
    claimed, refusal = claim_generation(request.user, app, provider, model)
    if not claimed:
        messages.info(request, refusal)
        return redirect("graph", pk=pk)
    messages.success(
        request,
        f"Graph generation queued using {model}. Provider charges may apply. "
        "Publish the new version when you are happy with it."
        if model
        else "Structural graph generation queued. No AI model is used and nothing is charged.",
    )
    return redirect("graph", pk=pk)


def source_summary(app_id):
    from .source_map import summary

    return summary(app_id) if sources_for(app_id).exists() else None


@login_required
@require_http_methods(["GET", "POST"])
def graph_generate(request, pk):
    """The generate form on its own URL, so it can be a popup.

    `static/modal.js` lifts <main> out of a real page and posts the form back to
    the same address, so this must render and submit on its own - with
    JavaScript off the button is an ordinary link to an ordinary form. The POST
    path is `queue_generation`, unchanged, so the popup and the plain page
    cannot diverge.
    """
    app, grant = access(request.user, pk, "knowledge")
    if grant.role not in {"owner", "contributor"}:
        raise PermissionDenied("Only an owner or contributor may generate a graph.")
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        if request.POST.get("action") == "auto_refresh":
            # Owner-only: it commits the application to spending on its own.
            if grant.role != "owner":
                raise PermissionDenied("Only an owner may switch automatic refresh.")
            return set_auto_refresh(request, app, pk)
        if request.POST.get("action") == "ontology":
            # Owner-only: it decides what every later AI run is paid to read.
            if grant.role != "owner":
                raise PermissionDenied("Only an owner may change the industry domain.")
            return set_ontology(request, app, pk)
        return queue_generation(request, app, pk)
    graph = KnowledgeGraph.objects.filter(application=app).select_related("auto_refresh_by").first()
    saved = AIConfiguration.objects.filter(
        application=app, purpose="graph_generation", enabled=True
    ).first()
    return render(
        request,
        "graph_generate.html",
        {
            "application": app,
            "grant": grant,
            "graph": graph,
            "ai_plan": automatic_ai(app, graph),
            # The saved model is selected: AI is the default here too, and
            # structural only is something a person picks on purpose.
            "default_choice": f"{saved.provider}:{saved.model}" if saved else "structural",
            "model_choices": MODEL_CHOICES,
            "has_versions": GraphRevision.objects.filter(application=app).exists(),
            # Said, never enforced: a source that needs work still goes in.
            "source_summary": source_summary(app.pk),
            "graph_ai_configured": AIConfiguration.objects.filter(
                application=app, purpose="graph_generation", enabled=True
            ).exists(),
            "ontology": PACKS.get(app.ontology, CORE),
            "ontology_choices": PACKS.values(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def graph_view(request, pk):
    app, grant = access(request.user, pk, "knowledge")
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        action = request.POST.get("action")
        if action == "publish":
            number = request.POST.get("version", "")
            try:
                revision = publish_revision(request.user, pk, int(number))
            except (TypeError, ValueError):
                return HttpResponse("Invalid graph version.", status=400)
            except ValidationError as error:
                messages.error(request, " ".join(error.messages))
                return redirect(f"{reverse('graph', args=[pk])}?tab=versions")
            audit(
                request.user,
                "graph.published",
                app.pk,
                app.product.portfolio.organization,
                details={"version": revision.number},
            )
            messages.success(
                request,
                f"Version {revision.number} published. Chat and Code Factory now answer from it.",
            )
            return redirect(f"{reverse('graph', args=[pk])}?tab=versions")
        if action == "generate":
            return queue_generation(request, app, pk)
        if action != "retry":
            return HttpResponse("Unknown graph action.", status=400)
        if run_in_flight(KnowledgeGraph.objects.filter(application=app).first()):
            messages.info(request, "That generation is still running. Watch its progress below.")
            return redirect("graph", pk=pk)
        target = KnowledgeGraph.objects.filter(application=app, status__in=["failed", "building"])
        if target.filter(status="building").exists():
            target = target.filter(updated_at__lt=timezone.now() - timedelta(minutes=STALL_MINUTES))
        queued = target.update(status="queued", fingerprint="", stage="Queued")
        if queued:
            request_refresh(app.pk, delay=timedelta(0))
        messages.success(
            request,
            "Eligible failed or interrupted graph generation queued. Provider charges may apply."
            if queued
            else "There is no failed or interrupted generation to retry.",
        )
        return redirect("graph", pk=pk)
    if request.GET.get("tab") == "operations":
        # The operations layer lives under ServiceOps now, which is what reads
        # it; the old address still arrives there.
        return redirect("serviceops-graph", pk=pk)
    active_graph = KnowledgeGraph.objects.filter(application=app).first()
    graph = active_graph
    current = bool(graph and graph.status == "ready" and graph.fingerprint == fingerprint(pk))
    selected = None
    version_number = request.GET.get("version")
    if version_number:
        if not version_number.isdecimal() or len(version_number) > 9:
            return HttpResponse("Invalid graph version.", status=400)
        selected = get_object_or_404(GraphRevision, application=app, number=int(version_number))
        current = revision_readable(selected, pk)
        graph = selected
    # Requesting a regenerate clears the working graph's fingerprint, which used to
    # blank this whole screen until the run finished - and strand the user if it
    # failed. The last good version is still perfectly viewable, and the published
    # one is what Chat answers from regardless, so keep showing it and report the
    # run in a banner instead of replacing the workspace with it.
    showing_fallback = False
    if not current and selected is None:
        fallback = published_revision(pk) or (
            GraphRevision.objects.filter(application=app).order_by("-number").first()
        )
        if fallback and revision_readable(fallback, pk):
            graph, selected, current, showing_fallback = fallback, fallback, True, True
    status = (
        "ready"
        if current
        else (
            "unavailable"
            if selected
            else "failed"
            if active_graph and active_graph.status == "failed"
            else "idle"
            if active_graph and active_graph.status == "idle"
            else "building"
        )
    )
    if selected is None and current:
        selected = GraphRevision.objects.filter(pk=graph.version, application=app).first()
    if request.GET.get("format") == "json":
        if not current:
            return JsonResponse({"status": status}, status=409)
        response = HttpResponse(
            json.dumps(
                {
                    "application_id": str(app.pk),
                    "version": str(graph.version),
                    "version_number": selected.number if selected else None,
                    "fingerprint": graph.fingerprint,
                    "graph": graph.data,
                    "quality": graph.quality,
                }
            ),
            content_type="application/json",
        )
        response["Content-Disposition"] = f'attachment; filename="graph-{app.pk}.json"'
        return response
    published = published_revision(pk)
    latest_revision = GraphRevision.objects.filter(application=app).first()
    # Drift on the revision that is actually answering. Display only - the
    # answer still comes from the snapshot, which is the point of publishing.
    published_changed, published_total = revision_drift(published, pk) if published else (0, 0)
    published_added = revision_added(published, pk) if published else 0
    # The Sources rail reuses the documents screen's own gating rather than
    # reimplementing it, so one set of rules governs both places.
    from .documents import LinkForm, intake_enabled
    from .link_sources import sharepoint_available

    can_upload = bool(
        intake_enabled()
        and feature_enabled("document_uploads", app)
        and grant.role in {"owner", "contributor"}
    )
    recent_documents = list(
        Document.objects.filter(application=app)
        .exclude(status="deleted")
        .order_by("-created_at")[:12]
    )
    document_total = Document.objects.filter(application=app).exclude(status="deleted").count()
    # A connector import is a source with no Document behind it, so the rail's
    # own list can never be the source count. Counted rather than subtracted:
    # a document whose conversion failed has no entry either, so the two
    # totals do not differ by one thing.
    imported_total = KnowledgeEntry.objects.filter(
        application=app, active=True, document__isnull=True
    ).count()
    tab = request.GET.get("tab", "graph")
    if tab not in {"graph", "quality", "versions"}:
        tab = "graph"
    health = None
    graph_themes = None
    if graph and current:
        from .graph_quality import health_report, node_themes

        health = health_report(graph.data, graph.quality)
        if tab == "graph":
            # The canvas colours by the same Graphify themes the Quality table
            # lists. A clustering failure falls back to colouring by kind; it is
            # never a reason not to show the graph.
            try:
                graph_themes = node_themes(graph.data)
            except Exception:
                logger.warning("graph_themes_failed", extra={"event": "graph_themes_failed"})
    versions = Paginator(GraphRevision.objects.filter(application=app).defer("data"), 20).get_page(
        request.GET.get("page")
    )
    if tab == "versions":
        # One query for the page's scores, not one per row.
        from .benchmark import latest_runs

        scores = latest_runs(app, list(versions.object_list))
        for revision in versions.object_list:
            revision.benchmark = scores.get(revision.pk)
    glance = quality_notes = quality_summary = None
    if tab == "quality" and health:
        # Benchmark is no longer a tab of its own: this version's score is one
        # of the Quality headlines, and its questions open as a popup.
        from .benchmark import latest_runs
        from .graph_quality import at_a_glance, notes, quality_overview

        scored = latest_runs(app, [selected]).get(selected.pk) if selected else None
        glance = at_a_glance(health, graph.quality, sources_for(pk).count(), scored)
        quality_summary = quality_overview(health, glance, graph.quality)
        quality_notes = notes(graph.quality.get("warnings"))
    trend = None
    if tab == "quality":
        from .graph_trend import trend as version_trend

        trend = version_trend(app)
    return render(
        request,
        "graph.html",
        {
            "application": app,
            "grant": grant,
            "graph": graph if current else None,
            "graph_status": status,
            "selected_revision": selected,
            "code_links_enabled": feature_enabled("code_graph", app),
            "knowledge_view": tab,
            "trend": trend,
            "active_version": active_graph.version if active_graph else None,
            "versions": versions,
            "source_count": sources_for(pk).count(),
            "published": published,
            "latest_revision": latest_revision,
            "published_changed": published_changed,
            "published_added": published_added,
            "published_total": published_total,
            "is_published": bool(selected and selected.published_at),
            "ai_plan": automatic_ai(app, KnowledgeGraph.objects.filter(application=app).first()),
            "model_choices": MODEL_CHOICES,
            "graph_ai_configured": AIConfiguration.objects.filter(
                application=app, purpose="graph_generation", enabled=True
            ).exists(),
            "generating": bool(active_graph and active_graph.status in {"queued", "building"}),
            # Progress for a live run, and whether a retry is even possible: the
            # page must not offer a button that would start a second paid run.
            "run": active_graph if run_in_flight(active_graph) else None,
            "run_model": (active_graph.requested_model or "structural only")
            if active_graph
            else "",
            "showing_fallback": showing_fallback,
            "failure_reason": active_graph.failure_reason if active_graph else "",
            "run_failed": bool(
                active_graph and active_graph.status == "failed" and not run_in_flight(active_graph)
            ),
            "can_retry": bool(
                active_graph
                and not run_in_flight(active_graph)
                and active_graph.status in {"failed", "building"}
            ),
            "health": health,
            "glance": glance,
            "quality_notes": quality_notes,
            "quality_summary": quality_summary,
            "graph_themes": graph_themes,
            "can_upload": can_upload,
            "recent_documents": recent_documents,
            "link_form": LinkForm(),
            "sharepoint_ready": sharepoint_available(app),
            "document_total": document_total,
            "imported_total": imported_total,
            "pending_documents": Document.objects.filter(
                application=app, status__in=Document.IN_FLIGHT
            ).count(),
        },
    )
