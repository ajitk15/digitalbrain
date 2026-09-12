"""Bounded local text conversion with optional fail-closed scanning."""

import hashlib
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

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


def process_document(user, app_id, document_id):
    app, _ = access(user, app_id, "knowledge", write=True)
    if settings.PRODUCTION:
        raise ValidationError("Production processing requires the isolated storage/worker adapter.")
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
    try:
        converted = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "scripts/extract_text.py"),
                str(path),
                Path(doc.name).suffix.lower(),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=45,
            check=False,
        )
        if converted.returncode != 0 or len(converted.stdout) > 4000000:
            raise ValueError
        content = converted.stdout.decode("utf-8").strip()
        if not content or len(content) > 1000000 or "\x00" in content:
            raise ValueError
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        raise ValidationError(
            "MarkItDown could not convert this file. It may be unsupported, empty, or exceed "
            "1,000,000 Markdown characters. Image-only PDFs require OCR."
        ) from None
    with transaction.atomic():
        app, _ = access(user, app_id, "knowledge", write=True)
        locked = Document.objects.select_for_update().get(pk=doc.pk, application=app)
        if locked.status == "ready":
            return KnowledgeEntry.objects.get(document=locked)
        if locked.status not in {"quarantined", "failed", "queued", "converting"}:
            raise ValidationError("This document is no longer eligible for processing.")
        entry = add_knowledge(user, app_id, doc.name, content, document=locked)
        locked.status = "ready"
        locked.converter = "MarkItDown " + version("markitdown")
        locked.conversion_error = ""
        locked.save(update_fields=["status", "converter", "conversion_error"])
        audit(
            user,
            "document.processed",
            doc.pk,
            app.product.portfolio.organization,
            details={"security_scan_required": settings.DOCUMENT_SCAN_REQUIRED},
        )
        return entry


def convert_document(user, app_id, document_id):
    """Persist actionable conversion failures without removing the uploaded original."""
    app, _ = access(user, app_id, "knowledge", write=True)
    try:
        return process_document(user, app_id, document_id)
    except ValidationError as error:
        with transaction.atomic():
            doc = Document.objects.select_for_update().get(pk=document_id, application=app)
            if doc.status not in {"ready", "rejected", "deleted"}:
                doc.status = "failed"
                doc.conversion_error = " ".join(error.messages)[:500]
                doc.save(update_fields=["status", "conversion_error"])
                audit(
                    user, "document.conversion_failed", doc.pk, app.product.portfolio.organization
                )
        raise
