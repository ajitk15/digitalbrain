"""Every paid model call passes the gateway: one rule per purpose, a slot and a reservation.

Phase 4 of the scale work (`gateway.py`). What it adds over the checks that
were already there: two calls at the budget cap no longer both start, one
application cannot hold every call a provider allows, and a purpose's rules
are written once.
"""

import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.http import Http404
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from digitalbrain.configuration import load_config
from platform_core import budget, gateway
from platform_core.ai import invoke_ai
from platform_core.models import (
    AI_PURPOSES,
    AIAdmission,
    AIBudget,
    AIConfiguration,
    AIUsage,
    ApplicationGrant,
)
from platform_core.workbench import add_knowledge

from . import test_documents

GATEWAY = {
    "max_calls_per_application": 4,
    "max_calls_per_provider": 16,
    "slot_wait_seconds": 0,
    "admission_minutes": 15,
}


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    AI_GATEWAY=GATEWAY,
)
class GatewayTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Architecture", "Service Alpha sends messages to Queue Beta."
        )
        for app in (self.app, self.other):
            for purpose in ("chat", "graph_generation"):
                AIConfiguration.objects.create(
                    application=app,
                    purpose=purpose,
                    provider="claude",
                    model="claude-sonnet-5",
                    input_rate=Decimal("2"),
                    output_rate=Decimal("10"),
                    configured_by=self.owner,
                )
        patcher = patch("platform_core.secrets.application_secret", return_value="key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def result(self):
        return {"id": f"req-{AIUsage.objects.count()}", "usage": {
            "prompt_tokens": 10, "completion_tokens": 5}}, "Answer"

    def ask(self, purpose="chat", user=None):
        citations = [{"id": str(self.source.pk), "title": "A", "excerpt": "x"}]
        return invoke_ai(user or self.owner, self.app.pk, purpose, "Q?", citations)

    def in_flight(self, app=None, provider="claude", reserved="0", expires=None):
        return AIAdmission.objects.create(
            application_id=(app or self.app).pk,
            purpose="chat",
            provider=provider,
            actor=self.owner,
            reserved=Decimal(reserved),
            expires_at=expires or timezone.now() + timedelta(minutes=15),
        )

    def test_every_purpose_has_one_rule(self):
        self.assertEqual(set(gateway.PURPOSES), {key for key, _ in AI_PURPOSES})

    def test_an_unknown_purpose_is_refused(self):
        with self.assertRaises(ValidationError):
            gateway.authorize(self.owner, self.app.pk, "anything")

    def test_a_viewer_may_not_spend_on_graph_generation(self):
        with self.assertRaises(PermissionDenied):
            gateway.authorize(self.viewer, self.app.pk, "graph_generation")
        gateway.authorize(self.viewer, self.app.pk, "chat")

    def test_a_call_holds_a_slot_while_it_runs_and_leaves_a_receipt(self):
        during = []

        def answer(*args, **kwargs):
            during.append(AIAdmission.objects.filter(application_id=self.app.pk).count())
            return self.result()

        with patch("platform_core.claude_agents.completion", side_effect=answer):
            self.assertEqual(self.ask(), "Answer")
        self.assertEqual(during, [1])
        self.assertFalse(AIAdmission.objects.exists())
        self.assertEqual(AIUsage.objects.filter(application=self.app).count(), 1)

    def test_a_failed_call_gives_its_slot_back(self):
        with (
            patch(
                "platform_core.claude_agents.completion", side_effect=ValidationError("down")
            ),
            self.assertRaises(ValidationError),
        ):
            self.ask()
        self.assertFalse(AIAdmission.objects.exists())

    @override_settings(AI_GATEWAY={**GATEWAY, "max_calls_per_application": 1})
    def test_a_full_application_waits_then_is_told_nothing_was_sent(self):
        self.in_flight()
        with (
            patch("platform_core.claude_agents.completion") as call,
            self.assertLogs("platform_core.gateway", "WARNING"),
            self.assertRaises(gateway.ProviderBusy) as busy,
        ):
            self.ask()
        call.assert_not_called()
        self.assertIn("nothing was charged", " ".join(busy.exception.messages))

    @override_settings(AI_GATEWAY={**GATEWAY, "max_calls_per_application": 1})
    def test_another_applications_calls_do_not_fill_this_ones_slots(self):
        self.in_flight(app=self.other)
        with patch("platform_core.claude_agents.completion", return_value=self.result()):
            self.assertEqual(self.ask(), "Answer")

    @override_settings(AI_GATEWAY={**GATEWAY, "max_calls_per_provider": 1})
    def test_the_provider_limit_spans_applications(self):
        self.in_flight(app=self.other)
        with (
            patch("platform_core.claude_agents.completion") as call,
            self.assertLogs("platform_core.gateway", "WARNING"),
            self.assertRaises(gateway.ProviderBusy),
        ):
            self.ask()
        call.assert_not_called()
        self.in_flight(app=self.other, provider="openai")

    @override_settings(AI_GATEWAY={**GATEWAY, "max_calls_per_application": 1})
    def test_a_slot_left_by_a_dead_process_expires_and_is_swept(self):
        self.in_flight(expires=timezone.now() - timedelta(seconds=1))
        with patch("platform_core.claude_agents.completion", return_value=self.result()):
            self.assertEqual(self.ask(), "Answer")
        self.in_flight(expires=timezone.now() - timedelta(seconds=1))
        with self.assertLogs("platform_core.gateway", "INFO"):
            gateway.sweep()
        self.assertFalse(AIAdmission.objects.exists())

    def test_a_call_in_flight_holds_its_cost_against_the_cap(self):
        AIBudget.objects.create(application=self.app, monthly_limit=Decimal("1"))
        AIUsage.objects.create(
            application=self.app, actor=self.owner, provider="Anthropic",
            model="claude-sonnet-5", request_id="spent", input_tokens=1, output_tokens=1,
            amount=Decimal("0.50"), estimated=True,
        )
        self.in_flight(reserved="0.60")
        with (
            patch("platform_core.claude_agents.completion") as call,
            self.assertRaises(budget.BudgetExceeded) as refused,
        ):
            self.ask()
        call.assert_not_called()
        self.assertIn("held by calls in flight", " ".join(refused.exception.messages))
        AIAdmission.objects.all().delete()
        with patch("platform_core.claude_agents.completion", return_value=self.result()):
            self.assertEqual(self.ask(), "Answer")

    def test_a_reservation_is_the_most_the_call_can_cost(self):
        config = AIConfiguration.objects.get(application=self.app, purpose="chat")
        # 3,000 characters is at most 1,001 tokens at $2/M, and 1,000 reply tokens at $10/M.
        self.assertEqual(gateway.estimate(config, 3000, 1000), Decimal("0.01200200"))

    def test_no_budget_reserves_nothing(self):
        reserved = []

        def answer(*args, **kwargs):
            reserved.extend(AIAdmission.objects.values_list("reserved", flat=True))
            return self.result()

        with patch("platform_core.claude_agents.completion", side_effect=answer):
            self.ask()
        self.assertEqual(reserved, [Decimal("0")])

    def test_a_grant_lost_during_the_call_keeps_the_receipt_and_not_the_answer(self):
        def answer(*args, **kwargs):
            ApplicationGrant.objects.filter(user=self.owner, application=self.app).delete()
            return self.result()

        with patch("platform_core.claude_agents.completion", side_effect=answer):
            with self.assertRaises(Http404):
                self.ask()
        self.assertEqual(AIUsage.objects.filter(application=self.app).count(), 1)
        self.assertFalse(AIAdmission.objects.exists())


    @override_settings(AI_GATEWAY={**GATEWAY, "max_calls_per_application": 1})
    def test_a_streamed_answer_waits_for_a_slot_too(self):
        from types import SimpleNamespace

        from platform_core.ai import stream_chat_answer

        config = AIConfiguration.objects.get(application=self.app, purpose="chat")
        session = SimpleNamespace(emit=lambda *a: None, cancel=None, stopped=False)
        self.in_flight()
        with (
            patch("platform_core.agent_runtime.claude_runtime.run") as run,
            self.assertLogs("platform_core.gateway", "WARNING"),
            self.assertRaises(gateway.ProviderBusy),
        ):
            stream_chat_answer(self.owner, self.app, config, "key", "Q?", [], [], session)
        run.assert_not_called()

    def test_a_provider_failure_in_a_streamed_answer_is_logged_by_class(self):
        from types import SimpleNamespace

        from django.core.exceptions import ValidationError

        from platform_core.ai import stream_chat_answer

        config = AIConfiguration.objects.get(application=self.app, purpose="chat")
        session = SimpleNamespace(emit=lambda *a: None, cancel=None, stopped=False)
        with (
            patch(
                "platform_core.agent_runtime.claude_runtime.run",
                side_effect=RuntimeError("rate limited for Q? and its sources"),
            ),
            self.assertLogs("platform_core.ai", "WARNING") as logged,
            self.assertRaises(ValidationError),
        ):
            stream_chat_answer(self.owner, self.app, config, "key", "Q?", [], [], session)
        record = logged.records[0]
        self.assertEqual(record.event, "chat_answer_failed")
        self.assertEqual(record.exception_type, "RuntimeError")
        self.assertEqual(record.provider, config.provider)
        self.assertNotIn("sources", " ".join(logged.output))
        self.assertFalse(AIAdmission.objects.exists())

    def test_an_embedding_request_holds_a_slot_and_leaves_a_receipt(self):
        import numpy

        from platform_core import embeddings

        AIConfiguration.objects.create(
            application=self.app, purpose="embeddings", provider="openai",
            model="text-embedding-3-small", input_rate=Decimal("0.02"),
            output_rate=Decimal("0"), configured_by=self.owner,
        )
        during = []

        def vectors(token, model, texts):
            during.append(AIAdmission.objects.filter(purpose="embeddings").count())
            unit = numpy.zeros(embeddings.DIMENSIONS, dtype="float32")
            unit[0] = 1
            return [unit for _ in texts], "emb-1", 7

        with patch.object(embeddings, "request", side_effect=vectors):
            embeddings.embed(self.owner, self.app, [("Queue Beta", "question", None, 0)])
        self.assertEqual(during, [1])
        self.assertFalse(AIAdmission.objects.exists())
        self.assertTrue(AIUsage.objects.filter(purpose="embeddings", request_id="emb-1").exists())

    def test_queue_status_counts_calls_in_flight_by_provider(self):
        import io

        from django.core.management import call_command

        self.in_flight()
        self.in_flight(app=self.other)
        self.in_flight(provider="openai")
        self.in_flight(expires=timezone.now() - timedelta(seconds=1))
        self.assertEqual(gateway.in_flight(), {"claude": 2, "openai": 1})
        output = io.StringIO()
        call_command("queue_status", stdout=output)
        self.assertIn("AI calls in flight: claude 2, openai 1", output.getvalue())

    def test_embeddings_are_not_an_answer_purpose(self):
        with self.assertRaises(ValidationError):
            invoke_ai(self.owner, self.app.pk, "embeddings", "Q?", [{"id": "x"}])


class GatewaySettingTests(SimpleTestCase):
    BASE = 'mode = "development"\nhosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(self.BASE + body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_limits_must_be_positive_whole_numbers(self):
        for body in (
            "[ai_gateway]\nmax_calls_per_application = 0\n",
            '[ai_gateway]\nmax_calls_per_provider = "16"\n',
            "[ai_gateway]\nunknown = 1\n",
        ):
            with self.subTest(body=body), self.assertRaises(ImproperlyConfigured):
                self.load(body)
        loaded = self.load("[ai_gateway]\nmax_calls_per_application = 2\n")
        self.assertEqual(loaded["ai_gateway"]["max_calls_per_application"], 2)
