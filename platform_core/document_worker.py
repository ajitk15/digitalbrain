"""Durable local document queue, hosted by the managed application process."""

import logging
import time
from datetime import timedelta

from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from .models import Document
from .processing import convert_document

logger = logging.getLogger(__name__)


def process_next_link():
    """Download one queued link. Fetching is separate from conversion so a slow
    host delays only itself, and the rail can show downloading apart from
    converting."""
    from .link_sources import download

    expired = timezone.now() - timedelta(minutes=5)
    doc = (
        Document.objects.filter(
            Q(status="pending") | Q(status="fetching", conversion_started_at__lt=expired)
        )
        .select_related("application")
        .order_by("created_at")
        .first()
    )
    if doc is None:
        return False
    if doc.status == "fetching":
        # A download interrupted by a restart becomes eligible again.
        Document.objects.filter(pk=doc.pk, status="fetching").update(status="pending")
        doc.status = "pending"
    try:
        download(doc)
    except Exception:
        Document.objects.filter(pk=doc.pk).exclude(status="deleted").update(
            status="failed", conversion_error="The link could not be downloaded."
        )
        logger.warning("document_link_failed")
    return True


def process_next_document():
    expired = timezone.now() - timedelta(minutes=5)
    doc = (
        Document.objects.filter(
            Q(status="queued") | Q(status="converting", conversion_started_at__lt=expired)
        )
        .select_related("conversion_actor", "uploaded_by")
        .order_by("created_at")
        .first()
    )
    if doc is None:
        return False
    claimed = Document.objects.filter(
        pk=doc.pk, status=doc.status, conversion_started_at=doc.conversion_started_at
    ).update(status="converting", conversion_started_at=timezone.now())
    if not claimed:
        return True
    actor = doc.conversion_actor or doc.uploaded_by
    try:
        convert_document(actor, doc.application_id, doc.pk)
    except Exception:
        # Validation messages were persisted by convert_document. No content/traceback logging.
        Document.objects.filter(pk=doc.pk, status="converting").update(
            status="failed", conversion_error="Conversion could not finish. Check access and retry."
        )
        logger.warning("document_conversion_failed")
    return True


def run_document_worker():
    while True:
        try:
            close_old_connections()
            process_next_link()
            process_next_document()
            from .graphs import process_next_graph

            process_next_graph()
        except Exception:
            logger.warning("document_worker_retry")
        finally:
            close_old_connections()
        time.sleep(1)
