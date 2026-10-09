"""Server-sent event plumbing for streamed chat answers.

PROCESS-LOCAL BY DEFAULT. The session registry is module state, so a stop request
only reaches a stream started by the same process. `scripts/serve.py` runs one
waitress process, which is what makes that correct.

More than one process sets `shared_streams = true`, and every stream also holds
a `StreamLease` row: the streaming process renews it and polls it for a Stop
another process wrote, and any process can ask whether an answer is still being
written (`is_live`). A polled database column rather than Redis: an answer and
its stream always live in the same process - the stream request starts the
worker - so only "stop" and "is it live?" cross processes, and that does not
need a broker. The lease is written by a keeper thread, never by the SSE
generator, which keeps the no-ORM rule below.

Division of labour, which the rest of the chat code depends on:

* The SSE generator (a waitress request thread) performs **no ORM work**. It
  relays frames and emits heartbeats.
* The worker thread owns every database write for its message. A single writer is
  what keeps a racing stop from corrupting the row.
"""

import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 10
MAX_STREAM_SECONDS = 180
MAX_QUEUED_EVENTS = 1000
#: Concurrent streams allowed. Each one occupies a waitress request thread for
#: its whole life, so this must stay comfortably below the thread count or a
#: handful of long answers starve navigation, Stop and the readiness probe.
#: scripts/serve.py derives its thread count from this so the two cannot drift.
MAX_ACTIVE_STREAMS = 12
SESSION_TTL_SECONDS = 600

#: Pushed by the worker to mark the end of the event stream.
END_OF_STREAM = object()

#: Shared mode: how often the streaming process renews its lease and looks for
#: a Stop written by another process, and how long a silent lease stays live.
LEASE_POLL_SECONDS = 2
LEASE_STALE_SECONDS = 30
#: This process, as a lease names it.
PROCESS = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class StreamCapacityError(RuntimeError):
    """Raised when too many streams are already running in this process."""


@dataclass
class StreamSession:
    message_id: str
    events: queue.Queue = field(default_factory=lambda: queue.Queue(MAX_QUEUED_EVENTS))
    cancel: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.monotonic)

    def emit(self, event, data):
        """Publish one event. Drops silently once the reader is gone."""
        if self.cancel.is_set() and event == "delta":
            return
        try:
            self.events.put((event, data), timeout=5)
        except queue.Full:
            self.cancel.set()

    def finish(self):
        try:
            self.events.put(END_OF_STREAM, timeout=5)
        except queue.Full:
            pass

    @property
    def stopped(self):
        return self.cancel.is_set()


_sessions: dict[str, StreamSession] = {}
_lock = threading.Lock()


def _sweep(now):
    stale = [
        key for key, session in _sessions.items() if now - session.started_at > SESSION_TTL_SECONDS
    ]
    for key in stale:
        _sessions[key].cancel.set()
        del _sessions[key]


def shared():
    from django.conf import settings

    return bool(getattr(settings, "SHARED_STREAMS", False))


def open_session(message_id):
    """Register a stream, refusing to start one when the process is saturated.

    Shared, it also refuses an answer another process is streaming: a second
    worker for the same answer would be a second paid call.
    """
    key = str(message_id)
    now = time.monotonic()
    with _lock:
        _sweep(now)
        if key in _sessions:
            raise StreamCapacityError("This answer is already streaming.")
        if len(_sessions) >= MAX_ACTIVE_STREAMS:
            raise StreamCapacityError("Too many answers are streaming right now.")
        session = StreamSession(message_id=key)
        _sessions[key] = session
    if shared():
        try:
            claim_lease(key)
        except StreamCapacityError:
            close_session(key)
            raise
        start_keeper(session)
    return session


def get_session(message_id):
    with _lock:
        return _sessions.get(str(message_id))


def close_session(message_id):
    # Local only and no ORM: the SSE generator calls this. The keeper thread
    # sees the session gone and removes the lease itself.
    with _lock:
        _sessions.pop(str(message_id), None)


def request_stop(message_id):
    """Signal a running stream to stop. True when a live stream was signalled.

    Shared, a stream in another process is signalled through its lease, and
    that process's keeper sees it within `LEASE_POLL_SECONDS`.
    """
    session = get_session(message_id)
    if session is not None:
        session.cancel.set()
        return True
    if not shared():
        return False
    from ..models import StreamLease

    return bool(
        StreamLease.objects.filter(message_id=message_id, heartbeat_at__gte=fresh_since()).update(
            stop_requested=True
        )
    )


def is_live(message_id):
    """Whether an answer is being streamed now: by this process, or, shared, any."""
    if get_session(message_id) is not None:
        return True
    if not shared():
        return False
    from ..models import StreamLease

    return StreamLease.objects.filter(
        message_id=message_id, heartbeat_at__gte=fresh_since()
    ).exists()


# ── shared mode ──────────────────────────────────────────────────────────────


def fresh_since():
    from django.utils import timezone

    return timezone.now() - timedelta(seconds=LEASE_STALE_SECONDS)


def claim_lease(key):
    """Take the answer's lease, unless another live process holds it."""
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    from ..models import StreamLease

    try:
        with transaction.atomic():
            lease = StreamLease.objects.select_for_update().filter(message_id=key).first()
            if lease and lease.owner != PROCESS and lease.heartbeat_at >= fresh_since():
                raise StreamCapacityError("This answer is already streaming.")
            StreamLease.objects.update_or_create(
                message_id=key,
                defaults={
                    "owner": PROCESS,
                    "heartbeat_at": timezone.now(),
                    "stop_requested": False,
                },
            )
    except IntegrityError:
        # Another process created it between the read and the write.
        raise StreamCapacityError("This answer is already streaming.") from None


def renew(session):
    """One keeper tick: renew the lease and honour a Stop written elsewhere.

    Returns False once the keeper should stop: the stream ended here, or the
    lease is gone or was taken over by another process.
    """
    from django.utils import timezone

    from ..models import StreamLease

    lease = StreamLease.objects.filter(message_id=session.message_id, owner=PROCESS)
    if get_session(session.message_id) is not session:
        lease.delete()
        return False
    if not lease.update(heartbeat_at=timezone.now()):
        session.cancel.set()
        return False
    if lease.filter(stop_requested=True).exists():
        session.cancel.set()
    return True


def keep_lease(session):
    from django.db import connection

    try:
        while renew(session):
            time.sleep(LEASE_POLL_SECONDS)
    except Exception:
        # A lease that cannot be renewed goes stale and reads as gone; the
        # stream itself carries on, as it would in a single process.
        logger.exception("stream_lease_failed", extra={"event": "stream_lease_failed"})
    finally:
        connection.close()


def start_keeper(session):
    threading.Thread(
        target=keep_lease, args=(session,), name=f"stream-lease-{session.message_id}", daemon=True
    ).start()


def encode(event, data, sequence):
    """One SSE frame. `data` is always a JSON object so the client parse is uniform."""
    body = json.dumps(data, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event}\ndata: {body}\n\n"


def stream_frames(session):
    """Drain a session onto the wire.

    Heartbeats serve two purposes: they keep waitress's `channel_timeout`
    inactivity timer from closing an idle stream, and the failed write on a
    vanished client is how we learn about a disconnect.
    """
    sequence = 0
    try:
        while True:
            if time.monotonic() - session.started_at > MAX_STREAM_SECONDS:
                sequence += 1
                yield encode("error", {"message": "This answer took too long."}, sequence)
                session.cancel.set()
                return
            try:
                item = session.events.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if item is END_OF_STREAM:
                return
            event, data = item
            sequence += 1
            yield encode(event, data, sequence)
    finally:
        # A client that walked away must not leave the worker producing forever.
        session.cancel.set()
        close_session(session.message_id)
