"""Sensitivity labels: a source an owner restricts is never used by AI.

A restricted source stays in Sources for the people with access to the
application - it is their data - but nothing hands it to a model and nothing
cites it: it is left out of graph builds, keyword and vector search, the
search and fetch tools, ServiceOps' incident and record layers, Code
Factory's ticket picker and its run intake. It is one queryset method,
`KnowledgeEntry.objects.answerable()`, so a new reader has one thing to call.

Because citations are verified against `graphs.sources_for`, which is
answerable, restricting a source also takes its relationships out of every
already-published graph version at once - no rebuild has to happen first.

Personal data is *suggested*, never decided: `scan` counts the patterns that
usually mean a person (email addresses, telephone numbers, US social security
numbers, payment card numbers that pass the Luhn check, and dates of birth
where the text says so) and the source page says what it found. Health
identifiers - a medical record number, a health plan member number, an NHS
number, a named patient - are counted too, and make the finding PHI rather than
PII (`category`): HIPAA treats them as identifying a patient wherever they
appear. Each one needs its label ("MRN", "Member ID", "Patient name:") beside
the value, so a document about MRNs is not a document holding one. Labelling stays
a person's call, because a runbook's on-call number is not a patient's. A
restriction carries over to the next import of the same record, so a sync
cannot quietly lift it.
"""

import re

STANDARD = "standard"
RESTRICTED = "restricted"
CHOICES = [
    (STANDARD, "Standard"),
    (RESTRICTED, "Restricted - never used by AI"),
]

PATTERNS = {
    "email address": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "telephone number": re.compile(
        # Not the inside of a longer run of digit groups, such as a card number.
        r"(?<![\w-])(?<!\d[ .-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?|\d{2,4}[ .-])"
        r"\d{3,4}[ .-]\d{3,4}(?![\w-])(?![ .-]\d)"
    ),
    "social security number": re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])"),
    "date of birth": re.compile(
        r"\b(?:date of birth|d\.?o\.?b\.?|born(?: on)?)\s*[:\-]?\s*"
        r"(?:\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}|\d{1,2} \w+ \d{4}|\w+ \d{1,2},? \d{4})",
        re.IGNORECASE,
    ),
    # Health identifiers. Each needs its label next to the value, so text
    # about MRNs ("a duplicate MRN is refused") is not text holding one.
    "medical record number": re.compile(
        r"\b(?:mrn|medical[ _]record[ _](?:number|no\.?|#))\W{0,4}[A-Z]{0,4}-?\d{5,10}\b",
        re.IGNORECASE,
    ),
    "health plan member number": re.compile(
        r"\b(?:member|subscriber|policy|medicaid|medicare|beneficiary|insurance)[ _]?"
        r"(?:id|number|no\.?|#)\W{0,4}[A-Z]{0,4}[ -]?\d[\dA-Z-]{5,}\b",
        re.IGNORECASE,
    ),
    "NHS number": re.compile(
        r"\bNHS(?:[ _](?:number|no\.?|#))?\W{0,4}\d{3}[ -]?\d{3}[ -]?\d{4}\b"
    ),
    "patient name": re.compile(
        r"\b(?i:patient)(?:'s)?(?:[ _](?i:full[ _])?(?i:name))?[ \t]*:[ \t]*"
        r"[A-Z][a-z'-]+(?:[ \t]+[A-Z][a-z'-]+)+"
    ),
}
#: The kinds that are health identifiers. Any of them makes a finding PHI.
PHI_KINDS = frozenset(
    {"medical record number", "health plan member number", "NHS number", "patient name"}
)
CARD = re.compile(r"(?<![\w-])(?:\d[ -]?){13,19}(?![\w-])")
#: Enough to judge a source; a million characters of logs is not read twice over.
SCAN_LIMIT = 200000


def luhn(digits):
    total, double = 0, False
    for character in reversed(digits):
        value = int(character)
        if double:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
        double = not double
    return total % 10 == 0


def scan(text):
    """{kind: count} of what looks like personal data. Empty when nothing does."""
    text = text[:SCAN_LIMIT]
    # A health identifier claims its digits: an NHS number is not also a
    # telephone number because it is ten digits in three groups.
    claimed = [m.span() for kind in PHI_KINDS for m in PATTERNS[kind].finditer(text)]

    def counted(kind, match):
        return kind in PHI_KINDS or not any(
            start < match.end() and match.start() < end for start, end in claimed
        )

    found = {
        kind: sum(counted(kind, match) for match in pattern.finditer(text))
        for kind, pattern in PATTERNS.items()
    }
    cards = [re.sub(r"\D", "", match) for match in CARD.findall(text)]
    found["payment card number"] = sum(13 <= len(card) <= 19 and luhn(card) for card in cards)
    return {kind: count for kind, count in found.items() if count}


def category(findings):
    """"PHI" when a health identifier was found, "PII" for any other personal
    data, "" for none. A tag on the Sources list, so it says which in one word."""
    if not findings:
        return ""
    return "PHI" if PHI_KINDS & set(findings) else "PII"


def summary(findings):
    """The findings in words, for the source page: "2 email addresses, 1 date of birth"."""
    return ", ".join(
        f"{count} {kind}{'es' if count > 1 and kind.endswith('ss') else 's' if count > 1 else ''}"
        for kind, count in sorted(findings.items())
    )
