"""Answers streamed through Redis: replayed, resumed, stopped and never asked twice.

Each test runs against fakeredis, and again against a real Redis when
`DIGITAL_BRAIN_TEST_REDIS` names one (`host:port`, as CI's service does) - the
second because blocking reads and expiry are where a fake is likeliest to be
kind. It skips and says why when none is named.
"""

import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import request_finished
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from digitalbrain.configuration import load_config
from platform_core import api_auth, broker
from platform_core.agent_runtime import streaming
from platform_core.models import ChatMessage

from . import test_documents

REAL = os.environ.get("DIGITAL_BRAIN_TEST_REDIS", "")
ANSWER = "7d2c1e44-0b5a-4f7e-9c33-5a1d2e3f4b6c"


def fake_client():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def real_client():
    import redis

    host, port = REAL.rsplit(":", 1)
    return redis.Redis(host=host, port=int(port), decode_responses=True)


def frames_of(chunks):
    """Each frame in what a reader sent: several words read at once leave as one chunk."""
    return [frame + "\n\n" for chunk in chunks for frame in chunk.split("\n\n") if frame]


class RedisCase:
    """Every test here, against whichever client `make_client` returns."""

    def make_client(self):
        return fake_client()

    def setUp(self):
        super().setUp()
        # Its own answer per test: a keeper from the test before may still be
        # blocked on that answer's Stop list, and Redis serves the oldest waiter.
        self.answer = str(uuid.uuid4())
        self.redis = self.make_client()
        for key in self.redis.scan_iter(f"{broker.PREFIX}*"):
            self.redis.delete(key)
        broker.use(self.redis)
        self.addCleanup(broker.use, None)
        redis_on = override_settings(REDIS={"host": "redis"})
        redis_on.enable()
        self.addCleanup(redis_on.disable)
        quick = patch.multiple(streaming, LEASE_POLL_SECONDS=0.05, HEARTBEAT_SECONDS=1)
        quick.start()
        self.addCleanup(quick.stop)
        self.addCleanup(self.close_sessions)

    def close_sessions(self):
        for key, session in list(streaming._sessions.items()):
            session.cancel.set()
            if isinstance(session, streaming.RedisSession):
                session.ended.set()
            streaming.close_session(key)

    def attach(self, last=None, message_id=None):
        started, interrupted = [], []
        frames = streaming.attach(
            message_id or self.answer, last, started.append, interrupted.append
        )
        self.addCleanup(frames.close)
        return frames, started, interrupted

    def produce(self, session, *texts, end=True):
        for text in texts:
            session.emit("delta", {"t": text})
        if end:
            session.emit("done", {"status": "complete"})
            session.finish()

    def test_a_new_answer_is_started_once_and_read_from_its_first_event(self):
        frames, started, _ = self.attach()
        self.assertEqual(len(started), 1)
        self.produce(started[0], "Thirty ", "days.")
        read = frames_of(frames)
        self.assertEqual([frame.split("\n")[1] for frame in read], ["event: delta"] * 2 + [
            "event: done"
        ])
        self.assertIn('"t":"Thirty "', read[0])
        self.assertTrue(read[0].startswith("id: "))

    def test_a_reconnect_resumes_after_the_last_event_it_saw(self):
        frames, started, _ = self.attach()
        session = started[0]
        self.produce(session, "one ", "two ", end=False)
        first = next(frames)
        last_seen = first.split("\n")[0].removeprefix("id: ")
        frames.close()
        self.assertFalse(session.stopped, "a reader leaving must not stop the answer")
        self.produce(session, "three")
        again, started_again, _ = self.attach(last=last_seen)
        self.assertEqual(started_again, [], "a reconnect must not ask the model again")
        texts = [frame for frame in frames_of(again) if "event: delta" in frame]
        self.assertEqual(len(texts), 2)
        self.assertIn('"t":"two "', texts[0])
        self.assertIn('"t":"three"', texts[1])

    def test_a_malformed_last_event_id_replays_from_the_start(self):
        _, started, _ = self.attach()
        self.produce(started[0], "a", "b")
        frames, _, _ = self.attach(last="999999999999999999999-0; DROP")
        self.assertEqual(len([frame for frame in frames_of(frames) if "event: delta" in frame]), 2)

    def test_a_second_reader_follows_rather_than_starting_another(self):
        _, started, _ = self.attach()
        _, second, _ = self.attach()
        self.assertEqual((len(started), second), (1, []))
        self.assertTrue(streaming.is_live(self.answer))

    def test_stop_reaches_the_writing_process_through_redis(self):
        _, started, _ = self.attach()
        session = started[0]
        # As another process sees it: no local session to signal.
        with patch.object(streaming, "get_session", return_value=None):
            self.assertTrue(streaming.request_stop(self.answer))
        deadline = time.monotonic() + 5
        while not session.stopped and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(session.stopped)
        self.produce(session, "late")
        self.assertFalse(streaming.request_stop(self.answer), "a finished answer is not live")

    def test_an_answer_whose_process_died_is_closed_out_once_and_not_asked_again(self):
        _, started, _ = self.attach()
        self.produce(started[0], "Refunds expire ", end=False)
        # The writing process dies: its keeper stops and its owner key lapses.
        started[0].ended.set()
        self.redis.delete(streaming.owner_key(self.answer))
        frames, again, interrupted = self.attach()
        self.assertEqual(again, [])
        self.assertEqual(interrupted, ["Refunds expire "])
        read = list(frames)
        self.assertTrue(any(streaming.INTERRUPTED in frame for frame in read))
        self.assertIn('"status":"failed"', read[-1])
        _, third, interrupted_again = self.attach()
        self.assertEqual((third, interrupted_again), ([], []))

    def test_a_reader_stops_when_nothing_owns_the_answer_any_more(self):
        _, started, _ = self.attach()
        self.produce(started[0], "x", end=False)
        frames, _, _ = self.attach()
        next(frames)
        started[0].ended.set()
        self.redis.delete(streaming.owner_key(self.answer))
        # Ends the response, so the browser reconnects and closes it out.
        self.assertEqual(list(frames), [])

    def test_readers_are_bounded_and_a_closed_one_gives_its_place_back(self):
        _, started, _ = self.attach()
        held = []
        with patch.object(streaming, "_readers", threading.BoundedSemaphore(2)):
            held.append(streaming.attach(self.answer, None, started.append, list.append))
            held.append(streaming.attach(self.answer, None, started.append, list.append))
            with self.assertRaises(streaming.StreamCapacityError):
                streaming.attach(self.answer, None, started.append, list.append)
            held.pop().close()
            held.append(streaming.attach(self.answer, None, started.append, list.append))
            for frames in held:
                frames.close()

    def test_events_expire_once_the_answer_ends(self):
        _, started, _ = self.attach()
        self.produce(started[0], "done")
        ttl = self.redis.ttl(streaming.events_key(self.answer))
        self.assertTrue(0 < ttl <= streaming.SESSION_TTL_SECONDS)
        self.assertFalse(self.redis.exists(streaming.owner_key(self.answer)))

    def test_an_answer_that_runs_too_long_is_stopped_by_its_own_process(self):
        with patch.object(streaming, "MAX_STREAM_SECONDS", 0):
            _, started, _ = self.attach()
            session = started[0]
            deadline = time.monotonic() + 5
            while not session.stopped and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertTrue(session.stopped)
        self.produce(session)
        self.assertTrue(
            any(
                fields.get("e") == "error"
                for _, fields in self.redis.xrange(streaming.events_key(self.answer))
            )
        )

    def test_the_api_rate_limit_is_one_counter_for_every_process(self):
        token = type("Token", (), {"pk": 42})()
        api_auth.reset_throttle()
        with patch.object(api_auth, "RATE_LIMIT", 3):
            for _ in range(3):
                api_auth.throttle(token)
            # A process that has counted nothing itself still sees the limit.
            api_auth.reset_throttle()
            with self.assertRaises(api_auth.ApiError) as limited:
                api_auth.throttle(token)
        self.assertEqual(limited.exception.status, 429)


class FakeRedisStreamTests(RedisCase, SimpleTestCase):
    pass


@skipUnless(REAL, "Set DIGITAL_BRAIN_TEST_REDIS=host:port to run these against a real Redis.")
class RealRedisStreamTests(RedisCase, SimpleTestCase):
    def make_client(self):
        return real_client()


class RedisRequiredTests(SimpleTestCase):
    def test_a_run_that_requires_a_real_redis_has_one(self):
        """CI sets DIGITAL_BRAIN_REQUIRE_REDIS, so the real-Redis run cannot skip
        unnoticed, as the browser tests cannot."""
        if os.environ.get("DIGITAL_BRAIN_REQUIRE_REDIS") == "1":
            self.assertTrue(REAL, "DIGITAL_BRAIN_TEST_REDIS is required for this run.")
            real_client().ping()


class RedisFailureTests(SimpleTestCase):
    def test_a_rate_limit_redis_cannot_count_refuses_rather_than_admits(self):
        token = type("Token", (), {"pk": 7})()
        with (
            override_settings(REDIS={"host": "redis"}),
            patch.object(broker, "count", side_effect=ConnectionError),
            self.assertLogs("platform_core.api_auth", "ERROR"),
            self.assertRaises(api_auth.ApiError) as refused,
        ):
            api_auth.throttle(token)
        self.assertEqual(refused.exception.status, 503)

    def test_without_redis_nothing_uses_it(self):
        with override_settings(REDIS={}), patch.object(broker, "client") as client:
            self.assertFalse(streaming.resumable())
            streaming.is_live(ANSWER)
            streaming.request_stop(ANSWER)
        client.assert_not_called()


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    REDIS={"host": "redis"},
)
class RedisChatViewTests(TestCase):
    """The browser's own stream view, with Redis: a reconnect follows, never re-asks."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        quiet = patch("django.db.close_old_connections")
        quiet.start()
        self.addCleanup(quiet.stop)
        broker.use(fake_client())
        self.addCleanup(broker.use, None)
        from platform_core.models import AIConfiguration

        AIConfiguration.objects.create(
            application=self.app, purpose="chat", provider="openai", model="gpt-5.6-luna",
            enabled=True, input_rate=1, output_rate=2, configured_by=self.owner,
        )
        self.client.force_login(self.owner)
        self.addCleanup(RedisCase.close_sessions, self)

    def test_a_reconnect_with_last_event_id_follows_the_running_answer(self):
        posted = self.client.post(
            reverse("chat", args=[self.app.pk]),
            {"question": "Refund deadline?", "mode": "ai"},
            headers={"X-Digital-Brain-Stream": "1"},
        ).json()
        self.assertTrue(posted["resumes"])
        sessions = []

        def worker(user_id, app_id, message_id, question, history, session, *rest):
            sessions.append(session)

        with patch("platform_core.chat_views.answer_worker", side_effect=worker):
            first = self.client.get(posted["stream"])
            self.assertEqual(first.status_code, 200)
            deadline = time.monotonic() + 5
            while not sessions and time.monotonic() < deadline:
                time.sleep(0.02)
            session = sessions[0]
            session.emit("delta", {"t": "Thirty "})
            frames = iter(first.streaming_content)
            seen = next(frames).decode()
            # Dropped mid-answer, as a browser that loses its connection is.
            # Closing a response sends request_finished, whose handler closes
            # the connection - inside a TestCase, PostgreSQL really closes it.
            with patch.object(request_finished, "send"):
                first.close()
            session.emit("delta", {"t": "days."})
            session.emit("done", {"status": "complete"})
            ChatMessage.objects.filter(pk=posted["message"]).update(
                status="streaming"
            )
            session.finish()
            again = self.client.get(
                posted["stream"], headers={"Last-Event-ID": seen.split("\n")[0][4:]}
            )
            body = b"".join(again.streaming_content).decode()
        self.assertEqual(len(sessions), 1, "the reconnect must not start a second answer")
        self.assertIn('"t":"days."', body)
        self.assertNotIn('"t":"Thirty "', body)

    def test_a_finished_answer_is_replayed_and_never_started_again(self):
        posted = self.client.post(
            reverse("chat", args=[self.app.pk]),
            {"question": "Refund deadline?", "mode": "ai"},
            headers={"X-Digital-Brain-Stream": "1"},
        ).json()
        session = streaming.begin(posted["message"], broker.client())
        session.emit("delta", {"t": "Thirty days."})
        session.emit("done", {"status": "complete"})
        session.finish()
        ChatMessage.objects.filter(pk=posted["message"]).update(
            status="complete", body="Thirty days."
        )
        with patch("platform_core.chat_views.answer_worker") as worker:
            late = self.client.get(posted["stream"])
            self.assertEqual(late.status_code, 200)
            self.assertIn('"t":"Thirty days."', b"".join(late.streaming_content).decode())
            broker.client().delete(streaming.events_key(posted["message"]))
            self.assertEqual(self.client.get(posted["stream"]).status_code, 404)
        worker.assert_not_called()

    def test_the_page_offers_to_follow_an_answer_another_process_is_writing(self):
        posted = self.client.post(
            reverse("chat", args=[self.app.pk]),
            {"question": "Refund deadline?", "mode": "ai"},
            headers={"X-Digital-Brain-Stream": "1"},
        ).json()
        broker.client().set(streaming.owner_key(posted["message"]), "elsewhere", ex=30)
        page = self.client.get(
            reverse("chat", args=[self.app.pk]), {"conversation": posted["conversation"]}
        )
        self.assertContains(page, "data-resumes")
        self.assertContains(page, f'data-resume="{posted["stream"]}"')


class RedisSettingTests(SimpleTestCase):
    BASE = 'mode = "development"\nhosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body, base=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text((base or self.BASE) + body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_a_redis_table_is_checked_and_holds_no_credentials(self):
        for body in (
            '[redis]\nhost = "redis"\npassword = "x"\n',
            '[redis]\nhost = "redis host"\n',
            '[redis]\nhost = "redis"\nport = "6379"\n',
            '[redis]\nhost = "redis"\ntls = "yes"\n',
            '[redis]\nhost = "redis"\nca_certificate = "ca.crt"\n',
            'shared_streams = true\n[redis]\nhost = "redis"\n',
        ):
            with self.subTest(body=body), self.assertRaises(ImproperlyConfigured):
                self.load(body)
        loaded = self.load('[redis]\nhost = "redis"\nport = 6380\n')
        self.assertEqual(loaded["redis"]["port"], 6380)

    def test_production_needs_verified_tls(self):
        production = self.BASE.replace("development", "production")
        with self.assertRaises(ImproperlyConfigured):
            self.load('[redis]\nhost = "redis"\n', base=production)

    def test_the_password_is_a_mounted_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with override_settings(SECRET_DIRECTORY=directory, PRODUCTION=False):
                self.assertIsNone(broker.password())
                (Path(directory) / "redis_password").write_text("s3cret\n", encoding="utf-8")
                self.assertEqual(broker.password(), "s3cret")
            with override_settings(SECRET_DIRECTORY=directory + "-missing", PRODUCTION=True):
                with self.assertRaises(ImproperlyConfigured):
                    broker.password()
