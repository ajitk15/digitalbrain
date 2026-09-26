"""Reproducible, application-scoped structural knowledge graphs with source evidence."""

import hashlib
import json
import logging
import re
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

from .graph_ai import EXTRACTION_TIMEOUT
from .model_catalog import MODEL_CHOICES
from .models import AIConfiguration, Document, GraphRevision, KnowledgeEntry, KnowledgeGraph
from .services import audit, feature_enabled
from .workbench import access

logger = logging.getLogger(__name__)

MAX_NODES = 6000
MAX_RECORDS_PER_SOURCE = 2000
MAX_SOURCES = 100


def sources_for(app_id):
    return KnowledgeEntry.objects.filter(application_id=app_id, active=True).order_by("id")


def fingerprint(app_id):
    records = list(sources_for(app_id).values_list("id", "digest"))
    config = AIConfiguration.objects.filter(
        application_id=app_id, purpose="graph_generation", enabled=True
    ).first()
    ai_settings = [config.provider, config.model, str(config.configured_by_id)] if config else None
    return hashlib.sha256(
        json.dumps(
            {
                "generator": "structural-v1",
                "ai": ai_settings,
                "limits": [MAX_NODES, MAX_RECORDS_PER_SOURCE, MAX_SOURCES],
                "sources": [(str(pk), digest) for pk, digest in records],
            },
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def cells(line):
    return [
        cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
    ]


def build_graph(entries, source_count):
    namespace = str(entries[0].application_id) if entries else ""
    if any(str(entry.application_id) != namespace for entry in entries):
        raise ValueError("Graph sources must belong to one application.")
    nodes, edges, source_refs = {}, [], []
    total_rows = included_rows = missing_values = duplicate_rows = capped_sources = 0
    total_sections = included_sections = 0

    def node(kind, identity, label, **extra):
        key = hashlib.sha256(f"{namespace}:{kind}:{identity}".encode()).hexdigest()[:24]
        if key not in nodes:
            if len(nodes) >= MAX_NODES:
                return None
            nodes[key] = {"id": key, "kind": kind, "label": label[:120], **extra}
        return key

    def edge(start, end, relation, entry, line, evidence):
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

    for entry in entries:
        source_refs.append({"id": str(entry.pk), "title": entry.title, "digest": entry.digest})
        document = node("document", str(entry.pk), entry.title, knowledge_id=str(entry.pk))
        lines = entry.content.splitlines()
        headers = None
        row_seen = set()
        local_included = 0
        omitted = False
        for index, line in enumerate(lines):
            if line.strip().startswith("|") and "|" in line.strip()[1:]:
                values = cells(line)
                if all(re.fullmatch(r":?-{2,}:?", value.replace(" ", "")) for value in values):
                    continue
                if index + 1 < len(lines):
                    following = cells(lines[index + 1])
                    if following and all(
                        re.fullmatch(r":?-{2,}:?", v.replace(" ", "")) for v in following
                    ):
                        headers = values
                        continue
                if headers:
                    total_rows += 1
                    signature = tuple(values)
                    if signature in row_seen:
                        duplicate_rows += 1
                    row_seen.add(signature)
                    missing_values += sum(not value for value in values)
                    if (
                        local_included >= MAX_RECORDS_PER_SOURCE
                        or len(nodes) + 1 + len(values) > MAX_NODES
                    ):
                        omitted = True
                        continue
                    label = next((value for value in values if value), f"Row {index + 1}")
                    record = node(
                        "record",
                        f"{entry.pk}:{index}",
                        label,
                        knowledge_id=str(entry.pk),
                        line=index + 1,
                    )
                    edge(document, record, "contains record", entry, index + 1, line)
                    for column, value in enumerate(values):
                        if not value:
                            continue
                        field = headers[column] if column < len(headers) else f"column {column + 1}"
                        field = field or f"column {column + 1}"
                        value_node = node(
                            "value",
                            json.dumps([field.casefold(), value]),
                            f"{field}: {value}",
                            field=field,
                            value=value[:1000],
                        )
                        edge(record, value_node, field, entry, index + 1, line)
                    included_rows += 1
                    local_included += 1
                    continue
            else:
                headers = None
            if line.strip() and not line.strip().startswith("|"):
                total_sections += 1
                if local_included >= MAX_RECORDS_PER_SOURCE or len(nodes) >= MAX_NODES:
                    omitted = True
                    continue
                text = line.lstrip("# ").strip()
                section = node(
                    "section",
                    f"{entry.pk}:{index}",
                    text,
                    knowledge_id=str(entry.pk),
                    line=index + 1,
                )
                edge(document, section, "contains section", entry, index + 1, line)
                included_sections += 1
                local_included += 1
        if omitted:
            capped_sources += 1

    connected = {endpoint for e in edges for endpoint in [e["source"], e["target"]]}
    isolated = sum(key not in connected for key in nodes)
    coverage = round(
        100 * (included_rows + included_sections) / max(1, total_rows + total_sections), 1
    )
    warnings = []
    if source_count > len(source_refs):
        warnings.append(f"Only the first {MAX_SOURCES} of {source_count} sources were processed.")
    if capped_sources:
        warnings.append(f"{capped_sources} source(s) reached the row or graph-size limit.")
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
        "method": "Deterministic Markdown structure v1",
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
    entries = list(sources_for(app_id)[:MAX_SOURCES])
    data, quality = build_graph(entries, sources_for(app_id).count())
    # AI enrichment runs only for a run someone asked for, with the model they
    # chose. A background rebuild keeps the structural graph current and never
    # incurs provider charges on its own.
    config = requested_configuration(graph) if requested else None
    if config is None and graph.requested_model and not requested:
        # A stale request from a failed attempt: clear it so no later rebuild
        # picks it up, and leave this one structural.
        KnowledgeGraph.objects.filter(pk=graph.pk).update(
            requested_provider="", requested_model=""
        )
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
            provider=config.provider if config else "",
            model=config.model if config else "",
        )
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
    """The newest published snapshot, or None."""
    return (
        GraphRevision.objects.filter(application_id=app_id, published_at__isnull=False)
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


def process_next_graph():
    # Also revisit existing graphs after the final source has been removed.
    app_ids = set(
        KnowledgeEntry.objects.filter(active=True).values_list("application_id", flat=True)
    ) | set(KnowledgeGraph.objects.values_list("application_id", flat=True))
    for app_id in sorted(app_ids, key=str):
        from .models import Application

        app = Application.objects.select_related("product__portfolio__organization").get(pk=app_id)
        if (
            not app.active
            or not app.product.portfolio.organization.active
            or not feature_enabled("knowledge", app)
        ):
            continue
        # Nothing is rebuilt while intake is still running. Waiting for the
        # whole batch rather than for each file is what keeps one import to one
        # version instead of one version per document.
        if Document.objects.filter(
            application_id=app_id, status__in=Document.IN_FLIGHT
        ).exists():
            continue
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
            continue
        if graph is None or graph.fingerprint != current_fingerprint or graph.status != "ready":
            try:
                rebuild(app_id)
            except Exception:
                KnowledgeGraph.objects.filter(application_id=app_id).update(status="failed")
                raise
            return True
    return False


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
    current = {str(pk): digest for pk, digest in sources_for(app_id).values_list("id", "digest")}
    recorded = revision.data.get("sources", [])
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
        return queue_generation(request, app, pk)
    return render(
        request,
        "graph_generate.html",
        {
            "application": app,
            "model_choices": MODEL_CHOICES,
            "has_versions": GraphRevision.objects.filter(application=app).exists(),
            "graph_ai_configured": AIConfiguration.objects.filter(
                application=app, purpose="graph_generation", enabled=True
            ).exists(),
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
            target = target.filter(
                updated_at__lt=timezone.now() - timedelta(minutes=STALL_MINUTES)
            )
        queued = target.update(status="queued", fingerprint="", stage="Queued")
        messages.success(
            request,
            "Eligible failed or interrupted graph generation queued. Provider charges may apply."
            if queued
            else "There is no failed or interrupted generation to retry.",
        )
        return redirect("graph", pk=pk)
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
    document_total = (
        Document.objects.filter(application=app).exclude(status="deleted").count()
    )
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
    return render(
        request,
        "graph.html",
        {
            "application": app,
            "grant": grant,
            "graph": graph if current else None,
            "graph_status": status,
            "selected_revision": selected,
            "knowledge_view": tab,
            "active_version": active_graph.version if active_graph else None,
            "versions": Paginator(
                GraphRevision.objects.filter(application=app).defer("data"), 20
            ).get_page(request.GET.get("page")),
            "source_count": sources_for(pk).count(),
            "published": published,
            "latest_revision": latest_revision,
            "published_changed": published_changed,
            "published_total": published_total,
            "is_published": bool(selected and selected.published_at),
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
