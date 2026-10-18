"""Durable local document queue, hosted by the managed application process."""

import hashlib
import logging
import time
from datetime import timedelta
from threading import Thread

from django.db import close_old_connections, connection, connections, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Document
from .processing import convert_document, settle_failure

logger = logging.getLogger(__name__)


def fairly(waiting, working, active_since):
    """The oldest waiting document from an application with nothing in `working`.

    Oldest first across everyone let one application's ten-thousand-file import
    hold every other application's single upload behind it. Preferring an
    application that has nothing in progress gives each one a turn; when every
    waiting application is already busy, the oldest is taken anyway, so a lone
    large import still uses every worker it has.
    """
    busy = Document.objects.filter(
        status=working, conversion_started_at__gte=active_since
    ).values("application_id")
    ordered = waiting.order_by("created_at")
    return ordered.exclude(application_id__in=busy).first() or ordered.first()


def process_next_link():
    """Download one queued link. Fetching is separate from conversion so a slow
    host delays only itself, and the rail can show downloading apart from
    converting."""
    from .link_sources import download

    expired = timezone.now() - timedelta(minutes=5)
    doc = fairly(
        Document.objects.filter(
            Q(status="pending") | Q(status="fetching", conversion_started_at__lt=expired)
        ).select_related("application"),
        "fetching",
        expired,
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
    doc = fairly(
        Document.objects.filter(
            Q(status="queued") | Q(status="converting", conversion_started_at__lt=expired)
        ).select_related("conversion_actor", "uploaded_by"),
        "converting",
        expired,
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
    except Exception as failure:
        # Validation messages were persisted by convert_document. No content or
        # traceback is logged - a traceback from a conversion can carry the
        # document's own text. The exception's *type* carries none of it, and
        # without it a transient failure and a permanent one look identical to
        # everyone: the reader, and the operator reading the log afterwards.
        with transaction.atomic():
            stuck = Document.objects.select_for_update().filter(
                pk=doc.pk, status="converting"
            ).first()
            if stuck:
                settle_failure(stuck, "Conversion could not finish. Check access and retry.")
        logger.warning(
            "document_conversion_failed",
            extra={
                "event": "document_conversion_failed",
                "document_id": str(doc.pk),
                "exception_type": type(failure).__name__,
            },
        )
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
    """Refresh what a change asked for; now and then, ask about everything."""
    from .graphs import reconcile_if_due
    from .jobs import step_for

    return (step_for("graph"), reconcile_if_due)


def factory_steps():
    """Analysis first, then the implementation agents.

    One lane rather than two: they are the same work on the same run, and a
    run cannot be in both states at once, so nothing is gained by letting them
    contend.
    """
    from .code_factory import process_next_run
    from .code_factory_build import (
        process_next_checks,
        process_next_preparation,
        reclaim_stalled_runs,
    )

    # Reclaiming first: a run this process abandoned by restarting is holding a
    # ticket nobody else may start, so clearing it is what lets work continue.
    return (
        reclaim_stalled_runs,
        process_next_run,
        process_next_preparation,
        process_next_checks,
    )


def code_graph_steps():
    """Index what was asked for, then notice what has moved since.

    Indexing first: a repository waiting to be read matters more than one whose
    branch may have advanced.
    """
    from .code_docs import process_next_docs_ai
    from .code_graph_ingest import process_next_head, process_next_repository
    from .code_review import process_next_review, process_next_review_ai

    # A review reads a change the way indexing reads a repository, and its AI
    # step waits on a provider the way a graph run does; both are asked for by
    # someone waiting on the page, so they come before the branch check.
    return (
        process_next_repository,
        process_next_review,
        process_next_review_ai,
        process_next_docs_ai,
        process_next_head,
    )


def serviceops_steps():
    from .serviceops_triage import process_next_triage

    return (process_next_triage,)


def connector_steps():
    """Ask for every import that is due, then run one."""
    from .connectors import schedule_due
    from .jobs import step_for

    return (schedule_due, step_for("connectors"))


def serviceops_test_steps():
    from .serviceops_testing import process_next_test

    return (process_next_test,)


def automation_steps():
    from .automation_execution import process_next_execution

    return (process_next_execution,)


def maintenance_steps():
    from .chunks import backfill_step
    from .gateway import sweep
    from .graph_store import record_step
    from .jobs import maintain
    from .knowledge_sources import process_next_source
    from .queue_status import log_if_due

    # Checking a source is maintenance, not intake: it reads one cheap signal,
    # writes a status and touches no document. It shares the lane with the
    # retention sweep because neither is urgent and neither may hold up a
    # conversion - and because putting it in the intake lane would have it
    # competing with the downloads it might one day cause. The queue figures
    # are logged here for the same reason: they are read, never acted on.
    # Reclaiming abandoned jobs is here too: it must run even when the lane
    # whose worker died is the one that would have run it. Writing passages
    # for sources stored before they existed is a batch at a time, last, so a
    # large library catching up never delays the rest; recording graph versions
    # saved before their elements were rows is the same kind of catching up.
    # Giving back AI gateway slots a dead process left beside the jobs it left.
    return (
        purge_chat_if_due,
        process_next_source,
        maintain,
        sweep,
        log_if_due,
        backfill_step,
        record_step,
    )


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
    # Its own lane, checked every second: somebody is watching a triage run's
    # steps as it goes, and it must not wait behind a Code Factory run.
    ("serviceops", serviceops_steps, 1.0),
    ("serviceops-tests", serviceops_test_steps, 3.0),
    ("automations", automation_steps, 2.0),
    # Its own lane, not maintenance: an import waits on somebody else's instance
    # and may take tens of seconds, which must not sit in front of the retention
    # sweep. Ten seconds between ticks is ample when the shortest interval an
    # owner can choose is fifteen minutes.
    ("connectors", connector_steps, 10.0),
    ("maintenance", maintenance_steps, 30.0),
)


#: Lanes whose every step claims its own row - a document by a conditional
#: update, a job by a row lock - so any number of threads and processes may run
#: them at once. Every other lane is still one process at a time, held by
#: `LaneKeeper`'s advisory lock, because some of their steps pick a row and act
#: on it. `run_worker --concurrency intake=4` runs more than one thread.
SHARED_LANES = frozenset({"intake", "graph", "connectors"})


#: How long a lane this process does not hold waits before asking again, and
#: how often a lane it does hold proves its lock connection alive and writes
#: its heartbeat.
LANE_RETRY_SECONDS = 15
HEARTBEAT_SECONDS = 15


def lane_key(name):
    """The advisory lock number for a lane: the same in every process and release."""
    digest = hashlib.sha256(f"digitalbrain-lane:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class LaneKeeper:
    """At most one process per database runs each lane.

    The lanes were written for a single worker. Several steps claim their row
    with a conditional update and would be safe twice over; others pick a row
    and act on it - a scheduled connector import, a triage run - and would do
    the work twice. Until every step claims for itself, the lane is the unit of
    exclusion: a PostgreSQL session advisory lock, held on a connection of its
    own so `close_old_connections` between steps never releases it.

    A second process starting the same lane waits as a standby and takes over
    once the first one's connection goes. That is what lets the worker run
    apart from the web process, or in two places, without anything running
    twice.

    SQLite is one machine's file and has no advisory locks: there every keeper
    holds, as the single in-process worker always did. A session lock does not
    survive a transaction-mode pooler such as PgBouncer, so a worker must reach
    the database directly.
    """

    def __init__(self, name):
        self.name = name
        #: A shared lane takes no lock and still beats, so a quiet lane is seen.
        self.exclusive = name not in SHARED_LANES
        self.key = lane_key(name)
        self.connection = None
        self.holding = False
        self.since = None
        self.checked = 0.0
        self.waiting_said = False

    def ready(self):
        """Whether this process may run the lane now. Never raises."""
        now = time.monotonic()
        if self.holding and now - self.checked < HEARTBEAT_SECONDS:
            return True
        try:
            if self.holding:
                self.confirm()
            else:
                self.acquire()
            if self.holding:
                self.beat()
        except Exception as failure:
            self.drop()
            logger.warning(
                "worker lane lock failed",
                extra={
                    "event": "worker_lane_lock_failed",
                    "lane": self.name,
                    "exception_type": type(failure).__name__,
                },
            )
        self.checked = now
        return self.holding

    def acquire(self):
        if connection.vendor != "postgresql" or not self.exclusive:
            self.holding = True
            self.since = timezone.now()
            return
        self.connection = connections.create_connection("default")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [self.key])
            self.holding = bool(cursor.fetchone()[0])
        if self.holding:
            self.since = timezone.now()
            self.waiting_said = False
            logger.info(
                "worker lane taken", extra={"event": "worker_lane_taken", "lane": self.name}
            )
            return
        # Another process runs this lane. Nothing is held open while waiting.
        self.connection.close()
        self.connection = None
        if not self.waiting_said:
            self.waiting_said = True
            logger.info(
                "worker lane standing by",
                extra={"event": "worker_lane_standby", "lane": self.name},
            )

    def confirm(self):
        """Raise if the connection holding the lock has gone, and the lock with it."""
        if self.connection is None:
            return
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def beat(self):
        from .agent_runtime.streaming import PROCESS
        from .models import LaneHeartbeat

        LaneHeartbeat.objects.update_or_create(
            lane=self.name,
            defaults={"owner": PROCESS, "started_at": self.since, "beat_at": timezone.now()},
        )

    def drop(self):
        self.holding = False
        self.since = None
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None

    def release(self):
        """Give the lane up at a deliberate stop, so a standby need not wait."""
        if self.connection is not None:
            try:
                with self.connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [self.key])
            except Exception:
                pass
        self.drop()


def pause(seconds, stop):
    """Sleep, waking early for a stop request when there is one to wake for."""
    if stop is None:
        time.sleep(seconds)
    else:
        stop.wait(seconds)


def run_lane(name, steps_for, idle_seconds, stop=None, keeper=None):
    """Run one queue until `stop` is set, or forever without one.

    A failure in this lane never reaches another.

    The loop does not sleep while it is finding work, so a backlog drains at the
    speed of the work rather than one item per tick.

    Each step is contained on its own. The steps once shared one try, so a step
    that raised on every tick - one row it cannot read is enough - starved every
    step after it: a broken reclaim would have stopped every Code Factory run.

    A stop is honoured between steps, never inside one: a conversion or a paid
    call that has started is finished and recorded.
    """
    steps = steps_for()
    try:
        while stop is None or not stop.is_set():
            if keeper is not None and not keeper.ready():
                pause(LANE_RETRY_SECONDS, stop)
                continue
            worked = False
            for step in steps:
                if stop is not None and stop.is_set():
                    break
                try:
                    close_old_connections()
                    worked = bool(step()) or worked
                except Exception as failure:
                    # The event name is what makes this findable; the formatter
                    # deliberately keeps the message and traceback out of the log.
                    logger.warning(
                        "worker lane failed",
                        extra={
                            "event": "worker_lane_failed",
                            "lane": name,
                            "step": step.__name__,
                            "exception_type": type(failure).__name__,
                        },
                        exc_info=True,
                    )
                finally:
                    close_old_connections()
            pause(0 if worked else idle_seconds, stop)
    finally:
        if keeper is not None:
            keeper.release()


def run_document_worker():
    """Start every lane in this process. The calling thread becomes the first.

    What `scripts/serve.py` runs unless it is started with `--no-worker`, when
    `manage.py run_worker` runs the lanes in a process of their own instead.
    """
    first, *rest = LANES
    for name, steps_for, idle_seconds in rest:
        Thread(
            target=run_lane,
            args=(name, steps_for, idle_seconds, None, LaneKeeper(name)),
            name=f"worker-{name}",
            daemon=True,
        ).start()
    run_lane(*first, None, LaneKeeper(first[0]))


def lanes_named(names):
    """The LANES entries for these names, in the order given. Unknown names raise."""
    known = {lane[0]: lane for lane in LANES}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"Unknown lane: {', '.join(unknown)}. Lanes are {', '.join(known)}.")
    return [known[name] for name in dict.fromkeys(names)]


def start_lanes(lanes, stop, concurrency=None):
    """Run each lane on threads of its own until `stop` is set. Returns the threads.

    `concurrency` maps a shared lane to how many threads run it; one otherwise.
    """
    concurrency = concurrency or {}
    threads = []
    for name, steps_for, idle_seconds in lanes:
        count = concurrency.get(name, 1)
        for index in range(count):
            thread = Thread(
                target=run_lane,
                args=(name, steps_for, idle_seconds, stop, LaneKeeper(name)),
                name=f"worker-{name}" if count == 1 else f"worker-{name}-{index + 1}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
    return threads


#: The most threads one process runs for a lane. A thread holds a database
#: connection for as long as it works, so this is also a connection budget.
MAX_CONCURRENCY = 16


def concurrency_from(text):
    """Parse `lane=N,lane=N`. Only shared lanes may run more than one thread."""
    known = {lane[0] for lane in LANES}
    result = {}
    for part in filter(None, (piece.strip() for piece in (text or "").split(","))):
        name, _, value = part.partition("=")
        name = name.strip()
        if name not in known:
            raise ValueError(f"Unknown lane: {name}. Lanes are {', '.join(known)}.")
        try:
            count = int(value)
        except ValueError:
            raise ValueError(f"{name}: give a number of threads, as {name}=2.") from None
        if not 1 <= count <= MAX_CONCURRENCY:
            raise ValueError(f"{name}: between 1 and {MAX_CONCURRENCY} threads.")
        if count > 1 and name not in SHARED_LANES:
            raise ValueError(
                f"{name} runs one at a time; only {', '.join(sorted(SHARED_LANES))} "
                "may run more than one thread."
            )
        result[name] = count
    return result
