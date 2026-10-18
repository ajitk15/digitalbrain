"""How many answer streams a web process can follow, and what pages cost meanwhile.

`manage.py load_streams` runs the real application - views, middleware, the
CSP, `streaming.attach` and Redis - under each web server with the options
`digitalbrain/servers.py` gives it in deployment, in a throwaway database. Only
the model is replaced: each answer is a worker that writes `EVENTS_PER_SECOND`
words a second - about what a provider's stream sends - so nothing is paid for,
and every answer lasts as long as the stage does. Readers follow the answers
the way browsers do - several to an answer when a second tab or a reconnect
follows one through Redis - while page requests go to the chat page alongside
them. They run in a process of their own (`load_clients`), so parsing every
line of every answer is not counted against the server.

What it reports per stage: how many readers were accepted, refused (503) or
never answered; how long admission took and how many received words; page
latency and failures; and the most threads the server process held, by kind.
Under waitress each reader holds a request thread, so at most
`MAX_ACTIVE_STREAMS` are accepted and the pool's other half serves pages.
Under uvicorn a reader awaits Redis on the event loop, so up to
`MAX_ASYNC_READERS` follow at once on the same threads.
"""

import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from .load_clients import ANSWER_SECONDS, Stage

SERVERS = ("waitress", "uvicorn")
#: A provider streams a few tokens an event: a 1,024-token answer over twenty
#: seconds is about this many events a second.
EVENTS_PER_SECOND = 20
ROOT = Path(__file__).resolve().parent.parent

# ── the answer, without a model ───────────────────────────────────────────────


def fake_worker(seconds):
    """An `answer_worker` that writes `EVENTS_PER_SECOND` words a second for `seconds`."""

    def worker(user_id, app_id, message_id, question, history, session, mode="ai",
               graph_version=None):
        from django.db import close_old_connections

        from .chat_views import finish_answer

        deadline, words = time.monotonic() + seconds, 0
        try:
            while time.monotonic() < deadline and not session.stopped:
                session.emit("delta", {"t": "word "})
                words += 1
                time.sleep(1 / EVENTS_PER_SECOND)
            status = "stopped" if session.stopped else "complete"
            finish_answer(message_id, status, "word " * words, [])
            session.emit("done", {"status": status})
        finally:
            session.finish()
            close_old_connections()

    return worker


# ── the process under test ────────────────────────────────────────────────────


@contextmanager
def throwaway_database():
    """A test database in a file of its own, so many threads may read it at once.

    SQLite's in-memory test database is one shared cache, which refuses a reader
    while another thread writes instead of waiting; a file waits.
    """
    from django.db import connections
    from django.test.utils import setup_databases, teardown_databases

    with tempfile.TemporaryDirectory(prefix="load-streams-") as scratch:
        settings_dict = connections["default"].settings_dict
        if settings_dict["ENGINE"].endswith("sqlite3"):
            settings_dict.setdefault("TEST", {})["NAME"] = str(Path(scratch) / "load.sqlite3")
        old = setup_databases(verbosity=0, interactive=False)
        try:
            yield
        finally:
            connections.close_all()
            teardown_databases(old, verbosity=0)


@contextmanager
def redis(address):
    """Redis for the readers: `host:port` of a throwaway server, or "fake".

    A throwaway server has no password, whatever the secret directory holds.
    """
    from django.test.utils import override_settings

    from . import broker

    with ExitStack() as stack:
        if address == "fake":
            import fakeredis

            server = fakeredis.FakeServer()
            broker.use(fakeredis.FakeRedis(server=server, decode_responses=True))
            broker.use_async(
                lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
            )
            stack.enter_context(override_settings(REDIS={"host": "fake"}))
        else:
            host, _, port = address.rpartition(":")
            stack.enter_context(override_settings(REDIS={"host": host, "port": int(port)}))
            stack.enter_context(patch("platform_core.broker.password", return_value=None))
            broker.use(None)
            broker.use_async(None)
        stack.callback(broker.use, None)
        stack.callback(broker.use_async, None)
        yield


@contextmanager
def running(server):
    """`server` on a free local port, as deployment runs it. Yields the port."""
    from digitalbrain import servers

    if server == "waitress":
        from waitress.server import create_server

        from digitalbrain.wsgi import application

        instance = create_server(
            application, host="127.0.0.1", port=0, **servers.waitress_options()
        )
        thread = threading.Thread(target=instance.run, name="load-waitress", daemon=True)
        thread.start()
        try:
            yield instance.effective_port
        finally:
            instance.task_dispatcher.shutdown(timeout=5)
            instance.close()
        return
    import uvicorn

    instance = uvicorn.Server(uvicorn.Config(
        servers.uvicorn_application(), host="127.0.0.1", port=0, **servers.uvicorn_options()
    ))
    thread = threading.Thread(target=instance.run, name="load-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not instance.started:
        raise RuntimeError("uvicorn did not start")
    try:
        yield instance.servers[0].sockets[0].getsockname()[1]
    finally:
        instance.should_exit = True
        thread.join(15)


class ErrorCount(logging.Handler):
    """Errors logged while a stage runs, leaving out the refusals it counts itself."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0
        self.first = []

    def emit(self, record):
        if getattr(record, "status_code", None) == 503:
            return
        self.count += 1
        if len(self.first) < 5:
            failure = record.exc_info[1] if record.exc_info else None
            self.first.append(" ".join(filter(None, (
                record.name, getattr(record, "event", "") or record.getMessage()[:80],
                type(failure).__name__ if failure else "",
            ))))


@contextmanager
def quiet():
    """Count the errors every logger reports, and print nothing a request logs."""
    counted = ErrorCount()
    loggers = [logging.getLogger()] + [
        found for found in logging.root.manager.loggerDict.values()
        if isinstance(found, logging.Logger) and found.handlers
    ]
    saved = [(found, found.handlers[:]) for found in loggers]
    for found in loggers:
        found.handlers[:] = [counted]
    try:
        yield counted
    finally:
        for found, handlers in saved:
            found.handlers[:] = handlers


def workspace():
    """One application, its owner and a signed-in session cookie."""
    from django.conf import settings
    from django.test import Client

    from .models import (
        Application,
        ApplicationGrant,
        Organization,
        OrganizationMember,
        Portfolio,
        Product,
        User,
    )

    org = Organization.objects.create(name="Load")
    product = Product.objects.create(
        name="Load", portfolio=Portfolio.objects.create(name="Load", organization=org)
    )
    app = Application.objects.create(name="Load streams", product=product)
    user = User.objects.create_user("load-owner", email="owner@load.example")
    OrganizationMember.objects.create(organization=org, user=user)
    ApplicationGrant.objects.create(application=app, user=user, role="owner")
    client = Client()
    client.force_login(user)
    cookie = f"{settings.SESSION_COOKIE_NAME}={client.cookies[settings.SESSION_COOKIE_NAME].value}"
    return app, user, cookie


class Threads:
    """The most threads this process held at once while a stage ran, and their kinds."""

    def __init__(self):
        self.peak, self.kinds = 0, {}
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.watch, name="load-threads", daemon=True)

    def watch(self):
        while not self.done.wait(0.1):
            alive = [found for found in threading.enumerate() if found is not self.thread]
            if len(alive) > self.peak:
                self.peak = len(alive)
                kinds = {}
                for found in alive:
                    kind = re.sub(r"[-_ ]?[0-9a-f-]{6,}|[-_ ]?\d+", "", found.name) or "?"
                    kinds[kind] = kinds.get(kind, 0) + 1
                self.kinds = dict(sorted(kinds.items(), key=lambda item: -item[1])[:4])

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(5)


# ── running it ────────────────────────────────────────────────────────────────


def wait_for_answers(seconds):
    from .agent_runtime import streaming

    deadline = time.monotonic() + seconds
    while streaming._sessions and time.monotonic() < deadline:
        time.sleep(0.1)


def clients(spec):
    """The readers and page requests for one stage, in a process of their own."""
    done = subprocess.run(
        [sys.executable, "-m", "platform_core.load_clients"],
        input=json.dumps(spec), capture_output=True, text=True, cwd=ROOT,
        timeout=spec["seconds"] + ANSWER_SECONDS * 3, check=False,
    )
    if done.returncode:
        raise RuntimeError(f"The load clients failed: {done.stderr.strip()[-500:]}")
    return Stage(**json.loads(done.stdout))


def stage(server, port, app, user, cookie, readers, answers, seconds):
    from django.urls import reverse

    from .chat_views import start_answer

    answers = min(answers, readers)
    streams = []
    for number in range(answers):
        _, message = start_answer(app, user, None, f"Load question {number}", "ai")
        streams.append(reverse("chat-stream", args=[app.pk, message.pk]))
    spec = {"server": server, "readers": readers, "port": port, "cookie": cookie,
            "streams": streams, "page": reverse("chat", args=[app.pk]), "seconds": seconds}
    with quiet() as errors, Threads() as threads:
        result = clients(spec)
        # Answers outlast the stage, so the next one starts with none left.
        wait_for_answers(10)
    result.errors_logged, result.first_errors = errors.count, errors.first
    result.peak_threads, result.thread_kinds = threads.peak, threads.kinds
    return result


def run(readers=(12, 100, 200), answers=12, seconds=30, servers=SERVERS, redis_at="fake",
        report=print):
    """Each stage under each server. Returns the stages' summaries."""
    found = []
    with ExitStack() as stack:
        stack.enter_context(throwaway_database())
        stack.enter_context(redis(redis_at))
        stack.enter_context(patch("platform_core.chat_views.answer_worker",
                                  fake_worker(seconds + 3)))
        app, user, cookie = workspace()
        for server in servers:
            with running(server) as port:
                for count in readers:
                    summary = stage(server, port, app, user, cookie, count, answers,
                                    seconds).summary()
                    report(summary)
                    found.append(summary)
    return found
