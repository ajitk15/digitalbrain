"""Old audit events and usage receipts leave - only when a deployment says so.

Both tables grow with use and nothing ever removed a row: every sign-in,
every change and every paid call is one more, forever. That is the right
default - an audit trail and a bill are records, not caches - so nothing here
runs unless `[retention]` in the configuration names a number of days, and a
number below the floor is refused by `load_config`:

* ``audit_days`` - at least six years (2,192 days, leap days included). HIPAA's
  documentation rule (§164.316(b)(2)) keeps six years from creation; a shorter
  window would make the platform the reason a covered entity cannot show it.
* ``usage_days`` - at least 400. A receipt is what the monthly budget is
  measured from and what a provider's invoice is reconciled against; 400 days
  covers the current month, a year's comparison and a late invoice.

Rows go oldest first, `BATCH` at a time, each batch its own short delete, from
the maintenance lane at most once every `INTERVAL` - so a first run against a
decade of rows is many small steps rather than one statement holding a lock
on a table every request writes to. What went is logged as counts, never rows.
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from digitalbrain.configuration import RETENTION_FLOORS

logger = logging.getLogger(__name__)

#: The least each window may be, in days. `configuration.load_config` refuses less.
FLOORS = RETENTION_FLOORS
BATCH = 1000
#: Batches a step deletes from one table before it gives the lane back.
BATCHES_PER_STEP = 10
INTERVAL = 3600


def windows():
    """`{setting: days}` for each window a deployment set; empty means keep all."""
    configured = getattr(settings, "RETENTION", {}) or {}
    return {name: days for name, days in configured.items() if days}


def tables():
    from .models import AIUsage, AuditEvent

    return {"audit_days": AuditEvent, "usage_days": AIUsage}


def purge(model, days, now=None):
    """Delete up to `BATCHES_PER_STEP` batches of `model` rows older than `days`."""
    cutoff = (now or timezone.now()) - timedelta(days=days)
    removed = 0
    for _ in range(BATCHES_PER_STEP):
        batch = list(
            model.objects.filter(created_at__lt=cutoff)
            .order_by("created_at")
            .values_list("pk", flat=True)[:BATCH]
        )
        if not batch:
            break
        # Counted by model: nothing cascades from these, but a total would
        # count anything that one day did.
        removed += model.objects.filter(pk__in=batch).delete()[1].get(model._meta.label, 0)
        if len(batch) < BATCH:
            break
    return removed


_last = 0.0


def purge_if_due(now=None):
    """One retention step for the maintenance lane. Never raises."""
    global _last
    chosen = windows()
    if not chosen or time.monotonic() - _last < INTERVAL:
        return False
    _last = time.monotonic()
    counts = {}
    for name, days in chosen.items():
        try:
            counts[name] = purge(tables()[name], days, now)
        except Exception as failure:
            logger.warning(
                "retention step failed",
                extra={
                    "event": "retention_failed",
                    "window": name,
                    "exception_type": type(failure).__name__,
                },
            )
    if any(counts.values()):
        logger.info("retention purged", extra={"event": "retention_purged", **counts})
    return any(counts.values())
