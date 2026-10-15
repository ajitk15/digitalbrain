"""What kind of document a source is, and what that kind should contain.

`source_quality` asks whether a document can be read; this asks whether it is
a good one of its kind. A business requirements document with no requirements
in it reads perfectly and is still no use, and only knowing what it is meant to
be can say so.

A closed registry, like `ontology.TYPES`: each type names the words that call
a document one (its title, its document number), the sections a reader expects
in it and the checks its content must pass. Classifying reads the document
itself - its own title, its headings, its table columns - and only weakly the
file name, because people name files any way they like while the parts of a
kind of document stay much the same. No model is called and nothing is
charged, so the same text always gets the same type, and every type says which
lines chose it. When no type is clearly ahead the answer is Other, said with
what it looked most like: a confident wrong type would judge a document against
the wrong list. A person can always set the type, and their choice wins.

It suggests and never blocks, like the source check. Tables are read through
`graphs.walk`, the walk the graph makes.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from . import ranking
from .graphs import walk
from .ticket_quality import NOT_REQUIREMENTS, Check, quote

#: Part of what is stored with a result, so a registry change re-runs it.
REGISTRY_VERSION = 1
OTHER = "other"
#: A type needs this much evidence, and this far ahead of the next one, to be
#: chosen. Below either, the document is Other and says what it looked like.
MIN_SCORE = 5
MIN_MARGIN = 2
#: A margin this wide is said as high confidence.
CLEAR_MARGIN = 5
#: A section with fewer words than this under its heading is there in name only.
THIN_WORDS = 12
OPENING = 3000

#: Words that make a requirement impossible to test: ISO/IEC/IEEE 29148's list
#: of vague terms, in the forms documents actually use.
VAGUE = re.compile(
    r"\b(?:user[- ]friendly|easy to use|easy|simple|intuitive|seamless(?:ly)?|fast|quick(?:ly)?"
    r"|efficient(?:ly)?|flexible|robust|adequate(?:ly)?|appropriate(?:ly)?|as needed"
    r"|as required|if possible|where possible|as far as possible|state[- ]of[- ]the[- ]art"
    r"|optimal|minimal|sufficient|reasonable|and/or|etc\.?|best effort|normally|usually"
    r"|approximately)(?!\w)",
    re.I,
)
#: A reference as documents define them: BR-05, NFR-06, US-01, REG-07, R-04.
DEFINED = re.compile(r"[A-Z]{1,6}-\d{1,4}\b")
MEASURE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|ms\b|s\b|sec(?:ond)?s?\b|min(?:ute)?s?\b|hours?\b|days?\b"
    r"|weeks?\b|months?\b|years?\b|rps\b|requests\b|per (?:second|minute|hour|day|month)"
    r"|users\b|patients\b|tb\b|gb\b|mb\b)"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twelve|thirty|ninety)\s+"
    r"(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\b",
    re.I,
)
#: A rule a test can check without a number: "no", "never", "every".
ABSOLUTE = re.compile(r"\b(?:no|never|every|each|all|only|none|must not|always)\b", re.I)
STEP = re.compile(r"^\s*(?:\d{1,2}[.)]|step\s+\d+\b|- \[[ x]\])\s*\S", re.I)
REGULATION = re.compile(
    r"§\s?\d|\b(?:HIPAA|GDPR|CFR|HITECH|SOX|PCI[- ]DSS|ISO(?:/IEC)?\s?\d{4,5}|NIST)\b"
)
#: An endpoint by method and path, in prose or a table: GET /patients, `GET` | `/health`.
ENDPOINT = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)`?\s*(?:\|\s*)?`?/[\w{}/.?=&-]*")
TIME = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
STORY = re.compile(
    r"\bas\b(?: an?\b| the\b)?(.{1,120}?)\bi (?:want|need|can)\b(.{1,300}?)(?:\.|$)", re.I | re.M
)
ACCEPTANCE = re.compile(r"acceptance criteria|\bgiven\b.{1,200}\bwhen\b.{1,200}\bthen\b", re.I)
ADR_STATUS = re.compile(
    r"\bstatus\b\W{0,6}(proposed|accepted|rejected|superseded|deprecated|draft)\b", re.I
)
MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
SLIDE = re.compile(r"^<!--\s*slide number:\s*\d+\s*-->$", re.I)
BOLD_LINE = re.compile(r"^\*{1,2}([^*]{2,80})\*{1,2}:?$")
UNDERLINE = re.compile(r"^[=\-~_*\s]{4,}$")


@lru_cache(maxsize=8192)
def fold(word):
    """`ranking.term`, then a final "e" dropped from a long word: `stem` turns
    "consequences" into "consequenc" and leaves "consequence" whole."""
    word = ranking.term(word)
    return word[:-1] if len(word) >= 6 and word.endswith("e") else word


def tokens(text):
    """Words as `fold` reads them, keeping the short ones: "out of scope"."""
    return [fold(word) for word in re.findall(r"[a-z]+", text.casefold())]


def phrase(text):
    return tuple(tokens(text))


def covers(name, have):
    """Every token of `name` is in `have`; a long one may be the start of one,
    so "escalat" names "Escalation" and "Escalating"."""
    return bool(name) and all(
        token in have or (len(token) >= 5 and any(word.startswith(token) for word in have))
        for token in name
    )


def contains(sequence, wanted):
    """`wanted` appears in `sequence` as consecutive tokens."""
    size = len(wanted)
    return bool(size) and any(
        tuple(sequence[start:start + size]) == wanted for start in range(len(sequence) - size + 1)
    )


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    #: Phrases, any one of which in a heading names this section.
    names: tuple
    required: bool = True

    def matches(self, heading_tokens):
        found = set(heading_tokens)
        return any(covers(phrase(name), found) for name in self.names)


@dataclass(frozen=True)
class Rule:
    """A content check: `check(parsed, points, **options)` returns a `Check`."""
    check: object
    points: int
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentType:
    key: str
    label: str
    #: Phrases that name the type in a title: "business requirements".
    names: tuple
    #: Document-number prefixes that name it: BRD-..., ADR-0001.
    prefixes: tuple = ()
    #: Prefixes of the items it numbers: BR-01, NFR-06, R-04.
    items: tuple = ()
    sections: tuple = ()
    #: Column groups its tables usually carry, each a tuple of synonyms.
    columns: tuple = ()
    #: Patterns in the text that are characteristic of it.
    patterns: tuple = ()
    rules: tuple = ()
    description: str = ""


# ── reading the document ──────────────────────────────────────────────────────


@dataclass
class Heading:
    line: int
    level: int
    text: str
    tokens: list
    words: int = 0


@dataclass
class Parsed:
    content: str
    lines: list
    headings: list
    tables: list
    title: str
    file_tokens: list
    title_line: int = 0

    @property
    def title_tokens(self):
        return tokens(self.title)

    @property
    def opening(self):
        return self.content[:OPENING]


def clean_heading(text):
    """A heading's words without its numbering, markup or trailing references."""
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[A-Z]{1,4}-\d+\b|[ivx]+\.)\s*", "", text)
    return text.strip(" :—-")


def heading_of(text, after_slide):
    """(level, text) when a line of text is a heading, else None."""
    stripped = text.strip()
    match = MARKDOWN_HEADING.match(stripped)
    if match:
        return len(match.group(1)), match.group(2)
    if after_slide:
        return 2, stripped
    letters = re.sub(r"[^A-Za-z]", "", stripped)
    if len(letters) >= 4 and letters.isupper() and len(stripped.split()) <= 8:
        return 2, stripped
    match = BOLD_LINE.match(stripped)
    if match:
        return 4, match.group(1)
    return None


def parse(content, title=""):
    content = content or ""
    lines = content.splitlines()
    headings, tables = [], []
    after_slide = False
    first = True
    for item in walk(content):
        if item.kind == "header":
            tables.append({"line": item.index + 1, "headers": item.headers, "rows": []})
            first = False
            continue
        if item.kind == "row":
            tables[-1]["rows"].append(item.values)
            continue
        text = item.text.strip()
        if SLIDE.match(text):
            after_slide = True
            continue
        if UNDERLINE.match(text):
            continue
        found = heading_of(text, after_slide)
        after_slide = False
        # The first line is the title when it reads as one: a Word title paragraph,
        # a text file's banner, the first slide's heading.
        if first and len(text.split()) <= 16 and not text.endswith(".") and (
            not found or (found[0] == 2 and not MARKDOWN_HEADING.match(text))
        ):
            found = (1, found[1] if found else text)
        first = False
        if found:
            level, name = found
            headings.append(Heading(item.index + 1, level, name, tokens(clean_heading(name))))
    # The words under each heading, down to the next heading at its level or above.
    for position, heading in enumerate(headings):
        end = len(lines)
        for later in headings[position + 1:]:
            if later.level <= heading.level:
                end = later.line - 1
                break
        body = "\n".join(lines[heading.line:end])
        heading.words = len(re.findall(r"[^\W_]{2,}", body))
    top = next((h for h in headings if h.level == 1), None)
    named = top.text if top else ""
    for table in tables:
        for row in table["rows"]:
            if len(row) >= 2 and row[0].strip("* ").casefold() == "title":
                named = named or row[1]
    stem = re.sub(r"\.[a-z0-9]{1,5}$", "", (title or "").rsplit("/", 1)[-1], flags=re.I)
    return Parsed(content, lines, headings, tables, clean_heading(named), tokens(stem),
                  top.line if top else 0)


def matched_sections(parsed, kind):
    """{section key: the heading that names it}, first heading wins; the title is
    the document's name, not one of its sections."""
    found = {}
    for section in kind.sections:
        for heading in parsed.headings:
            if heading.line != parsed.title_line and section.matches(heading.tokens):
                found[section.key] = heading
                break
    return found


def column_groups(headers, groups):
    """The groups (by index) some header names."""
    header_tokens = [set(tokens(header)) for header in headers if header]
    return {
        index
        for index, synonyms in enumerate(groups)
        if any(covers(phrase(name), have) for name in synonyms for have in header_tokens)
    }


# ── classifying ───────────────────────────────────────────────────────────────


def score(parsed, kind):
    points, evidence = 0, []
    title = parsed.title_tokens
    name = next((n for n in kind.names if contains(title, phrase(n))), None)
    if name:
        points += 6
        evidence.append(f"its title, “{parsed.title}”")
    else:
        name = next((n for n in kind.names if contains(parsed.file_tokens, phrase(n))), None)
        if name:
            points += 2
            evidence.append(f"its file name says “{name}”")
    for prefix in kind.prefixes:
        # A document's own number - BRD-CAREPATH-001, or ADR-0001 in its title -
        # not a requirement the opening happens to list: NFR-03 is a row.
        match = re.search(rf"(?<![\w-]){prefix}-(?:[A-Z][A-Z0-9]*-)+\d+\b", parsed.opening) or (
            re.search(rf"(?<![\w-]){prefix}-\d[\d-]*\b", parsed.title)
        )
        if match:
            points += 3
            evidence.append(f"document number {match.group(0)}")
            break
    # What it numbers is what it is: a document defining NFR-01 to NFR-14 is a
    # list of non-functional requirements, whatever it is called.
    numbered = items(parsed)
    if len(numbered) >= 3 and numbered[0][0].split("-")[0] in kind.items:
        points += 3
        evidence.append(f"items numbered {span([name for name, _, _ in numbered])}")
    sections = matched_sections(parsed, kind)
    for section in kind.sections:
        if section.key in sections:
            points += 2 if section.required else 1
    if sections:
        labels = [sections[s.key].text for s in kind.sections if s.key in sections]
        evidence.append("sections " + ", ".join(f"“{clean_heading(t)}”" for t in labels[:5])
                        + (f" and {len(labels) - 5} more" if len(labels) > 5 else ""))
    if kind.columns:
        seen = set()
        for table in parsed.tables:
            seen |= column_groups(table["headers"], kind.columns)
        if seen:
            points += min(len(seen), 3)
            evidence.append(f"tables with {len(seen)} of the columns it usually has")
    for pattern, label in kind.patterns:
        if len(pattern.findall(parsed.content)) >= 2:
            points += 4
            evidence.append(label)
    return points, evidence


def classify(content, title=""):
    """The type the rules choose, with what chose it.

    {"key", "label", "confidence", "score", "evidence", "runner_up"}; Other when
    nothing is clearly ahead, with `runner_up` naming what it looked most like.
    """
    parsed = parse(content, title)
    ranked = sorted(
        ((score(parsed, kind), kind) for kind in TYPES if kind.key != OTHER),
        key=lambda item: -item[0][0],
    )
    (best, evidence), kind = ranked[0]
    second = ranked[1][0][0] if len(ranked) > 1 else 0
    if best < MIN_SCORE or best - second < MIN_MARGIN:
        return {
            "key": OTHER,
            "label": BY_KEY[OTHER].label,
            "confidence": "low",
            "score": best,
            "evidence": evidence,
            "runner_up": kind.key if best else "",
        }
    return {
        "key": kind.key,
        "label": kind.label,
        "confidence": "high" if best - second >= CLEAR_MARGIN else "medium",
        "score": best,
        "evidence": evidence,
        "runner_up": ranked[1][1].key if second else "",
    }


# ── judging it as that type ───────────────────────────────────────────────────


def plural(count, word, many=None):
    return f"{count} {word if count == 1 else many or word + 's'}"


def listed(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def state_for(earned, points):
    if earned >= points:
        return "good"
    return "partial" if earned * 2 >= points else "missing"


def sections_check(parsed, kind, required, points):
    wanted = [s for s in kind.sections if s.required == required]
    found = matched_sections(parsed, kind)
    label = "Expected sections" if required else "Recommended sections"
    names = listed(s.label.lower() for s in wanted)
    rule = (f"Has a section for each of: {names}, each with at least {THIN_WORDS} words. "
            + ("A thin section earns half." if required else "Each one present earns its share."))
    present = [s for s in wanted if s.key in found and found[s.key].words >= THIN_WORDS]
    thin = [s for s in wanted if s.key in found and found[s.key].words < THIN_WORDS]
    absent = [s for s in wanted if s.key not in found]
    credit = len(present) + 0.5 * len(thin)
    earned = round(points * credit / len(wanted))
    parts = []
    if present:
        parts.append("Has " + listed(f"{s.label.lower()} (line {found[s.key].line})"
                                     for s in present))
    if thin:
        parts.append(("thin: " if present else "Thin: ")
                     + listed(f"{s.label.lower()} (line {found[s.key].line}, "
                              f"{plural(found[s.key].words, 'word')})" for s in thin))
    if absent:
        parts.append(("no " if parts else "No ") + listed(s.label.lower() for s in absent))
    finding = "; ".join(parts) + "."
    improve = ""
    if absent or thin:
        improve = ("Add " + listed(f"a “{s.label}” section" for s in absent) if absent else "")
        if thin:
            improve += ("; and fill " if absent else "Fill ") + listed(
                f"“{s.label}”" for s in thin
            ) + " out"
        improve += f", so a reader finds each part of a {kind.label.lower()} under its own name."
    return Check(f"type-{'required' if required else 'recommended'}", label, points, earned,
                 state_for(earned, points), finding, improve, rule)


def defined_ids(parsed):
    """The references a document defines - at the start of a table row, a list item
    or a heading - in order, with the line each was defined on."""
    found = []
    for item in walk(parsed.content):
        if item.kind == "row" and item.values:
            match = DEFINED.match(item.values[0].strip("* "))
            if match:
                found.append((match.group(0), item.index + 1, item.text))
        elif item.kind == "section":
            text = re.sub(r"^\s*(?:#{1,6}|[-*•]|\d+[.)])\s*", "", item.text.strip())
            match = DEFINED.match(text.strip("* "))
            if match:
                found.append((match.group(0), item.index + 1, item.text))
    return found


def items(parsed):
    """The document's own numbered items: the defined references under the prefix
    it uses most. A BRD defines BR-01 to BR-06 and also constraints C-01 and
    assumptions A-01; its requirements are the BRs."""
    found = defined_ids(parsed)
    prefixes = Counter(name.split("-")[0] for name, _, _ in found)
    if not prefixes:
        return []
    main = prefixes.most_common(1)[0][0]
    return [item for item in found if item[0].split("-")[0] == main]


def span(names):
    """"NFR-01 to NFR-14", lowest to highest whatever order they were defined in."""
    ordered = sorted(set(names), key=lambda name: [int(n) for n in re.findall(r"\d+", name)])
    return f"{ordered[0]} to {ordered[-1]}"


def identifiers_check(parsed, points, what="requirements", minimum=3):
    rule = (f"Each of the {what} has its own identifier (such as BR-01), so a test, a design "
            f"or a ticket can point at it; at least {minimum}, and none used twice.")
    label = f"Numbered {what}"
    counts = Counter(name for name, _, _ in items(parsed))
    twice = sorted(name for name, count in counts.items() if count > 1)
    distinct = len(counts)
    names = list(counts)
    if distinct >= minimum and not twice:
        sample = span(names) if distinct > 2 else listed(names)
        return Check("type-identifiers", label, points, points, "good",
                     f"{plural(distinct, 'identifier')}, {sample}.", "", rule)
    if twice:
        return Check("type-identifiers", label, points, round(points / 2), "partial",
                     f"{plural(distinct, 'identifier')}, but {listed(twice[:4])} "
                     f"{'is' if len(twice) == 1 else 'are'} defined more than once.",
                     "Give each one its own identifier: two items sharing one cannot be told "
                     "apart when cited.", rule)
    improve = (f"Number the {what} (for example BR-01, BR-02) at the start of each row or "
               "item, so each can be cited on its own.")
    if distinct:
        return Check("type-identifiers", label, points, round(points / 2), "partial",
                     f"Only {plural(distinct, 'identifier')}: {listed(names)}.", improve, rule)
    return Check("type-identifiers", label, points, 0, "missing",
                 f"None of the {what} has an identifier.", improve, rule)


def columns_check(parsed, points, label, groups, what="items", key="columns"):
    """Each group of columns appears in one table - the one that names the most.

    Every table is a candidate, rows or not: a PDF table often reaches the text
    as a header with its rows broken into lines, and the header still says what
    the document records.
    """
    names = [group[0] for group in groups]
    rule = f"The table of {what} has a column for each of: {listed(names)}."
    tables = [table for table in parsed.tables if any(table["headers"])]
    if not tables:
        return Check(f"type-{key}", label, points, 0, "missing", f"No table of {what}.",
                     f"Put the {what} in a table with columns for {listed(names)}, so each "
                     "one can be checked for all of them.", rule)
    best, at = set(), tables[0]
    for table in tables:
        seen = column_groups(table["headers"], groups)
        if len(seen) > len(best):
            best, at = seen, table
    lacking = [names[i] for i in range(len(groups)) if i not in best]
    earned = round(points * len(best) / len(groups))
    if not lacking:
        return Check(f"type-{key}", label, points, points, "good",
                     f"The table at line {at['line']} has columns for {listed(names)}.", "",
                     rule)
    have = [names[i] for i in sorted(best)]
    finding = (f"The table at line {at['line']} has {listed(have)} but no column for "
               f"{listed(lacking)}." if have else
               f"No table has a column for {listed(lacking)}.")
    return Check(f"type-{key}", label, points, earned, state_for(earned, points), finding,
                 f"Add a column for {listed(lacking)} to the table of {what}, filled in for "
                 "every row.", rule)


def requirement_lines(parsed):
    """The text of each numbered item, or the whole text when nothing is numbered."""
    found = items(parsed)
    if found:
        return [(line, text) for _, line, text in found]
    return [(index + 1, line) for index, line in enumerate(parsed.lines)]


def vague_check(parsed, points):
    rule = ("No vague words a test cannot check (fast, user-friendly, as appropriate, etc., "
            "and/or …) in the requirements; one or two earns half.")
    label = "Testable wording"
    hits = []
    for line, text in requirement_lines(parsed):
        for match in VAGUE.finditer(text):
            hits.append((line, text, match))
    if not hits:
        return Check("type-vague", label, points, points, "good",
                     "No vague words in the requirements.", "", rule)
    lines = sorted({line for line, _, _ in hits})
    words = listed(sorted({f"“{m.group(0).lower()}”" for _, _, m in hits})[:5])
    line, text, match = hits[0]
    finding = (f"{plural(len(hits), 'vague word')} ({words}) on "
               f"{plural(len(lines), 'line')}, the first at line {line}: “{quote(text, match)}”")
    improve = ("Replace each with something a test can check: a number, a limit, a named "
               "case - “p95 under 300 ms”, not “fast”.")
    earned = round(points / 2) if len(hits) <= 2 else 0
    return Check("type-vague", label, points, earned, state_for(earned, points), finding,
                 improve, rule)


def measurable_check(parsed, points):
    rule = ("Each requirement can be checked: a number with a unit (300 ms, 99.9%), or an "
            "absolute rule (no, never, every). 80% earns all, half earns half.")
    label = "Verifiable targets"
    found = items(parsed)
    if not found:
        return Check("type-measurable", label, points, 0, "missing",
                     "No numbered requirements to check.",
                     "Number each requirement and give it a target with a unit.", rule)
    # Only the requirement itself: a row's verification and breach columns name
    # numbers and absolutes of their own.
    def statement(text):
        cells = [cell.strip() for cell in text.strip().strip("|").split("|")]
        return cells[1] if text.lstrip().startswith("|") and len(cells) > 1 else text

    loose = [name for name, _, text in found
             if not (MEASURE.search(statement(text)) or ABSOLUTE.search(statement(text)))]
    share = 1 - len(loose) / len(found)
    earned = points if share >= 0.8 else round(points / 2) if share >= 0.5 else 0
    if not loose:
        return Check("type-measurable", label, points, points, "good",
                     f"All {len(found)} state a target or an absolute rule.", "", rule)
    return Check("type-measurable", label, points, earned, state_for(earned, points),
                 f"{len(found) - len(loose)} of {len(found)} state a target or an absolute rule; "
                 f"{listed(loose[:5])}{' …' if len(loose) > 5 else ''} "
                 f"{'does' if len(loose) == 1 else 'do'} not.",
                 "" if earned == points else
                 f"Give {listed(loose[:3])} a threshold and a unit - a percentage, a time, a "
                 "rate - so it can be verified rather than argued about.", rule)


def cited(parsed):
    """References the document names but does not define, in order, upper-cased.

    A record number (RGH-0142857) and a standard (SHA-256) are not something a
    document answers to.
    """
    defined = {name.casefold() for name, _, _ in defined_ids(parsed)}
    found = []
    for name in ranking.identifiers(parsed.content):
        prefix, _, number = name.partition("-")
        if (name in defined or prefix.upper() in NOT_REQUIREMENTS or len(number) > 4
                or name.upper() in found):
            continue
        found.append(name.upper())
    return found


def references_check(parsed, points, what="requirements"):
    rule = (f"Names the {what} it answers to by identifier (FR-01, BR-05, NFR-06); "
            "three or more earns all, one or two half.")
    label = f"Traces to {what}"
    names = cited(parsed)
    count = len(names)
    improve = (f"Name the {what} each part answers to by identifier, so a change to one "
               "shows what else must change.")
    if count >= 3:
        return Check("type-references", label, points, points, "good",
                     f"Names {plural(count, 'reference')}: {listed(names[:4])}"
                     f"{' …' if count > 4 else ''}.", "", rule)
    if count:
        return Check("type-references", label, points, round(points / 2), "partial",
                     f"Names only {listed(names)}.", improve, rule)
    return Check("type-references", label, points, 0, "missing",
                 f"Names no {what} by identifier.", improve, rule)


def stories_check(parsed, points):
    rule = ("Every story says who, what and why: “As a … I want … so that …”, with its "
            "own acceptance criteria.")
    label = "Stories with who, what, why"
    text = re.sub(r"[*_]", "", parsed.content)
    stories = STORY.findall(text)
    why = sum("so that" in (want or "").casefold() for _, want in stories)
    criteria = len(ACCEPTANCE.findall(text))
    if not stories:
        return Check("type-stories", label, points, 0, "missing",
                     "No story is written as “As a … I want … so that …”.",
                     "Write each story as who wants what and why, then list its acceptance "
                     "criteria.", rule)
    lacking = []
    if why < len(stories):
        lacking.append(f"{len(stories) - why} without “so that”")
    if criteria < len(stories):
        lacking.append(f"{len(stories) - criteria} without acceptance criteria")
    if not lacking:
        return Check("type-stories", label, points, points, "good",
                     f"{plural(len(stories), 'story', 'stories')}, each with why and "
                     "acceptance criteria.", "", rule)
    earned = round(points / 2)
    return Check("type-stories", label, points, earned, "partial",
                 f"{plural(len(stories), 'story', 'stories')}: {listed(lacking)}.",
                 "Give every story the reason it matters and the conditions that make it "
                 "done.", rule)


def steps_check(parsed, points):
    rule = "Procedures are numbered steps or checklists, three or more; one or two earns half."
    label = "Numbered steps"
    lines = [index + 1 for index, line in enumerate(parsed.lines) if STEP.match(line)]
    # A checklist table (Step | Owner) counts too: each row is a step.
    for table in parsed.tables:
        if column_groups(table["headers"], (("step", "check", "task", "action"),)):
            lines.extend(table["line"] + 1 + n for n in range(len(table["rows"])))
    if len(lines) >= 3:
        return Check("type-steps", label, points, points, "good",
                     f"{plural(len(lines), 'numbered step')}, from line {min(lines)}.", "", rule)
    improve = ("Write each procedure as numbered steps, one action each, so someone under "
               "pressure can follow it and say where they stopped.")
    if lines:
        return Check("type-steps", label, points, round(points / 2), "partial",
                     f"Only {plural(len(lines), 'numbered step')}.", improve, rule)
    return Check("type-steps", label, points, 0, "missing", "No numbered steps.", improve, rule)


def timeline_check(parsed, points):
    rule = "The timeline gives times: at least three timestamped events."
    label = "Timed timeline"
    times = [index + 1 for index, line in enumerate(parsed.lines) if TIME.search(line)]
    if len(times) >= 3:
        return Check("type-timeline", label, points, points, "good",
                     f"{plural(len(times), 'timestamped line')}, from line {times[0]}.", "",
                     rule)
    earned = round(points / 2) if times else 0
    return Check("type-timeline", label, points, earned, state_for(earned, points),
                 f"{plural(len(times), 'timestamped line')}.",
                 "Give each event in the timeline its time (and time zone), so detection "
                 "and recovery can be measured.", rule)


def opening_check(parsed, points, label, pattern, what, improve, key, anywhere=False):
    """`pattern` in the opening - or anywhere, for what a document lists in its body."""
    where = "The document" if anywhere else "The opening"
    rule = f"{where} says {what}."
    text = parsed.content if anywhere else parsed.opening
    match = re.search(pattern, text, re.I)
    if match:
        line = text.count("\n", 0, match.start()) + 1
        return Check(f"type-{key}", label, points, points, "good",
                     f"Line {line}: “{quote(text, match)}”", "", rule)
    return Check(f"type-{key}", label, points, 0, "missing", f"{where} does not say {what}.",
                 improve, rule)


def adr_status_check(parsed, points):
    rule = "The status is one of Proposed, Accepted, Rejected, Superseded or Deprecated."
    label = "Decision status"
    match = ADR_STATUS.search(parsed.opening)
    if match:
        return Check("type-status", label, points, points, "good",
                     f"Status: {match.group(1).capitalize()}.", "", rule)
    return Check("type-status", label, points, 0, "missing", "No decision status is given.",
                 "Add “Status: Proposed / Accepted / Superseded by ADR-…”, so a reader knows "
                 "whether the decision still holds.", rule)


RULES = {
    "identifiers": identifiers_check,
    "columns": columns_check,
    "vague": vague_check,
    "measurable": measurable_check,
    "references": references_check,
    "stories": stories_check,
    "steps": steps_check,
    "timeline": timeline_check,
    "opening": opening_check,
    "adr_status": adr_status_check,
}


def rule(name, points, **options):
    return Rule(RULES[name], points, options)


def section(key, label, *names, required=True):
    return Section(key, label, names, required)


def recommended(key, label, *names):
    return Section(key, label, names, False)


def checks(content, key, title=""):
    """The checks for a document judged as type `key`; empty for Other."""
    kind = BY_KEY.get(key)
    if not kind or key == OTHER:
        return []
    parsed = parse(content, title)
    found = []
    if any(s.required for s in kind.sections):
        found.append(sections_check(parsed, kind, True, 30))
    if any(not s.required for s in kind.sections):
        found.append(sections_check(parsed, kind, False, 10))
    for item in kind.rules:
        found.append(item.check(parsed, item.points, **item.options))
    return found


# ── the registry ──────────────────────────────────────────────────────────────

PURPOSE = ("context", "background", "purpose", "objective", "business need", "problem",
           "overview", "introduction", "executive summary", "goal")

TYPES = [
    DocumentType(
        "business_requirements", "Business requirements document",
        names=("business requirements", "business requirement", "brd",
               "business requirements specification", "business needs"),
        prefixes=("BRD",),
        items=("BR", "BRQ"),
        sections=(
            section("purpose", "Purpose or context", *PURPOSE),
            section("scope", "Scope", "scope"),
            section("requirements", "Requirements", "requirement", "business need",
                    "capabilit", "feature"),
            recommended("out", "Out of scope", "out of scope", "exclusion", "not in scope"),
            recommended("stakeholders", "Stakeholders", "stakeholder", "persona", "user",
                        "audience", "raci", "actor"),
            recommended("constraints", "Constraints", "constraint"),
            recommended("assumptions", "Assumptions", "assumption"),
            recommended("dependencies", "Dependencies", "dependency", "related document",
                        "reference"),
            recommended("success", "Success measures", "success", "kpi", "metric", "benefit",
                        "measure"),
        ),
        columns=(("requirement",), ("rationale", "why", "justification"),
                 ("measure", "success", "kpi", "acceptance")),
        rules=(
            rule("identifiers", 15),
            rule("columns", 15, label="Says why and how it is measured",
                 groups=(("rationale", "why", "justification", "reason", "benefit",
                          "business value"),
                         ("measure of success", "measure", "success", "kpi", "acceptance",
                          "metric", "target", "criteria")),
                 what="requirements", key="why"),
            rule("columns", 10, label="Prioritised",
                 groups=(("priority", "moscow", "importance", "rank"),),
                 what="requirements", key="priority"),
            rule("vague", 10),
        ),
        description="What the business needs and why, before anyone designs it.",
    ),
    DocumentType(
        "user_stories", "User stories",
        names=("user stories", "user story", "backlog", "epic", "epics"),
        items=("US", "STORY"),
        sections=(
            recommended("personas", "Personas", "persona", "user", "actor", "role"),
        ),
        patterns=((STORY, "stories written “As a … I want …”"),
                  (ACCEPTANCE, "acceptance criteria")),
        rules=(
            rule("stories", 25),
            rule("identifiers", 10, what="stories"),
            rule("references", 10),
            rule("vague", 10),
        ),
        description="What each user wants, why, and how anyone knows it is done.",
    ),
    DocumentType(
        "non_functional_requirements", "Non-functional requirements",
        names=("non functional requirements", "non functional requirement", "nfr", "nfrs",
               "quality attributes", "quality requirements", "service requirements"),
        items=("NFR", "NF", "QR"),
        sections=(
            recommended("availability", "Availability", "availability", "resilience",
                        "reliability", "recovery"),
            recommended("performance", "Performance", "performance", "latency", "capacity",
                        "scalability", "throughput"),
            recommended("security", "Security", "security", "privacy"),
            recommended("observability", "Observability", "observability", "monitoring",
                        "logging"),
            recommended("maintainability", "Maintainability", "maintainability",
                        "supportability", "operability"),
        ),
        columns=(("requirement",), ("verification", "verify", "test", "method"),),
        rules=(
            rule("identifiers", 15),
            rule("measurable", 25),
            rule("columns", 15, label="Says how each is verified",
                 groups=(("verification", "verify", "test", "method", "evidence",
                          "measured"),),
                 what="requirements", key="verified"),
            rule("vague", 10),
        ),
        description="How well the system must work: each quality measurable and verifiable.",
    ),
    DocumentType(
        "regulatory_requirements", "Regulatory requirements",
        names=("regulatory requirements", "regulatory requirement", "compliance requirements",
               "regulatory obligations", "legal requirements", "statutory requirements"),
        items=("REG", "OBL"),
        sections=(
            recommended("applicable", "Applicable obligations", "applicable", "obligation",
                        "regulation", "in scope"),
            recommended("excluded", "Not applicable", "not applicable", "exclusion",
                        "out of scope"),
        ),
        columns=(("source", "regulation", "clause"), ("obligation",),
                 ("control", "evidence")),
        patterns=((REGULATION, "citations of laws and standards"),),
        rules=(
            rule("identifiers", 15, what="obligations"),
            rule("columns", 20, label="Each obligation is sourced and answered",
                 groups=(("source", "regulation", "law", "clause", "citation", "standard"),
                         ("obligation", "requirement", "what the software must do", "must"),
                         ("control", "evidence", "answered by", "implementation")),
                 what="obligations", key="sourced"),
            rule("references", 10, what="controls or requirements"),
        ),
        description="Which laws and standards apply, and what each demands of the system.",
    ),
    DocumentType(
        "adr", "Architecture decision record",
        names=("adr", "architecture decision", "decision record", "architectural decision"),
        prefixes=("ADR",),
        sections=(
            section("context", "Context", "context", "problem", "background", "forces"),
            section("decision", "Decision", "decision", "decided"),
            section("consequences", "Consequences", "consequence", "implication", "outcome"),
            recommended("alternatives", "Alternatives considered", "alternative", "option",
                        "considered"),
            recommended("revisit", "When to revisit", "revisit", "review"),
        ),
        rules=(
            rule("adr_status", 15),
            rule("opening", 5, label="Says who decided and when",
                 pattern=r"\b(?:deciders?|decided by|approved by|author|owner)\b",
                 what="who made the decision",
                 improve="Name the deciders and the date, so the decision can be questioned "
                         "by the right people.", key="deciders"),
            rule("references", 10, what="requirements or constraints"),
        ),
        description="One decision, why it was made, and what it costs.",
    ),
    DocumentType(
        "design", "Architecture or design document",
        names=("architecture", "solution design", "design document", "high level design",
               "low level design", "hld", "lld", "technical design", "system design",
               "security design", "design specification", "solution architecture"),
        prefixes=("DES", "HLD", "LLD", "SAD"),
        sections=(
            section("structure", "Components or structure", "component", "layer", "module",
                    "service", "boundary", "building block", "architecture", "request path",
                    "flow", "interface", "deployment view"),
            recommended("context", "Context", *PURPOSE),
            recommended("quality", "Quality attributes", "quality attribute", "non functional",
                        "scenario", "performance", "scalability"),
            recommended("security", "Security", "security", "threat", "authentication",
                        "authorisation", "authorization", "trust"),
            recommended("risks", "Risks and trade-offs", "risk", "deferred", "trade off",
                        "limitation", "decision", "residual"),
        ),
        rules=(rule("references", 15),),
        description="How the system is built, and why that way.",
    ),
    DocumentType(
        "data_model", "Data model",
        names=("data model", "data dictionary", "database schema", "schema", "entity relationship",
               "erd", "logical model", "physical model"),
        sections=(
            section("entities", "Entities or tables", "entity", "entities", "table", "schema"),
            recommended("keys", "Keys and indexes", "index", "key", "constraint"),
            recommended("retention", "Retention", "retention", "archiv", "lifecycle",
                        "deletion"),
            recommended("migration", "Migration", "migration", "evolution", "change"),
        ),
        columns=(("column", "field", "attribute"), ("type", "data type"),),
        rules=(
            rule("columns", 20, label="Columns are typed and described",
                 groups=(("column", "field", "attribute", "name"),
                         ("type", "data type", "format"),
                         ("notes", "description", "meaning", "constraint", "definition")),
                 what="columns", key="typed"),
            rule("references", 10),
        ),
        description="What is stored, in what shape, and for how long.",
    ),
    DocumentType(
        "api_specification", "API specification",
        names=("api specification", "api reference", "api documentation", "api spec",
               "interface specification", "openapi", "swagger", "api guide", "api"),
        sections=(
            section("auth", "Authentication", "authentication", "auth", "authorisation",
                    "authorization", "security"),
            section("endpoints", "Endpoints", "endpoint", "resource", "operation", "route",
                    "method"),
            recommended("errors", "Errors and status codes", "status code", "error",
                        "response code"),
            recommended("versioning", "Versioning", "version", "deprecation", "change log"),
            recommended("limits", "Rate limits", "rate limit", "throttl", "quota", "limit"),
        ),
        patterns=((ENDPOINT, "HTTP endpoints"),),
        rules=(
            rule("opening", 10, label="Endpoints are named",
                 pattern=ENDPOINT.pattern,
                 what="which endpoints exist, by method and path",
                 improve="List each endpoint by method and path (GET /patients/{id}).",
                 key="endpoints", anywhere=True),
            rule("references", 10),
        ),
        description="What each endpoint takes, returns and refuses.",
    ),
    DocumentType(
        "standards", "Standards or guidelines",
        names=("coding standards", "coding standard", "coding guidelines", "style guide",
               "engineering standards", "development standards", "conventions",
               "best practices", "guidelines"),
        sections=(
            section("rules", "Rules", "rule", "standard", "guideline", "convention",
                    "principle", "practice"),
            recommended("tooling", "Tooling", "tool", "lint", "automation"),
            recommended("review", "Review checklist", "review", "checklist"),
        ),
        rules=(rule("steps", 10),),
        description="The rules a team holds itself to, and how they are checked.",
    ),
    DocumentType(
        "test_plan", "Test plan or strategy",
        names=("test plan", "test strategy", "test approach", "qa plan", "master test plan",
               "testing strategy", "test report", "test summary"),
        prefixes=("TST", "TP"),
        items=("TC", "TS"),
        sections=(
            section("scope", "Scope or objective", "scope", "objective", "purpose", "approach",
                    "shape", "overview"),
            section("criteria", "Entry and exit criteria", "exit criteria", "entry criteria",
                    "criteria", "acceptance"),
            recommended("environments", "Environments and data", "environment", "test data",
                        "data"),
            recommended("schedule", "Cycles or schedule", "cycle", "schedule", "phase",
                        "running"),
            recommended("results", "Results", "result", "report", "summary"),
            recommended("exclusions", "What is not covered", "not covered", "risk",
                        "limitation", "exclusion", "out of scope"),
        ),
        rules=(rule("references", 15),),
        description="What will be tested, how, and when testing is done.",
    ),
    DocumentType(
        "traceability_matrix", "Traceability matrix",
        names=("traceability matrix", "requirements traceability", "rtm", "traceability"),
        columns=(("requirement", "br", "fr"), ("test", "verified by"),),
        rules=(
            rule("columns", 25, label="Links requirements to tests",
                 groups=(("requirement", "br", "fr", "nfr", "id"),
                         ("test", "verified by", "verification", "test case"),
                         ("realised by", "code", "design", "implemented", "story")),
                 what="requirements", key="links"),
            rule("references", 15),
        ),
        description="Which test proves which requirement.",
    ),
    DocumentType(
        "runbook", "Runbook or procedure",
        names=("runbook", "run book", "playbook", "operations manual", "standard operating "
               "procedure", "sop", "operating procedure", "checklist", "work instruction",
               "how to"),
        sections=(
            section("procedure", "Procedures", "deploy", "procedure", "step", "situation",
                    "task", "troubleshoot", "response", "operation", "before", "after",
                    "how to", "action"),
            section("recovery", "Rollback or recovery", "rollback", "roll back", "recovery",
                    "failover", "fail over", "back out", "restore", "escalat"),
            recommended("prerequisites", "Prerequisites", "prerequisite", "pre deployment",
                        "before", "preparation", "access"),
            recommended("verification", "Verification", "verification", "verify",
                        "post deployment", "smoke", "check", "after"),
            recommended("overview", "Overview", "overview", "glance", "model", "purpose",
                        "service", "architecture"),
        ),
        patterns=((STEP, "numbered steps"),),
        rules=(
            rule("steps", 20),
            rule("opening", 10, label="Says when it was last exercised",
                 pattern=r"\blast (?:exercised|reviewed|tested|validated|updated|run)\b",
                 what="when it was last exercised or reviewed",
                 improve="Add “Last exercised: <date>” to the opening: a runbook nobody has "
                         "followed since the deployment changed is followed confidently and "
                         "wrongly.", key="exercised"),
        ),
        description="What to do, step by step, when something has to be done or undone.",
    ),
    DocumentType(
        "release_plan", "Release plan",
        names=("release plan", "deployment plan", "go live plan", "cutover plan",
               "implementation plan", "rollout plan"),
        sections=(
            section("scope", "Scope", "scope", "content"),
            section("criteria", "Release criteria", "criteria", "readiness", "gate",
                    "go no go"),
            section("schedule", "Schedule", "schedule", "timeline", "date", "plan",
                    "milestone"),
            recommended("communications", "Communications", "communication", "stakeholder",
                        "announcement"),
            recommended("risks", "Risks and open items", "risk", "open item", "known issue",
                        "waiver"),
            recommended("rollback", "Rollback", "rollback", "roll back", "back out"),
        ),
        rules=(rule("references", 10),),
        description="What ships, when, and what must be true first.",
    ),
    DocumentType(
        "release_notes", "Release notes",
        names=("release notes", "release note", "changelog", "change log", "what s new",
               "release summary"),
        sections=(
            section("changes", "What changed", "what this release does", "change", "new",
                    "feature", "fix", "highlight"),
            recommended("known", "Known issues", "known", "limitation", "issue",
                        "does not do"),
            recommended("upgrade", "Upgrading", "upgrad", "migration", "install"),
            recommended("support", "Support", "support", "contact", "help"),
        ),
        rules=(
            rule("opening", 10, label="Names the release",
                 pattern=r"\b(?:v|version |release )?\d+\.\d+(?:\.\d+)?\b",
                 what="which version it describes",
                 improve="Put the version number and release date at the top.",
                 key="version"),
        ),
        description="What changed for the people who use it.",
    ),
    DocumentType(
        "postmortem", "Incident postmortem",
        names=("postmortem", "post mortem", "incident report", "incident review",
               "post incident review", "pir", "root cause analysis", "rca",
               "major incident report"),
        prefixes=("INC", "PIR"),
        sections=(
            section("impact", "Impact", "impact", "summary"),
            section("timeline", "Timeline", "timeline", "chronology", "sequence of events"),
            section("cause", "Cause", "what happened", "root cause", "cause", "why",
                    "contributing"),
            section("actions", "Actions", "action", "follow up", "remediation",
                    "next step", "corrective"),
            recommended("detection", "Detection", "detection", "not caught", "how it was found",
                        "caught", "detect"),
            recommended("well", "What went well", "went well", "worked"),
            recommended("lessons", "Lessons learned", "lesson", "learn", "contributing factor"),
        ),
        rules=(
            rule("timeline", 15),
            rule("columns", 15, label="Actions have owners and dates",
                 groups=(("action",), ("owner", "assignee", "who"),
                         ("due", "date", "deadline", "by when")),
                 what="actions", key="actions"),
        ),
        description="What happened, why, and what will stop it happening again.",
    ),
    DocumentType(
        "service_levels", "Service level objectives",
        names=("service level objectives", "service level objective", "slo", "slos",
               "service level agreement", "sla", "service levels", "slas"),
        sections=(
            section("objectives", "Objectives", "objective", "target", "slo", "indicator",
                    "sli", "service level"),
            recommended("budget", "Error budget", "error budget", "budget"),
            recommended("alerting", "Alerting", "alert", "monitor", "page", "notification"),
            recommended("review", "Review", "review", "report"),
        ),
        columns=(("indicator", "sli"), ("target", "objective"), ("window", "period")),
        rules=(
            rule("columns", 20, label="Each objective has an indicator, target and window",
                 groups=(("indicator", "sli", "metric", "measure"),
                         ("target", "objective", "threshold"),
                         ("window", "period", "measured over")),
                 what="objectives", key="slo"),
        ),
        description="How reliable the service promises to be, and how that is measured.",
    ),
    DocumentType(
        "policy", "Policy or process",
        names=("policy", "change management", "governance", "escalation",
               "code of conduct", "charter"),
        prefixes=("POL", "GOV"),
        sections=(
            section("rules", "What it requires", "rule", "policy", "requirement", "class",
                    "process", "procedure", "when", "situation", "who decides", "approval"),
            recommended("roles", "Roles and responsibilities", "role", "responsibilit",
                        "owner", "who", "raci", "decide"),
            recommended("exceptions", "Exceptions", "exception", "waiver", "emergency"),
            recommended("records", "Records and review", "record", "review", "audit",
                        "compliance", "enforcement"),
        ),
        description="What people must do, who decides, and what happens otherwise.",
    ),
    DocumentType(
        "risk_register", "Risk register",
        names=("risk register", "risk log", "raid log", "risk assessment", "risks and issues"),
        items=("R", "RISK", "RSK"),
        columns=(("risk",), ("likelihood", "probability", "l"), ("impact", "i"),
                 ("treatment", "mitigation")),
        rules=(
            rule("identifiers", 10, what="risks"),
            rule("columns", 30, label="Each risk is scored, treated and owned",
                 groups=(("risk", "description", "threat"),
                         ("likelihood", "probability", "l"),
                         ("impact", "severity", "consequence", "i"),
                         ("treatment", "mitigation", "response", "action"),
                         ("owner", "assignee"),
                         ("status", "state")),
                 what="risks", key="risks"),
        ),
        description="What could go wrong, how likely, how bad, and who is on it.",
    ),
    DocumentType(
        "control_matrix", "Control matrix",
        names=("control matrix", "controls matrix", "compliance matrix", "control framework",
               "control assessment", "controls assessment", "security controls"),
        columns=(("control", "safeguard"), ("implementation",), ("evidence",)),
        rules=(
            rule("columns", 30, label="Each control is implemented and evidenced",
                 groups=(("control", "safeguard", "requirement", "clause"),
                         ("implementation", "how", "implemented", "design"),
                         ("evidence", "test", "verification", "proof"),
                         ("effectiveness", "status", "result", "assessment", "gap")),
                 what="controls", key="controls"),
            rule("references", 10, what="requirements or obligations"),
        ),
        description="Which control answers which obligation, and the evidence it works.",
    ),
    DocumentType(
        "review", "Review or decision pack",
        names=("readiness review", "go no go", "steering", "board pack", "review pack",
               "meeting notes", "minutes", "decision paper"),
        sections=(
            section("purpose", "Purpose or ask", "purpose", "agenda", "objective", "ask",
                    "decision"),
            recommended("findings", "Findings and risks", "risk", "issue", "finding",
                        "waiver"),
            recommended("recommendation", "Recommendation", "recommendation", "outcome",
                        "decision", "proposal"),
            recommended("actions", "Actions", "action", "next step"),
        ),
        description="What a group is asked to decide, on what evidence.",
    ),
    DocumentType(
        OTHER, "Other",
        names=(),
        description="No type is clearly ahead, so only the source check applies.",
    ),
]

BY_KEY = {kind.key: kind for kind in TYPES}
CHOICES = [(kind.key, kind.label) for kind in TYPES]
