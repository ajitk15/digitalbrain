"""Masking personal data before a source is stored.

`sensitivity.scan` only suggests. An owner can go further in AI settings and
have chosen kinds of personal data **replaced before the text is stored**, so the
knowledge record itself never holds them. Everything downstream - the digest,
exact-quote verification, the structural graph, AI extraction, embeddings, chat,
Code Factory, the API and the MCP server - reads the stored text and needs no
masking of its own, so no path added later can leak a value it never sees. That
is why masking happens here, in `workbench.add_knowledge`, and not at each model
call.

A value becomes a **typed, stable placeholder**: `MRN: RGH-0142857` is stored as
`MRN: [MRN-QZKTWBAF]`. The label stays, so a model still knows what it is. The
letters are a keyed hash of the normalised value under a per-application key, so
the same patient's MRN is the same placeholder in every document - the graph
still joins them - while nobody without the key can recover it by trying every
MRN. They are letters only, so the scan never mistakes a placeholder for a value
and masking twice changes nothing.

The key is a secret, so it lives in a file like every other one
(`masking_key_<application id>`): an operator's mount first, then the
browser-managed directory, where switching masking on writes one if none
exists. Never the shared connector folder. A missing key with masking on is an
error, never a reason to store the text unmasked.

This is minimization, not de-identification: only what the scan's rules find
is masked, and they find labelled identifiers, not every name, address or
diagnosis in prose.
"""

import hashlib
import hmac
import re
import secrets as random
from string import ascii_uppercase

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction

from digitalbrain.configuration import read_secret

from .sensitivity import CARD, PATTERNS, PHI_KINDS, luhn, scan

CARD_KIND = "payment card number"
#: Every kind that can be masked, health identifiers first: they claim their
#: characters before the general rules run, as they do in the scan.
KINDS = [
    "medical record number",
    "health plan member number",
    "NHS number",
    "patient name",
    "email address",
    "telephone number",
    "social security number",
    CARD_KIND,
    "date of birth",
]
#: Masked unless an owner unticks them. The rest are opt-in: a runbook's on-call
#: number is the point of the runbook.
DEFAULT_KINDS = [kind for kind in KINDS if kind in PHI_KINDS]
LABELS = {
    "medical record number": "MRN",
    "health plan member number": "MEMBER-ID",
    "NHS number": "NHS-NUMBER",
    "patient name": "PATIENT",
    "email address": "EMAIL",
    "telephone number": "PHONE",
    "social security number": "SSN",
    CARD_KIND: "CARD",
    "date of birth": "DOB",
}
DIGITS = {"telephone number", "social security number", CARD_KIND, "NHS number"}
IDENTIFIERS = {"medical record number", "health plan member number"}
KEY = "masking_key"
TOKEN_LETTERS = 8


def key_name(app_id):
    return f"{KEY}_{app_id}"


def kinds_for(app_id):
    """The kinds this application masks, in `KINDS` order; empty when it masks none."""
    from .models import PersonalDataPolicy

    policy = PersonalDataPolicy.objects.filter(application_id=app_id, enabled=True).first()
    return [kind for kind in KINDS if policy and kind in policy.kinds]


def _directories():
    from .secrets import managed_directory

    return [d for d in (settings.SECRET_DIRECTORY, managed_directory()) if d]


def key_source(app_id):
    """"operator", "managed" or "": where the key is, without reading it."""
    from pathlib import Path

    from .secrets import managed_directory

    name = key_name(app_id)
    if (Path(settings.SECRET_DIRECTORY) / name).is_file():
        return "operator"
    managed = managed_directory()
    if managed is not None and (managed / name).is_file():
        return "managed"
    return ""


def read_key(app_id):
    name = key_name(app_id)
    for directory in _directories():
        try:
            return read_secret(directory, name).encode()
        except ImproperlyConfigured:
            continue
    raise ValidationError(
        f"Personal data masking is on, but its key file {name} is missing, so nothing "
        "can be stored. An operator mounts it, or an owner switches masking off and on "
        "again to write a new one - which changes every placeholder from then on."
    )


def ensure_key(app_id):
    """Write a key when there is none. Refused when nowhere is writable."""
    from .secrets import managed_directory, write_secret

    if key_source(app_id):
        return
    directory = managed_directory()
    if directory is None:
        raise ValidationError(
            f"Masking needs a key. An operator mounts {key_name(app_id)} in the secret "
            "directory, or sets managed_secret_directory so one can be written here."
        )
    write_secret(directory, key_name(app_id), random.token_hex(32))


def normal(kind, value):
    """One spelling per value, so `RGH-0142857` and `rgh 0142857` share a placeholder."""
    if kind in DIGITS:
        return re.sub(r"\D", "", value)
    if kind in IDENTIFIERS:
        return re.sub(r"[^0-9A-Za-z]", "", value).upper()
    return " ".join(value.split()).casefold()


def placeholder(key, kind, value):
    digest = hmac.new(key, f"{kind}\0{normal(kind, value)}".encode(), hashlib.sha256).digest()
    token = "".join(ascii_uppercase[byte % 26] for byte in digest[:TOKEN_LETTERS])
    return f"[{LABELS[kind]}-{token}]"


def spans(text, kinds):
    """[(start, end, kind)] of each value to replace, in order and never overlapping.

    The whole text, not the scan's first `SCAN_LIMIT` characters: a value past
    that point is still a value. Labelled kinds replace only their value.
    """
    chosen = []

    def free(start, end):
        return not any(start < until and since < end for since, until, _ in chosen)

    for kind in (kind for kind in KINDS if kind in kinds):
        if kind == CARD_KIND:
            for match in CARD.finditer(text):
                digits = re.sub(r"\D", "", match.group())
                if 13 <= len(digits) <= 19 and luhn(digits) and free(*match.span()):
                    chosen.append((*match.span(), kind))
            continue
        for match in PATTERNS[kind].finditer(text):
            start, end = match.span("v") if "v" in match.re.groupindex else match.span()
            if free(start, end):
                chosen.append((start, end, kind))
    return sorted(chosen)


def mask(text, kinds, key):
    """(masked text, {kind: count})."""
    parts, counts, last = [], {}, 0
    for start, end, kind in spans(text, kinds):
        parts += [text[last:start], placeholder(key, kind, text[start:end])]
        counts[kind] = counts.get(kind, 0) + 1
        last = end
    parts.append(text[last:])
    return "".join(parts), counts


def merged(*findings):
    """Per kind, the larger count: a connector's title is also its text's first line."""
    total = {}
    for found in findings:
        for kind, count in found.items():
            total[kind] = max(total.get(kind, 0), count)
    return total


def prepare(app_id, title, content):
    """(title, content, masked) as this application stores them."""
    kinds = kinds_for(app_id)
    if not kinds:
        return title, content, {}
    key = read_key(app_id)
    title, in_title = mask(title, kinds, key)
    content, in_content = mask(content, kinds, key)
    return title, content, merged(in_content, in_title)


def stored_content(app_id, content):
    """The text as it would be stored, for comparing with what already is."""
    return prepare(app_id, "", content)[1]


def save_policy(user, app, enabled, kinds):
    from .models import PersonalDataPolicy
    from .services import audit

    kinds = [kind for kind in KINDS if kind in kinds]
    with transaction.atomic():
        if enabled:
            ensure_key(app.pk)
        PersonalDataPolicy.objects.update_or_create(
            application=app, defaults={"enabled": enabled, "kinds": kinds, "set_by": user}
        )
        audit(
            user,
            "personal_data.policy_set",
            app.pk,
            app.product.portfolio.organization,
            details={"enabled": enabled, "kinds": kinds},
        )


def remask(user, app):
    """Mask every stored source of `app` under today's policy. (sources changed, {kind: count}).

    In place, retired revisions included: superseding would keep the values in
    the row it retires, and removing them from the store is the point. A changed
    digest takes the old text out of published versions' citations, the same way
    any changed source leaves them.
    """
    from .document_ai import cached_type
    from .models import KnowledgeEntry
    from .services import audit
    from .source_quality import run as check_source

    kinds = kinds_for(app.pk)
    if not kinds:
        raise ValidationError("Switch masking on before masking existing sources.")
    key = read_key(app.pk)
    changed, totals = 0, {}
    with transaction.atomic():
        entries = (
            # Only the entries are locked: PostgreSQL refuses FOR UPDATE on the
            # nullable side of the outer join select_related makes to document.
            KnowledgeEntry.objects.select_for_update(of=("self",))
            .filter(application=app)
            .select_related("document")
            .order_by("pk")
        )
        for entry in entries:
            title, in_title = mask(entry.title, kinds, key)
            content, in_content = mask(entry.content, kinds, key)
            found = merged(in_content, in_title)
            if not found:
                continue
            entry.title, entry.content = title, content
            entry.digest = hashlib.sha256(content.encode()).hexdigest()
            entry.masked = {
                kind: entry.masked.get(kind, 0) + found.get(kind, 0)
                for kind in KINDS
                if entry.masked.get(kind) or found.get(kind)
            }
            entry.personal_data = scan(f"{title}\n{content}")
            fields = ["title", "content", "digest", "masked", "personal_data"]
            if entry.document is not None:
                entry.quality = check_source(
                    content, entry.document.conversion_report, title,
                    entry.document_type, cached_type(app.pk, entry.digest),
                )
                fields.append("quality")
            entry.save(update_fields=fields)
            changed += 1
            for kind, count in found.items():
                totals[kind] = totals.get(kind, 0) + count
        audit(
            user,
            "personal_data.remasked",
            app.pk,
            app.product.portfolio.organization,
            details={"sources": changed, "masked": totals},
        )
    return changed, totals
