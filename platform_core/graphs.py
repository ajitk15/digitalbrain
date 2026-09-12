"""Reproducible, application-scoped structural knowledge graphs with source evidence."""

import hashlib
import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import AIConfiguration, Document, GraphRevision, KnowledgeEntry, KnowledgeGraph
from .services import feature_enabled
from .workbench import access

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


def rebuild(app_id):
    before = fingerprint(app_id)
    graph, _ = KnowledgeGraph.objects.get_or_create(application_id=app_id)
    if graph.status == "ready" and graph.fingerprint == before:
        return True
    if graph.status == "building" and graph.fingerprint == before:
        return False
    claimed = KnowledgeGraph.objects.filter(
        pk=graph.pk, status=graph.status, fingerprint=graph.fingerprint
    ).update(
        status="building",
        fingerprint=before,
        updated_at=timezone.now(),
    )
    if not claimed:
        return False
    entries = list(sources_for(app_id)[:MAX_SOURCES])
    data, quality = build_graph(entries, sources_for(app_id).count())
    config = AIConfiguration.objects.filter(
        application_id=app_id, purpose="graph_generation", enabled=True
    ).first()
    if config and entries:
        from .graph_ai import enrich_graph

        try:
            data, quality = enrich_graph(app_id, config, entries, data, quality)
        except Exception:
            KnowledgeGraph.objects.filter(
                pk=graph.pk, fingerprint=before, status="building"
            ).update(status="failed")
            raise
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
            application_id=app_id, number=number, fingerprint=before, data=data, quality=quality
        )
        locked.data, locked.quality = data, quality
        locked.fingerprint = before
        locked.version = revision.pk
        locked.status = "ready"
        locked.save()
    return True


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
        if Document.objects.filter(
            application_id=app_id, status__in=["queued", "converting"]
        ).exists():
            continue
        graph = KnowledgeGraph.objects.filter(application_id=app_id).first()
        current_fingerprint = fingerprint(app_id)
        # Failed or interrupted paid calls are not automatically repeated on every worker tick.
        if (
            graph
            and graph.fingerprint == current_fingerprint
            and graph.status in {"failed", "building"}
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


def revision_readable(revision, app_id):
    current = {str(pk): digest for pk, digest in sources_for(app_id).values_list("id", "digest")}
    return all(
        current.get(source["id"]) == source["digest"] for source in revision.data.get("sources", [])
    )


@login_required
@require_http_methods(["GET", "POST"])
def graph_view(request, pk):
    app, grant = access(request.user, pk, "knowledge")
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        if request.POST.get("action") != "retry":
            return HttpResponse("Unknown graph action.", status=400)
        target = KnowledgeGraph.objects.filter(application=app, status__in=["failed", "building"])
        if target.filter(status="building").exists():
            from datetime import timedelta

            target = target.filter(updated_at__lt=timezone.now() - timedelta(minutes=2))
        target.update(status="queued", fingerprint="")
        messages.success(
            request,
            "Eligible failed or interrupted graph generation queued. Provider charges may apply.",
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
    status = (
        "ready"
        if current
        else (
            "unavailable"
            if selected
            else "failed"
            if active_graph and active_graph.status == "failed"
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
    tab = request.GET.get("tab", "graph")
    if tab not in {"graph", "quality", "versions"}:
        tab = "graph"
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
            "pending_documents": Document.objects.filter(
                application=app, status__in=["queued", "converting"]
            ).count(),
        },
    )
