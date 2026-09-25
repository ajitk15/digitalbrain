"""Incident context, precedent retrieval and ServiceOps browser views."""

import hashlib
import re
import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from .models import KnowledgeEntry, TriageHypothesis, TriageRun, TriageVerdict
from .services import audit
from .workbench import access, add_knowledge

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
    "Start",
    "End",
    "Change type",
}


class ManualIncidentForm(forms.Form):
    title = forms.CharField(max_length=200)
    description = forms.CharField(max_length=5000, widget=forms.Textarea(attrs={"rows": 4}))
    number = forms.CharField(max_length=80, required=False)
    service = forms.CharField(max_length=200, required=False)
    ci = forms.CharField(max_length=200, required=False, label="Configuration item")
    priority = forms.ChoiceField(
        choices=[("", "Unknown")] + [(str(value), f"P{value}") for value in range(1, 6)],
        required=False,
    )


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


def precedent_rows(app, incident, limit=5, as_of=None):
    """Rank resolved incidents, without mistaking lexical fit for confidence."""
    fields, description = fields_and_description(incident)
    terms = symptom_terms(incident.title, description)
    signature = fingerprint(incident.title, description)
    candidates = incident_queryset(app).exclude(pk=incident.pk)
    if as_of is not None:
        candidates = candidates.filter(created_at__lte=as_of)
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
        if as_of is not None:
            from .serviceops_triage import _date

            resolved_at = _date(other.get("Resolved"))
            if resolved_at is None or resolved_at > as_of:
                continue
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
                "differences": [
                    f"different {key.lower()}: {other[key]}"
                    for key in ("Service", "CI", "Environment")
                    if fields.get(key)
                    and other.get(key)
                    and fields[key].casefold() != other[key].casefold()
                ],
                "excerpt": excerpt,
            }
        )
    rows.sort(
        key=lambda row: (-row["score"], -row["entry"].created_at.timestamp(), str(row["entry"].pk))
    )
    return rows[:limit]


def related_open_rows(app, incident, limit=5):
    """Group live incidents with exactly the same stable symptom fingerprint."""
    _, description = fields_and_description(incident)
    signature = fingerprint(incident.title, description)
    if not signature:
        return []
    rows = []
    for candidate in incident_queryset(app).exclude(pk=incident.pk).order_by("-created_at")[:500]:
        if not verified(candidate):
            continue
        other, other_description = fields_and_description(candidate)
        if other.get("Resolved") or other.get("State", "").casefold() in {"resolved", "closed"}:
            continue
        if fingerprint(candidate.title, other_description) == signature:
            rows.append({"entry": candidate, "fields": other})
            if len(rows) == limit:
                break
    return rows


def change_rows(app, incident, limit=5):
    """Recent same-CI changes preceding the incident, when timestamps are known."""
    from datetime import timedelta

    from .serviceops_triage import _date

    fields, _ = fields_and_description(incident)
    opened = _date(fields.get("Opened"))
    ci = fields.get("CI")
    if not (opened and ci):
        return []
    candidates = KnowledgeEntry.objects.filter(
        application=app,
        active=True,
        source__icontains="change_request.do",
        content__icontains=f"CI: {ci}",
    ).order_by("-created_at")[:500]
    rows = []
    for entry in candidates:
        if not verified(entry):
            continue
        other, description = fields_and_description(entry)
        started = _date(other.get("Start"))
        if other.get("CI", "").casefold() != ci.casefold() or not started:
            continue
        gap = opened - started
        if timedelta(0) <= gap <= timedelta(days=2):
            rows.append(
                {
                    "entry": entry,
                    "fields": other,
                    "excerpt": description[:500] or entry.title,
                    "hours_before": round(gap.total_seconds() / 3600, 1),
                }
            )
    rows.sort(key=lambda row: (row["hours_before"], str(row["entry"].pk)))
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
@require_http_methods(["GET", "POST"])
def serviceops(request, pk):
    app, grant = access(request.user, pk, "service_ops")
    access(request.user, pk, "knowledge")
    if request.method == "POST":
        access(request.user, pk, "service_ops", write=True)
        if request.POST.get("action") == "manual":
            access(request.user, pk, "knowledge", write=True)
            manual = ManualIncidentForm(request.POST)
            if not manual.is_valid():
                messages.error(request, "Enter a title and description for the incident.")
                return redirect("serviceops", pk=app.pk)
            values = manual.cleaned_data
            header = "\n".join(
                f"{key}: {' '.join(value.split())}"
                for key, value in (
                    ("Type", "Incident"),
                    ("Number", values["number"]),
                    ("Service", values["service"]),
                    ("CI", values["ci"]),
                    ("Priority", values["priority"]),
                )
                if value
            )
            content = f"{values['title']}\n\n{header}\n\n{values['description']}"
            entry = add_knowledge(request.user, app.pk, values["title"], content)
            from .serviceops_triage import profile

            profile(entry)
            return redirect(f"{reverse('serviceops', args=[app.pk])}?incident={entry.pk}")
        try:
            incident_uuid = uuid.UUID(request.POST.get("incident", ""))
        except ValueError:
            raise Http404 from None
        incident = get_object_or_404(incident_queryset(app), pk=incident_uuid)
        from .serviceops_triage import create_run

        try:
            run = create_run(request.user, app.pk, incident)
        except ValidationError:
            raise Http404 from None
        return redirect(
            f"{reverse('serviceops', args=[app.pk])}?incident={incident.pk}&run={run.pk}"
        )
    incident_id = request.GET.get("incident", "").strip()
    selected = None
    fields = {}
    description = ""
    precedents = []
    related_open = []
    changes = []
    graph = []
    graph_available = False
    run = None
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
        related_open = related_open_rows(app, selected)
        changes = change_rows(app, selected)
        graph, graph_available = graph_rows(app, selected)
        run_id = request.GET.get("run", "")
        if run_id:
            try:
                run_uuid = uuid.UUID(run_id)
            except ValueError:
                raise Http404 from None
            run = get_object_or_404(
                TriageRun.objects.prefetch_related("hypotheses"),
                pk=run_uuid,
                application=app,
                incident=selected,
            )
        else:
            run = TriageRun.objects.filter(application=app, incident=selected).first()
        if run and run.incident_digest != selected.digest:
            run = None
        if run:
            from .serviceops_triage import run_still_verified

            if not run_still_verified(run):
                run = None
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
            "related_open": related_open,
            "changes": changes,
            "graph": graph,
            "graph_available": graph_available,
            "fingerprint": fingerprint(selected.title, description) if selected else "",
            "run": run,
            "manual_form": ManualIncidentForm(),
        },
    )


@login_required
@require_POST
def triage_verdict(request, pk, hypothesis_id):
    app, _ = access(request.user, pk, "service_ops", write=True)
    access(request.user, pk, "knowledge")
    hypothesis = get_object_or_404(
        TriageHypothesis.objects.select_related("run"),
        pk=hypothesis_id,
        run__application=app,
    )
    from .serviceops_triage import run_still_verified

    if not run_still_verified(hypothesis.run):
        raise Http404
    verdict = request.POST.get("verdict")
    if verdict not in {"accepted", "rejected", "partial"}:
        raise ValidationError("Choose a verdict.")
    note = request.POST.get("note", "").strip()[:1000]
    with transaction.atomic():
        TriageVerdict.objects.create(
            hypothesis=hypothesis,
            user=request.user,
            verdict=verdict,
            note=note,
        )
        audit(
            request.user,
            "serviceops.verdict",
            hypothesis.pk,
            app.product.portfolio.organization,
            details={"verdict": verdict},
        )
    run = hypothesis.run
    return redirect(
        f"{reverse('serviceops', args=[app.pk])}?incident={run.incident_id}&run={run.pk}"
    )
