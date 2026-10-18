"""Server-sent event plumbing for streamed chat answers.

Three modes, chosen by the deployment: process-local (the default), shared
through a database lease (`shared_streams = true`), and through Redis (a
`[redis]` table), described at "Redis mode" below - the only one in which an
answer outlives the connection that started it.

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

* The SSE generator (a waitress request thread, or under ASGI a coroutine)
  performs **no ORM work**. It relays frames and emits heartbeats.

UNDER ASGI (`web_server = "uvicorn"`, `digitalbrain/asgi.py`) the stream
endpoints are served by an event loop and every other request by the same
WSGI application in a bounded thread pool. `attach(..., asynchronous=True)`
does the same setup - capacity, the session, starting the worker - and hands
back a reader that awaits instead of blocking: it polls a local session's
queue every `POLL_SECONDS`, or awaits Redis. A reader then holds no thread,
so `MAX_ASYNC_READERS` can follow answers at once where waitress allowed
`MAX_ACTIVE_STREAMS`; the answers themselves - a worker thread and a paid call
each - are bounded as before.
* The worker thread owns every database write for its message. A single writer is
  what keeps a racing stop from corrupting the row.
"""

import asyncio
import json
import logging
import os
import queue
import re
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

#: Readers an event loop may hold at once (see "UNDER ASGI" above), and how
#: often one looks at a process-local session's queue.
MAX_ASYNC_READERS = 500
POLL_SECONDS = 0.05

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
    that process's keeper sees it within `LEASE_POLL_SECONDS`. With Redis it is
    pushed onto a list that process is blocked on.
    """
    session = get_session(message_id)
    if session is not None:
        session.cancel.set()
        return True
    if resumable():
        return redis_stop(message_id)
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
    if resumable():
        from .. import broker

        return bool(broker.client().exists(owner_key(message_id)))
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
        end_local(session)


def end_local(session):
    session.cancel.set()
    close_session(session.message_id)


async def stream_frames_async(session):
    """`stream_frames` for an event loop: the same frames, awaited not blocked on.

    The queue is the worker thread's, so it is polled rather than waited on - a
    wait would need a thread, which is what this exists to avoid. A cancelled
    reader (the client went away) ends here like a vanished one does there.
    """
    sequence = 0
    quiet_since = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - session.started_at > MAX_STREAM_SECONDS:
                sequence += 1
                yield encode("error", {"message": "This answer took too long."}, sequence)
                session.cancel.set()
                return
            try:
                item = session.events.get_nowait()
            except queue.Empty:
                if now - quiet_since >= HEARTBEAT_SECONDS:
                    quiet_since = now
                    yield ": keep-alive\n\n"
                await asyncio.sleep(POLL_SECONDS)
                continue
            quiet_since = now
            if item is END_OF_STREAM:
                return
            event, data = item
            sequence += 1
            yield encode(event, data, sequence)
    finally:
        end_local(session)


# ── Redis mode ───────────────────────────────────────────────────────────────
#
# An answer's events are appended to a Redis stream as the worker produces
# them, and every reader - the request that started the answer, a browser that
# reconnected, a second tab, on any web process - reads that stream from the
# start or from the `Last-Event-ID` it last saw. So:
#
# * **A dropped connection resumes.** The browser's own EventSource reconnect
#   carries `Last-Event-ID`, and the reader picks up after it. A reader leaving
#   no longer stops the answer; Stop does, and `MAX_STREAM_SECONDS` still does.
# * **Nothing is paid for twice.** The process writing an answer holds its
#   owner key (renewed every `LEASE_POLL_SECONDS`, gone `LEASE_STALE_SECONDS`
#   after it stops). A reader that finds an owner follows; one that finds
#   events but no owner has found an answer whose process died, and closes it
#   out as interrupted - once, under the same key - rather than asking the
#   model again. Only an answer with no events at all is started.
# * **Rules unchanged.** The worker still owns every database write for its
#   message; readers touch Redis only. Events expire `SESSION_TTL_SECONDS`
#   after an answer ends: the row is the record, the stream a copy in transit.

#: Events one answer may write before it is stopped, bounding what Redis holds.
MAX_STREAM_EVENTS = 20000
#: What a reader says when the process writing an answer went away.
INTERRUPTED = "The server stopped while this answer was being written. Press Regenerate."
#: A reader holds a waitress thread for its whole life, like a local stream.
_readers = threading.BoundedSemaphore(MAX_ACTIVE_STREAMS)
#: Under ASGI it holds a coroutine, and these are its slots.
_async_readers = threading.BoundedSemaphore(MAX_ASYNC_READERS)
LAST_EVENT = re.compile(r"\d{1,20}-\d{1,20}")


def resumable():
    """Whether answers stream through Redis, and so survive a dropped connection."""
    from .. import broker

    return broker.enabled()


def replayable(message_id):
    """Whether Redis still holds this answer's events, finished or not."""
    if not resumable():
        return False
    from .. import broker

    return bool(broker.client().exists(events_key(message_id)))


def events_key(message_id):
    from .. import broker

    return broker.key("answer", message_id, "events")


def owner_key(message_id):
    from .. import broker

    return broker.key("answer", message_id, "owner")


def stop_key(message_id):
    from .. import broker

    return broker.key("answer", message_id, "stop")


class RedisSession:
    """The producing side of one answer: what `answer_worker` writes to."""

    def __init__(self, message_id, client):
        self.message_id = str(message_id)
        self.client = client
        self.cancel = threading.Event()
        self.ended = threading.Event()
        self.started_at = time.monotonic()
        self.written = 0

    def append(self, event, data=None):
        fields = {"e": event}
        if data is not None:
            fields["d"] = json.dumps(data, separators=(",", ":"))
        self.client.xadd(events_key(self.message_id), fields)

    def emit(self, event, data):
        if self.cancel.is_set() and event == "delta":
            return
        self.written += 1
        if self.written > MAX_STREAM_EVENTS:
            self.cancel.set()
            return
        try:
            self.append(event, data)
        except Exception:
            # Nobody can read this answer now; stop paying for it. The worker
            # still saves what it has.
            logger.exception("stream_publish_failed", extra={"event": "stream_publish_failed"})
            self.cancel.set()

    def finish(self):
        try:
            self.append("_end")
            self.client.expire(events_key(self.message_id), SESSION_TTL_SECONDS)
            release_owner(self.client, self.message_id)
        except Exception:
            logger.exception("stream_publish_failed", extra={"event": "stream_publish_failed"})
        finally:
            self.ended.set()
            close_session(self.message_id)

    @property
    def stopped(self):
        return self.cancel.is_set()


def release_owner(client, message_id):
    if client.get(owner_key(message_id)) == PROCESS:
        client.delete(owner_key(message_id))


def begin(message_id, client):
    """Start producing an answer here, or None when another process already is."""
    key = str(message_id)
    with _lock:
        _sweep(time.monotonic())
        if key in _sessions:
            return None
        if len(_sessions) >= MAX_ACTIVE_STREAMS:
            raise StreamCapacityError("Too many answers are streaming right now.")
        if not client.set(owner_key(key), PROCESS, nx=True, ex=LEASE_STALE_SECONDS):
            return None
        session = RedisSession(key, client)
        _sessions[key] = session
    # Written before the model is asked, so a reader can tell "started and its
    # process died" from "never started".
    session.append("_start")
    client.expire(events_key(key), SESSION_TTL_SECONDS)
    threading.Thread(
        target=keep_owner, args=(session,), name=f"stream-owner-{key}", daemon=True
    ).start()
    return session


def keep_owner(session):
    """Renew the owner key, honour Stop at once, and bound the answer's time."""
    client, key = session.client, session.message_id
    try:
        while not session.ended.is_set():
            if client.blpop([stop_key(key)], timeout=LEASE_POLL_SECONDS):
                session.cancel.set()
            if session.ended.is_set():
                return
            if client.get(owner_key(key)) != PROCESS:
                # Taken over by a reader that judged this process gone.
                session.cancel.set()
                return
            client.expire(owner_key(key), LEASE_STALE_SECONDS)
            if time.monotonic() - session.started_at > MAX_STREAM_SECONDS and not session.stopped:
                session.emit("error", {"message": "This answer took too long."})
                session.cancel.set()
    except Exception:
        # An owner key that cannot be renewed expires, and readers then treat
        # the answer as interrupted; the worker carries on and saves its row.
        logger.exception("stream_owner_failed", extra={"event": "stream_owner_failed"})


def redis_stop(message_id):
    from .. import broker

    connection = broker.client()
    if not connection.exists(owner_key(message_id)):
        return False
    pipe = connection.pipeline()
    pipe.rpush(stop_key(message_id), "1")
    pipe.expire(stop_key(message_id), LEASE_STALE_SECONDS)
    pipe.execute()
    return True


def written_text(client, message_id):
    """The answer text an interrupted stream got as far as."""
    return "".join(
        json.loads(fields.get("d", "{}")).get("t", "")
        for _, fields in client.xrange(events_key(message_id))
        if fields.get("e") == "delta"
    )


def close_interrupted(client, message_id, interrupted):
    """An answer whose process died: closed out once, never asked again."""
    key = str(message_id)
    if not client.set(owner_key(key), PROCESS, nx=True, ex=LEASE_STALE_SECONDS):
        return
    try:
        interrupted(written_text(client, key))
        session = RedisSession(key, client)
        session.append("error", {"message": INTERRUPTED})
        session.append("done", {"status": "failed"})
        session.append("_end")
        client.expire(events_key(key), SESSION_TTL_SECONDS)
    finally:
        release_owner(client, key)


def ended(client, message_id):
    last = client.xrevrange(events_key(message_id), count=1)
    return bool(last) and last[0][1].get("e") == "_end"


def attach(message_id, last_event_id, start, interrupted, asynchronous=False):
    """The frames for one answer, started here if nothing has started it.

    `start(session)` launches the worker for a new answer; `interrupted(text)`
    closes out one whose process died. Called by the view, before the response
    exists, so both may touch the database. Either is None for an answer that
    is already finished, which is then only replayed. Without Redis this is
    exactly the old path: one reader, which starts the answer and stops it by
    leaving. `asynchronous` returns an async iterator for an ASGI response.
    """
    if not resumable():
        session = open_session(message_id)
        start(session)
        if asynchronous:
            return AsyncReader(stream_frames_async(session), lambda: end_local(session))
        return stream_frames(session)
    from .. import broker

    client = broker.client()
    slots = _async_readers if asynchronous else _readers
    if not slots.acquire(blocking=False):
        raise StreamCapacityError("Too many answers are streaming right now.")
    try:
        if not client.exists(owner_key(message_id)):
            if client.exists(events_key(message_id)):
                if interrupted is not None and not ended(client, message_id):
                    close_interrupted(client, message_id, interrupted)
            elif start is not None:
                session = begin(message_id, client)
                if session is not None:
                    start(session)
    except BaseException:
        slots.release()
        raise
    cursor = last_event_id if last_event_id and LAST_EVENT.fullmatch(last_event_id) else "0-0"
    if asynchronous:
        return AsyncReader(follow_async(str(message_id), cursor), slots.release)
    return Reader(follow(client, str(message_id), cursor), slots.release)


class Reader:
    """Frames that give their reader slot back however they end.

    A generator's own `finally` never runs if it is closed before its first
    frame, so the slot is released here, by `close` - which Django calls on a
    streaming response's content - or on exhaustion, whichever comes first.
    """

    def __init__(self, frames, release):
        self.frames, self._release = frames, release

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.frames)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.frames.close()
        release, self._release = self._release, None
        if release is not None:
            release()


class AsyncReader:
    """`Reader` for an event loop.

    Django closes a streaming response's content with `close()`, which it
    looks for on the iterator it was given, and an ASGI disconnect cancels
    the reader where it awaits. Either way the slot is given back once.
    """

    def __init__(self, frames, release):
        self.frames, self._release = frames, release

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.frames.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        await self.frames.aclose()
        self.close()

    def close(self):
        release, self._release = self._release, None
        if release is not None:
            release()


def decoded(entries):
    """Frames for a batch of an answer's events: (frames, last id, ended).

    One reading of an entry for both readers, so a blocking and an awaiting
    reader cannot send different frames for the same answer.
    """
    frames, cursor = [], None
    for entry, fields in entries:
        cursor = entry
        event = fields.get("e", "")
        if event == "_end":
            return frames, cursor, True
        if event.startswith("_"):
            continue
        frames.append(encode(event, json.loads(fields.get("d", "{}")), entry))
    return frames, cursor, False


def follow(client, message_id, cursor):
    """Replay an answer's events after `cursor`, then follow it until it ends.

    A reader that leaves does not stop the answer - that is what lets a
    browser reconnect.
    """
    started = time.monotonic()
    while True:
        if time.monotonic() - started > MAX_STREAM_SECONDS + LEASE_STALE_SECONDS:
            return
        try:
            found = client.xread(
                {events_key(message_id): cursor}, count=200, block=HEARTBEAT_SECONDS * 1000
            )
        except Exception:
            # Redis went away mid-answer. Ending the response makes the browser
            # reconnect from the last event it has; the worker saves the row.
            logger.exception("stream_follow_failed", extra={"event": "stream_follow_failed"})
            return
        if not found:
            if not client.exists(owner_key(message_id)):
                # Nothing more is coming from anywhere. Ending the response
                # makes the browser reconnect, and that request closes the
                # answer out as interrupted.
                return
            yield ": keep-alive\n\n"
            continue
        frames, cursor, ended = decoded(found[0][1])
        yield from frames
        if ended:
            return


async def follow_async(message_id, cursor):
    """`follow`, awaiting Redis through this event loop's own client."""
    from .. import broker

    client = broker.async_client()
    started = time.monotonic()
    while True:
        if time.monotonic() - started > MAX_STREAM_SECONDS + LEASE_STALE_SECONDS:
            return
        try:
            found = await client.xread(
                {events_key(message_id): cursor}, count=200, block=HEARTBEAT_SECONDS * 1000
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stream_follow_failed", extra={"event": "stream_follow_failed"})
            return
        if not found:
            if not await client.exists(owner_key(message_id)):
                return
            yield ": keep-alive\n\n"
            continue
        frames, cursor, ended = decoded(found[0][1])
        for frame in frames:
            yield frame
        if ended:
            return
