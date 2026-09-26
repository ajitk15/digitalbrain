"""Incident context, precedent retrieval and ServiceOps browser views."""

import hashlib
import re
import uuid
from dataclasses import dataclass

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .models import KnowledgeEntry, TriageHypothesis, TriageRun, TriageVerdict
from .services import audit
from .workbench import access, add_knowledge

INCIDENT_LIMIT = 100
CANDIDATE_LIMIT = 500
#: Words that say nothing about a fault. Two groups: words every incident uses
#: ("alert", "error", "service"), and English filler. The filler used to count: a
#: live CareOps precedent matched on "and, export, for", and "the, other, clinic"
#: made a token-directory incident look like an export timeout. Changing this list
#: changes every fingerprint, so `manage.py serviceops_rebuild` re-profiles.
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
    # English filler, three letters and up (shorter words never become terms).
    "all",
    "also",
    "and",
    "any",
    "are",
    "been",
    "before",
    "being",
    "but",
    "can",
    "could",
    "did",
    "does",
    "each",
    "for",
    "had",
    "has",
    "its",
    "just",
    "more",
    "most",
    "not",
    "now",
    "only",
    "other",
    "our",
    "out",
    "over",
    "same",
    "some",
    "such",
    "than",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "those",
    "too",
    "very",
    "was",
    "were",
    "what",
    "which",
    "while",
    "who",
    "will",
    "would",
    "yet",
    "you",
    # Ticket filler: words any report may use, which a small application's
    # history makes look rare - and so, weighted by rarity, telling. They are not.
    "another",
    "both",
    "data",
    "earlier",
    "few",
    "large",
    "larger",
    "less",
    "many",
    "morning",
    "much",
    "normal",
    "normally",
    "report",
    "reported",
    "reports",
    "return",
    "returning",
    "returns",
    "second",
    "seconds",
    "small",
    "smaller",
    "started",
    "ticket",
    # Timing phrasings are folded into the "timeout" concept; alone, "at a time"
    # or "some time" says nothing.
    "time",
    "times",
    "today",
    "user",
    "users",
    "work",
    "working",
    "works",
    "yesterday",
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


#: Ops phrasings that mean the same thing, folded into one concept word before
#: matching. Numbers are otherwise discarded as ids, which threw away "504" and
#: "401" - the most telling words in many tickets. Deterministic and free: the
#: same text always gives the same terms. A concept word joins the text; the
#: original words stay, so nothing already matched stops matching.
CONCEPTS = (
    ("timeout", r"\b(?:504|gateway[ -]time-?out|timed[ -]?out|time[ -]?outs?|timing[ -]out)\b"),
    ("auth_failure", r"\b(?:401|403|unauthori[sz]ed|forbidden|access[ -]denied)\b"),
    ("server_error", r"\b(?:500|502|503|internal[ -]server[ -]error|bad[ -]gateway)\b"),
    ("db_lock", r"\b(?:database[ -]is[ -]locked|dead-?locks?|lock[ -]wait)\b"),
    ("slowness", r"\b(?:slow(?:ness|ly)?|latency|p95|p99|sluggish)\b"),
    ("out_of_memory", r"\b(?:oom|out[ -]of[ -]memory|memory[ -]exhausted)\b"),
    ("disk_full", r"\b(?:disk[ -]full|no[ -]space[ -]left|out[ -]of[ -]disk)\b"),
    ("crash", r"\b(?:crash(?:ed|es|ing)?|crash-?loop|segfault)\b"),
)
_CONCEPT_PATTERNS = tuple((word, re.compile(pattern)) for word, pattern in CONCEPTS)
CONCEPT_WORDS = frozenset(word for word, _ in CONCEPTS)


def stem(word):
    """A light, predictable stem: plural and tense endings only.

    "exports" and "export" were two terms, as were "refused" and "refuses".
    Deliberately crude rather than clever: a stemmer that sometimes merges the
    wrong words would be worse than one that merges too few.
    """
    if "_" in word:
        return word
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for ending in ("ing", "ed", "es"):
        if len(word) > len(ending) + 3 and word.endswith(ending):
            return word[: -len(ending)]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def symptom_terms(title, description):
    """A stable signature of what was reported, ignoring ids, timestamps and filler.

    Concepts first, while the numbers that name them are still there; then ids,
    dates and bare numbers go; then each word is stemmed.
    """
    text = f"{title} {description[:2000]}".lower()
    concepts = [word for word, pattern in _CONCEPT_PATTERNS if pattern.search(text)]
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", " ", text)
    text = re.sub(r"\b(?:20\d{2}-\d\d-\d\d|\d\d:\d\d(?::\d\d)?)\b", " ", text)
    text = re.sub(r"\b(?:0x)?[0-9a-f]{8,}\b", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    words = (
        stem(term)
        for term in re.findall(r"[a-z][a-z0-9_]{2,}", text)
        if term not in STOP_WORDS
    )
    return frozenset(term for term in words if term not in STOP_WORDS) | frozenset(concepts)


def term_weights(app):
    """How telling each symptom term is in this application: rare terms count more.

    Inverse document frequency over the application's incident profiles, which
    already hold each incident's terms. "export" is in half of CareOps' tickets
    and says little; "fhir" or "consent" say a lot. Computed per request - a few
    hundred rows - so it follows every import without a job to keep it current.
    """
    from math import log

    from .models import IncidentProfile

    counts = {}
    total = 0
    for terms in IncidentProfile.objects.filter(application=app).values_list("terms", flat=True):
        total += 1
        for term in set(terms or ()):
            counts[term] = counts.get(term, 0) + 1
    ceiling = log((total + 1) / 1) + 1
    weights = {term: log((total + 1) / (count + 1)) + 1 for term, count in counts.items()}
    return weights, ceiling


#: A concept word stands for a whole family of phrasings someone chose on
#: purpose, so it counts for more than an ordinary word of the same rarity.
CONCEPT_BOOST = 1.5


def weight_of(term, weights):
    table, ceiling = weights
    weight = table.get(term, ceiling)
    return weight * CONCEPT_BOOST if "_" in term or term in CONCEPT_WORDS else weight


def similarity(left, right, weights):
    """Weighted cosine similarity of two term sets, from 0 (nothing) to 1 (same)."""
    shared = left & right
    if not shared:
        return 0.0

    def square(terms):
        return sum(weight_of(term, weights) ** 2 for term in terms)

    return square(shared) / ((square(left) ** 0.5) * (square(right) ** 0.5))


def telling(terms, weights, limit=3):
    """The most telling terms, for a reason a person can read."""
    ranked = sorted(terms, key=lambda term: (-weight_of(term, weights), term))
    return [term.replace("_", " ") for term in ranked[:limit]]


def fingerprint(title, description):
    """Same normalized symptoms produce the same opaque identifier."""
    terms = sorted(symptom_terms(title, description))
    return hashlib.sha256(" ".join(terms).encode()).hexdigest()[:16] if terms else ""


def verified(entry):
    return entry.active and hashlib.sha256(entry.content.encode()).hexdigest() == entry.digest


@dataclass(frozen=True)
class Record:
    """An incident or change read once: its header fields and its symptoms."""

    entry: KnowledgeEntry
    fields: dict
    description: str
    terms: frozenset
    signature: str


def parse(entry):
    fields, description = fields_and_description(entry)
    return Record(
        entry=entry,
        fields=fields,
        description=description,
        terms=symptom_terms(entry.title, description),
        signature=fingerprint(entry.title, description),
    )


def is_resolved(fields):
    """Whether a record says it is finished, in any of the ways connectors say it."""
    return bool(
        fields.get("Resolved")
        or fields.get("Close code")
        or fields.get("Close notes")
        or fields.get("Status", "").lower() in {"resolved", "closed", "done"}
        or fields.get("State", "").lower() in {"resolved", "closed"}
    )


def is_open(fields):
    """Open for "the same event": not resolved and not closed. Narrower than
    `not is_resolved` on purpose - a close note on a still-open ticket is a
    draft, and that ticket may well be a second report of what is happening now."""
    state = fields.get("State", "").casefold()
    return not (fields.get("Resolved") or state in {"resolved", "closed"})


def _same(key, left, right):
    return bool(left.get(key)) and left[key].casefold() == right.get(key, "").casefold()


def score_pair(record, other, weights):
    """How far a resolved incident is a precedent for another: the one definition.

    Returns (score, reasons, differences, similarity). A score of 0 means not a
    precedent. The page, triage, the replay command and the operations graph all
    score through here, so none of them can disagree about what "similar" means.
    """
    fields, theirs = record.fields, other.fields
    score = 0
    reasons = []
    if _same("CI", fields, theirs):
        score += 4
        reasons.append("same CI")
    if _same("Service", fields, theirs):
        score += 3
        reasons.append("same service")
    if _same("Environment", fields, theirs):
        score += 1
        reasons.append("same environment")
    if record.signature and record.signature == other.signature:
        score += 4
        reasons.append("same symptom signature")
    alike = similarity(record.terms, other.terms, weights)
    if alike >= SIMILAR_SYMPTOMS:
        # Up to 8, so what was reported can outweigh merely sharing a CI and
        # a service (7) - which every ticket on a busy component does.
        score += round(alike * 8)
        reasons.append(
            "similar symptoms: " + ", ".join(telling(record.terms & other.terms, weights))
        )
    differences = [
        f"different {key.lower()}: {theirs[key]}"
        for key in ("Service", "CI", "Environment")
        if fields.get(key) and theirs.get(key) and fields[key].casefold() != theirs[key].casefold()
    ]
    return score, reasons, differences, alike


def same_event(record, other, weights):
    """Whether two open incidents look like one event, and why; None if not.

    When both carry a service it must match; then they need an identical
    fingerprint or symptoms at least `RELATED_SIMILARITY` alike, weighted so
    rare words count more than common ones.
    """
    if not record.terms:
        return None
    mine, theirs = record.fields.get("Service"), other.fields.get("Service")
    if mine and theirs and mine.casefold() != theirs.casefold():
        return None
    alike = similarity(record.terms, other.terms, weights)
    same = bool(record.signature) and record.signature == other.signature
    if not same and alike < RELATED_SIMILARITY:
        return None
    return {
        "same_fingerprint": same,
        "similarity": round(alike, 2),
        "shared": telling(record.terms & other.terms, weights, limit=6),
    }


def precedent_rows(app, incident, limit=5, as_of=None):
    """Rank resolved incidents, without mistaking lexical fit for confidence."""
    record = parse(incident)
    fields, terms = record.fields, record.terms
    weights = term_weights(app)
    candidates = incident_queryset(app).exclude(pk=incident.pk)
    if as_of is not None:
        candidates = candidates.filter(created_at__lte=as_of)
    narrowing = Q()
    if fields.get("CI"):
        narrowing |= Q(content__icontains=f"CI: {fields['CI']}")
    if fields.get("Service"):
        narrowing |= Q(content__icontains=f"Service: {fields['Service']}")
    if terms:
        # The five most telling words, not the first five alphabetically. A
        # concept word is not literally in any title, so it narrows nothing.
        plain = [term for term in terms if "_" not in term]
        for term in sorted(plain, key=lambda term: (-weight_of(term, weights), term))[:5]:
            narrowing |= Q(title__icontains=term)
    if narrowing:
        candidates = candidates.filter(narrowing)
    rows = []
    for candidate in candidates.order_by("-created_at")[:CANDIDATE_LIMIT]:
        if not verified(candidate):
            continue
        theirs = parse(candidate)
        other = theirs.fields
        if as_of is not None:
            from .serviceops_triage import _date

            resolved_at = _date(other.get("Resolved"))
            if resolved_at is None or resolved_at > as_of:
                continue
        if not is_resolved(other):
            continue
        score, reasons, differences, _ = score_pair(record, theirs, weights)
        if not score:
            continue
        excerpt = other.get("Close notes") or theirs.description[:450]
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
                "differences": differences,
                "excerpt": excerpt,
            }
        )
    rows.sort(
        key=lambda row: (-row["score"], -row["entry"].created_at.timestamp(), str(row["entry"].pk))
    )
    return rows[:limit]


#: How alike two reports must be, 0 to 1, to count as similar at all.
SIMILAR_SYMPTOMS = 0.12
#: How alike two open incidents must be to be listed as possibly one event. A
#: shared common word is coincidence; an identical fingerprint always qualifies.
RELATED_SIMILARITY = 0.35


def related_open_rows(app, incident, limit=5):
    """Open incidents that look like the same event, most alike first.

    The one definition, used by the page and by the triage evidence pack. They
    used to disagree: the page wanted an identical fingerprint and said "no
    other open incident", while the pack matched any title word and sent the
    model an incident the reader had just been told did not exist. A live clinic
    C timeout and its near-duplicate report were worded differently, so the
    fingerprint never matched - sharing terms is what finds a second report.

    Open means not resolved or closed. When both carry a service it must match;
    then the incident needs an identical fingerprint or symptoms at least
    `RELATED_SIMILARITY` alike, weighted so rare words count more than common ones.
    """
    record = parse(incident)
    if not record.terms:
        return []
    weights = term_weights(app)
    rows = []
    for candidate in incident_queryset(app).exclude(pk=incident.pk).order_by("-created_at")[:500]:
        if not verified(candidate):
            continue
        theirs = parse(candidate)
        if not is_open(theirs.fields):
            continue
        match = same_event(record, theirs, weights)
        if match:
            rows.append({"entry": candidate, "fields": theirs.fields, **match})
    rows.sort(key=lambda row: (not row["same_fingerprint"], -row["similarity"]))
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
    # The quote itself as the excerpt: it is what stands in the source, so it
    # can be checked there, and it is what a model may cite word for word. The
    # relation it supports goes with it as context.
    return [
        dict(row, excerpt=row["quote"], context=row["relation"])
        for row in citations
        if row["id"] != str(incident.pk)
    ][:5], True


#: A run that has stopped moving, and so has a brief (or a reason it has none).
FINISHED = {"completed", "evidence"}


def step_model_label(app, run):
    """The model the model step used, or will use: what the badge beside it says."""
    from .models import AIConfiguration
    from .workbench import MODEL_LABELS

    if run is not None and run.model:
        return MODEL_LABELS.get(run.model, run.model)
    config = AIConfiguration.objects.filter(
        application=app, purpose="serviceops_triage", enabled=True
    ).first()
    return MODEL_LABELS.get(config.model, config.model) if config else "AI off"


def score_breakdown(run):
    """Each part of the evidence score with its weight, for the page to show.

    Read from the same constants the scorer uses, so what is shown is what was
    computed rather than a description of it.
    """
    from .serviceops_triage import (
        INSUFFICIENT_BELOW,
        LOW_BELOW,
        SCORE_LABELS,
        SCORE_WEIGHTS,
    )

    components = run.score_components or {}
    parts = []
    for key, weight in SCORE_WEIGHTS.items():
        value = float(components.get(key) or 0)
        label, meaning = SCORE_LABELS[key]
        parts.append(
            {
                "label": label,
                "meaning": meaning,
                "weight": round(weight * 100),
                "value": round(value * 100),
                "points": round(weight * value, 3),
            }
        )
    return {
        "parts": parts,
        "raw": float(components.get("raw_score") or 0),
        "insufficient_below": INSUFFICIENT_BELOW,
        "low_below": LOW_BELOW,
    }


def labelled_hypotheses(app, run):
    """A run's hypotheses, each citation named by its ticket rather than its id.

    A citation stores the source id, which is what verification needs. Shown as
    such it read "cb2a252b-0ddf-...: consent enforcement moves..." - the one
    thing a reader needs to recognise, which ticket said it, was missing. The
    label is looked up inside this application only, like the link beside it.
    """
    hypotheses = list(run.hypotheses.all())
    ids = {citation["id"] for item in hypotheses for citation in item.citations}
    labels = {}
    for entry in KnowledgeEntry.objects.filter(application=app, pk__in=ids):
        fields, _ = fields_and_description(entry)
        number = fields.get("Number", "")
        labels[str(entry.pk)] = (number, entry.title)
    for item in hypotheses:
        # A headline to scan; older hypotheses have none, so their statement's
        # first sentence stands in for one.
        first = item.statement.split(". ")[0].rstrip(".")
        if len(first) > 110:
            first = first[:108].rsplit(" ", 1)[0].rstrip(",;:") + "…"
        item.headline = item.title or first
        item.labelled_citations = [
            {
                **citation,
                "number": labels.get(citation["id"], ("", ""))[0],
                "title": labels.get(citation["id"], ("", "Source"))[1],
            }
            for citation in item.citations
        ]
        # What the verdict form offers as "the actual cause": the records this
        # idea cited, once each, never the incident itself.
        seen = {str(run.incident_id)}
        item.cause_choices = []
        for citation in item.labelled_citations:
            if citation["id"] not in seen:
                seen.add(citation["id"])
                item.cause_choices.append(citation)
    return hypotheses


def create_manual_incident(user, app, values):
    """An incident typed in rather than imported, profiled like an imported one."""
    access(user, app.pk, "knowledge", write=True)
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
    entry = add_knowledge(user, app.pk, values["title"], content)
    from .ops_graph import ensure_current
    from .serviceops_triage import profile

    profile(entry)
    ensure_current(app)
    return entry


def incident_url(app, incident_id, run_id=None):
    url = f"{reverse('serviceops', args=[app.pk])}?incident={incident_id}"
    return f"{url}&run={run_id}" if run_id else url


def latest_runs(app):
    """Each incident's most recent run, for the list's Last triage column."""
    latest = {}
    for run in TriageRun.objects.filter(application=app).only(
        "id", "incident_id", "number", "status", "band"
    ).order_by("-number"):
        latest.setdefault(run.incident_id, run)
    return latest


@login_required
@require_http_methods(["GET", "POST"])
def serviceops(request, pk):
    """The incident list, or one incident: its triage and the evidence behind it."""
    app, grant = access(request.user, pk, "service_ops")
    access(request.user, pk, "knowledge")
    if request.method == "POST":
        access(request.user, pk, "service_ops", write=True)
        if request.POST.get("action") == "manual":
            manual = ManualIncidentForm(request.POST)
            if not manual.is_valid():
                messages.error(request, "Enter a title and description for the incident.")
                return redirect("serviceops", pk=app.pk)
            entry = create_manual_incident(request.user, app, manual.cleaned_data)
            return redirect(incident_url(app, entry.pk))
        try:
            incident_uuid = uuid.UUID(request.POST.get("incident", ""))
        except ValueError:
            raise Http404 from None
        incident = get_object_or_404(incident_queryset(app), pk=incident_uuid)
        from .serviceops_triage import queue_run

        # Queued, not run here: the serviceops worker does the steps and this
        # page follows them, the way a Code Factory run is followed.
        try:
            run = queue_run(request.user, app.pk, incident)
        except ValidationError:
            raise Http404 from None
        return redirect(incident_url(app, incident.pk, run.pk))

    incident_id = request.GET.get("incident", "").strip()
    if not incident_id:
        latest = latest_runs(app)
        unassessed = set(
            TriageHypothesis.objects.filter(
                run__in=[run.pk for run in latest.values() if run.status == "completed"],
                verdicts__isnull=True,
            ).values_list("run__incident_id", flat=True)
        )
        incidents = []
        for entry in incident_queryset(app).order_by("-created_at")[:INCIDENT_LIMIT]:
            incident_fields, _ = fields_and_description(entry)
            incidents.append(
                {
                    "entry": entry,
                    "number": incident_fields.get("Number", ""),
                    "priority": incident_fields.get("Priority", ""),
                    "priority_tone": priority_tone(incident_fields.get("Priority")),
                    "state": incident_fields.get("State") or incident_fields.get("Status", ""),
                    "service": incident_fields.get("Service", ""),
                    "last_run": latest.get(entry.pk),
                    "needs_assessment": entry.pk in unassessed,
                }
            )
        show = request.GET.get("show", "")
        priority = request.GET.get("priority", "")
        counts = {
            "all": len(incidents),
            "untriaged": sum(1 for item in incidents if not item["last_run"]),
            "unassessed": sum(1 for item in incidents if item["needs_assessment"]),
        }
        priorities = sorted({item["priority_tone"] for item in incidents} - {"unknown"})
        if show == "untriaged":
            incidents = [item for item in incidents if not item["last_run"]]
        elif show == "unassessed":
            incidents = [item for item in incidents if item["needs_assessment"]]
        if priority in priorities:
            incidents = [item for item in incidents if item["priority_tone"] == priority]
        return render(
            request,
            "serviceops.html",
            {
                "application": app,
                "grant": grant,
                "incidents": incidents,
                "view": "incidents",
                "show": show if show in {"untriaged", "unassessed"} else "",
                "priority": priority if priority in priorities else "",
                "priorities": priorities,
                "counts": counts,
            },
        )

    try:
        incident_uuid = uuid.UUID(incident_id)
    except ValueError:
        raise Http404 from None
    selected = get_object_or_404(incident_queryset(app), pk=incident_uuid)
    if not verified(selected):
        raise Http404
    fields, description = fields_and_description(selected)
    from .ops_graph import neighbourhood, neighbourhood_map

    # The same walk triage makes, so the page shows exactly what a run would use.
    walk = neighbourhood(app, selected)
    drawn_map = neighbourhood_map(walk)
    run = pick_run(app, selected, request.GET.get("run", ""))
    return render(
        request,
        "serviceops_incident.html",
        {
            "application": app,
            "grant": grant,
            "view": "incidents",
            "selected": selected,
            "fields": fields,
            "priority_tone": priority_tone(fields.get("Priority")),
            "number": fields.get("Number", ""),
            "state": fields.get("State") or fields.get("Status", ""),
            "description": description,
            "precedents": walk["precedents"],
            "related_open": walk["related"],
            "changes": walk["changes"],
            "confirmed": walk["confirmed"],
            "graph": walk["passages"],
            "graph_available": walk["graph_available"],
            "map": drawn_map,
            "map_legend": map_legend(drawn_map),
            "origin": origin_link(selected),
            "typical_cost": typical_cost(app),
            **run_context(app, run),
            "run_count": TriageRun.objects.filter(application=app, incident=selected).count(),
        },
    )


#: The incident map's key, in the order a reader meets the rings.
MAP_LEGEND = (
    ("incident", "This incident"),
    ("component", "Component"),
    ("service", "Service"),
    ("symptom", "Symptom"),
    ("confirmed", "Confirmed cause"),
    ("change", "Change"),
    ("precedent", "Similar, resolved"),
    ("related", "Maybe the same event"),
    ("passage", "Document passage"),
)


def map_legend(drawn_map):
    """The key for the kinds this map actually draws, and no others."""
    drawn = {item["kind"] for item in (drawn_map or {}).get("items", [])}
    return [(kind, label) for kind, label in MAP_LEGEND if kind in drawn]


def origin_link(entry):
    """(label, url) for the ticket this incident was imported from, or None.

    Only an http(s) address the connector recorded; a hand-added incident has none.
    """
    url = entry.source or ""
    if not url.startswith(("https://", "http://")):
        return None
    return ("Open in ServiceNow" if "service-now.com" in url else "Open original", url)


def typical_cost(app):
    """What a triage has cost here lately, for the note under the button.

    Recorded spend, never an estimate made up for the page. None until a triage
    has been paid for, and the note then says so rather than guessing.
    """
    from decimal import Decimal

    from .models import AIUsage

    amounts = list(
        AIUsage.objects.filter(application=app, purpose="serviceops_triage")
        .order_by("-created_at")
        .values_list("amount", "currency")[:10]
    )
    if not amounts:
        return None
    average = sum((amount for amount, _ in amounts), Decimal(0)) / len(amounts)
    return f"{amounts[0][1]} {average:.2f}"


def pick_run(app, incident, run_id):
    """The run asked for, or the incident's latest; None when it no longer holds."""
    if run_id:
        try:
            run_uuid = uuid.UUID(run_id)
        except ValueError:
            raise Http404 from None
        run = get_object_or_404(TriageRun, pk=run_uuid, application=app, incident=incident)
    else:
        run = (
            TriageRun.objects.filter(application=app, incident=incident)
            .order_by("-number")
            .first()
        )
    if run and run.incident_digest != incident.digest:
        return None
    if run and run.status in FINISHED:
        from .serviceops_triage import run_still_verified

        if not run_still_verified(run):
            return None
    return run


def run_context(app, run, verified_brief=True):
    """What a run's section, page and popup all show about it.

    A finished run whose evidence has since changed keeps its steps, but its
    brief is withheld: quotes that no longer match their sources are not shown.
    """
    finished = bool(run and run.status in FINISHED and verified_brief)
    from .serviceops_triage import plain_limitations

    return {
        "run": run,
        "notes": plain_limitations(run) if finished else [],
        "run_live": bool(run and run.status in {"queued", "running"}),
        "hypotheses": labelled_hypotheses(app, run) if finished else [],
        "score": score_breakdown(run) if finished else None,
        "model_label": step_model_label(app, run),
    }


@login_required
def serviceops_runs(request, pk):
    """Every triage run in this application, newest first, with how each ended."""
    app, grant = access(request.user, pk, "service_ops")
    access(request.user, pk, "knowledge")
    runs = (
        TriageRun.objects.filter(application=app)
        .select_related("incident", "requested_by")
        .prefetch_related("hypotheses")
        .order_by("-number")
    )
    incident_id = request.GET.get("incident", "").strip()
    focus = None
    if incident_id:
        try:
            focus = get_object_or_404(incident_queryset(app), pk=uuid.UUID(incident_id))
        except ValueError:
            raise Http404 from None
        runs = runs.filter(incident=focus)
    rows = []
    for run in runs[:200]:
        incident_fields, _ = fields_and_description(run.incident)
        rows.append(
            {
                "run": run,
                "number": incident_fields.get("Number", ""),
                "hypotheses": len(run.hypotheses.all()),
            }
        )
    return render(
        request,
        "serviceops_runs.html",
        {"application": app, "grant": grant, "view": "runs", "rows": rows, "focus": focus},
    )


@login_required
def serviceops_run(request, pk, run_id):
    """One run on a page of its own: every step, the score, the hypotheses.

    Opened as a popup from the incident and from the runs list; with
    JavaScript off it is simply this page.
    """
    app, grant = access(request.user, pk, "service_ops")
    access(request.user, pk, "knowledge")
    run = get_object_or_404(
        TriageRun.objects.select_related("incident", "requested_by"), pk=run_id, application=app
    )
    incident_fields, _ = fields_and_description(run.incident)
    stale = run.status in FINISHED and (
        not run.incident.active or pick_run(app, run.incident, str(run.pk)) is None
    )
    return render(
        request,
        "serviceops_run.html",
        {
            "application": app,
            "grant": grant,
            "view": "runs",
            "incident": run.incident,
            "incident_number": incident_fields.get("Number", ""),
            "stale": stale,
            **run_context(app, run, verified_brief=not stale),
        },
    )


@login_required
def serviceops_guide(request, pk):
    """How triage works, for someone meeting it for the first time. A popup."""
    app, grant = access(request.user, pk, "service_ops")
    from .serviceops_triage import INSUFFICIENT_BELOW, LOW_BELOW

    return render(
        request,
        "serviceops_guide.html",
        {
            "application": app,
            "grant": grant,
            "typical_cost": typical_cost(app),
            "model_label": step_model_label(app, None),
            "insufficient_below": INSUFFICIENT_BELOW,
            "low_below": LOW_BELOW,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def serviceops_incident_new(request, pk):
    """Add an incident by hand. A page of its own, opened as a popup."""
    app, grant = access(request.user, pk, "service_ops", write=True)
    access(request.user, pk, "knowledge", write=True)
    form = ManualIncidentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        entry = create_manual_incident(request.user, app, form.cleaned_data)
        return redirect(incident_url(app, entry.pk))
    return render(
        request,
        "serviceops_incident_new.html",
        {"application": app, "grant": grant, "form": form},
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

    run = hypothesis.run
    if request.POST.get("action") == "retract":
        # Only the person who gave a verdict takes it back. The confirmed-cause
        # edge it made goes with it (CASCADE), so the graph forgets it too.
        try:
            verdict_id = uuid.UUID(request.POST.get("verdict_id", ""))
        except ValueError:
            raise Http404 from None
        mine = get_object_or_404(
            TriageVerdict, pk=verdict_id, hypothesis=hypothesis, user=request.user
        )
        with transaction.atomic():
            audit(
                request.user,
                "serviceops.verdict_retracted",
                hypothesis.pk,
                app.product.portfolio.organization,
                details={"verdict": mine.verdict, "had_cause": bool(mine.actual_cause_id)},
            )
            mine.delete()
        return redirect(incident_url(app, run.incident_id, run.pk))

    if not run_still_verified(run):
        raise Http404
    verdict = request.POST.get("verdict")
    if verdict not in {"accepted", "rejected", "partial"}:
        raise ValidationError("Choose a verdict.")
    note = request.POST.get("note", "").strip()[:1000]
    cause = None
    chosen = request.POST.get("cause", "").strip()
    if chosen and verdict != "rejected":
        # A closed choice: only a record this idea cited, in this application,
        # still verifying. The id never widens scope - a foreign or retired one
        # reads as "not found".
        quotes = {citation["id"]: citation["quote"] for citation in hypothesis.citations}
        if chosen not in quotes or chosen == str(run.incident_id):
            raise Http404
        cause = KnowledgeEntry.objects.filter(pk=chosen, application=app, active=True).first()
        if cause is None or not verified(cause):
            raise Http404
    with transaction.atomic():
        record = TriageVerdict.objects.create(
            hypothesis=hypothesis,
            user=request.user,
            verdict=verdict,
            note=note,
            actual_cause=cause,
            confirmed_at=timezone.now() if cause else None,
        )
        if cause:
            from .ops_graph import confirm_cause

            confirm_cause(app, run.incident, cause, record, quotes[chosen])
        audit(
            request.user,
            "serviceops.verdict",
            hypothesis.pk,
            app.product.portfolio.organization,
            details={"verdict": verdict, "actual_cause": str(cause.pk) if cause else ""},
        )
    return redirect(incident_url(app, run.incident_id, run.pk))
