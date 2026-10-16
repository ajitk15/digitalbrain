"""Bounded local text conversion with optional fail-closed scanning."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Document, KnowledgeEntry
from .services import audit
from .workbench import access, add_knowledge


def scanner_path():
    configured = settings.CONFIG.get("scanner", "")
    found = configured or shutil.which("clamscan")
    if not found or not Path(found).is_absolute() or not Path(found).is_file():
        raise ValidationError(
            "Security scanner is not configured. Install ClamAV with current signatures and "
            "set scanner to its absolute executable path in the non-secret TOML configuration."
        )
    return str(found)


def scanning_ready():
    """Whether a document could be scanned right now, without scanning one.

    This is the question the old blanket refusal was standing in for. Production
    used to raise here unconditionally - "requires the isolated storage/worker
    adapter" - which closed every intake path there is: upload, link, Jira
    connector, SharePoint. No documents means no knowledge graph, and a
    published graph is mandatory for Code Factory, so the refusal disabled the
    product rather than a feature of it.

    What it was protecting is content nobody checked, and that is enforced
    directly: DOCUMENT_SCAN_REQUIRED is true whenever PRODUCTION is, and
    `scanner_path` refuses anything but an absolute path to a real executable.
    So a production deployment with no scanner still takes in nothing - it just
    says which thing is missing, and can be fixed by configuring it rather than
    by editing the platform.

    Asked of the running system rather than of a constant, because a scanner is
    configuration: it can be mounted, or go missing, without a new release.
    """
    if not settings.DOCUMENT_SCAN_REQUIRED:
        return True
    try:
        scanner_path()
    except ValidationError:
        return False
    return True


#: Seconds a conversion may take. Longer with OCR on, and still inside the five
#: minutes after which the worker treats a conversion as stuck and claims it again.
CONVERSION_TIMEOUT = 45
OCR_CONVERSION_TIMEOUT = 240
#: `scripts/extract_text.MAX_OCR_PAGES`, said here without importing the converter
#: into the web process. A test holds the two equal.
MAX_OCR_PAGES = 50


def tesseract_path():
    """The Tesseract binary when OCR is on and one is installed, else ""."""
    if not settings.OCR_ENABLED:
        return ""
    found = settings.TESSERACT_PATH or shutil.which("tesseract") or ""
    if not found or not Path(found).is_absolute() or not Path(found).is_file():
        return ""
    return str(found)


def ocr_ready():
    """Whether scanned pages would be read right now, the way `scanning_ready` asks
    about the scanner: OCR switched on and its binary present."""
    return bool(tesseract_path())


def process_document(user, app_id, document_id):
    app, _ = access(user, app_id, "knowledge", write=True)
    doc = Document.objects.exclude(status="deleted").get(application=app, pk=document_id)
    if doc.status == "ready":
        return KnowledgeEntry.objects.get(document=doc)
    if doc.status == "rejected":
        raise ValidationError("This document was rejected by security scanning.")
    path = (
        Path(settings.BASE_DIR)
        / ".runtime"
        / "documents"
        / str(app.organization_id)
        / str(app.pk)
        / f"{doc.pk}.quarantine"
    )
    try:
        if path.stat().st_size != doc.size:
            raise ValueError
        if hashlib.sha256(path.read_bytes()).hexdigest() != doc.sha256:
            raise ValueError
    except (OSError, ValueError):
        raise ValidationError(
            "The stored file is unavailable or its integrity check failed."
        ) from None
    if settings.DOCUMENT_SCAN_REQUIRED:
        scanner = scanner_path()
        try:
            scan = subprocess.run(
                [
                    scanner,
                    "--no-summary",
                    "--fail-if-cvd-older-than=7",
                    "--alert-exceeds-max=yes",
                    "--max-filesize=25M",
                    "--max-scansize=50M",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValidationError(
                "Document scanning failed. The file remains stored privately."
            ) from None
        if scan.returncode == 1:
            with transaction.atomic():
                doc.status = "rejected"
                doc.save(update_fields=["status"])
                audit(user, "document.rejected", doc.pk, app.product.portfolio.organization)
            raise ValidationError("The scanner rejected this file. It will not be processed.")
        if scan.returncode != 0:
            raise ValidationError("Scanner unavailable or signatures stale. File remains private.")
    ocr = tesseract_path()
    refused = None
    try:
        with tempfile.TemporaryDirectory() as scratch:
            report_path = Path(scratch) / "report.json"
            converted = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parent.parent / "scripts/extract_text.py"),
                    str(path),
                    Path(doc.name).suffix.lower(),
                    str(report_path),
                    ocr,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=OCR_CONVERSION_TIMEOUT if ocr else CONVERSION_TIMEOUT,
                check=False,
            )
            report = read_report(report_path)
            refused = read_failure(report_path) if converted.returncode != 0 else None
        if converted.returncode != 0 or len(converted.stdout) > 4000000:
            raise ValueError
        content = converted.stdout.decode("utf-8").strip()
        if not content or len(content) > 1000000 or "\x00" in content:
            raise ValueError
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        raise ValidationError(failure_message(refused, ocr)) from None
    with transaction.atomic():
        app, _ = access(user, app_id, "knowledge", write=True)
        locked = Document.objects.select_for_update().get(pk=doc.pk, application=app)
        if locked.status == "ready":
            return KnowledgeEntry.objects.get(document=locked)
        if locked.status not in {"quarantined", "failed", "queued", "converting"}:
            raise ValidationError("This document is no longer eligible for processing.")
        locked.conversion_report = report
        entry = supersede(user, app_id, locked, content)
        locked.status = "ready"
        locked.converter = "MarkItDown " + version("markitdown")
        locked.conversion_error = ""
        locked.save(
            update_fields=["status", "converter", "conversion_error", "conversion_report"]
        )
        if not settings.KEEP_ORIGINAL_DOCUMENTS:
            # After commit: until the text is stored for certain, the file is
            # the only copy of what was uploaded.
            transaction.on_commit(lambda: remove_original(locked.pk, path))
        audit(
            user,
            "document.processed",
            doc.pk,
            app.product.portfolio.organization,
            details={"security_scan_required": settings.DOCUMENT_SCAN_REQUIRED},
        )
        return entry


#: How to turn OCR on, said where a file needed it. An operator's step, so it
#: names the setting rather than a button.
TURN_ON_OCR = ("An administrator can install Tesseract and set ocr = true in the "
               "deployment's configuration; then Retry reads it.")


def pages(count):
    return f"{count} page{'s' if count != 1 else ''}"


def failure_message(refused, ocr):
    """Why a file was not converted, in words its uploader can act on.

    `refused` is the extractor's own reason (`read_failure`). Without one - a
    crash, a timeout, a converter that gave up without saying - the old
    general sentence is all that can honestly be said.
    """
    reason, count = (refused or {}).get("failure"), (refused or {}).get("pages") or 0
    said = {
        "scanned": (
            f"This PDF is a scan: none of its {pages(count)} has a text layer, only "
            "pictures of text, so there is nothing to convert. Scanned PDFs need OCR, "
            f"which is not on in this deployment. {TURN_ON_OCR}"
        ),
        "image_needs_ocr": (
            "This file is an image, and images need OCR, which is not on in this "
            f"deployment. {TURN_ON_OCR}"
        ),
        "ocr_no_text": (
            f"OCR found no text on {'any of its ' + pages(count) if count else 'it'}. "
            "A blank or very faint scan reads this way: scan it again at a higher quality."
        ),
        "ocr_failed": "OCR could not read a page: Tesseract failed. Check its installation, "
                      "then Retry.",
        "ocr_page_limit": (
            f"{pages(count)} need OCR, and at most {MAX_OCR_PAGES} are read in one "
            "document. Split it into smaller files."
        ),
        "too_many_pages": f"It has {pages(count)}, and at most 200 are read. Split it into "
                          "smaller files.",
        "encrypted": "The PDF is password-protected. Upload a copy without a password.",
        "too_large": "It is over 20 MB, the most one file can be. Split it or export a "
                     "smaller copy.",
        "archive_limits": "It unpacks to more than 2,000 parts or 30 MB. Split it into "
                          "smaller files.",
        "oversized": "Its text is over 1,000,000 characters, the most one source holds. "
                     "Split it into smaller documents.",
        "empty": "No text was found in it. It may be empty, or a format MarkItDown "
                 "does not read.",
    }.get(reason)
    if said:
        return said
    return (
        "MarkItDown could not convert this file. It may be unsupported, empty, or exceed "
        "1,000,000 Markdown characters. "
        + (
            f"Scanned pages are read by OCR, at most {MAX_OCR_PAGES} a document."
            if ocr
            else "Scanned PDFs and images need OCR, which is not on in this deployment."
        )
    )


def read_failure(path):
    """The extractor's reason for refusing a file, held to its closed set."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict) or not isinstance(found.get("failure"), str):
        return None
    count = found.get("pages")
    return {
        "failure": found["failure"][:40],
        "pages": count if isinstance(count, int) and 0 <= count < 100000 else 0,
    }


def read_report(path):
    """The extractor's report, held to the shape the source check reads."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(found, dict) or not isinstance(found.get("pages"), int):
        return {}
    def numbers(key):
        value = found.get(key)
        return [n for n in value if isinstance(n, int)][:500] if isinstance(value, list) else []

    report = {"pages": found["pages"], "pages_without_text": numbers("pages_without_text")}
    read = found.get("ocr_pages")
    if isinstance(read, list):
        report["ocr_pages"] = [
            {"page": page["page"], "confidence": page["confidence"]}
            for page in read[:MAX_OCR_PAGES]
            if isinstance(page, dict)
            and isinstance(page.get("page"), int)
            and isinstance(page.get("confidence"), int)
        ]
        report["low_confidence_pages"] = numbers("low_confidence_pages")
    return report


def supersede(user, app_id, document, content):
    """The document's entry for this text: kept when the text is unchanged,
    otherwise a new one, with the old one retired as an updated record is.

    A document has one entry, so the old one gives the document up and goes
    inactive - which takes it out of answers and of published versions the way
    any superseded source leaves them. A restriction carries over: converting
    again must never quietly make a restricted source usable, and a document type
    a person set carries over too.
    """
    from .document_ai import cached_type
    from .sensitivity import RESTRICTED
    from .source_quality import run as check_source

    previous = KnowledgeEntry.objects.select_for_update().filter(document=document).first()
    if previous and previous.active and previous.content == content:
        previous.quality = check_source(
            content, document.conversion_report, document.name, previous.document_type,
            cached_type(app_id, previous.digest),
        )
        previous.save(update_fields=["quality"])
        return previous
    if previous:
        previous.document = None
        previous.active = False
        previous.save(update_fields=["document", "active"])
    entry = add_knowledge(user, app_id, document.name, content, document=document)
    if previous and previous.sensitivity == RESTRICTED:
        entry.sensitivity = RESTRICTED
        entry.save(update_fields=["sensitivity"])
    # So does the type a person set: new text of the same document is the same
    # kind of document, and checking it as whatever the rules guess would undo them.
    if previous and previous.document_type:
        entry.document_type = previous.document_type
        entry.quality = check_source(
            content, document.conversion_report, document.name, entry.document_type,
            cached_type(app_id, entry.digest),
        )
        entry.save(update_fields=["document_type", "quality"])
    return entry


def remove_original(document_id, path):
    """Delete a converted document's stored file, and say when."""
    path.unlink(missing_ok=True)
    Document.objects.filter(pk=document_id, status="ready").update(
        original_removed_at=timezone.now()
    )


def settle_failure(doc, message):
    """Where a failed conversion leaves a document: failed, unless it already
    had text - a re-conversion that failed leaves the text it had, and says so."""
    if KnowledgeEntry.objects.filter(document=doc, active=True).exists():
        doc.status = "ready"
        doc.conversion_error = f"Converting again failed: {message}"[:500]
    else:
        doc.status = "failed"
        doc.conversion_error = message[:500]
    doc.save(update_fields=["status", "conversion_error"])


def convert_document(user, app_id, document_id):
    """Persist actionable conversion failures without removing the uploaded original."""
    app, _ = access(user, app_id, "knowledge", write=True)
    try:
        entry = process_document(user, app_id, document_id)
    except ValidationError as error:
        with transaction.atomic():
            doc = Document.objects.select_for_update().get(pk=document_id, application=app)
            if doc.status not in {"ready", "rejected", "deleted"}:
                settle_failure(doc, " ".join(error.messages))
                audit(
                    user, "document.conversion_failed", doc.pk, app.product.portfolio.organization
                )
        raise
    # After the text is committed, and never a reason the conversion failed: a
    # document the rules call Other may be typed by a model, when an owner has
    # switched that on, charged to whoever converted it.
    from .document_ai import classify_quietly

    classify_quietly(user, entry)
    return entry
