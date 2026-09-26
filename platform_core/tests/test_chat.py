from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.models import (
    ApplicationGrant,
    AuditEvent,
    ChatConversation,
    ChatMessage,
    ChatRetention,
)
from platform_core.templatetags.chat_text import chat_text, source_chips, source_groups
from platform_core.workbench import (
    add_knowledge,
    conversation_context,
    new_graph_version,
    purge_expired_conversations,
)

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ConversationTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.url = reverse("chat", args=[self.app.pk])
        self.source = add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )

    def send(self, question="Refund deadline?", conversation=None, **extra):
        data = {"question": question, "mode": "search", **extra}
        if conversation:
            data["conversation"] = str(conversation.pk)
        return self.client.post(self.url, data)

    def answers(self, conversation):
        return conversation.messages.filter(role="assistant").order_by("sequence")

    def latest_answer(self, conversation):
        return self.answers(conversation).last()

    def test_new_conversations_and_chronological_continuation(self):
        self.send()
        first = ChatConversation.objects.get()
        response = self.send("Explain that deadline", first)
        self.assertRedirects(response, f"{self.url}?conversation={first.pk}")
        self.assertEqual(first.messages.count(), 4)
        response = self.client.get(response.url)
        self.assertLess(
            response.content.index(b"Refund deadline?</div>"),
            response.content.index(b"Explain that deadline</div>"),
        )
        self.assertContains(response, "thirty days")
        # Sources are listed under the answer, with the quoted passage one click away.
        self.assertContains(response, 'class="message-sources"')
        self.assertContains(response, '<h4>Sources <span class="source-count">1</span></h4>')
        self.assertContains(response, "Refund policy")
        self.assertNotContains(response, 'class="evidence" open')
        self.assertNotContains(self.client.get(self.url, {"new": "1"}), 'name="conversation"')
        self.send("A separate topic")
        self.assertEqual(ChatConversation.objects.count(), 2)
        self.assertEqual(first.messages.count(), 4)

    def test_topic_survives_multiple_short_followups(self):
        self.send()
        conversation = ChatConversation.objects.get()
        for question in ["Explain it", "Why?", "Tell me more", "What about that?"]:
            self.send(question, conversation)
            self.assertEqual(
                self.latest_answer(conversation).citations[0]["id"], str(self.source.pk)
            )

    def test_conversation_ids_are_private_on_get_and_post(self):
        self.send()
        conversation = ChatConversation.objects.get()
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(
            self.client.get(self.url, {"conversation": conversation.pk}).status_code, 404
        )
        self.assertEqual(self.send(conversation=conversation).status_code, 404)
        self.assertNotContains(self.client.get(self.url), "Refund deadline?")
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        other_url = reverse("chat", args=[self.other.pk])
        self.assertEqual(
            self.client.get(other_url, {"conversation": conversation.pk}).status_code, 404
        )
        self.assertEqual(
            self.client.post(
                other_url, {"conversation": conversation.pk, "question": "leak"}
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get(self.url, {"conversation": "invalid"}).status_code, 404)
        self.assertEqual(ChatMessage.objects.count(), 2)

    @patch("platform_core.ai.answer_with_ai", return_value="The deadline is **thirty days** [1].")
    def test_ai_receives_only_selected_conversation_and_followup_sources(self, answer):
        self.send(mode="ai")
        conversation = ChatConversation.objects.get()
        self.send("Explain it", conversation)
        self.assertEqual(len(answer.call_args.kwargs["history"]), 1)
        self.assertEqual(answer.call_args.args[3][0]["id"], str(self.source.pk))
        self.send("Separate new chat", mode="ai")
        self.assertEqual(answer.call_args.kwargs["history"], [])
        response = self.client.get(self.url, {"conversation": conversation.pk})
        self.assertContains(response, "<strong>thirty days</strong>", html=True)

    def test_archived_or_modified_evidence_is_excluded_from_followup_context(self):
        self.send()
        conversation = ChatConversation.objects.get()
        self.assertEqual(len(conversation_context(conversation, self.app, self.owner)), 1)
        self.source.active = False
        self.source.save()
        self.assertEqual(conversation_context(conversation, self.app, self.owner), [])
        self.send("Explain it", conversation)
        self.assertIn("No matching evidence", self.latest_answer(conversation).body)

    @patch("platform_core.ai.answer_with_ai", side_effect=ValidationError("Provider unavailable"))
    def test_provider_error_preserves_draft_and_conversation(self, answer):
        self.send()
        conversation = ChatConversation.objects.get()
        conversation.mode = "ai"
        conversation.save(update_fields=["mode"])
        response = self.send("Explain it please", conversation)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Provider unavailable")
        self.assertContains(response, "Explain it please")
        self.assertContains(response, str(conversation.pk))
        self.assertEqual(conversation.messages.count(), 2)

    def test_markdown_escapes_html_links_and_formats_tables_code_lists(self):
        result = str(
            chat_text(
                "**Summary**\n\n- First\n- Second\n\n"
                "| Field | Value |\n| --- | --- |\n| name | <img src=x onerror=alert(1)> |\n\n"
                "```html\n<script>alert(1)</script>\n```\n[jump](javascript:alert(1))"
            )
        )
        self.assertIn("<strong>Summary</strong>", result)
        self.assertIn("<ul><li>First</li><li>Second</li></ul>", result)
        self.assertIn("<table>", result)
        self.assertNotIn("<img", result)
        self.assertNotIn("<script>", result)
        self.assertNotIn("href=", result)
        self.assertIn("&lt;script&gt;", result)

    def test_context_is_bounded_and_long_history_is_paginated(self):
        self.send()
        conversation = ChatConversation.objects.get()
        ChatMessage.objects.bulk_create(
            [
                ChatMessage(
                    application=self.app,
                    user=self.owner,
                    conversation=conversation,
                    role="user" if index % 2 == 0 else "assistant",
                    body=f"Message {index}",
                    sequence=2 + index,
                )
                for index in range(70)
            ]
        )
        self.assertEqual(len(conversation_context(conversation, self.app, self.owner)), 8)
        response = self.client.get(self.url, {"conversation": conversation.pk})
        self.assertEqual(response.context["message_page"].number, 2)
        self.assertContains(response, "Load earlier messages")

    def test_mode_belongs_to_the_conversation_and_a_send_cannot_change_it(self):
        self.send()
        conversation = ChatConversation.objects.get()
        self.assertEqual(conversation.mode, "search")
        # A crafted mode on a follow-up must not switch the thread to a paid provider.
        with patch("platform_core.ai.answer_with_ai") as answer:
            self.send("Explain it", conversation, mode="ai")
            answer.assert_not_called()
        conversation.refresh_from_db()
        self.assertEqual(conversation.mode, "search")
        self.assertEqual(self.latest_answer(conversation).mode, "search")

    def test_owner_can_rename_and_delete_their_conversation(self):
        self.send()
        conversation = ChatConversation.objects.get()
        url = reverse("chat-conversation", args=[self.app.pk, conversation.pk])
        self.client.post(url, {"action": "rename", "title": "Refund questions"})
        conversation.refresh_from_db()
        self.assertEqual(conversation.title, "Refund questions")
        self.assertTrue(conversation.title_locked)
        self.client.post(url, {"action": "delete"})
        self.assertEqual(ChatConversation.objects.count(), 0)
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_conversation_actions_reject_another_users_conversation(self):
        self.send()
        conversation = ChatConversation.objects.get()
        url = reverse("chat-conversation", args=[self.app.pk, conversation.pk])
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.post(url, {"action": "delete"}).status_code, 404)
        self.assertEqual(ChatConversation.objects.count(), 1)

    def test_regenerate_replaces_the_last_answer_without_duplicating_the_question(self):
        self.send()
        conversation = ChatConversation.objects.get()
        answer = self.latest_answer(conversation)
        response = self.client.post(
            reverse("chat-regenerate", args=[self.app.pk, answer.pk])
        )
        self.assertRedirects(response, f"{self.url}?conversation={conversation.pk}")
        self.assertEqual(conversation.messages.count(), 2)
        self.assertEqual(
            conversation.messages.filter(role="user").get().body, "Refund deadline?"
        )

    def test_the_chat_page_offers_the_edit_form_on_a_question(self):
        """The view was reachable only by URL: nothing rendered a form at it.

        Pinned here because an unlinked POST view looks healthy from the server
        side - it is wired, it is tested, and no one can use it.
        """
        self.send()
        question = ChatMessage.objects.get(role="user")
        page = self.client.get(self.url).content.decode()
        action = reverse("chat-edit", args=[self.app.pk, question.pk])
        self.assertIn(f'action="{action}"', page)
        self.assertIn('name="question"', page)

    def test_editing_a_question_drops_every_later_message(self):
        self.send()
        conversation = ChatConversation.objects.get()
        self.send("Explain it", conversation)
        first_question = conversation.messages.filter(role="user").order_by("sequence").first()
        self.client.post(
            reverse("chat-edit", args=[self.app.pk, first_question.pk]),
            {"question": "Refund window?"},
        )
        bodies = list(
            conversation.messages.filter(role="user")
            .order_by("sequence")
            .values_list("body", flat=True)
        )
        self.assertEqual(bodies, ["Refund window?"])


class SourceChipTests(SimpleTestCase):
    """Citations group by document so one source cited five times is one chip."""

    def chip(self, **overrides):
        return {"id": "d1", "title": "node_dump.csv", "digest": "abc", "excerpt": "x", **overrides}

    def test_repeated_citations_of_one_document_collapse(self):
        chips = source_chips([self.chip(), self.chip(excerpt="y"), self.chip(excerpt="z")])
        self.assertEqual(len(chips), 1)
        # The numbers are the inline [n] markers, so the answer still resolves.
        self.assertEqual(chips[0]["numbers"], [1, 2, 3])
        self.assertEqual(len(chips[0]["excerpts"]), 3)

    def test_distinct_documents_stay_distinct_and_keep_their_numbers(self):
        chips = source_chips(
            [self.chip(), self.chip(id="d2", title="qmgr.csv"), self.chip(excerpt="y")]
        )
        self.assertEqual([c["title"] for c in chips], ["node_dump.csv", "qmgr.csv"])
        self.assertEqual(chips[0]["numbers"], [1, 3])
        self.assertEqual(chips[1]["numbers"], [2])

    def test_the_digest_never_reaches_the_browser(self):
        """It is a server-side integrity token, not display data."""
        chips = source_chips([self.chip()])
        self.assertNotIn("digest", chips[0])

    def test_a_graph_version_is_carried_through(self):
        self.assertEqual(source_chips([self.chip(graph_version="2")])[0]["graph_version"], "2")

    def test_no_citations_is_not_an_error(self):
        self.assertEqual(source_chips([]), [])
        self.assertEqual(source_chips(None), [])


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ConversationLifecycleTests(TestCase):
    """Clearing, automatic retention, and pinning a conversation to its version."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.url = reverse("chat", args=[self.app.pk])
        add_knowledge(self.owner, self.app.pk, "Policy", "Refunds expire after thirty days.")

    def conversation(self, user=None, **fields):
        return ChatConversation.objects.create(
            application=self.app, user=user or self.owner, title="Thread", **fields
        )

    # ---- clearing ----

    def test_clearing_removes_your_conversations_and_their_messages(self):
        conversation = self.conversation()
        ChatMessage.objects.create(
            application=self.app,
            user=self.owner,
            conversation=conversation,
            role="user",
            body="Hello",
            sequence=1,
        )
        response = self.client.post(self.url, {"action": "clear"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ChatConversation.objects.filter(user=self.owner).exists())
        self.assertFalse(ChatMessage.objects.exists())

    def test_clearing_never_reaches_another_members_conversations(self):
        mine = self.conversation()
        theirs = self.conversation(user=self.viewer)
        self.client.post(self.url, {"action": "clear"})
        self.assertFalse(ChatConversation.objects.filter(pk=mine.pk).exists())
        self.assertTrue(ChatConversation.objects.filter(pk=theirs.pk).exists())

    def test_clearing_is_audited(self):
        self.conversation()
        self.client.post(self.url, {"action": "clear"})
        self.assertTrue(AuditEvent.objects.filter(action="chat.cleared").exists())

    # ---- retention ----

    def test_a_conversation_past_the_window_is_purged(self):
        stale = self.conversation()
        ChatConversation.objects.filter(pk=stale.pk).update(
            updated_at=timezone.now() - timedelta(days=ChatRetention.DEFAULT_DAYS + 1)
        )
        self.assertEqual(purge_expired_conversations(self.app), 1)
        self.assertFalse(ChatConversation.objects.filter(pk=stale.pk).exists())

    def test_age_is_measured_from_the_last_message_not_the_start(self):
        """A thread someone is still using is not stale."""
        active = self.conversation()
        ChatConversation.objects.filter(pk=active.pk).update(updated_at=timezone.now())
        self.assertEqual(purge_expired_conversations(self.app), 0)
        self.assertTrue(ChatConversation.objects.filter(pk=active.pk).exists())

    def test_the_window_is_configurable_per_application(self):
        ChatRetention.objects.update_or_create(application=self.app, defaults={"days": 30})
        old = self.conversation()
        ChatConversation.objects.filter(pk=old.pk).update(
            updated_at=timezone.now() - timedelta(days=10)
        )
        self.assertEqual(purge_expired_conversations(self.app), 0)
        ChatRetention.objects.update_or_create(application=self.app, defaults={"days": 7})
        self.assertEqual(purge_expired_conversations(self.app), 1)

    def test_zero_days_keeps_conversations_indefinitely(self):
        ChatRetention.objects.update_or_create(application=self.app, defaults={"days": 0})
        ancient = self.conversation()
        ChatConversation.objects.filter(pk=ancient.pk).update(
            updated_at=timezone.now() - timedelta(days=4000)
        )
        self.assertEqual(purge_expired_conversations(self.app), 0)

    def test_an_owner_can_change_the_window_and_a_viewer_cannot(self):
        settings_url = reverse("chat-settings", args=[self.app.pk])
        self.assertEqual(
            self.client.post(settings_url, {"days": "14"}).status_code, 302
        )
        self.assertEqual(ChatRetention.objects.get(application=self.app).days, 14)
        # A member who is not an owner is refused; someone with no grant at all is
        # not even told the application exists.
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(settings_url).status_code, 403)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(settings_url).status_code, 404)

    def test_an_absurd_window_is_refused_in_favour_of_zero(self):
        response = self.client.post(
            reverse("chat-settings", args=[self.app.pk]), {"days": "99999"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Use 0 for indefinite retention")

    # ---- frozen graph version ----

    def test_a_new_conversation_pins_the_version_published_when_it_started(self):
        from platform_core.models import GraphRevision

        GraphRevision.objects.create(
            application=self.app,
            number=1,
            fingerprint="f",
            published_at=timezone.now(),
            data={"nodes": [], "edges": [], "sources": []},
            quality={},
        )
        request = RequestFactory().post("/", {})
        self.assertEqual(new_graph_version(request, self.app.pk, True), 1)

    def test_publishing_a_newer_version_does_not_move_an_existing_conversation(self):
        conversation = self.conversation(graph_version=1)
        conversation.refresh_from_db()
        self.assertEqual(conversation.graph_version, 1)
        # A later publish changes nothing about a conversation already under way.
        from platform_core.models import GraphRevision

        GraphRevision.objects.create(
            application=self.app,
            number=2,
            fingerprint="g",
            published_at=timezone.now(),
            data={"nodes": [], "edges": [], "sources": []},
            quality={},
        )
        conversation.refresh_from_db()
        self.assertEqual(conversation.graph_version, 1)


class ConversationalAnswerTests(SimpleTestCase):
    """An answer reads as a message: citations as badges, cited sources first."""

    def test_citation_markers_become_badges_that_are_not_links(self):
        result = str(chat_text("A gap [4][1], and two more [2, 3]."))
        for number in ("4", "1", "2", "3"):
            self.assertIn(f'<sup class="cite" data-cite="{number}">', result)
        self.assertNotIn("[4]", result)
        self.assertNotIn("href=", result)

    def test_a_bracketed_number_in_code_is_code(self):
        result = str(chat_text("Use `items[1]` here.\n\n```\nrows[2]\n```"))
        self.assertIn("<code>items[1]</code>", result)
        self.assertIn("rows[2]", result)
        self.assertNotIn('class="cite"', result)

    def test_badges_are_built_from_escaped_text(self):
        result = str(chat_text('<b>x</b> [1] "q"'))
        self.assertIn("&lt;b&gt;", result)
        self.assertIn('data-cite="1"', result)

    def test_the_sources_an_answer_cites_come_first_and_the_rest_are_marked(self):
        rows = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}, {"id": "c", "title": "C"}]
        groups = source_groups(rows, "Only C matters [3].")
        self.assertEqual([chip["title"] for chip in groups["cited"]], ["C"])
        self.assertEqual([chip["title"] for chip in groups["more"]], ["A", "B"])

    def test_an_answer_with_no_markers_cites_everything_it_was_given(self):
        rows = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
        self.assertEqual(len(source_groups(rows, "An excerpt with no markers.")["cited"]), 2)


class TemplateCommentTests(SimpleTestCase):
    def test_no_short_comment_spans_lines(self):
        """`{# #}` is one line only; across lines it renders as page text.

        One did, inside every question in the chat.
        """
        from pathlib import Path

        from django.conf import settings

        for template in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
                with self.subTest(template=template.name, line=number):
                    self.assertFalse("{#" in line and "#}" not in line.split("{#", 1)[1])
