import json
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.ai import draft_plan, generate_title
from platform_core.models import AIConfiguration, ChangePlan, ChatConversation
from platform_core.workbench import add_knowledge, answer_question

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AIAuthoringTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )

    def configure(self, purpose):
        return AIConfiguration.objects.create(
            application=self.app,
            purpose=purpose,
            provider="claude",
            model="claude-sonnet-5",
            enabled=True,
            input_rate=Decimal("2"),
            output_rate=Decimal("10"),
            configured_by=self.owner,
        )

    def citations(self):
        return [
            {
                "id": str(self.source.pk),
                "title": self.source.title,
                "digest": self.source.digest,
                "excerpt": "Refund requests expire after thirty days.",
            }
        ]

    # ---- conversation titles ----

    def test_titles_need_their_own_configuration_and_fail_silently(self):
        """A second billable call must be opted into, and never break the answer."""
        self.assertIsNone(generate_title(self.owner, self.app.pk, "Question", "Answer"))

    @patch("platform_core.ai.invoke_ai", return_value="  **Refund window rules**  \n")
    def test_a_generated_title_is_cleaned_of_markup_and_whitespace(self, invoke):
        self.assertEqual(
            generate_title(self.owner, self.app.pk, "Q", "A"), "Refund window rules"
        )

    @patch("platform_core.ai.invoke_ai", side_effect=ValidationError("provider down"))
    def test_a_failed_title_call_is_swallowed(self, invoke):
        self.assertIsNone(generate_title(self.owner, self.app.pk, "Q", "A"))

    @patch("platform_core.ai.generate_title", return_value="Refund window rules")
    def test_the_first_exchange_renames_an_unnamed_conversation(self, title):
        answer_question(self.owner, self.app, None, "Refund deadline?", "search")
        conversation = ChatConversation.objects.get()
        self.assertEqual(conversation.title, "Refund window rules")

    @patch("platform_core.ai.generate_title", return_value="Should not be used")
    def test_a_human_named_conversation_is_never_renamed(self, title):
        conversation = ChatConversation.objects.create(
            application=self.app,
            user=self.owner,
            title="My own name",
            mode="search",
            title_locked=True,
        )
        answer_question(self.owner, self.app, conversation, "Refund deadline?", "search")
        conversation.refresh_from_db()
        self.assertEqual(conversation.title, "My own name")

    @patch("platform_core.ai.generate_title", return_value="Renamed")
    def test_only_the_opening_exchange_triggers_a_rename(self, title):
        answer_question(self.owner, self.app, None, "First?", "search")
        conversation = ChatConversation.objects.get()
        title.reset_mock()
        answer_question(self.owner, self.app, conversation, "Second?", "search")
        title.assert_not_called()

    # ---- Code Factory drafting ----

    def model_plan(self, **overrides):
        payload = {
            "title": "Shorten the refund window",
            "proposal": "Reduce the refund window from thirty days.",
            "validation": "Add a regression test; roll back by restoring the constant.",
            "sources": [
                {"id": str(self.source.pk), "quote": "Refund requests expire after thirty days."}
            ],
            **overrides,
        }
        return json.dumps(payload)

    def test_a_draft_returns_form_fields_with_verified_evidence(self):
        with patch("platform_core.ai.invoke_ai", return_value=self.model_plan()):
            draft = draft_plan(self.owner, self.app.pk, "Shorten refunds", self.citations())
        self.assertEqual(draft["title"], "Shorten the refund window")
        self.assertEqual(len(draft["citations"]), 1)
        self.assertEqual(draft["rejected"], 0)

    def test_a_fabricated_quote_is_dropped_from_the_draft(self):
        fabricated = self.model_plan(
            sources=[{"id": str(self.source.pk), "quote": "Refunds are never permitted."}]
        )
        with patch("platform_core.ai.invoke_ai", return_value=fabricated):
            draft = draft_plan(self.owner, self.app.pk, "Shorten refunds", self.citations())
        self.assertEqual(draft["citations"], [])
        self.assertEqual(draft["rejected"], 1)

    def test_a_fenced_json_reply_is_still_understood(self):
        fenced = f"```json\n{self.model_plan()}\n```"
        with patch("platform_core.ai.invoke_ai", return_value=fenced):
            draft = draft_plan(self.owner, self.app.pk, "Shorten refunds", self.citations())
        self.assertEqual(draft["title"], "Shorten the refund window")

    def test_an_unusable_reply_is_reported_rather_than_guessed_at(self):
        for reply in ["not json at all", "[1, 2, 3]", ""]:
            with self.subTest(reply=reply):
                with patch("platform_core.ai.invoke_ai", return_value=reply):
                    with self.assertRaises(ValidationError):
                        draft_plan(self.owner, self.app.pk, "Shorten refunds", self.citations())

    def test_drafting_saves_nothing(self):
        """The "Propose a change" panel this fed has been removed from the Code
        Factory screen, so the two assertions about its markup went with it. The
        endpoint is still served and still refuses to write, which is the part
        that mattered: drafting is not proposing, and approval is untouched."""
        self.configure("plan_drafting")
        url = reverse("plans", args=[self.app.pk])
        with (
            patch("platform_core.secrets.application_secret", return_value="scoped-key"),
            patch(
                "platform_core.claude_agents.completion",
                return_value=(
                    {"id": "r1", "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
                    self.model_plan(),
                ),
            ),
        ):
            response = self.client.post(url, {"action": "draft", "requirement": "Shorten refunds"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ChangePlan.objects.exists())

    def test_a_viewer_cannot_draft(self):
        self.configure("plan_drafting")
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(
            reverse("plans", args=[self.app.pk]),
            {"action": "draft", "requirement": "Shorten refunds"},
        )
        self.assertEqual(response.status_code, 403)

    def test_no_drafting_control_is_offered_on_the_screen(self):
        """It used to appear once an owner configured plan_drafting. The panel
        that held it is gone, and configuring the purpose must not bring it back
        by some other route."""
        self.configure("plan_drafting")
        response = self.client.get(reverse("plans", args=[self.app.pk]))
        self.assertNotContains(response, "Draft with AI")
        self.assertNotContains(response, "Propose a change")
