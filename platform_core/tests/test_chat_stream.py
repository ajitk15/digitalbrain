import json
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.agent_runtime import streaming
from platform_core.models import ChatMessage
from platform_core.workbench import add_knowledge

from . import test_documents


class StreamSessionTests(SimpleTestCase):
    def tearDown(self):
        for key in list(streaming._sessions):
            streaming.close_session(key)

    def test_frames_are_well_formed_server_sent_events(self):
        frame = streaming.encode("delta", {"t": "hi"}, 3)
        self.assertEqual(frame, 'id: 3\nevent: delta\ndata: {"t":"hi"}\n\n')

    def test_a_session_drains_events_in_order_then_ends(self):
        session = streaming.open_session("11111111-1111-1111-1111-111111111111")
        session.emit("delta", {"t": "a"})
        session.emit("delta", {"t": "b"})
        session.emit("done", {"status": "complete"})
        session.finish()
        frames = list(streaming.stream_frames(session))
        self.assertEqual(len(frames), 3)
        self.assertIn('"t":"a"', frames[0])
        self.assertIn('"t":"b"', frames[1])
        self.assertIn("event: done", frames[2])

    def test_an_idle_stream_emits_a_heartbeat_rather_than_closing(self):
        session = streaming.open_session("22222222-2222-2222-2222-222222222222")
        with patch.object(streaming, "HEARTBEAT_SECONDS", 0.05):
            frames = streaming.stream_frames(session)
            first = next(frames)
            self.assertEqual(first, ": keep-alive\n\n")
            session.finish()
            frames.close()

    def test_a_stopped_session_drops_further_deltas(self):
        session = streaming.open_session("33333333-3333-3333-3333-333333333333")
        self.assertTrue(streaming.request_stop(session.message_id))
        session.emit("delta", {"t": "late"})
        session.emit("done", {"status": "stopped"})
        session.finish()
        frames = list(streaming.stream_frames(session))
        self.assertTrue(all("late" not in frame for frame in frames))

    def test_stopping_an_unknown_stream_reports_that_it_was_not_live(self):
        self.assertFalse(streaming.request_stop("44444444-4444-4444-4444-444444444444"))

    def test_the_process_refuses_more_streams_than_it_has_capacity_for(self):
        opened = []
        with patch.object(streaming, "MAX_ACTIVE_STREAMS", 2):
            opened.append(streaming.open_session("a"))
            opened.append(streaming.open_session("b"))
            with self.assertRaises(streaming.StreamCapacityError):
                streaming.open_session("c")
        for session in opened:
            streaming.close_session(session.message_id)

    def test_the_same_message_cannot_be_streamed_twice_at_once(self):
        streaming.open_session("dupe")
        with self.assertRaises(streaming.StreamCapacityError):
            streaming.open_session("dupe")

    def test_a_closed_generator_cancels_and_unregisters_its_session(self):
        session = streaming.open_session("55555555-5555-5555-5555-555555555555")
        frames = streaming.stream_frames(session)
        session.emit("delta", {"t": "x"})
        next(frames)
        frames.close()
        self.assertTrue(session.cancel.is_set())
        self.assertIsNone(streaming.get_session(session.message_id))

    def test_a_full_queue_cancels_instead_of_blocking_forever(self):
        session = streaming.StreamSession(message_id="full", events=queue.Queue(1))
        session.emit("delta", {"t": "1"})
        with patch.object(session.events, "put", side_effect=queue.Full):
            session.emit("delta", {"t": "2"})
        self.assertTrue(session.stopped)


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ChatStreamViewTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )
        self.url = reverse("chat", args=[self.app.pk])

    def start(self, question="Refund deadline?"):
        return self.client.post(
            self.url,
            {"question": question, "mode": "ai"},
            headers={"X-Digital-Brain-Stream": "1"},
        )

    def test_a_streaming_post_persists_the_question_and_a_placeholder(self):
        response = self.start()
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(ChatMessage.objects.count(), 2)
        placeholder = ChatMessage.objects.get(role="assistant")
        self.assertEqual(placeholder.status, "streaming")
        self.assertEqual(placeholder.body, "")
        self.assertEqual(payload["message"], str(placeholder.pk))
        self.assertIn("stream", payload)
        # The user's question is durable even if the browser disappears now.
        self.assertEqual(
            ChatMessage.objects.get(role="user").body, "Refund deadline?"
        )

    def test_search_conversations_never_take_the_streaming_path(self):
        response = self.client.post(
            self.url,
            {"question": "Refund deadline?", "mode": "search"},
            headers={"X-Digital-Brain-Stream": "1"},
        )
        self.assertEqual(response.status_code, 302)

    def test_the_stream_is_chunked_event_stream_without_a_content_length(self):
        payload = self.start().json()

        def fake_worker(
            user_id, app_id, message_id, question, history, session, mode="ai", graph_version=None
        ):
            session.emit("delta", {"t": "Thirty "})
            session.emit("delta", {"t": "days."})
            session.emit("done", {"status": "complete"})
            session.finish()

        with patch("platform_core.workbench.answer_worker", fake_worker):
            response = self.client.get(payload["stream"])
            self.assertEqual(response["Content-Type"], "text/event-stream")
            self.assertNotIn("Content-Length", response)
            self.assertEqual(response["X-Accel-Buffering"], "no")
            body = b"".join(response.streaming_content).decode()
            response.close()
        self.assertIn('data: {"t":"Thirty "}', body)
        self.assertIn("event: done", body)

    def test_another_user_cannot_open_someone_elses_stream(self):
        payload = self.start().json()
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(payload["stream"]).status_code, 404)

    def test_an_anonymous_visitor_is_redirected_not_streamed(self):
        payload = self.start().json()
        self.client.logout()
        self.assertEqual(self.client.get(payload["stream"]).status_code, 302)

    def test_a_finished_message_cannot_be_streamed_again(self):
        payload = self.start().json()
        ChatMessage.objects.filter(pk=payload["message"]).update(status="complete")
        self.assertEqual(self.client.get(payload["stream"]).status_code, 404)

    def test_stop_marks_an_orphaned_answer_stopped(self):
        payload = self.start().json()
        response = self.client.post(payload["stop"])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(ChatMessage.objects.get(pk=payload["message"]).status, "stopped")

    def test_the_message_fragment_renders_through_the_trusted_filter(self):
        payload = self.start().json()
        ChatMessage.objects.filter(pk=payload["message"]).update(
            status="complete", body="**Thirty days** <script>alert(1)</script>"
        )
        response = self.client.get(payload["fragment"])
        self.assertContains(response, "<strong>Thirty days</strong>", html=False)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def configured(self):
        """Stand in for an owner-configured provider and its mounted key."""
        config = SimpleNamespace(provider="openai", model="gpt-5.6-luna")
        return patch(
            "platform_core.ai.chat_configuration", return_value=(self.app, config, "scoped-key")
        )

    def test_the_worker_writes_the_answer_and_closes_the_stream(self):
        from platform_core.workbench import answer_worker

        payload = self.start().json()
        session = streaming.open_session(payload["message"])
        try:
            with (
                self.configured(),
                patch(
                    "platform_core.ai.stream_chat_answer",
                    return_value=(
                        "Thirty days [1].",
                        [{"id": "x", "title": "t", "excerpt": "e", "digest": "d"}],
                    ),
                ),
            ):
                answer_worker(
                    self.owner.pk, self.app.pk, payload["message"], "Refund deadline?", [], session
                )
        finally:
            streaming.close_session(payload["message"])
        message = ChatMessage.objects.get(pk=payload["message"])
        self.assertEqual(message.status, "complete")
        self.assertEqual(message.body, "Thirty days [1].")
        self.assertEqual(message.provider, "openai")
        self.assertEqual(message.model, "gpt-5.6-luna")

    def test_an_unconfigured_application_fails_closed_with_a_setup_message(self):
        from platform_core.workbench import answer_worker

        payload = self.start().json()
        session = streaming.open_session(payload["message"])
        try:
            answer_worker(
                self.owner.pk, self.app.pk, payload["message"], "Refund deadline?", [], session
            )
        finally:
            streaming.close_session(payload["message"])
        message = ChatMessage.objects.get(pk=payload["message"])
        self.assertEqual(message.status, "failed")
        self.assertIn("configure Chat conversation", message.error)

    def test_a_provider_failure_marks_the_answer_failed_without_losing_the_question(self):
        from django.core.exceptions import ValidationError

        from platform_core.workbench import answer_worker

        payload = self.start().json()
        session = streaming.open_session(payload["message"])
        try:
            with (
                self.configured(),
                patch(
                    "platform_core.ai.stream_chat_answer",
                    side_effect=ValidationError("Provider unavailable"),
                ),
            ):
                answer_worker(
                    self.owner.pk, self.app.pk, payload["message"], "Refund deadline?", [], session
                )
        finally:
            streaming.close_session(payload["message"])
        message = ChatMessage.objects.get(pk=payload["message"])
        self.assertEqual(message.status, "failed")
        self.assertIn("Provider unavailable", message.error)
        self.assertEqual(ChatMessage.objects.get(role="user").body, "Refund deadline?")

    def test_citation_frames_never_expose_the_source_digest(self):
        from platform_core.workbench import public_citations

        public = public_citations(
            [{"id": "1", "title": "Policy", "excerpt": "text", "digest": "secret"}]
        )
        self.assertNotIn("digest", public[0])


class StreamThreadingTests(SimpleTestCase):
    def test_a_reader_sees_events_a_producer_thread_emits(self):
        session = streaming.open_session("66666666-6666-6666-6666-666666666666")

        def produce():
            time.sleep(0.02)
            session.emit("delta", {"t": "from-thread"})
            session.finish()

        threading.Thread(target=produce, daemon=True).start()
        frames = [f for f in streaming.stream_frames(session) if not f.startswith(":")]
        self.assertEqual(len(frames), 1)
        self.assertIn("from-thread", json.loads(frames[0].split("data: ")[1])["t"])
