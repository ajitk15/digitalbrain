"""Server-sent event plumbing for streamed chat answers.

PROCESS-LOCAL BY DESIGN. The session registry is module state, so a stop request
only reaches a stream started by the same process. `scripts/serve.py` runs one
waitress process, which is what makes this correct today. Running multiple worker
processes would require a shared cancellation channel (a polled database column
or an external broker) before `stop` could be trusted.

Division of labour, which the rest of the chat code depends on:

* The SSE generator (a waitress request thread) performs **no ORM work**. It
  relays frames and emits heartbeats.
* The worker thread owns every database write for its message. A single writer is
  what keeps a racing stop from corrupting the row.
"""

import json
import queue
import threading
import time
from dataclasses import dataclass, field

HEARTBEAT_SECONDS = 10
MAX_STREAM_SECONDS = 180
MAX_QUEUED_EVENTS = 1000
MAX_ACTIVE_STREAMS = 16
SESSION_TTL_SECONDS = 600

#: Pushed by the worker to mark the end of the event stream.
END_OF_STREAM = object()


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
        key
        for key, session in _sessions.items()
        if now - session.started_at > SESSION_TTL_SECONDS
    ]
    for key in stale:
        _sessions[key].cancel.set()
        del _sessions[key]


def open_session(message_id):
    """Register a stream, refusing to start one when the process is saturated."""
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
        return session


def get_session(message_id):
    with _lock:
        return _sessions.get(str(message_id))


def close_session(message_id):
    with _lock:
        _sessions.pop(str(message_id), None)


def request_stop(message_id):
    """Signal a running stream to stop. True when a live stream was signalled."""
    session = get_session(message_id)
    if session is None:
        return False
    session.cancel.set()
    return True


def active_streams():
    with _lock:
        return len(_sessions)


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
