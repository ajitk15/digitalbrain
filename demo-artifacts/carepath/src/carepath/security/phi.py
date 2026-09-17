"""Redaction of protected health information from anything we emit. NFR-04.

The patterns below cover the *structured* identifiers - the ones with a shape.
A name, a diagnosis or a free-text referral reason has no shape, so nothing here
can catch it; keeping those out of logs is a rule the caller has to follow.
"""

import re

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Z]{3}-\d{7}\b"), "[mrn]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
    (re.compile(r"\b\+?\d[\d ()-]{8,}\d\b"), "[phone]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[email]"),
]


def redact(text: str) -> str:
    """Replace every structured identifier in `text` with a placeholder."""
    for pattern, placeholder in PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def mask_mrn(mrn: str) -> str:
    """Show only the issuing facility and the last two digits of an MRN."""
    if len(mrn) < 3:
        return "[mrn]"
    return f"{mrn[:4]}****{mrn[-2:]}"
