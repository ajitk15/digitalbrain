"""Identifier formats for the network. FR-01.

A medical record number is the join key between this service and every other
system in the network, so it is validated once, here, and never reformatted
downstream. A number that arrives with different padding is a different patient
as far as the partner systems are concerned.
"""

import re
import uuid

#: Three uppercase letters for the issuing facility, a hyphen, seven digits.
MRN = re.compile(r"^[A-Z]{3}-\d{7}$")


def new_id() -> str:
    """A surrogate key. Opaque on purpose - it carries no patient information."""
    return uuid.uuid4().hex


def valid_mrn(value: str) -> str:
    """Return the canonical form of a medical record number, or raise.

    Refused rather than corrected. Silently upper-casing an MRN would make this
    service the only one in the network that accepts a malformed one, and the
    mismatch would surface as a duplicate patient somewhere else entirely.
    """
    candidate = (value or "").strip()
    if not MRN.fullmatch(candidate):
        raise ValueError("A medical record number looks like RGH-0142857.")
    return candidate
