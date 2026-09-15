"""Durable local document queue, hosted by the managed application process."""

import logging
import time
from datetime import timedelta
from threading import Thread

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


_last_purge = 0.0

#: Retention is measured in days; checking hourly is ample and keeps the lane
#: from running a table scan on every tick.
PURGE_INTERVAL = 3600


def purge_chat_if_due():
    global _last_purge
    now = time.monotonic()
    if now - _last_purge < PURGE_INTERVAL:
        return False
    _last_purge = now
    from .workbench import purge_expired_conversations

    removed = purge_expired_conversations()
    if removed:
        logger.info(
            "conversations purged",
            extra={"event": "chat_conversations_purged", "count": removed},
        )
    return bool(removed)


def intake_steps():
    """Download queued links, then convert whatever is waiting."""
    return (process_next_link, process_next_document)


def graph_steps():
    from .graphs import process_next_graph

    return (process_next_graph,)


def factory_steps():
    from .code_factory import process_next_run

    return (process_next_run,)


def code_graph_steps():
    from .code_graph_ingest import process_next_repository

    return (process_next_repository,)


def maintenance_steps():
    return (purge_chat_if_due,)


#: Independent queues, each on its own thread.
#:
#: These shared one loop, so a graph run - which may legitimately spend ten
#: minutes waiting on a provider - stalled every document conversion and link
#: import behind it, across every application. They contend for nothing that
#: needed the shared loop: process_next_graph already declines an application
#: whose documents are still converting, which is the only ordering that ever
#: mattered, and it still does.
#:
#: Each lane carries its own idle delay because they are not equally urgent: an
#: upload should start converting promptly, a retention sweep does not need to be
#: asked about every second.
LANES = (
    ("intake", intake_steps, 1.0),
    ("graph", graph_steps, 2.0),
    ("code-graph", code_graph_steps, 2.0),
    # Its own lane for the same reason graph has one: a Code Factory run makes
    # three provider calls in sequence and must not sit in front of an upload.
    ("factory", factory_steps, 2.0),
    ("maintenance", maintenance_steps, 30.0),
)


def run_lane(name, steps_for, idle_seconds):
    """Run one queue forever. A failure in this lane never reaches another.

    The loop does not sleep while it is finding work, so a backlog drains at the
    speed of the work rather than one item per tick.
    """
    steps = steps_for()
    while True:
        worked = False
        try:
            close_old_connections()
            for step in steps:
                worked = bool(step()) or worked
        except Exception:
            # The event name is what makes this findable; the formatter
            # deliberately keeps the message and traceback out of the log.
            logger.warning(
                "worker lane failed",
                extra={"event": "worker_lane_failed", "lane": name},
                exc_info=True,
            )
        finally:
            close_old_connections()
        time.sleep(0 if worked else idle_seconds)


def run_document_worker():
    """Start every lane. The calling thread becomes the first of them."""
    first, *rest = LANES
    for name, steps_for, idle_seconds in rest:
        Thread(
            target=run_lane,
            args=(name, steps_for, idle_seconds),
            name=f"worker-{name}",
            daemon=True,
        ).start()
    run_lane(*first)
