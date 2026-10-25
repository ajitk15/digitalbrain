"""Application rules: documents an owner says the work must be checked against.

A coding standard, a security design, a control matrix: an application has
documents that are not just knowledge to cite but rules its own output must
keep. An owner marks one as rules (`KnowledgeEntry.rules_by`, audited); this
reads its statements and says which apply to a piece of work, so the reviewer
checks the work against them. Code Factory's plan review is the first consumer.

This is the first, measured step, and it is deliberately narrow:

* **Rules only add checks.** A rule is shown to a reviewer as something to check
  the work against. It never approves, relaxes a gate, grants access or changes
  what any invariant says; a plan that breaks one is still decided by a person.
* **Read by rules, never by a model.** A statement is a numbered rule heading,
  a table row that starts with an identifier (NFR-07, C-09), or a prose line
  that says must, shall or never. Each is quoted from a line of the source,
  and the quote is checked against the live source when it is shown, like any
  citation. A rule from a source restricted or retired since does not show.
* **Which rules apply is ranked, and measured.** BM25 over each rule's own
  section, the terms the work and the rule share coming first (`ranking`), with
  at least `MIN_SHARED` shared terms. `benchmarks/rules-pack.json` labels the
  rule each seeded CarePath ticket breaks, and the scorecard holds the ranking
  to it - "Rules" - before anyone is asked to trust it.
"""

import re
from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .ranking import BM25, query_terms, terms
from .services import audit

#: How many rules one piece of work is shown.
SHOWN = 5
#: Distinct terms a rule and the work must share to apply at all.
MIN_SHARED = 2
#: A rule scoring under this share of the best one is not shown: on the pack the
#: broken rule scored two to four times the next, and a checklist of five where
#: one applies teaches a reviewer to skim it.
RELATIVE = 0.5
#: Statements read from one source: a rules document is a list, not a library.
MAX_PER_SOURCE = 80

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_NUMBERED = re.compile(r"^(?:rule\s+)?\d+(?:\.\d+)*[.)]?\s+\S", re.IGNORECASE)
_ROW_ID = re.compile(r"^\|\s*\**([A-Z][A-Z0-9]*-\d+[A-Z0-9-]*)\**\s*\|")
_NORMATIVE = re.compile(r"\b(must|must not|shall|shall not|never)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    entry_id: str
    source: str
    statement: str
    #: The exact text of the line it was read from, and that line's number.
    quote: str
    line: int
    #: The statement and what its section says, for matching.
    section: str

    @property
    def explanation(self):
        """What a numbered rule's own section says under it, for the reviewer."""
        lines = self.section.splitlines()[1:]
        text = " ".join(line.strip() for line in lines if line.strip() and not line.startswith("#"))
        return text if len(text) <= 280 else text[:279].rsplit(" ", 1)[0] + "…"


def _cells(line):
    return [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]


def statements(entry):
    """The rules one source states, in order, each quoted from one of its lines."""
    lines = entry.content.splitlines()
    found = []
    headings = [
        (number, len(match.group(1)), match.group(2))
        for number, line in enumerate(lines, 1)
        if (match := _HEADING.match(line))
    ]

    def section(start, level):
        end = next(
            (number for number, depth, _ in headings if number > start and depth <= level),
            len(lines) + 1,
        )
        return "\n".join(lines[start : end - 1])

    in_heading = {number for number, _, _ in headings}
    # A numbered rule's own section explains it: a "must" in there is the same
    # rule again, not another one.
    explained = set()
    for number, level, text in headings:
        if level > 1 and _NUMBERED.match(text):
            statement = re.sub(r"^(?:rule\s+)?\d+(?:\.\d+)*[.)]?\s+", "", text, flags=re.I)
            body = section(number, level)
            found.append((number, statement, f"{text}\n{body}"))
            explained.update(range(number + 1, number + 1 + len(body.splitlines())))
    for number, line in enumerate(lines, 1):
        if number in in_heading or number in explained or not line.strip():
            continue
        match = _ROW_ID.match(line)
        if match:
            cells = [cell for cell in _cells(line) if cell]
            found.append((number, " · ".join(cells[:3])[:300], line))
        elif not line.lstrip().startswith("|") and _NORMATIVE.search(line):
            found.append((number, line.strip()[:300], line))
    found.sort(key=lambda item: item[0])
    return [
        Rule(str(entry.pk), entry.title, statement, lines[number - 1], number, text)
        for number, statement, text in found[:MAX_PER_SOURCE]
    ]


def sources(app):
    """The sources an owner marked as rules, that AI may read and that still answer."""
    from .models import KnowledgeEntry

    return KnowledgeEntry.objects.answerable().filter(
        application=app, rules_by__isnull=False
    )


def verified(rule, entry):
    """The quote is still the line it was read from, in the live source."""
    lines = entry.content.splitlines()
    return 0 < rule.line <= len(lines) and lines[rule.line - 1] == rule.quote


def applicable(app, text, limit=SHOWN, entries=None):
    """[(rule, shared terms)] that apply to this work, best first."""
    entries = list(sources(app) if entries is None else entries)
    found = [rule for entry in entries for rule in statements(entry)]
    if not found:
        return []
    by_id = {str(entry.pk): entry for entry in entries}
    index = BM25([terms(rule.section) for rule in found])
    query = query_terms(text)
    chosen, best = [], None
    for score, position in index.rank_covering(query):
        rule = found[position]
        shared = [term for term in query if index.counts[position].get(term)]
        if len(shared) < MIN_SHARED:
            continue
        best = score if best is None else best
        if score < RELATIVE * best:
            continue
        if not verified(rule, by_id[rule.entry_id]):
            continue
        chosen.append((rule, shared))
        if len(chosen) >= limit:
            break
    return chosen


def for_run(run, plan=None):
    """The rules that apply to a Code Factory run's ticket and its plan."""
    from .code_factory_evidence import ticket_entry
    from .serviceops import fields_and_description

    entry = ticket_entry(run) if run else None
    if entry is not None:
        _, body = fields_and_description(entry)
        text = f"{entry.title}\n{body}"
    else:
        text = f"{run.ticket_title}\n{run.ticket_body}" if run else ""
    if plan is not None:
        text += "\n" + "\n".join(item.title for item in plan.items.all())
    if not text.strip():
        return []
    return applicable(run.application if run else plan.application, text)


@login_required
@require_http_methods(["POST"])
def mark_view(request, pk, entry_id):
    """An owner marks a source as rules, or stops. A plain POST from the source page."""
    from .models import KnowledgeEntry
    from .workbench import access

    app, grant = access(request.user, pk, "knowledge", write=True)
    if grant.role != "owner":
        raise PermissionDenied("Only an owner decides what the work is checked against.")
    entry = get_object_or_404(KnowledgeEntry, pk=entry_id, application=app, active=True)
    on = request.POST.get("rules") == "on"
    KnowledgeEntry.objects.filter(pk=entry.pk).update(
        rules_by=request.user if on else None, rules_at=timezone.now() if on else None
    )
    audit(
        request.user,
        "knowledge.rules_on" if on else "knowledge.rules_off",
        entry.pk,
        app.product.portfolio.organization,
    )
    count = len(statements(entry))
    messages.success(
        request,
        f"Marked as rules: {count} statement{'s' if count != 1 else ''} read from it. Code "
        "Factory's plan review shows the ones that apply to each ticket."
        if on
        else "No longer used as rules.",
    )
    if request.POST.get("next") == "document" and entry.document_id:
        return redirect(f"{reverse('document-detail', args=[pk, entry.document_id])}#rules")
    return redirect(f"{reverse('knowledge-detail', args=[pk, entry.pk])}#rules")
