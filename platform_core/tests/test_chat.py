from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import ApplicationGrant, ChatConversation, ChatTurn
from platform_core.templatetags.chat_text import chat_text
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

    def test_new_conversations_and_chronological_continuation(self):
        self.send()
        first = ChatConversation.objects.get()
        response = self.send("Explain that deadline", first)
        self.assertRedirects(response, f"{self.url}?conversation={first.pk}")
        self.assertEqual(first.turns.count(), 2)
        response = self.client.get(response.url)
        self.assertLess(
            response.content.index(b"Refund deadline?</div>"),
            response.content.index(b"Explain that deadline</div>"),
        )
        self.assertContains(response, "thirty days")
        self.assertContains(response, 'class="chat-sources"')
        self.assertNotContains(response, 'class="evidence" open')
        self.assertNotContains(self.client.get(self.url, {"new": "1"}), 'name="conversation"')
        self.send("A separate topic")
        self.assertEqual(ChatConversation.objects.count(), 2)
        self.assertEqual(first.turns.count(), 2)

    def test_topic_survives_multiple_short_followups(self):
        self.send()
        conversation = ChatConversation.objects.get()
        for question in ["Explain it", "Why?", "Tell me more", "What about that?"]:
            self.send(question, conversation)
            self.assertEqual(
                conversation.turns.latest("created_at").citations[0]["id"], str(self.source.pk)
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
        self.assertEqual(ChatTurn.objects.count(), 1)

    @patch("platform_core.ai.answer_with_ai", return_value="The deadline is **thirty days** [1].")
    def test_ai_receives_only_selected_conversation_and_followup_sources(self, answer):
        self.send()
        conversation = ChatConversation.objects.get()
        self.send("Explain it", conversation, mode="ai")
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
        self.assertIn("No matching evidence", conversation.turns.latest("created_at").answer)

    @patch("platform_core.ai.answer_with_ai", side_effect=ValidationError("Provider unavailable"))
    def test_provider_error_preserves_draft_and_conversation(self, answer):
        self.send()
        conversation = ChatConversation.objects.get()
        response = self.send("Explain it please", conversation, mode="ai")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Provider unavailable")
        self.assertContains(response, "Explain it please")
        self.assertContains(response, str(conversation.pk))
        self.assertEqual(conversation.turns.count(), 1)

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
        ChatTurn.objects.bulk_create(
            [
                ChatTurn(
                    application=self.app,
                    user=self.owner,
                    conversation=conversation,
                    question=f"Question {i}",
                    answer="Answer",
                )
                for i in range(35)
            ]
        )
        self.assertEqual(len(conversation_context(conversation, self.app, self.owner)), 8)
        response = self.client.get(self.url, {"conversation": conversation.pk})
        self.assertEqual(response.context["turn_page"].number, 2)
        self.assertContains(response, "Load earlier messages")
