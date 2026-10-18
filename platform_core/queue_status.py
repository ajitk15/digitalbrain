"""How much work each worker lane has waiting, and whether anything is running it.

Counts and ages only - never a title, a name or an id - so the same figures can
go to a console, a log line and, later, a metrics endpoint. They read the same
rows the lanes' own steps pick from, so "waiting" here means what the lane
would take next, not a guess about it.

With the worker in the web process a dead lane took the web process with it.
Run apart (`manage.py run_worker`), a lane can stop on its own, and its
heartbeat going quiet is the only sign: `stale` says so.
"""

import logging
import time
from datetime import timedelta

from django.db.models import Count, Min
from django.utils import timezone

logger = logging.getLogger(__name__)

#: A lane whose holder has not written its heartbeat for this long is reported
#: as stale. Several missed beats, so one slow step does not raise an alarm.
STALE_AFTER = timedelta(minutes=2)
#: How often the maintenance lane writes the figures to the log.
LOG_INTERVAL = 300


def waiting_work():
    """{lane: [(queryset, timestamp field)]} of what each lane would pick up next."""
    from .models import (
        AutomationRun,
        CodeDocumentation,
        CodeRepository,
        CodeReview,
        Document,
        FactoryRun,
        KnowledgeGraph,
        ServiceOpsTestRun,
        TriageRun,
    )

    return {
        "intake": [(Document.objects.filter(status__in=["pending", "queued"]), "created_at")],
        "graph": [(KnowledgeGraph.objects.filter(status="queued"), "updated_at")],
        "code-graph": [
            (CodeRepository.objects.filter(status="queued"), "updated_at"),
            (CodeReview.objects.filter(status="queued"), "updated_at"),
            (CodeReview.objects.filter(ai_status="queued", status="ready"), "updated_at"),
            (CodeDocumentation.objects.filter(ai_status="queued"), "updated_at"),
        ],
        "factory": [
            (FactoryRun.objects.filter(status__in=["pending", "prepare_queued"]), "created_at")
        ],
        "serviceops": [(TriageRun.objects.filter(status="queued"), "created_at")],
        "serviceops-tests": [(ServiceOpsTestRun.objects.filter(status="queued"), "created_at")],
        "automations": [
            (AutomationRun.objects.filter(mode="real", status="queued"), "created_at")
        ],
    }


def due_connectors(now):
    """(how many scheduled imports are due, when the longest-waiting one fell due)."""
    from .models import Connector

    count, oldest = 0, None
    for interval, last in Connector.objects.filter(
        enabled=True, sync_interval_minutes__gt=0
    ).values_list("sync_interval_minutes", "last_attempt_at"):
        due = last + timedelta(minutes=interval) if last else None
        if due is None or due <= now:
            count += 1
            # Never attempted: due since it was switched on, which is not
            # recorded - count it, but give it no age.
            if due is not None and (oldest is None or due < oldest):
                oldest = due
    return count, oldest


def lane_status(now=None):
    """One row per lane: waiting, oldest_seconds, owner, heartbeat_seconds, stale."""
    from .document_worker import LANES
    from .models import LaneHeartbeat

    now = now or timezone.now()
    beats = {beat.lane: beat for beat in LaneHeartbeat.objects.all()}
    work = waiting_work()
    rows = []
    for name, _, _ in LANES:
        waiting, oldest = 0, None
        for queryset, field in work.get(name, []):
            found = queryset.aggregate(count=Count("pk"), oldest=Min(field))
            waiting += found["count"]
            if found["oldest"] and (oldest is None or found["oldest"] < oldest):
                oldest = found["oldest"]
        if name == "connectors":
            waiting, oldest = due_connectors(now)
        beat = beats.get(name)
        rows.append(
            {
                "lane": name,
                "waiting": waiting,
                "oldest_seconds": int((now - oldest).total_seconds()) if oldest else None,
                "owner": beat.owner if beat else "",
                "heartbeat_seconds": int((now - beat.beat_at).total_seconds()) if beat else None,
                "stale": beat is None or now - beat.beat_at > STALE_AFTER,
            }
        )
    return rows


_last_logged = 0.0


def log_if_due():
    """Write each lane's figures as a structured event, every `LOG_INTERVAL` seconds.

    A maintenance step, so it returns False: logging is not work, and the lane
    should still rest between ticks.
    """
    global _last_logged
    now = time.monotonic()
    if now - _last_logged < LOG_INTERVAL:
        return False
    _last_logged = now
    for row in lane_status():
        logger.info(
            "queue status",
            extra={
                "event": "queue_status",
                "lane": row["lane"],
                "waiting": row["waiting"],
                "oldest_seconds": row["oldest_seconds"],
                "heartbeat_seconds": row["heartbeat_seconds"],
                "stale": row["stale"],
            },
        )
    return False
