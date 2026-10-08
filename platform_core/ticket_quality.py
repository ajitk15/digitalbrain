"""How ready an imported ticket is to be worked on, and why.

A fixed rubric read from the stored text: no model is called and nothing is
charged, so the same ticket always gets the same score and every point can be
traced to a line in it. Each check says what it found, why that earned what it
did, and what would earn the rest - a number alone tells a ticket's author
nothing they can act on.

The text is what `connector_kinds.jira_records` stores: the summary, the
`Key: / Type: / Status: / Priority: / Labels:` header, a blank line, then the
description.
"""

import re
from dataclasses import dataclass

HEADER = re.compile(r"^(Key|Type|Status|Priority|Labels|Updated):\s*(.*)$")
#: A requirement, decision or control this ticket answers to: NFR-06, ADR-0002,
#: REG-03, BR-05. Two letters minimum so "A-2" alone does not count, and at
#: most four digits so a record number (MRN RGH-0177310) is not read as one.
REFERENCE = re.compile(r"\b[A-Z]{2,6}-\d{1,4}\b")
#: Standards and encodings name a format, not a requirement.
NOT_REQUIREMENTS = {"ISO", "UTF", "SHA", "TLS", "HTTP", "UTC"}
#: Somewhere in the system: a path, an endpoint, a call, a dotted name.
LOCATION = re.compile(
    r"(?:\b(?:GET|POST|PUT|PATCH|DELETE)\s+/\S+)"
    r"|(?:\b[\w-]+(?:/[\w.-]+)+\.\w{1,5}\b)"
    r"|(?:\b[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*\(\))"
    r"|(?:\b[a-z_]+\.[a-z_]\w*\b(?=\s|[,.;:]|$))"
)
PLACEHOLDER = re.compile(
    r"\b(?:TBD|TODO|FIXME|to be (?:decided|confirmed|determined))\b|\?\?", re.I
)
RATIONALE = re.compile(r"\b(?:because|requires?|so that|in order to|impact|risk)\b", re.I)
ACCEPTANCE = re.compile(
    r"acceptance criteria|definition of done|\bgiven\b.+\bwhen\b.+\bthen\b", re.I | re.S
)
TESTS = re.compile(r"\btests?\b|\bassert", re.I)
GENERIC_TITLE = re.compile(
    r"^(?:fix(?:es)?|bug|issue|problem|update|change|todo)\b\W*\w{0,12}$", re.I
)


@dataclass
class Check:
    key: str
    label: str
    points: int
    earned: int
    #: "good", "partial" or "missing".
    state: str
    #: What was seen, in words a ticket author recognises.
    finding: str
    #: What would earn the rest. Empty when nothing is left to earn.
    improve: str
    #: The rule, so a score can be argued with rather than taken on trust.
    rule: str

    @property
    def missing_points(self):
        return self.points - self.earned


def parse(content):
    """(fields, description) from the stored record text."""
    fields = {}
    lines = (content or "").splitlines()
    start = 0
    for index, line in enumerate(lines):
        match = HEADER.match(line.strip())
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()
            start = index + 1
    description = "\n".join(lines[start:]).strip() if fields else (content or "").strip()
    return fields, description


def quote(text, match, width=90):
    """A short excerpt around a match, so a finding shows where it came from.

    Cut at word boundaries: an excerpt that starts mid-word reads as garbled.
    """
    begin = max(0, match.start() - 24)
    if begin:
        space = text.find(" ", begin, match.start())
        begin = space + 1 if space != -1 else match.start()
    end = min(len(text), begin + width)
    if end < len(text):
        space = text.rfind(" ", match.end(), end)
        end = space if space != -1 else end
    snippet = " ".join(text[begin:end].split())
    return ("…" if begin else "") + snippet + ("…" if end < len(text) else "")


def summary_check(title, key):
    name = re.sub(rf"^\s*{re.escape(key)}\s*[-:]\s*", "", title or "") if key else (title or "")
    name = re.sub(r"^\s*[A-Z]+-\d+\s*[-:]\s*", "", name).strip()
    words = len(name.split())
    rule = "A summary of 5 to 20 words that names the behaviour, not just the area."
    if words >= 5 and words <= 20 and not GENERIC_TITLE.match(name):
        return Check("summary", "Clear summary", 10, 10, "good",
                     f"“{name}” says what the ticket is about in {words} words.", "", rule)
    if words >= 3:
        return Check("summary", "Clear summary", 10, 5, "partial",
                     f"“{name}” is {words} words: "
                     + ("too long to scan." if words > 20
                        else "too short to say what it is about."),
                     "Rewrite the summary as one sentence naming the behaviour and where it "
                     "happens, in 5 to 20 words.", rule)
    return Check("summary", "Clear summary", 10, 0, "missing",
                 f"“{name or title}” does not say what is wrong.",
                 "Name the behaviour that is wrong and where, e.g. “Patient search accepts an "
                 "unbounded limit”.", rule)


def detail_check(description):
    words = len(description.split())
    rule = "At least 80 words of description; 30 to 79 earns half."
    if words >= 80:
        return Check("detail", "Enough detail", 10, 10, "good",
                     f"The description runs to {words} words.", "", rule)
    if words >= 30:
        return Check("detail", "Enough detail", 10, 5, "partial",
                     f"The description is {words} words - enough to start, thin to finish.",
                     "Add what currently happens, what should happen instead, and who is "
                     "affected.", rule)
    return Check("detail", "Enough detail", 10, 0, "missing",
                 f"The description is {words} word{'' if words == 1 else 's'}.",
                 "Describe the current behaviour, the expected behaviour and why it matters. "
                 "Someone new to the code should be able to start from the ticket alone.", rule)


def acceptance_check(description):
    rule = ("An explicit “Acceptance criteria” (or Given/When/Then) section with at least "
            "three separate, checkable conditions; one or two earns half.")
    match = ACCEPTANCE.search(description)
    if not match:
        return Check("acceptance", "Acceptance criteria", 25, 0, "missing",
                     "There is no acceptance criteria section.",
                     "Add “Acceptance criteria:” followed by the conditions that must hold "
                     "for this to be done, separated by semicolons or as a list - each one "
                     "something a reviewer can check.", rule)
    section = description[match.end():]
    conditions = [
        part for part in re.split(r";|\n\s*(?:[-*•]|\d+[.)])\s+|\n", section)
        if len(part.split()) >= 3
    ]
    count = len(conditions)
    if count >= 3:
        return Check("acceptance", "Acceptance criteria", 25, 25, "good",
                     f"{count} separate conditions are listed under acceptance criteria.",
                     "", rule)
    return Check("acceptance", "Acceptance criteria", 25, 12, "partial",
                 f"Acceptance criteria are present but list {count or 'no'} separate "
                 f"condition{'' if count == 1 else 's'}.",
                 "Split the criteria into separate checkable conditions: the main fix, the "
                 "edge cases, and what must keep working unchanged.", rule)


def reproduction_check(description):
    rule = "For a bug: steps to reproduce, plus the expected and the actual result."
    steps = re.search(r"steps to reproduce|to reproduce|reproduc", description, re.I)
    expected = re.search(r"\bexpected\b", description, re.I)
    actual = re.search(r"\bactual(?:ly)?\b|\binstead\b|\bproduces\b|\breturns\b", description, re.I)
    have = [name for name, found in
            (("steps", steps), ("expected result", expected), ("actual result", actual)) if found]
    lacking = [name for name in ("steps", "expected result", "actual result") if name not in have]
    if not lacking:
        return Check("reproduction", "Reproducible", 10, 10, "good",
                     "Steps, the expected result and the actual result are all given.", "", rule)
    if have:
        return Check("reproduction", "Reproducible", 10, 5, "partial",
                     f"Gives {', '.join(have)}; no {', '.join(lacking)}.",
                     f"Add the {' and '.join(lacking)}, so whoever picks it up can see the "
                     "bug before changing anything.", rule)
    return Check("reproduction", "Reproducible", 10, 0, "missing",
                 "No steps to reproduce, expected result or actual result.",
                 "Add “Steps to reproduce:”, then “Expected:” and “Actual:”.", rule)


def location_check(description):
    found = []
    for match in LOCATION.finditer(description):
        value = match.group(0).strip().rstrip(".,;:")
        if len(value) >= 5 and value not in found:
            found.append(value)
    rule = "Names at least two concrete places in the system: files, endpoints or functions."
    if len(found) >= 2:
        return Check("location", "Points at the code", 15, 15, "good",
                     "Names " + ", ".join(found[:4]) + (" and more." if len(found) > 4 else "."),
                     "", rule)
    if found:
        return Check("location", "Points at the code", 15, 8, "partial",
                     f"Names one place: {found[0]}.",
                     "Name the other files, endpoints or functions involved, so the change "
                     "can be scoped without searching for it.", rule)
    return Check("location", "Points at the code", 15, 0, "missing",
                 "No file, endpoint or function is named.",
                 "Name where the problem lives - a file path, an endpoint such as "
                 "GET /patients, or a function such as referrals.transition().", rule)


def rationale_check(description):
    references = []
    for match in REFERENCE.finditer(description):
        if match.group(0).split("-")[0] in NOT_REQUIREMENTS:
            continue
        if match.group(0) not in references:
            references.append(match.group(0))
    rule = ("Says why it matters by citing a requirement, decision or control it answers to "
            "(e.g. NFR-06, ADR-0002); a stated reason with no reference earns half.")
    if references:
        return Check("rationale", "Why it matters", 10, 10, "good",
                     "Cites " + ", ".join(references[:5]) + ".", "", rule)
    reason = RATIONALE.search(description)
    if reason:
        return Check("rationale", "Why it matters", 10, 5, "partial",
                     f"Gives a reason (“{quote(description, reason)}”) but cites no "
                     "requirement.",
                     "Link the requirement, decision record or control this ticket answers "
                     "to, so its priority can be checked rather than asserted.", rule)
    return Check("rationale", "Why it matters", 10, 0, "missing",
                 "Nothing says why this matters or what it breaks.",
                 "Say who or what is affected and cite the requirement it fails.", rule)


def tests_check(description):
    rule = ("Says how the fix will be tested, as one of the acceptance criteria; a mention "
            "elsewhere in the description earns half.")
    criteria = ACCEPTANCE.search(description)
    section = description[criteria.end():] if criteria else ""
    match = TESTS.search(section)
    if match:
        return Check("tests", "Test expectation", 10, 10, "good",
                     f"The acceptance criteria ask for tests: “{quote(section, match)}”",
                     "", rule)
    match = TESTS.search(description)
    if match:
        return Check("tests", "Test expectation", 10, 5, "partial",
                     f"Testing is mentioned (“{quote(description, match)}”) but is not one "
                     "of the acceptance criteria.",
                     "Add the tests to the acceptance criteria - which cases they cover and "
                     "what they assert - so “done” includes them.", rule)
    return Check("tests", "Test expectation", 10, 0, "missing",
                 "Nothing says how the change will be verified.",
                 "Add the tests that should exist when this is done to the acceptance "
                 "criteria: which cases they cover and what they assert.", rule)


def triage_check(fields):
    present = [name for name in ("type", "priority", "labels") if fields.get(name)]
    lacking = [name for name in ("type", "priority", "labels") if name not in present]
    rule = "Type, priority and at least one label are set in Jira."
    if not lacking:
        return Check("triage", "Triaged", 5, 5, "good",
                     f"{fields['type']}, {fields['priority']} priority, labelled "
                     f"{fields['labels']}.", "", rule)
    return Check("triage", "Triaged", 5, 2 if len(present) == 2 else 0,
                 "partial" if len(present) == 2 else "missing",
                 "Not set: " + ", ".join(lacking) + ".",
                 "Set the " + " and ".join(lacking) + " in Jira; they are imported with the "
                 "ticket.", rule)


def placeholder_check(description):
    rule = "No placeholders left in: TBD, TODO, FIXME, “to be decided”, ??."
    match = PLACEHOLDER.search(description)
    if not match:
        return Check("placeholders", "No open placeholders", 5, 5, "good",
                     "Nothing is left as TBD or TODO.", "", rule)
    return Check("placeholders", "No open placeholders", 5, 0, "missing",
                 f"Still open: “{quote(description, match)}”",
                 "Resolve the open question or move it to a separate ticket.", rule)


def tone(score):
    return "good" if score >= 80 else "fair" if score >= 55 else "poor"


def verdict(score):
    return "Ready" if score >= 80 else "Needs work" if score >= 55 else "Not ready"


def assess(title, content):
    """Score a ticket out of 100, with every check that went into it."""
    fields, description = parse(content)
    is_bug = fields.get("type", "").lower() in {"bug", "defect", "incident"}
    checks = [
        summary_check(title, fields.get("key", "")),
        detail_check(description),
        acceptance_check(description),
        *([reproduction_check(description)] if is_bug else []),
        location_check(description),
        rationale_check(description),
        tests_check(description),
        triage_check(fields),
        placeholder_check(description),
    ]
    points = sum(check.points for check in checks)
    earned = sum(check.earned for check in checks)
    # Reproduction applies to bugs only, so a story is scored out of what applies
    # to it rather than marked down for steps it was never going to have.
    score = round(100 * earned / points) if points else 0
    return {
        "score": score,
        "tone": tone(score),
        "verdict": verdict(score),
        "type": fields.get("type", ""),
        "checks": checks,
        "strengths": [check for check in checks if check.state == "good"],
        "gaps": sorted(
            (check for check in checks if check.state != "good"),
            key=lambda check: -check.missing_points,
        ),
        "earned": earned,
        "points": points,
        "applies_reproduction": is_bug,
    }
