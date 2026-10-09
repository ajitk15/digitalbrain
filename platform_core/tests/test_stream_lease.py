"""Shared streams: Stop and "is it live?" reach every process through a lease."""

from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.utils import timezone

from platform_core.agent_runtime import streaming
from platform_core.models import ChatMessage, StreamLease

from . import test_chat_stream


@override_settings(SHARED_STREAMS=True)
class LeaseTests(TestCase):
    setUp = test_chat_stream.ChatStreamViewTests.setUp
    start = test_chat_stream.ChatStreamViewTests.start

    def answer(self):
        payload = self.start().json()
        return payload, ChatMessage.objects.get(pk=payload["message"])

    def elsewhere(self, message, seconds_ago=0):
        """A lease held by another process, last renewed this long ago."""
        return StreamLease.objects.create(
            message=message,
            owner="other-host:1:abc",
            heartbeat_at=timezone.now() - timedelta(seconds=seconds_ago),
        )

    def open(self, message):
        with patch.object(streaming, "start_keeper"):
            return streaming.open_session(message.pk)

    def test_a_stream_holds_a_lease_and_releases_it_when_done(self):
        _, message = self.answer()
        session = self.open(message)
        self.assertEqual(StreamLease.objects.get().owner, streaming.PROCESS)
        self.assertTrue(streaming.renew(session))
        streaming.close_session(message.pk)
        # The keeper's next tick removes the lease and stops.
        self.assertFalse(streaming.renew(session))
        self.assertFalse(StreamLease.objects.exists())

    def test_an_answer_live_elsewhere_is_not_streamed_twice(self):
        _, message = self.answer()
        self.elsewhere(message)
        with self.assertRaises(streaming.StreamCapacityError):
            self.open(message)
        self.assertIsNone(streaming.get_session(message.pk))

    def test_a_dead_processes_lease_is_taken_over(self):
        _, message = self.answer()
        self.elsewhere(message, seconds_ago=streaming.LEASE_STALE_SECONDS + 5)
        self.open(message)
        self.assertEqual(StreamLease.objects.get().owner, streaming.PROCESS)
        streaming.close_session(message.pk)

    def test_stop_from_another_process_reaches_the_stream(self):
        _, message = self.answer()
        session = self.open(message)
        # The Stop request lands in a process with no local session.
        with patch.object(streaming, "get_session", return_value=None):
            self.assertTrue(streaming.request_stop(message.pk))
        self.assertFalse(session.stopped)
        streaming.renew(session)
        self.assertTrue(session.stopped)
        streaming.close_session(message.pk)

    def test_stop_for_an_answer_live_elsewhere_leaves_the_row_to_its_worker(self):
        payload, message = self.answer()
        self.elsewhere(message)
        response = self.client.post(payload["stop"])
        self.assertEqual(response.json()["status"], "stopping")
        message.refresh_from_db()
        # The single writer is the other process's worker; this one wrote nothing.
        self.assertEqual(message.status, "streaming")
        self.assertTrue(StreamLease.objects.get().stop_requested)

    def test_a_dead_lease_is_an_orphan_and_is_closed_out(self):
        payload, message = self.answer()
        self.elsewhere(message, seconds_ago=streaming.LEASE_STALE_SECONDS + 5)
        self.assertFalse(streaming.is_live(message.pk))
        response = self.client.post(payload["stop"])
        self.assertEqual(response.json()["status"], "stopped")

    def test_live_elsewhere_is_not_offered_as_stranded(self):
        payload, message = self.answer()
        self.elsewhere(message)
        self.assertTrue(streaming.is_live(message.pk))
        page = self.client.get(self.url + f"?conversation={message.conversation_id}")
        stranded = [m for m in page.context["message_page"].object_list if m.pk == message.pk]
        self.assertFalse(stranded[0].stranded)


class ProcessLocalTests(TestCase):
    setUp = test_chat_stream.ChatStreamViewTests.setUp
    start = test_chat_stream.ChatStreamViewTests.start

    def test_off_by_default_no_lease_is_written(self):
        payload = self.start().json()
        message = ChatMessage.objects.get(pk=payload["message"])
        streaming.open_session(message.pk)
        self.assertFalse(StreamLease.objects.exists())
        self.assertTrue(streaming.is_live(message.pk))
        streaming.close_session(message.pk)
        self.assertFalse(streaming.is_live(message.pk))

    def test_the_setting_must_be_a_boolean(self):
        from digitalbrain import configuration

        with patch.object(
            configuration.tomllib, "load", return_value={"mode": "development", "shared_streams": 1}
        ):
            with patch.object(configuration.Path, "is_file", return_value=True):
                with patch.object(configuration.Path, "open"):
                    with self.assertRaises(ImproperlyConfigured):
                        configuration.load_config()
