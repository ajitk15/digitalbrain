"""A monthly AI budget refuses new paid calls once this month's spend reaches it."""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import budget
from platform_core.ai import chat_configuration, invoke_ai
from platform_core.graph_ai import enrich_graph
from platform_core.graphs import build_graph, sources_for
from platform_core.models import AIBudget, AIConfiguration, AIUsage, AuditEvent
from platform_core.workbench import add_knowledge

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BudgetTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Architecture", "Service Alpha sends messages to Queue Beta."
        )
        for purpose in ("chat", "graph_generation"):
            AIConfiguration.objects.create(
                application=self.app,
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

    def spend(self, amount, app=None, when=None):
        receipt = AIUsage.objects.create(
            application=app or self.app,
            actor=self.owner,
            provider="Anthropic",
            model="claude-sonnet-5",
            request_id=f"r-{AIUsage.objects.count()}",
            input_tokens=1,
            output_tokens=1,
            amount=Decimal(amount),
            estimated=True,
        )
        if when:
            AIUsage.objects.filter(pk=receipt.pk).update(created_at=when)

    def cap(self, limit):
        AIBudget.objects.create(application=self.app, monthly_limit=Decimal(limit))

    def result(self):
        return {"id": "req", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, "Answer"

    def ask(self):
        citations = [{"id": str(self.source.pk), "title": "A", "excerpt": "x"}]
        return invoke_ai(self.owner, self.app.pk, "chat", "Q?", citations)

    def test_no_budget_means_no_cap(self):
        self.spend("1000")
        with patch("platform_core.claude_agents.completion", return_value=self.result()):
            self.assertEqual(self.ask(), "Answer")

    def test_a_reached_budget_refuses_before_any_call(self):
        self.cap("5")
        self.spend("5")
        with patch("platform_core.claude_agents.completion") as call:
            with self.assertRaises(budget.BudgetExceeded) as refused:
                self.ask()
        call.assert_not_called()
        self.assertIn("$5.00 of $5.00", " ".join(refused.exception.messages))
        with self.assertRaises(budget.BudgetExceeded):
            chat_configuration(self.owner, self.app.pk)

    def test_under_budget_calls_go_ahead(self):
        self.cap("5")
        self.spend("4.99")
        with patch("platform_core.claude_agents.completion", return_value=self.result()):
            self.assertEqual(self.ask(), "Answer")

    def test_last_month_and_other_applications_do_not_count(self):
        self.cap("5")
        self.spend("50", when=budget.month_start() - timedelta(seconds=1))
        self.spend("50", app=self.other)
        self.assertEqual(budget.spent(self.app.pk), Decimal("0"))
        with patch("platform_core.claude_agents.completion", return_value=self.result()):
            self.assertEqual(self.ask(), "Answer")

    def test_extraction_keeps_what_it_paid_for_when_the_cap_is_hit(self):
        second = add_knowledge(
            self.owner, self.app.pk, "Gamma", "Service Gamma uses Service Alpha."
        )
        entries = list(sources_for(self.app.pk))
        data, quality = build_graph(entries, len(entries))
        first_id = str(entries[0].pk)
        content = {str(self.source.pk): self.source.content, str(second.pk): second.content}

        def extract(user, app_id, purpose, question, citations, **options):
            source_id = citations[0]["id"]
            if source_id != first_id:
                raise budget.BudgetExceeded("This application has used its AI budget.")
            text = content[source_id].rstrip(".")
            subject, obj = (
                ("Service Alpha", "Queue Beta")
                if source_id == str(self.source.pk)
                else ("Service Gamma", "Service Alpha")
            )
            return json.dumps(
                {
                    "relationships": [
                        {
                            "subject": subject,
                            "subject_type": "Component",
                            "relation": "depends on",
                            "object": obj,
                            "object_type": "Component",
                            "source_id": source_id,
                            "quote": text,
                        }
                    ]
                }
            )

        config = AIConfiguration.objects.get(purpose="graph_generation")
        with patch("platform_core.ai.invoke_ai", side_effect=extract):
            data, quality = enrich_graph(self.app.pk, config, entries, data, quality)
        self.assertEqual(quality["semantic_relationships"], 1)
        self.assertTrue(any("stopped early" in w for w in quality["warnings"]))

    def test_extraction_with_nothing_yet_fails_with_the_reason(self):
        self.cap("1")
        self.spend("1")
        entries = list(sources_for(self.app.pk))
        data, quality = build_graph(entries, len(entries))
        config = AIConfiguration.objects.get(purpose="graph_generation")
        with self.assertRaises(ValidationError):
            enrich_graph(self.app.pk, config, entries, data, quality)

    def test_an_owner_sets_and_removes_it_and_it_is_audited(self):
        url = reverse("ai-settings", args=[self.app.pk])
        response = self.client.post(url, {"purpose": "budget", "budget-monthly_limit": "25"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AIBudget.objects.get().monthly_limit, Decimal("25"))
        self.spend("10")
        page = self.client.get(url)
        self.assertContains(page, "$10.00 of $25.00 this month")
        self.client.post(url, {"purpose": "budget", "budget-monthly_limit": ""})
        self.assertFalse(AIBudget.objects.exists())
        self.assertEqual(AuditEvent.objects.filter(action="ai.budget_set").count(), 2)

    def test_a_negative_budget_is_refused(self):
        url = reverse("ai-settings", args=[self.app.pk])
        response = self.client.post(url, {"purpose": "budget", "budget-monthly_limit": "-1"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AIBudget.objects.exists())

    def test_only_an_owner_can_set_it(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("ai-settings", args=[self.app.pk])
        response = self.client.post(url, {"purpose": "budget", "budget-monthly_limit": "999"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AIBudget.objects.exists())

    def test_embeddings_stop_at_the_cap_too(self):
        from platform_core import embeddings

        AIConfiguration.objects.create(
            application=self.app,
            purpose="embeddings",
            provider="openai",
            model="text-embedding-3-small",
            input_rate=Decimal("0.02"),
            output_rate=Decimal("0"),
            configured_by=self.owner,
        )
        self.cap("1")
        self.spend("1")
        with patch("platform_core.embeddings.request") as request:
            with self.assertRaises(budget.BudgetExceeded):
                embeddings.embed(self.owner, self.app, [("text", "question", None, None)])
        request.assert_not_called()

    def test_the_usage_page_shows_it(self):
        self.cap("10")
        self.spend("10")
        response = self.client.get(reverse("usage", args=[self.app.pk]))
        self.assertContains(response, "reached")


class MonthTests(TestCase):
    def test_the_month_starts_on_the_first_at_midnight(self):
        moment = timezone.now().replace(year=2026, month=10, day=9, hour=15)
        self.assertEqual(budget.month_start(moment).isoformat()[:19], "2026-10-01T00:00:00")
