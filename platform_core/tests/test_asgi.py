"""Streams on an event loop (`web_server = "uvicorn"`, `digitalbrain/asgi.py`).

Three things are held here. The awaiting readers send exactly the frames the
blocking ones send, from a local session and from Redis, and give their place
back however they end. The router does at the edge what waitress did - trusts
forwarding headers only from the named proxy and refuses an oversized or
unstated body - and sends only the two stream routes to the event loop. And,
over a real socket, streams held open do not take the threads pages are
served from, which is the reason any of this exists.
"""

import asyncio
import http.client
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from digitalbrain.asgi import MAX_BODY, Router, Streams, is_stream
from digitalbrain.configuration import load_config, web_server
from platform_core import broker
from platform_core.agent_runtime import streaming

from . import test_chat_stream
from .test_redis_streams import REAL, RedisCase, frames_of

STREAM = f"/applications/{uuid.uuid4()}/chat/messages/{uuid.uuid4()}/stream/"


def run(coroutine):
    return asyncio.run(coroutine)


async def read_all(frames):
    return [frame async for frame in frames]


class LocalAsyncReaderTests(SimpleTestCase):
    def tearDown(self):
        for key in list(streaming._sessions):
            streaming.close_session(key)

    def produce(self, session, *texts):
        for text in texts:
            session.emit("delta", {"t": text})
        session.emit("done", {"status": "complete"})
        session.finish()

    def attach(self, message_id, started):
        return streaming.attach(message_id, None, started.append, None, asynchronous=True)

    def test_an_awaiting_reader_sends_the_frames_a_blocking_one_sends(self):
        blocking = streaming.open_session("blocking")
        self.produce(blocking, "Thirty ", "days.")
        expected = list(streaming.stream_frames(blocking))
        started = []
        frames = self.attach("awaiting", started)
        self.produce(started[0], "Thirty ", "days.")
        self.assertEqual(run(read_all(frames)), expected)
        self.assertIsNone(streaming.get_session("awaiting"), "a finished reader unregisters")

    def test_an_idle_answer_gets_a_heartbeat_not_a_closed_stream(self):
        started = []

        async def first():
            frames = self.attach("idle", started)
            try:
                return await frames.__anext__()
            finally:
                await frames.aclose()

        with patch.multiple(streaming, HEARTBEAT_SECONDS=0.05, POLL_SECONDS=0.01):
            self.assertEqual(run(first()), ": keep-alive\n\n")

    def test_a_client_that_goes_away_stops_the_answer_and_frees_its_place(self):
        started = []

        async def abandon():
            frames = self.attach("gone", started)
            task = asyncio.ensure_future(frames.__anext__())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(abandon())
        self.assertTrue(started[0].cancel.is_set())
        self.assertIsNone(streaming.get_session("gone"))

    def test_a_reader_never_read_is_still_released_when_django_closes_it(self):
        started = []
        frames = self.attach("unread", started)
        frames.close()
        frames.close()
        self.assertTrue(started[0].cancel.is_set())
        self.assertIsNone(streaming.get_session("unread"))

    def test_an_answer_that_runs_too_long_ends_with_an_error_frame(self):
        started = []
        with patch.object(streaming, "MAX_STREAM_SECONDS", 0):
            frames = self.attach("slow", started)
            read = run(read_all(frames))
        self.assertEqual(len(read), 1)
        self.assertIn("event: error", read[0])
        self.assertTrue(started[0].cancel.is_set())


class AsyncRedisCase(RedisCase):
    """The Redis tests' setup, with an asyncio client onto the same data.

    Inheriting `RedisCase` runs every blocking Redis test again with the
    asyncio client in place, which is cheap and shows it changes nothing there.
    """

    def setUp(self):
        super().setUp()
        broker.use_async(self.make_async_client)
        self.addCleanup(broker.use_async, None)

    def make_client(self):
        import fakeredis

        self.server = fakeredis.FakeServer()
        return fakeredis.FakeRedis(server=self.server, decode_responses=True)

    def make_async_client(self):
        import fakeredis

        return fakeredis.FakeAsyncRedis(server=self.server, decode_responses=True)

    def attach_async(self, last=None):
        started, interrupted = [], []
        frames = streaming.attach(
            self.answer, last, started.append, interrupted.append, asynchronous=True
        )
        self.addCleanup(frames.close)
        return frames, started

    def test_an_awaiting_follower_reads_what_a_blocking_one_reads(self):
        frames, started = self.attach_async()
        self.produce(started[0], "Thirty ", "days.")
        blocking, _, _ = self.attach()
        self.assertEqual(run(read_all(frames)), list(blocking))

    def test_an_awaiting_reconnect_resumes_after_the_last_event_it_saw(self):
        _, started, _ = self.attach()
        self.produce(started[0], "one ", "two ", "three")
        first = next(iter(self.attach()[0]))
        last_seen = first.split("\n")[0].removeprefix("id: ")
        frames, again = self.attach_async(last=last_seen)
        self.assertEqual(again, [], "a reconnect must not ask the model again")
        texts = [frame for frame in frames_of(run(read_all(frames))) if "event: delta" in frame]
        self.assertEqual(len(texts), 2)
        self.assertIn('"t":"two "', texts[0])

    def test_awaiting_readers_have_places_of_their_own(self):
        _, started, _ = self.attach()
        held = []
        with (
            patch.object(streaming, "_readers", threading.BoundedSemaphore(1)),
            patch.object(streaming, "_async_readers", threading.BoundedSemaphore(2)),
        ):
            held.append(streaming.attach(self.answer, None, None, None))
            held.append(streaming.attach(self.answer, None, None, None, asynchronous=True))
            held.append(streaming.attach(self.answer, None, None, None, asynchronous=True))
            with self.assertRaises(streaming.StreamCapacityError):
                streaming.attach(self.answer, None, None, None, asynchronous=True)
            held.pop().close()
            held.append(streaming.attach(self.answer, None, None, None, asynchronous=True))
            for frames in held:
                frames.close()


class FakeAsyncRedisTests(AsyncRedisCase, SimpleTestCase):
    pass


@skipUnless(REAL, "Set DIGITAL_BRAIN_TEST_REDIS=host:port to run these against a real Redis.")
class RealAsyncRedisTests(AsyncRedisCase, SimpleTestCase):
    def make_client(self):
        from .test_redis_streams import real_client

        return real_client()

    def make_async_client(self):
        import redis.asyncio

        host, port = REAL.rsplit(":", 1)
        return redis.asyncio.Redis(host=host, port=int(port), decode_responses=True)


class Recorder:
    """An ASGI app that answers 200 and keeps the scope it was given."""

    def __init__(self):
        self.scopes = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def scope(path="/", headers=(), client=("203.0.113.7", 5000)):
    return {
        "type": "http",
        "path": path,
        "method": "GET",
        "scheme": "http",
        "client": client,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }


class RouterTests(SimpleTestCase):
    def call(self, request, trusted=""):
        streams, pages, sent = Recorder(), Recorder(), []
        router = Router(streams, pages, trusted_proxy=trusted)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        run(router(request, receive, send))
        return streams, pages, sent

    def headers(self, recorder):
        return dict(recorder.scopes[0]["headers"])

    def test_only_the_two_stream_routes_go_to_the_event_loop(self):
        api = reverse("api-chat-stream", args=["carepath", uuid.uuid4()])
        self.assertTrue(is_stream(STREAM))
        self.assertTrue(is_stream(api))
        self.assertFalse(is_stream("/"))
        self.assertFalse(is_stream("/no/such/page/"))
        streams, pages, _ = self.call(scope(STREAM))
        self.assertEqual((len(streams.scopes), len(pages.scopes)), (1, 0))
        streams, pages, _ = self.call(scope("/"))
        self.assertEqual((len(streams.scopes), len(pages.scopes)), (0, 1))

    def test_forwarding_headers_from_anyone_else_are_removed(self):
        forged = [
            ("X-Forwarded-For", "10.0.0.1"),
            ("X-Forwarded-Proto", "https"),
            ("X-Forwarded-Host", "evil.example"),
            ("Forwarded", "for=10.0.0.1"),
            ("Host", "brain.example"),
        ]
        _, pages, _ = self.call(scope(headers=forged), trusted="198.51.100.1")
        seen = pages.scopes[0]
        self.assertEqual(self.headers(pages), {b"host": b"brain.example"})
        self.assertEqual(seen["client"][0], "203.0.113.7")
        self.assertEqual(seen["scheme"], "http")
        _, pages, _ = self.call(scope(headers=forged))
        self.assertEqual(self.headers(pages), {b"host": b"brain.example"})

    def test_the_named_proxy_is_believed_for_its_own_hop_only(self):
        sent = [
            ("X-Forwarded-For", "10.0.0.1, 192.0.2.44"),
            ("X-Forwarded-Proto", "https"),
            ("X-Forwarded-Host", "brain.example"),
            ("X-Forwarded-Port", "443"),
            ("Host", "app:8000"),
        ]
        _, pages, _ = self.call(scope(headers=sent), trusted="203.0.113.7")
        seen = pages.scopes[0]
        self.assertEqual(seen["client"][0], "192.0.2.44")
        self.assertEqual(seen["scheme"], "https")
        headers = self.headers(pages)
        self.assertEqual(headers[b"host"], b"brain.example")
        self.assertNotIn(b"x-forwarded-port", headers)

    def test_a_body_over_the_limit_or_of_no_stated_length_is_refused(self):
        big = scope(headers=[("Content-Length", str(MAX_BODY + 1))])
        streams, pages, sent = self.call(big)
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual((streams.scopes, pages.scopes), ([], []))
        _, _, sent = self.call(scope(headers=[("Content-Length", "12, 99")]))
        self.assertEqual(sent[0]["status"], 413)
        _, pages, sent = self.call(scope(headers=[("Transfer-Encoding", "chunked")]))
        self.assertEqual(sent[0]["status"], 411)
        self.assertEqual(pages.scopes, [])
        _, pages, _ = self.call(scope(headers=[("Content-Length", str(MAX_BODY))]))
        self.assertEqual(len(pages.scopes), 1)

    def test_lifespan_is_answered_and_websockets_are_closed(self):
        router, sent = Router(Recorder(), Recorder()), []
        messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        run(router({"type": "lifespan"}, receive, send))
        self.assertEqual(
            [m["type"] for m in sent],
            ["lifespan.startup.complete", "lifespan.shutdown.complete"],
        )
        sent.clear()
        run(router({"type": "websocket", "path": "/"}, receive, send))
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])


class WebServerSettingTests(SimpleTestCase):
    BASE = 'mode = "development"\nhosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(self.BASE + body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_uvicorn_where_redis_is_set_and_waitress_otherwise(self):
        self.assertEqual(web_server(self.load("")), "waitress")
        self.assertEqual(web_server(self.load('[redis]\nhost = "redis"\n')), "uvicorn")

    def test_a_named_server_wins_either_way(self):
        self.assertEqual(web_server(self.load('web_server = "uvicorn"\n')), "uvicorn")
        self.assertEqual(
            web_server(self.load('web_server = "waitress"\n[redis]\nhost = "redis"\n')),
            "waitress",
        )
        with self.assertRaises(ImproperlyConfigured):
            self.load('web_server = "gunicorn"\n')


class StreamLaneTests(SimpleTestCase):
    """Stream requests' synchronous work shares a fixed set of threads.

    Django's own handler gave each request a thread for as long as its
    response lasted, so 400 open streams held 400 idle threads.
    """

    def test_however_many_streams_are_open_they_use_the_lanes_alone(self):
        from asgiref.sync import sync_to_async

        seen = []

        async def open_many():
            release = asyncio.Event()

            class Handler:
                async def handle(self, scope, receive, send):
                    await sync_to_async(lambda: seen.append(threading.current_thread()))()
                    await release.wait()

            streams = Streams(Handler(), threads=2)
            requests = [asyncio.ensure_future(streams({"type": "http"}, None, None))
                        for _ in range(6)]
            while len(seen) < 6:
                await asyncio.sleep(0.01)
            release.set()
            await asyncio.gather(*requests)

        asyncio.run(asyncio.wait_for(open_many(), 10))
        self.assertTrue(all(thread.name.startswith("stream-view") for thread in seen), seen)
        self.assertEqual(len({thread.ident for thread in seen}), 2)

    def test_the_lanes_are_asgirefs_thread_sensitive_contexts(self):
        """`Streams` relies on these two; an asgiref that renames them must fail here."""
        from asgiref.sync import SyncToAsync

        self.assertTrue(hasattr(SyncToAsync, "thread_sensitive_context"))
        self.assertTrue(hasattr(SyncToAsync, "context_to_thread_executor"))


class AsyncPoolTests(SimpleTestCase):
    def test_every_reader_the_cap_allows_has_a_connection(self):
        client = broker.connect({"host": "redis"}, asynchronous=True)
        self.assertEqual(client.connection_pool.max_connections, broker.ASYNC_CONNECTIONS)
        self.assertGreater(broker.ASYNC_CONNECTIONS, streaming.MAX_ASYNC_READERS)


class AsgiStreamViewTests(TestCase):
    """The stream view, reached through Django's ASGI handler."""

    setUp = test_chat_stream.ChatStreamViewTests.setUp
    start = test_chat_stream.ChatStreamViewTests.start

    def test_an_asgi_request_is_answered_by_an_awaiting_reader(self):
        payload = self.start().json()

        def fake_worker(user_id, app_id, message_id, question, history, session, *rest):
            session.emit("delta", {"t": "Thirty "})
            session.emit("done", {"status": "complete"})
            session.finish()

        async def read():
            response = await self.async_client.get(payload["stream"])
            self.assertTrue(response.is_async, "an ASGI stream must not hold a thread")
            self.assertEqual(response["Content-Type"], "text/event-stream")
            return b"".join([part async for part in response.streaming_content]).decode()

        self.async_client.cookies = self.client.cookies
        with patch("platform_core.chat_views.answer_worker", fake_worker):
            body = async_to_sync(read)()
        self.assertIn('data: {"t":"Thirty "}', body)
        self.assertIn("event: done", body)

    def test_a_wsgi_request_still_gets_the_blocking_reader(self):
        payload = self.start().json()

        def fake_worker(user_id, app_id, message_id, question, history, session, *rest):
            session.emit("done", {"status": "complete"})
            session.finish()

        with patch("platform_core.chat_views.answer_worker", fake_worker):
            response = self.client.get(payload["stream"])
            self.assertFalse(response.is_async)
            b"".join(response.streaming_content)


class Held:
    """A stream route that sends its headers and then waits to be let go."""

    def __init__(self):
        self.release = threading.Event()
        self.open = 0

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b": open\n\n", "more_body": True})
        self.open += 1
        while not self.release.is_set():
            await asyncio.sleep(0.02)
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def page(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"page"]


class UvicornTests(SimpleTestCase):
    """A real server on a real socket: streams held open, pages still served."""

    def test_open_streams_do_not_take_the_threads_pages_are_served_from(self):
        import uvicorn
        from a2wsgi import WSGIMiddleware

        held, workers = Held(), 2
        router = Router(held, WSGIMiddleware(page, workers=workers))
        server = uvicorn.Server(uvicorn.Config(
            router, host="127.0.0.1", port=0, lifespan="off", ws="none",
            log_config=None, access_log=False,
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, server, "should_exit", True)
        self.addCleanup(held.release.set)
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(server.started)
        port = server.servers[0].sockets[0].getsockname()[1]

        streams = []
        for _ in range(workers * 4):
            connection = socket.create_connection(("127.0.0.1", port), timeout=5)
            connection.sendall(f"GET {STREAM} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            streams.append(connection)
        self.addCleanup(lambda: [connection.close() for connection in streams])
        while held.open < len(streams) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(held.open, len(streams))

        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        client.request("GET", "/")
        answer = client.getresponse()
        self.assertEqual((answer.status, answer.read()), (200, b"page"))
        client.close()
