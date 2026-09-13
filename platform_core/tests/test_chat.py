from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.models import ApplicationGrant, ChatConversation, ChatMessage
from platform_core.templatetags.chat_text import chat_text, source_chips
from platform_core.workbench import add_knowledge, conversation_context

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
        self.assertContains(response, "<h4>Sources</h4>", html=False)
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
