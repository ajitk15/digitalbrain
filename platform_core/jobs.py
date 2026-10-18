"""Background work as rows: written with the change, claimed with a lease.

The graph lane used to find its work by computing every application's
fingerprint every two seconds, and the connector lane by reading every
connector every ten. That is a full scan per tick whatever happened, which is
the cost that grows with the number of applications rather than with the work.
Now a change writes a `Job` row in the same transaction as the change itself,
and a lane takes the next row that is due.

**Same transaction is the outbox.** A change that rolls back takes its job with
it, and a job is visible to a worker only once the change is. There is no
separate event table to drain, because nothing here needs the event after the
work it asks for has been done.

**One waiting row per key.** `enqueue` folds a second request for the same
thing into the one already waiting - the earlier due time wins - so a burst of
twenty uploads is one graph rebuild. A running job does not count: a change
made while it runs gets a row of its own, which waits until the running one
finishes, because two runs on one key at once are never allowed.

**Claims are row locks, not lane locks.** On PostgreSQL `SELECT ... FOR UPDATE
SKIP LOCKED` lets any number of workers take different rows at once; on SQLite,
which serialises writers anyway, a conditional update does the same job. An
application may hold at most `TENANT_CAP` running jobs in a lane, so one tenant
importing everything at once cannot occupy every worker.

**A lease, not a promise.** A claimed job carries `lease_expires_at`, renewed
by a thread while the handler runs. A worker that dies stops renewing, and
`maintain` puts its job back - or, past `max_attempts`, marks it dead. Kinds
that spend money or call somebody else's system have `max_attempts = 1`:
paid calls are never retried automatically, and a crash is not a reason to
import a board twice.

**Nothing here decides who the work runs as.** A handler reads that from the
thing the job is about - the connector's creator, whoever switched on automatic
refresh - and re-checks their grant, exactly as the scanning steps did.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.models import Count, DateTimeField, F, Value
from django.db.models.functions import Least
from django.utils import timezone
from django.utils.module_loading import import_string

from .models import Job

logger = logging.getLogger(__name__)


class Later(Exception):
    """Raised by a handler that cannot run yet. Not a failure: no attempt is used."""

    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


@dataclass(frozen=True)
class Kind:
    lane: str
    #: Dotted path, resolved when the job runs, so this registry imports nothing.
    handler: str
    max_attempts: int = 1
    #: How long a request waits before it is due. Requests inside that window
    #: fold into one job.
    delay: timedelta = timedelta(0)


#: The closed registry of background work, like `services.FEATURES`.
KINDS = {
    # Five seconds folds a burst of edits into one rebuild; a batch import that
    # takes longer is still one, because the handler waits while documents are
    # converting. One attempt: a failed rebuild was a failed paid call, and the
    # scan this replaced did not repeat those either - the next change does.
    "graph_refresh": Kind(
        lane="graph",
        handler="platform_core.graphs.refresh_job",
        delay=timedelta(seconds=5),
    ),
    # One attempt: an import that died halfway is not repeated by the queue.
    # The schedule asks again at the next interval, as it always did.
    "connector_sync": Kind(lane="connectors", handler="platform_core.connectors.scheduled_job"),
}

#: How long a claim holds without being renewed, and how often it is renewed.
LEASE = timedelta(minutes=2)
RENEW_SECONDS = 30
#: Running jobs one application may hold in one lane at once.
TENANT_CAP = 2
#: First retry after a failure; doubles with each attempt.
BACKOFF_SECONDS = 30
#: How long finished rows are kept for anyone asking what happened.
KEEP_DONE = timedelta(days=7)
KEEP_DEAD = timedelta(days=30)
MAINTAIN_INTERVAL = 60


def key_for(kind, subject):
    return f"{kind}:{subject}"


def enqueue(kind, subject, application_id=None, delay=None):
    """Ask for `kind` of work on `subject`. Folds into a waiting request.

    Call it inside the transaction that made the change: the row commits or
    rolls back with it.
    """
    spec = KINDS[kind]
    when = timezone.now() + (spec.delay if delay is None else delay)
    key = key_for(kind, subject)
    # An update first, not a read: if a worker is claiming the waiting row at
    # this moment the update waits for it and then matches nothing, so the
    # change gets a row of its own rather than folding into work that has
    # already started reading.
    if fold(key, when):
        return
    try:
        with transaction.atomic():
            Job.objects.create(
                kind=kind,
                lane=spec.lane,
                key=key,
                application_id=application_id,
                subject=str(subject),
                run_after=when,
                max_attempts=spec.max_attempts,
            )
    except IntegrityError:
        # Another request inserted the waiting row between the two statements.
        fold(key, when)


def fold(key, when):
    return Job.objects.filter(key=key, status="queued").update(
        run_after=Least(F("run_after"), Value(when, output_field=DateTimeField()))
    )


def owner():
    from .agent_runtime.streaming import PROCESS

    return f"{PROCESS}/{threading.get_ident()}"[:80]


def claim(lane, now=None):
    """Take the next due job in `lane`, or None."""
    now = now or timezone.now()
    running = Job.objects.filter(lane=lane, status="running")
    busy_keys = running.values("key")
    busy_applications = (
        running.filter(application_id__isnull=False)
        .values("application_id")
        .annotate(held=Count("id"))
        .filter(held__gte=TENANT_CAP)
        .values("application_id")
    )
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(lane=lane, status="queued", run_after__lte=now)
            .exclude(key__in=busy_keys)
            .exclude(application_id__in=busy_applications)
            .order_by("run_after", "id")
            .first()
        )
        if job is None:
            return None
        # The conditional update is the claim on SQLite, where FOR UPDATE does
        # nothing; on PostgreSQL the row lock already made it ours.
        mine = owner()
        claimed = Job.objects.filter(pk=job.pk, status="queued").update(
            status="running",
            attempts=F("attempts") + 1,
            lease_owner=mine,
            lease_expires_at=now + LEASE,
            started_at=now,
        )
    if not claimed:
        return None
    job.refresh_from_db()
    return job


class Renewal:
    """Extend a job's lease while its handler runs.

    It waits a whole interval before its first write, so a job that finishes
    quickly never touches the database from a second thread.
    """

    def __init__(self, job):
        self.job = job
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name=f"lease-{job.pk}", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)

    def run(self):
        try:
            while not self.stop.wait(RENEW_SECONDS):
                Job.objects.filter(
                    pk=self.job.pk, status="running", lease_owner=self.job.lease_owner
                ).update(lease_expires_at=timezone.now() + LEASE)
        except Exception as failure:
            logger.warning(
                "job lease renewal failed",
                extra={
                    "event": "job_lease_failed",
                    "kind": self.job.kind,
                    "exception_type": type(failure).__name__,
                },
            )
        finally:
            connection.close()


def mine(job):
    return Job.objects.filter(pk=job.pk, status="running", lease_owner=job.lease_owner)


def put_back(job, when, used_attempt=True, rows=None):
    """Return a running job to the queue, or fold it into one already waiting.

    A change made while this job ran has its own waiting row, and the key may
    have only one: that row does the same work, so this one is finished.
    """
    rows = mine(job) if rows is None else rows
    attempts = F("attempts") if used_attempt else F("attempts") - 1
    try:
        with transaction.atomic():
            moved = rows.update(
                status="queued",
                run_after=when,
                attempts=attempts,
                lease_owner="",
                lease_expires_at=None,
            )
    except IntegrityError:
        fold(job.key, when)
        moved = rows.update(
            status="done", lease_owner="", lease_expires_at=None, finished_at=timezone.now()
        )
    return moved


def run_next(lane):
    """Run one due job in `lane`. Returns True when there was one."""
    job = claim(lane)
    if job is None:
        return False
    spec = KINDS.get(job.kind)
    if spec is None:
        mine(job).update(status="dead", last_error_type="UnknownKind", finished_at=timezone.now())
        return True
    try:
        with Renewal(job):
            import_string(spec.handler)(job)
    except Later as later:
        put_back(job, timezone.now() + timedelta(seconds=later.seconds), used_attempt=False)
    except Exception as failure:
        failed(job, failure)
    else:
        mine(job).update(
            status="done", lease_owner="", lease_expires_at=None, finished_at=timezone.now()
        )
    return True


def failed(job, failure):
    error = type(failure).__name__[:80]
    logger.warning(
        "job failed",
        extra={
            "event": "job_failed",
            "kind": job.kind,
            "lane": job.lane,
            "attempts": job.attempts,
            "exception_type": error,
        },
        exc_info=True,
    )
    mine(job).update(last_error_type=error)
    if job.attempts < job.max_attempts:
        wait = BACKOFF_SECONDS * 2 ** (job.attempts - 1)
        put_back(job, timezone.now() + timedelta(seconds=wait))
    else:
        mine(job).update(
            status="dead", lease_owner="", lease_expires_at=None, finished_at=timezone.now()
        )


def step_for(lane):
    """A lane step that runs one job, named so the worker's log says which lane."""

    def step():
        return run_next(lane)

    step.__name__ = f"run_{lane.replace('-', '_')}_job"
    return step


def reclaim_expired(now=None):
    """Put back jobs whose worker stopped renewing them. Returns how many."""
    now = now or timezone.now()
    count = 0
    for job in Job.objects.filter(status="running", lease_expires_at__lt=now):
        # Still expired at the moment of writing: a renewal that lands first wins.
        rows = mine(job).filter(lease_expires_at__lt=now)
        if job.attempts < job.max_attempts:
            count += put_back(job, now, rows=rows)
        else:
            count += rows.update(
                status="dead",
                last_error_type="LeaseExpired",
                lease_owner="",
                lease_expires_at=None,
                finished_at=now,
            )
    if count:
        logger.warning("jobs reclaimed", extra={"event": "jobs_reclaimed", "count": count})
    return count


def purge_finished(now=None):
    now = now or timezone.now()
    removed, _ = Job.objects.filter(status="done", finished_at__lt=now - KEEP_DONE).delete()
    dead, _ = Job.objects.filter(status="dead", finished_at__lt=now - KEEP_DEAD).delete()
    return removed + dead


_last_maintained = 0.0


def maintain():
    """A maintenance step: reclaim abandoned jobs and drop old finished ones.

    Returns False - it is upkeep, and the lane should still rest between ticks.
    """
    global _last_maintained
    now = time.monotonic()
    if now - _last_maintained < MAINTAIN_INTERVAL:
        return False
    _last_maintained = now
    reclaim_expired()
    purge_finished()
    return False
