"""How a set of sources connects, before a graph is built from it.

`source_quality` judges one document; this reads them together. It works over
`graphs.sources_for` - exactly the set a graph would be built from, so a
restricted source is left out here as it is there - and calls no model.

Two ways sources meet. **References**: an ID such as BR-05 or C-09 is
*defined* where it starts a table row or a heading, or where three or more
lines of one source start with its prefix, and *mentioned* everywhere else.
That is where documents written by different people actually point at each
other. **Table values**: two sources whose rows hold the same field and value
meet at one value node in the structural graph - keyed exactly as
`build_graph` keys it, so "shares a value" means "is joined in the graph".

Everything it reports is a hint for a person. A reference read as missing may
be defined in the middle of a table cell; a source with few words in common
with the rest may still belong. Nothing here stops a graph being built.

Worked out once per `graphs.fingerprint` and kept per process: the result
depends only on the sources' ids and digests, which the fingerprint covers.
"""

import json
import math
import re
import threading
from collections import Counter, OrderedDict, defaultdict

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET

from . import ranking
from .graphs import MAX_SOURCES, fingerprint, sources_for, walk
from .source_quality import for_entries

#: A reference as documents write them: BR-05, C-09, NFR-06, ADR-0002,
#: INC-2026-0814. Upper case, one to six letters, up to four digits a group.
REFERENCE = re.compile(r"(?<![\w-])([A-Z]{1,6})((?:-\d{1,4})+)(?![\w-])")
#: Standards, encodings and severities name a kind of thing, not one thing.
#: `ticket_quality.NOT_REQUIREMENTS`, plus SEV.
NOT_REFERENCES = {"ISO", "UTF", "SHA", "TLS", "HTTP", "UTC", "SEV", "RFC"}
#: Lines of one source starting with a prefix before that prefix is its list.
LIST_LINES = 3
#: A source whose closest neighbour shares less vocabulary than this is
#: probably about something else. CarePath's lowest is 0.15; a recipe pasted
#: among them scores 0.04.
OFF_TOPIC = 0.08
#: Vocabulary this alike, or the same digest, is the same document twice.
DUPLICATE = 0.9
#: Results kept, most recently used last.
CACHE_SIZE = 16
_cache = OrderedDict()
_lock = threading.Lock()


def references(text):
    for match in REFERENCE.finditer(text):
        if match.group(1) not in NOT_REFERENCES:
            yield match.group(0)


def prefix(reference):
    return reference.split("-", 1)[0]


def defined_in(content):
    """The references this source defines, by where they stand.

    A table row whose first filled cell is the reference, a heading that starts
    with it, or a line starting with a prefix that starts three or more lines
    here - a numbered list of them.
    """
    defined = set()
    for item in walk(content):
        if item.kind == "row":
            first = next((value for value in item.values if value), "")
            match = REFERENCE.match(first.strip().strip("*").strip())
            if match and match.group(1) not in NOT_REFERENCES:
                defined.add(match.group(0))
    leading = []
    for line in content.splitlines():
        match = re.match(r"^\s*(#+\s*)?[*_]*" + REFERENCE.pattern, line)
        if match and match.group(2) not in NOT_REFERENCES:
            reference = match.group(2) + match.group(3)
            if match.group(1):
                defined.add(reference)
            leading.append(reference)
    starts = Counter(prefix(reference) for reference in leading)
    defined |= {reference for reference in leading if starts[prefix(reference)] >= LIST_LINES}
    return defined


def table_values(content):
    """The value nodes this source's rows make, keyed as `build_graph` keys them."""
    values = set()
    for item in walk(content):
        if item.kind == "row":
            for column, value in enumerate(item.values):
                if not value:
                    continue
                field = item.headers[column] if column < len(item.headers) else ""
                field = field or f"column {column + 1}"
                values.add(json.dumps([field.casefold(), value]))
    return values


def spans(found):
    """References as a reader would write them: "US-01 to US-12, R-04"."""
    groups = defaultdict(list)
    for reference in found:
        head, _, number = reference.rpartition("-")
        groups[head].append((int(number), number))
    parts = []
    for head in sorted(groups):
        numbers = sorted(set(groups[head]))
        run = [numbers[0]]
        for current in numbers[1:]:
            if current[0] == run[-1][0] + 1:
                run.append(current)
                continue
            parts.append(span(head, run))
            run = [current]
        parts.append(span(head, run))
    return parts


def span(head, run):
    first, last = f"{head}-{run[0][1]}", f"{head}-{run[-1][1]}"
    if len(run) == 1:
        return first
    return f"{first} and {last}" if len(run) == 2 else f"{first} to {last}"


def vectors(contents):
    """Unit TF-IDF vectors over `ranking.terms`, one per source."""
    counts = [Counter(ranking.terms(content)) for content in contents]
    frequency = Counter(term for count in counts for term in count)
    total = len(counts)
    idf = {term: math.log((total + 1) / (seen + 1)) + 1 for term, seen in frequency.items()}
    result = []
    for count in counts:
        weights = {term: n * idf[term] for term, n in count.items()}
        norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
        result.append({term: weight / norm for term, weight in weights.items()})
    return result


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(term, 0.0) for term, weight in a.items())


def analyse(entries):
    """What the sources say about each other. `entries` need pk, title, content, digest."""
    entries = list(entries)
    defines = [defined_in(entry.content) for entry in entries]
    mentions = [set(references(entry.content)) for entry in entries]
    values = [table_values(entry.content) for entry in entries]
    vectors_of = vectors([entry.content for entry in entries])

    definers = defaultdict(set)
    for index, found in enumerate(defines):
        for reference in found:
            definers[reference].add(index)
    holders = defaultdict(set)
    for index, found in enumerate(values):
        for value in found:
            holders[value].add(index)

    rows = []
    for index, entry in enumerate(entries):
        cites = {other for reference in mentions[index] - defines[index]
                 for other in definers.get(reference, ()) if other != index}
        cited_by = {other for other, found in enumerate(mentions)
                    if other != index and found & defines[index]}
        shares = {other for value in values[index]
                  for other in holders[value] if other != index}
        alike = sorted(
            ((cosine(vectors_of[index], vectors_of[other]), other)
             for other in range(len(entries)) if other != index),
            reverse=True,
        )
        rows.append({
            "entry": entry,
            "defines": len(defines[index]),
            "cites": len(cites),
            "cited_by": len(cited_by),
            "shares": len(shares),
            "connected": bool(cites or cited_by or shares),
            "closest": entries[alike[0][1]] if alike else None,
            "similarity": round(alike[0][0], 2) if alike else None,
        })

    mentioned_elsewhere = defaultdict(set)
    for index, found in enumerate(mentions):
        for reference in found:
            mentioned_elsewhere[reference].add(index)
    unreferenced = defaultdict(list)
    for reference, where in definers.items():
        if not mentioned_elsewhere.get(reference, set()) - where:
            unreferenced[prefix(reference)].append(reference)
    missing = sorted(reference for reference in mentioned_elsewhere if reference not in definers)

    duplicates = []
    for index, entry in enumerate(entries):
        for other in range(index + 1, len(entries)):
            same = entry.digest == entries[other].digest
            alike = cosine(vectors_of[index], vectors_of[other])
            if same or alike >= DUPLICATE:
                duplicates.append({"first": entry, "second": entries[other],
                                   "identical": same, "similarity": round(alike, 2)})

    many = len(entries) > 1
    return {
        "sources": rows,
        "count": len(entries),
        "connected": sum(row["connected"] for row in rows),
        "by_reference": sum(bool(row["cites"] or row["cited_by"]) for row in rows),
        "by_value": sum(bool(row["shares"]) for row in rows),
        "unreferenced": [
            {"prefix": head, "count": len(found), "spans": spans(found)}
            for head, found in sorted(unreferenced.items())
        ],
        "missing": spans(missing),
        "isolated": [row for row in rows if many and not row["connected"]],
        # Two sources unlike each other are unlike symmetrically: it takes a
        # third to say which one is the odd one out.
        "off_topic": [row for row in rows
                      if len(entries) > 2 and row["similarity"] is not None
                      and row["similarity"] < OFF_TOPIC],
        "duplicates": duplicates,
        "defined": len(definers),
    }


def for_application(app_id):
    """`analyse` over what the graph would be built from, cached per fingerprint."""
    key = (str(app_id), fingerprint(app_id))
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    result = analyse(sources_for(app_id)[:MAX_SOURCES].only("id", "title", "content", "digest"))
    with _lock:
        _cache[key] = result
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def summary(app_id):
    """One line's worth for the Sources tab and the Generate dialog."""
    from .models import KnowledgeEntry

    found = for_application(app_id)
    documents = KnowledgeEntry.objects.filter(
        pk__in=[row["entry"].pk for row in found["sources"]], document__isnull=False
    ).only("id", "quality")
    checks = for_entries(documents)
    verdicts = Counter(result["verdict"] for result in checks.values())
    # Sources, not findings: one that needs work and stands alone is one source.
    attention = {pk for pk, result in checks.items() if result["verdict"] != "Ready"}
    attention |= {row["entry"].pk for row in found["isolated"] + found["off_topic"]}
    attention |= {pair["second"].pk for pair in found["duplicates"]}
    return {
        "ready": verdicts["Ready"],
        "needs_work": verdicts["Needs work"],
        "poor": verdicts["Poor"],
        "checked": sum(verdicts.values()),
        "attention": len(attention),
        "count": found["count"],
        "connected": found["connected"],
    }


@login_required
@require_GET
def source_check(request, pk):
    """The set, read together. Opened as a popup from Sources and from Generate,
    and a page of its own: with JavaScript off the link simply comes here."""
    from .models import KnowledgeEntry
    from .workbench import access

    app, grant = access(request.user, pk, "knowledge")
    found = for_application(app.pk)
    documents = KnowledgeEntry.objects.filter(
        pk__in=[row["entry"].pk for row in found["sources"]], document__isnull=False
    ).only("id", "quality", "document_id")
    checks = for_entries(documents)
    document_of = {entry.pk: entry.document_id for entry in documents}
    rows = [
        {**row, "check": checks.get(row["entry"].pk),
         "document_id": document_of.get(row["entry"].pk)}
        for row in found["sources"]
    ]
    rows.sort(key=lambda row: (
        row["check"]["score"] if row["check"] else 101, row["connected"], row["entry"].title
    ))
    verdicts = Counter(check["verdict"] for check in checks.values())
    return render(request, "source_check.html", {
        "application": app,
        "grant": grant,
        "found": found,
        "rows": rows,
        "verdicts": {"ready": verdicts["Ready"], "needs_work": verdicts["Needs work"],
                     "poor": verdicts["Poor"]},
        "checked": len(checks),
    })
