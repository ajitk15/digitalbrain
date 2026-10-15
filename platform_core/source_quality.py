"""How good an uploaded document is for the graph, and why.

The document counterpart of `ticket_quality`: a fixed rubric read from the
converted text, no model and no charge, so the same source always gets the
same score and every finding names the line it came from. Tables are read
through `graphs.walk`, the walk `build_graph` itself makes, so what this says
about a table cannot disagree with what the graph made of it.

It suggests and never blocks. A low score does not stop a graph being built,
and personal data is not part of it: that is a reason to restrict a source,
not a sign of a bad one, and the source page says it separately.

It is for documents - uploads, links, folders, SharePoint - and not for typed
records or imported tickets, which are not documents; a Jira ticket has its own
score. The result is stored on `KnowledgeEntry.quality` when a document's text
is added.
`QUALITY_VERSION` is part of what is stored; a rule change bumps it, and a
stored result from older rules is worked out again when it is next read.

Beside it, what kind of document this is and how good it is as one
(`document_types`), as a second score: the six checks here say whether the
text can be read, and do not change with its kind.
"""

import re
from collections import Counter
from dataclasses import asdict

from . import document_types
from .graphs import MAX_RECORDS_PER_SOURCE, walk
from .models import KnowledgeEntry
from .ticket_quality import PLACEHOLDER, Check, quote

QUALITY_VERSION = 4

#: Text this short was most likely not read: a scanned page, an image, a
#: template with nothing filled in.
MIN_CHARACTERS = 200
#: What a table header says when nobody named the column: pandas' `Unnamed: 3`,
#: a converter's `Column 2`, a spreadsheet's `nan`.
GENERIC_HEADER = re.compile(r"^(?:unnamed:?\s*\d+|column\s*\d+|col\d+|nan|none)$", re.I)
#: Below this mean word confidence an OCR page is hard to read: the extractor's
#: LOW_CONFIDENCE, which reports it.
LOW_CONFIDENCE = 60
#: Above this share of empty cells a table says less than its shape suggests.
EMPTY_CELLS = 0.3
#: A prose document longer than this with no heading reads as one long list.
UNSTRUCTURED_LINES = 15
#: Where a document says what it is: read from the opening only, as a reader would.
OPENING = 3000
IDENTITY = {
    # Labelled ("Document: BRD-CAREPATH-001", "| Document | GOV-..."), shaped like
    # one (two letter groups, then a number), or leading the title ("# ADR-0001").
    # A bare BR-05 or FR-01 anywhere else is a requirement the document cites.
    "document number": re.compile(
        r"\b(?i:document|doc(?:ument)?\s*(?:id|no\.?|number))\b\W{0,4}[A-Z][\w-]*\d"
        r"|\b[A-Z]{2,}-[A-Z]{2,}(?:-[A-Z]+)*-\d{2,}\b"
        r"|\A\W*[A-Z]{2,6}-\d{3,}\b"
    ),
    "version": re.compile(r"\b(?:version|revision|rev\.?)\b\W{0,4}v?\d|\bv\d+\.\d+\b", re.I),
    "owner": re.compile(
        r"\b(?:owner|author|prepared by|approved by|deciders?|maintainer)\b", re.I
    ),
    "date": re.compile(
        r"\b(?:19|20)\d\d-\d\d-\d\d\b"
        r"|\b\d{1,2} (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w* (?:19|20)\d\d\b"
        r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w* \d{1,2},? (?:19|20)\d\d\b",
        re.I,
    ),
    "status": re.compile(r"\bstatus\b\W{0,4}\w", re.I),
}
#: How many line numbers a finding lists before it says "and N more".
SHOWN_LINES = 4


def where(lines):
    """"line 12", "lines 12, 40 and 88", "lines 12, 40, 88, 90 and 3 more"."""
    numbers = [str(number) for number in lines]
    if len(numbers) == 1:
        return f"line {numbers[0]}"
    if len(numbers) <= SHOWN_LINES:
        return f"lines {', '.join(numbers[:-1])} and {numbers[-1]}"
    return f"lines {', '.join(numbers[:SHOWN_LINES])} and {len(numbers) - SHOWN_LINES} more"


def sentence(text):
    """Capital first letter, the rest untouched: `str.capitalize` lowercases Word."""
    return text[:1].upper() + text[1:]


def plural(count, word, many=None):
    return f"{count} {word if count == 1 else many or word + 's'}"


def tables_in(content):
    """Each table the graph will read: its header line, header and rows."""
    tables = []
    for item in walk(content):
        if item.kind == "header":
            tables.append({"line": item.index + 1, "headers": item.headers, "rows": []})
        elif item.kind == "row":
            tables[-1]["rows"].append(item.values)
    return tables


def is_field_list(table):
    """A two-column table of **Label** | value, such as a document's front matter.

    Its header is blank because it has no columns to name: each row names itself.
    """
    rows = table["rows"]
    return len(table["headers"]) == 2 and bool(rows) and all(
        len(row) == 2 and re.fullmatch(r"\*\*[^*]+\*\*", row[0]) for row in rows
    )


def readable_check(content, report):
    rule = (f"At least {MIN_CHARACTERS} characters of text, no unreadable characters, and "
            "every page read.")
    label = "Readable text"
    problems, partial = [], True
    if len(content.strip()) < MIN_CHARACTERS:
        problems.append(f"only {plural(len(content.strip()), 'character')} of text were read")
        partial = False
    garbled = content.count("\ufffd")
    if garbled:
        problems.append(f"{plural(garbled, 'character')} could not be decoded")
        partial = partial and garbled * 100 < len(content)
    report = report or {}
    pages, blank = report.get("pages") or 0, report.get("pages_without_text") or []
    if blank:
        problems.append(f"{plural(len(blank), 'page')} of {pages} had no text "
                        f"(page{'' if len(blank) == 1 else 's'} "
                        f"{', '.join(str(page) for page in blank[:SHOWN_LINES])}"
                        f"{' and more' if len(blank) > SHOWN_LINES else ''})")
        partial = partial and len(blank) * 2 < pages
    scanned = report.get("ocr_pages") or []
    low = report.get("low_confidence_pages") or []
    if low:
        # A poor scan is the one case where text was read and still cannot be
        # trusted: it is said loudly, before anything is cited from it.
        problems.append(f"{plural(len(low), 'page')} read by OCR with under "
                        f"{LOW_CONFIDENCE}% confidence ({where(low).replace('line', 'page')})")
        partial = False
    if not problems:
        read = f" from {plural(pages, 'page')}" if pages else ""
        if scanned:
            mean = round(sum(page["confidence"] for page in scanned) / len(scanned))
            read += f", {len(scanned)} of them by OCR at {mean}% confidence"
        return Check("readable", label, 20, 20, "good",
                     f"{len(content):,} characters of text were read{read}.", "", rule)
    finding = sentence("; ".join(problems)) + "."
    improve = ("Upload the original Word, Excel or PowerPoint file rather than a scan or a "
               "PDF of one" + (", or turn on OCR so scanned pages are read." if blank else "."))
    if low:
        improve = ("Scan the pages again at 300 dpi or better, or upload the original "
                   "document: text read with low confidence may be cited wrongly.")
    if partial:
        return Check("readable", label, 20, 10, "partial", finding, improve, rule)
    return Check("readable", label, 20, 0, "missing", finding, improve, rule)


def tables_check(content):
    rule = ("Every table has a named header row and at least one row under it; no "
            f"placeholder headers, no repeated rows, under {round(EMPTY_CELLS * 100)}% "
            "empty cells.")
    label = "Tables the graph can read"
    tables = tables_in(content)
    if not tables:
        return Check("tables", label, 25, 25, "good",
                     "No tables: the graph reads the text as sections.", "", rule)
    blank, generic, header_only, repeated = [], [], [], []
    cells = empty = 0
    for table in tables:
        headers = [header.strip("* ").strip() for header in table["headers"]]
        if not any(headers):
            if not is_field_list(table):
                blank.append(table["line"])
        elif any(GENERIC_HEADER.match(header) for header in headers if header):
            generic.append(table["line"])
        if not table["rows"]:
            header_only.append(table["line"])
        if any(count > 1 for count in Counter(map(tuple, table["rows"])).values()):
            repeated.append(table["line"])
        for row in table["rows"]:
            cells += len(row)
            empty += sum(not value for value in row)
    flawed = set(blank) | set(generic) | set(header_only) | set(repeated)
    problems = []
    if blank:
        problems.append(f"{len(blank)} with a blank header row "
                        f"({where(blank)}), which the graph can only call “column 1”, “column 2”")
    if generic:
        problems.append(f"{len(generic)} with placeholder column names "
                        f"({where(generic)})")
    if header_only:
        problems.append(f"{len(header_only)} with a header and no rows "
                        f"({where(header_only)}), usually a table broken across pages")
    if repeated:
        problems.append(f"{len(repeated)} with repeated rows ({where(repeated)})")
    share = empty / cells if cells else 0
    if share > EMPTY_CELLS:
        problems.append(f"{round(share * 100)}% of table cells are empty")
    if not problems:
        rows = sum(len(table["rows"]) for table in tables)
        return Check("tables", label, 25, 25, "good",
                     f"{plural(len(tables), 'table')} and {plural(rows, 'row')}, every "
                     "column named.", "", rule)
    clean = len(tables) - len(flawed)
    earned = round(25 * clean / len(tables))
    if share > EMPTY_CELLS:
        earned = min(earned, 12)
    finding = f"Of {plural(len(tables), 'table')}: " + "; ".join(problems) + "."
    improve = []
    if blank:
        improve.append("In Word, mark each table's first row as a header row (Table Design → "
                       "Header Row) and upload again")
    if header_only:
        improve.append("for a PDF, upload the Word or Excel original: tables broken across "
                       "pages cannot be put back together reliably")
    if generic:
        improve.append("give every spreadsheet column a name in its first row")
    if repeated or share > EMPTY_CELLS:
        improve.append("remove repeated rows and fill or drop empty columns")
    return Check("tables", label, 25, earned, "partial" if earned * 2 >= 25 else "missing",
                 finding, sentence("; ".join(improve)) + ".", rule)


def structure_check(content):
    rule = (f"A document of more than {UNSTRUCTURED_LINES} lines of text has headings or "
            "tables, so its parts can be told apart.")
    label = "Structure"
    headings = text = tables = 0
    for item in walk(content):
        if item.kind == "header":
            tables += 1
        elif item.kind == "section":
            if item.text.lstrip().startswith("#"):
                headings += 1
            else:
                text += 1
    if headings or tables:
        parts = [plural(n, word) for n, word in ((headings, "heading"), (tables, "table")) if n]
        return Check("structure", label, 15, 15, "good",
                     f"{sentence(' and '.join(parts))} divide {plural(text, 'line')} of "
                     "text.", "", rule)
    if text <= UNSTRUCTURED_LINES:
        return Check("structure", label, 15, 15, "good",
                     f"{sentence(plural(text, 'line'))} of text: short enough to need no "
                     "headings.", "", rule)
    return Check("structure", label, 15, 0, "missing",
                 f"{sentence(plural(text, 'line'))} of text and no headings or tables: "
                 "the graph reads each line as a section with nothing grouping them.",
                 "Use heading styles (Heading 1, Heading 2) in the original, or Markdown "
                 "# headings in a text file, so each part is found under its own name.", rule)


def identity_check(content):
    rule = ("The opening says what the document is: three of a document number, version, "
            "owner, date and status. One or two earns half.")
    label = "Says what it is"
    opening = content[:OPENING]
    found = [name for name, pattern in IDENTITY.items() if pattern.search(opening)]
    lacking = [name for name in IDENTITY if name not in found]
    if len(found) >= 3:
        return Check("identity", label, 15, 15, "good",
                     f"The opening gives its {', '.join(found)}.", "", rule)
    improve = (f"Add a line near the top giving its {', '.join(lacking[:3])}, so a reader "
               "of an answer can tell which document, and which revision of it, was cited.")
    if found:
        return Check("identity", label, 15, 8, "partial",
                     f"The opening gives its {' and '.join(found)} only.", improve, rule)
    return Check("identity", label, 15, 0, "missing",
                 "The opening does not say what the document is: no document number, "
                 "version, owner, date or status.", improve, rule)


def finished_check(content):
    rule = "Nothing left open: TBD, TODO, FIXME, “to be decided”, ??. One or two earns half."
    label = "Finished"
    matches = list(PLACEHOLDER.finditer(content))
    if not matches:
        return Check("finished", label, 15, 15, "good", "Nothing is left as TBD or TODO.",
                     "", rule)
    lines = sorted({content.count("\n", 0, match.start()) + 1 for match in matches})
    finding = (f"{plural(len(matches), 'open placeholder')} ({where(lines)}), the first: "
               f"“{quote(content, matches[0])}”")
    improve = ("Resolve the open points, or mark the document as a draft so nobody cites "
               "an answer that is not settled.")
    if len(matches) <= 2:
        return Check("finished", label, 15, 8, "partial", finding, improve, rule)
    return Check("finished", label, 15, 0, "missing", finding, improve, rule)


def limits_check(content):
    cap = KnowledgeEntry._meta.get_field("content").max_length
    rule = (f"Under {MAX_RECORDS_PER_SOURCE:,} table rows and lines, which is what the graph "
            f"reads of one source, and clear of the {cap:,}-character limit.")
    label = "Within limits"
    read = sum(item.kind in {"row", "section"} for item in walk(content))
    problems = []
    if read > MAX_RECORDS_PER_SOURCE:
        problems.append(f"{read:,} rows and lines, of which the graph reads the first "
                        f"{MAX_RECORDS_PER_SOURCE:,}")
    if len(content) > cap * 0.9:
        problems.append(f"{len(content):,} characters, close to the {cap:,} limit")
    if not problems:
        return Check("limits", label, 10, 10, "good",
                     f"{plural(read, 'row or line', 'rows and lines')}, all within what the "
                     "graph reads.", "", rule)
    return Check("limits", label, 10, 5 if len(problems) == 1 else 0,
                 "partial" if len(problems) == 1 else "missing",
                 sentence("; ".join(problems)) + ".",
                 "Split it into smaller documents, one subject each, so all of it is read.",
                 rule)


def verdict(score, checks):
    """Ready needs the score and no check missing outright: a source whose tables
    the graph cannot read is not ready because its other parts are fine."""
    if score >= 80 and not any(check.state == "missing" for check in checks):
        return "Ready"
    return "Needs work" if score >= 55 else "Poor"


TONES = {"Ready": "good", "Needs work": "fair", "Poor": "poor"}


def ai_named(detected, ai_type, content):
    """The type a model named for this text (`document_ai`), if it may be used.

    Only where the rules said Other, only a verdict the checks accepted, and
    only while every quote it rests on is still in the text exactly - checked
    again here, as a citation is, rather than trusted because it once was.
    """
    if not ai_type or detected["key"] != document_types.OTHER:
        return None
    kind = document_types.BY_KEY.get(ai_type.get("key") or "")
    quotes = ai_type.get("quotes") or []
    if (ai_type.get("verdict") != "accepted" or not kind or kind.key == document_types.OTHER
            or not quotes or not all(quote.get("text", "") in content for quote in quotes)):
        return None
    return kind


def kind_of(content, title="", document_type="", ai_type=None):
    """What the document is: the type a person set, else the one the rules detect,
    else - where the rules said Other - the one a model named and quoted.

    The detected type is kept either way, so the page can say what the rules
    would have chosen beside what a person or a model did.
    """
    detected = document_types.classify(content, title)
    chosen = document_types.BY_KEY.get(document_type)
    if not chosen:
        named = ai_named(detected, ai_type, content)
        if named:
            return {
                **detected,
                "key": named.key,
                "label": named.label,
                "confidence": "ai",
                "source": "ai",
                "override": "",
                "quotes": ai_type["quotes"],
                "model": ai_type.get("model", ""),
                "detected": {"key": detected["key"], "label": detected["label"]},
            }
        return {**detected, "source": "rules", "override": ""}
    return {
        **detected,
        "key": chosen.key,
        "label": chosen.label,
        "source": "person",
        "override": chosen.key,
        "detected": {"key": detected["key"], "label": detected["label"]},
    }


def run(content, report=None, title="", document_type="", ai_type=None):
    """The stored form: the rules' version and each check, as plain values.

    Beside the source check, what kind of document it is and the checks for
    that kind (`document_types`): a separate score, so a document that reads
    well is not marked down for being a kind with no content rules. `ai_type`
    is a stored `document_ai` typing verdict for this exact text, if any.
    """
    content = content or ""
    checks = [
        readable_check(content, report),
        tables_check(content),
        structure_check(content),
        identity_check(content),
        finished_check(content),
        limits_check(content),
    ]
    kind = kind_of(content, title, document_type, ai_type)
    return {
        "version": QUALITY_VERSION,
        "types": document_types.REGISTRY_VERSION,
        "checks": [asdict(check) for check in checks],
        "type": kind,
        "type_checks": [
            asdict(check) for check in document_types.checks(content, kind["key"], title)
        ],
    }


def scored(checks):
    """The result shape `ticket_quality.assess` has, for a list of checks."""
    points = sum(check.points for check in checks)
    earned = sum(check.earned for check in checks)
    score = round(100 * earned / points) if points else 0
    said = verdict(score, checks)
    return {
        "score": score,
        "tone": TONES[said],
        "verdict": said,
        "checks": checks,
        "strengths": [check for check in checks if check.state == "good"],
        "gaps": sorted(
            (check for check in checks if check.state != "good"),
            key=lambda check: -check.missing_points,
        ),
        "earned": earned,
        "points": points,
    }


def result(stored):
    """What a page shows, from the stored form: the source check, the document's
    type, and - when its type has content rules - how good it is as one."""
    shown = scored([Check(**check) for check in stored["checks"]])
    kind = stored.get("type") or {}
    type_checks = [Check(**check) for check in stored.get("type_checks", [])]
    shown["type"] = kind
    shown["content"] = scored(type_checks) if type_checks else None
    if shown["content"]:
        shown["content"]["title"] = f"Content check · {kind['label']}"
    if kind:
        runner = document_types.BY_KEY.get(kind.get("runner_up") or "")
        shown["type"] = {**kind, "runner_up_label": runner.label if runner else ""}
    return shown


def assess(content, report=None, title="", document_type="", ai_type=None):
    return result(run(content, report, title, document_type, ai_type))


def current(stored, document_type=""):
    """Worked out under today's rules and today's registry, for the type a person set."""
    return (
        bool(stored)
        and stored.get("version") == QUALITY_VERSION
        and stored.get("types") == document_types.REGISTRY_VERSION
        and (stored.get("type") or {}).get("override", "") == (document_type or "")
    )


def for_entries(entries):
    """{entry pk: result} for these entries, working out again any that are stale.

    Content is read only for the stale ones, in one query, so a page of sources
    checked under today's rules reads no text at all. What is worked out again
    is written back, so it is worked out once.
    """
    results = {}
    stale = []
    for entry in entries:
        if current(entry.quality, entry.document_type):
            results[entry.pk] = result(entry.quality)
        else:
            stale.append(entry.pk)
    if stale:
        from .document_ai import cached_types

        rows = KnowledgeEntry.objects.filter(pk__in=stale).values_list(
            "pk", "content", "document__conversion_report", "title", "document_type",
            "application_id", "digest",
        )
        rows = list(rows)
        typed = cached_types((app_id, digest) for *_, app_id, digest in rows)
        for pk, content, report, title, document_type, app_id, digest in rows:
            stored = run(content, report, title, document_type, typed.get((app_id, digest)))
            KnowledgeEntry.objects.filter(pk=pk).update(quality=stored)
            results[pk] = result(stored)
    return results
