"""Read-only incident context and precedent retrieval for ServiceOps.

This first slice deliberately makes no model call and assigns no cause or
confidence. A similarity rank describes shared observable signals only.
"""

import hashlib
import re
import uuid

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .models import KnowledgeEntry
from .workbench import access

INCIDENT_LIMIT = 100
CANDIDATE_LIMIT = 500
STOP_WORDS = {
    "about",
    "after",
    "again",
    "alert",
    "error",
    "failed",
    "from",
    "have",
    "incident",
    "into",
    "node",
    "service",
    "that",
    "this",
    "when",
    "with",
}
STRUCTURED_FIELDS = {
    "Key",
    "Number",
    "Type",
    "State",
    "Status",
    "Priority",
    "Impact",
    "Service",
    "CI",
    "Environment",
    "Assignment group",
    "Opened",
    "Resolved",
    "Close code",
    "Close notes",
    "Problem",
    "Caused by change",
    "Updated",
}


def incident_queryset(app):
    """Only active incident sources inside the already-authorized application."""
    return KnowledgeEntry.objects.filter(application=app, active=True).filter(
        Q(source__icontains="incident.do")
        | Q(content__icontains="\nType: Incident\n")
        | Q(content__icontains="\nType: Incident\r\n")
    )


def fields_and_description(entry):
    """Read connector headers without interpreting arbitrary incident prose."""
    parts = entry.content.split("\n\n", 2)
    header = parts[1] if len(parts) > 1 else ""
    fields = {}
    for line in header.splitlines():
        key, separator, value = line.partition(": ")
        if not separator or key not in STRUCTURED_FIELDS:
            break
        fields[key] = value.strip()[:500]
    description = parts[2] if fields and len(parts) > 2 else entry.content.partition("\n\n")[2]
    return fields, description.strip()


def priority_tone(value):
    """Map only known ServiceNow priority numbers to CSS classes."""
    match = re.fullmatch(r"\s*([1-5])(?:\s*[-–:]\s*.+)?\s*", value or "")
    return f"p{match.group(1)}" if match else "unknown"


def symptom_terms(title, description):
    """A stable lexical signature that ignores volatile ids and timestamps."""
    text = f"{title} {description[:2000]}".lower()
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", " ", text)
    text = re.sub(r"\b(?:20\d{2}-\d\d-\d\d|\d\d:\d\d(?::\d\d)?)\b", " ", text)
    text = re.sub(r"\b(?:0x)?[0-9a-f]{8,}\b", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    return frozenset(
        term for term in re.findall(r"[a-z][a-z0-9_]{2,}", text) if term not in STOP_WORDS
    )


def fingerprint(title, description):
    """Same normalized symptoms produce the same opaque identifier."""
    terms = sorted(symptom_terms(title, description))
    return hashlib.sha256(" ".join(terms).encode()).hexdigest()[:16] if terms else ""


def verified(entry):
    return entry.active and hashlib.sha256(entry.content.encode()).hexdigest() == entry.digest


def precedent_rows(app, incident, limit=5):
    """Rank resolved incidents, without mistaking lexical fit for confidence."""
    fields, description = fields_and_description(incident)
    terms = symptom_terms(incident.title, description)
    signature = fingerprint(incident.title, description)
    candidates = incident_queryset(app).exclude(pk=incident.pk)
    narrowing = Q()
    if fields.get("CI"):
        narrowing |= Q(content__icontains=f"CI: {fields['CI']}")
    if fields.get("Service"):
        narrowing |= Q(content__icontains=f"Service: {fields['Service']}")
    if terms:
        for term in sorted(terms)[:5]:
            narrowing |= Q(title__icontains=term)
    if narrowing:
        candidates = candidates.filter(narrowing)
    rows = []
    for candidate in candidates.order_by("-created_at")[:CANDIDATE_LIMIT]:
        if not verified(candidate):
            continue
        other, other_description = fields_and_description(candidate)
        if not (
            other.get("Resolved")
            or other.get("Close code")
            or other.get("Close notes")
            or other.get("Status", "").lower() in {"resolved", "closed", "done"}
            or other.get("State", "").lower() in {"resolved", "closed"}
        ):
            continue
        reasons = []
        score = 0
        if fields.get("CI") and fields["CI"].casefold() == other.get("CI", "").casefold():
            score += 4
            reasons.append("same CI")
        if (
            fields.get("Service")
            and fields["Service"].casefold() == other.get("Service", "").casefold()
        ):
            score += 3
            reasons.append("same service")
        if (
            fields.get("Environment")
            and fields["Environment"].casefold() == other.get("Environment", "").casefold()
        ):
            score += 1
            reasons.append("same environment")
        if signature and signature == fingerprint(candidate.title, other_description):
            score += 4
            reasons.append("same symptom signature")
        overlap = terms & symptom_terms(candidate.title, other_description)
        if overlap:
            score += min(3, len(overlap))
            reasons.append("shared symptoms: " + ", ".join(sorted(overlap)[:3]))
        if not score:
            continue
        excerpt = other.get("Close notes") or other_description[:450]
        if excerpt and excerpt not in candidate.content:
            continue
        rows.append(
            {
                "entry": candidate,
                "fields": other,
                "priority_tone": priority_tone(other.get("Priority")),
                "close_code": other.get("Close code", ""),
                "score": score,
                "reasons": reasons,
                "excerpt": excerpt,
            }
        )
    rows.sort(
        key=lambda row: (-row["score"], -row["entry"].created_at.timestamp(), str(row["entry"].pk))
    )
    return rows[:limit]


def graph_rows(app, incident):
    """The existing graph function performs live source and quote verification."""
    from .graph_ai import graph_citations

    fields, _ = fields_and_description(incident)
    query = " ".join(
        part for part in (incident.title, fields.get("Service"), fields.get("CI")) if part
    )
    try:
        citations = graph_citations(app.pk, query)
    except ValidationError:
        return [], False
    return [row for row in citations if row["id"] != str(incident.pk)][:5], True


@login_required
@require_GET
def serviceops(request, pk):
    app, grant = access(request.user, pk, "service_ops")
    access(request.user, pk, "knowledge")
    incident_id = request.GET.get("incident", "").strip()
    selected = None
    fields = {}
    description = ""
    precedents = []
    graph = []
    graph_available = False
    if incident_id:
        try:
            incident_uuid = uuid.UUID(incident_id)
        except ValueError:
            raise Http404 from None
        selected = get_object_or_404(incident_queryset(app), pk=incident_uuid)
        if not verified(selected):
            raise Http404
        fields, description = fields_and_description(selected)
        precedents = precedent_rows(app, selected)
        graph, graph_available = graph_rows(app, selected)
    incidents = []
    for entry in incident_queryset(app).order_by("-created_at")[:INCIDENT_LIMIT]:
        incident_fields, _ = fields_and_description(entry)
        incidents.append(
            {
                "entry": entry,
                "number": incident_fields.get("Number", ""),
                "priority": incident_fields.get("Priority", ""),
                "priority_tone": priority_tone(incident_fields.get("Priority")),
            }
        )
    return render(
        request,
        "serviceops.html",
        {
            "application": app,
            "grant": grant,
            "incidents": incidents,
            "selected": selected,
            "fields": fields,
            "priority_tone": priority_tone(fields.get("Priority")),
            "description": description,
            "precedents": precedents,
            "graph": graph,
            "graph_available": graph_available,
            "fingerprint": fingerprint(selected.title, description) if selected else "",
        },
    )
