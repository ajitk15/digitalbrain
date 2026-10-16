"""AI settings as one list: a switch per task, one price per model, caps where needed.

The page used to be twelve identical forms, each asking for its own input and
output price. Six tasks on one model meant pricing that model six times, and a
wrong copy made that task's cost reports wrong while the others were right.
"""

import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import ai
from platform_core.model_catalog import SPENDS, TASK_GROUPS
from platform_core.models import (
    AI_PURPOSES,
    AIConfiguration,
    AIUsage,
    ApplicationFeature,
    AuditEvent,
)

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AISettingsListTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.url = reverse("ai-settings", args=[self.app.pk])
        secrets = tempfile.TemporaryDirectory()
        self.addCleanup(secrets.cleanup)
        self.secrets = Path(secrets.name)
        settings = override_settings(SECRET_DIRECTORY=str(self.secrets))
        settings.enable()
        self.addCleanup(settings.disable)
        login = patch(
            "platform_core.agent_runtime.credentials.host_login_enabled", return_value=False
        )
        login.start()
        self.addCleanup(login.stop)

    def configure(
        self,
        purpose,
        model="claude-sonnet-5",
        provider="claude",
        rates=("2", "10"),
        enabled=True,
        **values,
    ):
        return AIConfiguration.objects.create(
            application=self.app,
            purpose=purpose,
            provider=provider,
            model=model,
            enabled=enabled,
            input_rate=Decimal(rates[0]),
            output_rate=Decimal(rates[1]),
            configured_by=self.owner,
            **values,
        )

    def test_every_task_has_a_group_and_says_when_it_spends(self):
        grouped = [purpose for _, tasks in TASK_GROUPS for purpose, _ in tasks]
        self.assertEqual(sorted(grouped), sorted(purpose for purpose, _ in AI_PURPOSES))
        self.assertEqual(set(SPENDS), set(grouped))

    def test_the_switch_turns_a_task_on_and_off_and_is_audited(self):
        config = self.configure("chat")
        self.assertEqual(
            self.client.post(self.url, {"purpose": "chat", "enabled": "0"}).status_code, 302
        )
        config.refresh_from_db()
        self.assertFalse(config.enabled)
        self.client.post(self.url, {"purpose": "chat", "enabled": "1"})
        config.refresh_from_db()
        self.assertTrue(config.enabled)
        details = [
            event.details
            for event in AuditEvent.objects.filter(action="ai.configured").order_by("pk")
        ]
        self.assertEqual(
            details,
            [
                {"purpose": "chat", "enabled": False},
                {"purpose": "chat", "enabled": True},
            ],
        )

    def test_a_task_with_no_model_cannot_be_switched_on(self):
        response = self.client.post(self.url, {"purpose": "chat", "enabled": "1"}, follow=True)
        self.assertContains(
            response, "Choose a model for Chat conversation before switching it on."
        )
        self.assertFalse(AIConfiguration.objects.exists())

    def test_the_switch_takes_only_on_or_off(self):
        self.configure("chat")
        self.assertEqual(
            self.client.post(self.url, {"purpose": "chat", "enabled": "yes"}).status_code, 400
        )
        self.assertEqual(self.client.post(self.url, {"purpose": "chat"}).status_code, 400)

    def test_only_an_owner_sees_or_changes_it(self):
        self.configure("chat")
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.post(self.url, {"purpose": "chat", "enabled": "0"}).status_code, 403
        )
        self.assertTrue(AIConfiguration.objects.get().enabled)

    def test_another_applications_owner_gets_not_found(self):
        task = reverse("ai-task", args=[self.other.pk, "chat"])
        self.assertEqual(self.client.get(task).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("ai-task", args=[self.app.pk, "nope"])).status_code, 404
        )

    def test_one_price_per_model_applies_to_every_task_using_it(self):
        chat = self.configure("chat")
        graph = self.configure("graph_retrieval")
        opus = self.configure("graph_generation", model="claude-opus-5", rates=("5", "25"))
        page = self.client.get(self.url)
        # Two models in use, so two rows - not three.
        self.assertEqual(len(page.context["price_rows"]), 2)
        keys = [row["form"]["key"].value() for row in page.context["price_rows"]]
        data = {
            "purpose": "prices",
            "price-TOTAL_FORMS": "2",
            "price-INITIAL_FORMS": "2",
            "price-MIN_NUM_FORMS": "0",
            "price-MAX_NUM_FORMS": "40",
        }
        for index, key in enumerate(keys):
            data[f"price-{index}-key"] = key
            data[f"price-{index}-input_rate"] = "1.5" if "sonnet" in key else "5"
            data[f"price-{index}-output_rate"] = "7.5" if "sonnet" in key else "25"
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        for config in (chat, graph):
            config.refresh_from_db()
            self.assertEqual(
                (config.input_rate, config.output_rate), (Decimal("1.5"), Decimal("7.5"))
            )
        opus.refresh_from_db()
        self.assertEqual(opus.input_rate, Decimal("5"))
        event = AuditEvent.objects.get(action="ai.prices_set")
        # Only what changed is recorded.
        self.assertEqual(list(event.details["prices"]), ["claude:claude-sonnet-5"])

    def test_a_price_for_a_model_not_in_use_is_refused(self):
        self.configure("chat")
        data = {
            "purpose": "prices",
            "price-TOTAL_FORMS": "1",
            "price-INITIAL_FORMS": "1",
            "price-MIN_NUM_FORMS": "0",
            "price-MAX_NUM_FORMS": "40",
            "price-0-key": "claude:claude-opus-5",
            "price-0-input_rate": "0",
            "price-0-output_rate": "0",
        }
        self.assertEqual(self.client.post(self.url, data).status_code, 403)

    def test_embeddings_are_never_charged_for_output(self):
        config = self.configure(
            "embeddings",
            model="text-embedding-3-small",
            provider="openai",
            rates=("0.02", "0"),
            enabled=False,
        )
        data = {
            "purpose": "prices",
            "price-TOTAL_FORMS": "1",
            "price-INITIAL_FORMS": "1",
            "price-MIN_NUM_FORMS": "0",
            "price-MAX_NUM_FORMS": "40",
            "price-0-key": "openai:text-embedding-3-small",
            "price-0-input_rate": "0.03",
            "price-0-output_rate": "9",
        }
        self.client.post(self.url, data)
        config.refresh_from_db()
        self.assertEqual((config.input_rate, config.output_rate), (Decimal("0.03"), Decimal("0")))

    def test_an_unpriced_model_is_flagged(self):
        self.configure("chat", model="gpt-5.6-sol", provider="openai", rates=("0", "0"))
        page = self.client.get(self.url)
        self.assertContains(page, "Not priced")
        self.assertContains(page, "the budget cannot see it")

    def test_a_model_already_in_use_keeps_its_price_on_another_task(self):
        self.configure("chat", rates=("1.80", "9"))
        title = self.configure(
            "conversation_title", model="claude-haiku-4-5-20251001", rates=("1", "5")
        )
        response = self.client.post(
            reverse("ai-task", args=[self.app.pk, "conversation_title"]),
            {
                "conversation_title-model_choice": "claude:claude-sonnet-5",
                "conversation_title-provider": "claude",
                "conversation_title-model": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        title.refresh_from_db()
        self.assertEqual((title.input_rate, title.output_rate), (Decimal("1.80"), Decimal("9")))
        self.assertTrue(title.enabled)

    def test_saving_the_same_model_keeps_the_price(self):
        config = self.configure("chat", rates=("1.80", "9"))
        self.client.post(
            reverse("ai-task", args=[self.app.pk, "chat"]),
            {
                "chat-model_choice": "claude:claude-sonnet-5",
                "chat-provider": "claude",
                "chat-model": "",
                "chat-output_limit": "4000",
            },
        )
        config.refresh_from_db()
        self.assertEqual((config.input_rate, config.output_limit), (Decimal("1.80"), 4000))

    def test_a_task_set_up_for_the_first_time_starts_like_a_new_application(self):
        for purpose, enabled in (("chat", True), ("document_typing", False)):
            with self.subTest(purpose=purpose):
                self.client.post(
                    reverse("ai-task", args=[self.app.pk, purpose]),
                    {
                        f"{purpose}-model_choice": "claude:claude-sonnet-5",
                        f"{purpose}-provider": "claude",
                        f"{purpose}-model": "",
                    },
                )
                config = AIConfiguration.objects.get(application=self.app, purpose=purpose)
                self.assertEqual(config.enabled, enabled)

    def test_one_model_for_every_task_leaves_embeddings_alone(self):
        self.configure("chat")
        embeddings = self.configure(
            "embeddings",
            model="text-embedding-3-small",
            provider="openai",
            rates=("0.02", "0"),
            enabled=False,
        )
        response = self.client.post(
            self.url, {"purpose": "all", "all-model_choice": "claude:claude-opus-5"}
        )
        self.assertEqual(response.status_code, 302)
        configs = AIConfiguration.objects.filter(application=self.app).exclude(purpose="embeddings")
        self.assertEqual(set(configs.values_list("model", flat=True)), {"claude-opus-5"})
        self.assertEqual(configs.count(), len(AI_PURPOSES) - 1)
        self.assertEqual(set(configs.values_list("input_rate", flat=True)), {Decimal("5")})
        embeddings.refresh_from_db()
        self.assertEqual(embeddings.model, "text-embedding-3-small")
        # Rows it had to create start the way a new application's do.
        self.assertFalse(configs.get(purpose="reranking").enabled)

    def test_one_model_for_every_task_takes_only_a_listed_model(self):
        response = self.client.post(self.url, {"purpose": "all", "all-model_choice": "custom"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AIConfiguration.objects.exists())

    def test_a_task_whose_feature_is_off_is_not_listed(self):
        ApplicationFeature.objects.create(application=self.app, key="service_ops", enabled=False)
        page = self.client.get(self.url)
        self.assertNotContains(page, "<strong>ServiceOps</strong>", html=False)
        self.assertContains(page, "Not listed, because their feature is off here: ServiceOps.")

    def test_each_task_shows_what_it_spent_this_month(self):
        self.configure("chat")
        for amount in ("1.20", "0.03"):
            AIUsage.objects.create(
                application=self.app,
                actor=self.owner,
                provider="Anthropic",
                model="claude-sonnet-5",
                request_id=f"r{amount}",
                purpose="chat",
                input_tokens=1,
                output_tokens=1,
                amount=Decimal(amount),
            )
        rows = {
            row["purpose"]: row
            for _, group in self.client.get(self.url).context["groups"]
            for row in group
        }
        self.assertEqual(rows["chat"]["spent"], Decimal("1.23"))
        self.assertIsNone(rows["graph_retrieval"]["spent"])

    def test_a_missing_credential_is_said_once_naming_what_it_stops(self):
        self.configure("chat")
        self.configure("graph_retrieval")
        self.configure("reranking", enabled=False)
        page = self.client.get(self.url)
        self.assertEqual(
            page.context["blocked"], [("Claude", ["Chat conversation", "Graph retrieval"])]
        )
        self.assertContains(
            page, "The Claude credential is not set, so Chat conversation, Graph retrieval"
        )
        (self.secrets / f"claude_{self.app.pk}").write_text("sk-ant-api-test", encoding="utf-8")
        self.assertNotContains(self.client.get(self.url), "credential is not set")

    def test_prices_are_shown_as_people_write_them(self):
        self.assertEqual(ai.plain_price(Decimal("2.000000")), "2.00")
        self.assertEqual(ai.plain_price(Decimal("0.025000")), "0.025")
        self.assertEqual(ai.plain_price(Decimal("0")), "0.00")

    def test_the_page_works_with_no_task_configured(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Choose a model")
        self.assertContains(page, "No task has a model yet.")


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ReplyCapTests(TestCase):
    """Chat and graph answers were capped at 1,024 tokens in four places, unchangeable."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def configure(self, purpose, limit=None):
        return AIConfiguration.objects.create(
            application=self.app,
            purpose=purpose,
            provider="openai",
            model="gpt-5.6-sol",
            input_rate=Decimal("1"),
            output_rate=Decimal("1"),
            output_limit=limit,
        )

    def test_only_open_ended_tasks_offer_a_cap(self):
        self.assertEqual(
            set(ai.output_limits()),
            {"chat", "graph_retrieval", "plan_drafting", "serviceops_triage"},
        )

    def test_the_default_is_what_was_hard_coded(self):
        from platform_core.agent_runtime.runtime import ANSWER_TOKENS
        from platform_core.serviceops_triage import TRIAGE_OUTPUT_LIMIT

        self.assertEqual(ANSWER_TOKENS, 1024)
        self.configure("chat")
        self.assertEqual(ai.reply_cap(self.app.pk, "chat"), 1024)
        self.assertEqual(ai.reply_cap(self.app.pk, "serviceops_triage"), TRIAGE_OUTPUT_LIMIT)

    def test_a_stored_cap_out_of_range_is_ignored(self):
        for bad in (10, ai.MAX_ANSWER_TOKENS + 1):
            with self.subTest(value=bad):
                AIConfiguration.objects.all().delete()
                self.configure("chat", bad)
                self.assertEqual(ai.reply_cap(self.app.pk, "chat"), 1024)

    def test_an_owners_chat_cap_reaches_the_one_shot_adapter(self):
        self.configure("chat", 6000)
        result = ({"id": "r", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, "Answer")
        with (
            patch("platform_core.ai.completion", return_value=result) as adapter,
            patch("platform_core.ai.provider_credential", return_value="key"),
        ):
            ai.invoke_ai(self.owner, self.app.pk, "chat", "q", [{"id": "s", "excerpt": "x"}])
        self.assertEqual(adapter.call_args.kwargs["max_tokens"], 6000)

    def test_a_caller_that_names_its_cap_keeps_it(self):
        self.configure("chat", 6000)
        result = ({"id": "r", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, "Title")
        with (
            patch("platform_core.ai.completion", return_value=result) as adapter,
            patch("platform_core.ai.provider_credential", return_value="key"),
        ):
            ai.invoke_ai(self.owner, self.app.pk, "chat", "q", [], max_tokens=32)
        self.assertEqual(adapter.call_args.kwargs["max_tokens"], 32)

    def test_an_owners_cap_reaches_the_streaming_runtime_and_receipts_name_the_task(self):
        config = self.configure("graph_retrieval", 8000)
        captured = {}

        async def run(*args, max_tokens, **kwargs):
            captured["max_tokens"] = max_tokens
            return [], "Answer"

        session = SimpleNamespace(emit=lambda *a, **k: None, cancel=None)
        with (
            patch("platform_core.agent_runtime.openai_runtime.run", side_effect=run),
            patch("platform_core.ai.record_turns") as record,
        ):
            ai.stream_chat_answer(self.owner, self.app, config, "key", "q", [], [], session)
        self.assertEqual(captured["max_tokens"], 8000)
        self.assertEqual(record.call_args.args[3], "graph_retrieval")

    def test_triage_is_given_the_owners_cap(self):
        self.configure("serviceops_triage", 12000)
        self.assertEqual(ai.reply_cap(self.app.pk, "serviceops_triage"), 12000)
