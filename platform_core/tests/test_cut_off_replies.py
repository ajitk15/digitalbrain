"""A reply that ran out of output budget says so, and the budget is one owner-set cap.

A live run's work order used 2,145 output tokens against a 2,048 limit. The
reply stopped mid-sentence, the JSON never closed, and the run said only "did
not return usable JSON" - which points at the model, when the cause was the
budget.
"""

import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.code_factory import (
    MAX_OUTPUT_LIMIT,
    MIN_OUTPUT_LIMIT,
    OUTPUT_LIMIT,
    ask,
    finish_phase,
    output_limit,
    start_phase,
)
from platform_core.models import AIConfiguration, AuditEvent

from . import test_code_factory_delivery as delivery


@override_settings(**delivery.SETTINGS)
class CutOffReplyTests(TestCase):
    def setUp(self):
        delivery.DeliveryGateTests.setUp(self)

    def failed(self, spent):
        phase = start_phase(self.run, "work_order")
        finish_phase(
            phase,
            "failed",
            time.monotonic(),
            error="Work order did not return usable JSON.",
            usage={"completion_tokens": spent, "prompt_tokens": 10},
        )
        phase.refresh_from_db()
        return phase.error

    def test_a_reply_at_its_limit_is_said_to_be_cut_off(self):
        error = self.failed(OUTPUT_LIMIT)
        self.assertIn("most likely cut off", error)
        self.assertIn(f"{OUTPUT_LIMIT:,}-token limit", error)

    def test_a_short_reply_is_not(self):
        self.assertEqual(self.failed(300), "Work order did not return usable JSON.")

    def test_the_message_says_where_the_limit_is_changed(self):
        error = self.failed(OUTPUT_LIMIT)
        self.assertIn("including the model's thinking", error)
        self.assertIn("AI settings, under Code Factory", error)


@override_settings(**delivery.SETTINGS)
class OwnerSetLimitTests(TestCase):
    """One cap for every agent; an owner may change it, and blank is the default."""

    def setUp(self):
        delivery.DeliveryGateTests.setUp(self)
        self.url = reverse("ai-settings", args=[self.app.pk])

    def configure(self, limit):
        AIConfiguration.objects.update_or_create(
            application=self.app,
            purpose="plan_drafting",
            defaults={
                "provider": "claude",
                "model": "claude-sonnet-5",
                "input_rate": 3,
                "output_rate": 15,
                "output_limit": limit,
            },
        )

    def post(self, limit):
        values = {
            "model_choice": "claude:claude-sonnet-5",
            "provider": "claude",
            "model": "",
            "enabled": "on",
            "input_rate": "3",
            "output_rate": "15",
            "output_limit": limit,
        }
        return self.client.post(
            self.url,
            {"purpose": "plan_drafting", **{f"plan_drafting-{k}": v for k, v in values.items()}},
        )

    def test_without_a_setting_every_agent_gets_the_default(self):
        self.assertEqual(OUTPUT_LIMIT, 32000)
        self.assertEqual(output_limit(self.app.pk), OUTPUT_LIMIT)

    def test_every_agent_is_given_the_owners_cap(self):
        self.configure(24000)
        for phase in ("triage", "analysis", "work_order", "implementation", "review"):
            with self.subTest(phase=phase), patch(
                "platform_core.ai.invoke_ai", return_value="{}"
            ) as invoke:
                ask(self.owner, self.app.pk, phase, "i", "q", [], {})
                self.assertEqual(invoke.call_args.kwargs["max_tokens"], 24000)

    def test_a_stored_value_out_of_range_is_ignored_not_trusted(self):
        for bad in (10, MAX_OUTPUT_LIMIT + 1, None):
            with self.subTest(value=bad):
                self.configure(bad)
                self.assertEqual(output_limit(self.app.pk), OUTPUT_LIMIT)

    def test_the_cut_off_message_uses_the_owners_cap(self):
        self.configure(20000)
        phase = start_phase(self.run, "work_order")
        finish_phase(
            phase,
            "failed",
            time.monotonic(),
            error="Work order did not return usable JSON.",
            usage={"completion_tokens": 20000},
        )
        phase.refresh_from_db()
        self.assertIn("20,000-token limit", phase.error)

    def test_the_screen_offers_one_cap_and_saves_it(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'name="plan_drafting-output_limit"')
        self.assertContains(page, "Blank uses the default, 32,000.")
        # Only Code Factory has agents to cap.
        self.assertNotContains(page, 'name="chat-output_limit"')
        self.assertEqual(self.post(40000).status_code, 302)
        config = AIConfiguration.objects.get(application=self.app, purpose="plan_drafting")
        self.assertEqual(config.output_limit, 40000)
        self.assertEqual(
            AuditEvent.objects.filter(action="ai.configured").latest("pk").details["output_limit"],
            40000,
        )
        # Blank goes back to the default.
        self.assertEqual(self.post("").status_code, 302)
        config.refresh_from_db()
        self.assertIsNone(config.output_limit)

    def test_a_cap_outside_the_range_is_refused_on_the_form(self):
        for bad in (MIN_OUTPUT_LIMIT - 1, MAX_OUTPUT_LIMIT + 1):
            with self.subTest(value=bad):
                self.assertEqual(self.post(bad).status_code, 200)
        self.assertFalse(
            AIConfiguration.objects.filter(
                application=self.app, purpose="plan_drafting", output_limit__isnull=False
            ).exists()
        )

    def test_only_an_owner_can_change_it(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.post(40000).status_code, 403)

    def test_ai_settings_lists_the_everyday_features_first(self):
        page = self.client.get(self.url).content.decode()
        labels = [
            "Chat conversation",
            "Code Factory",
            "ServiceOps",
            "Graph generation",
            "Graph retrieval",
            "Conversation titles",
        ]
        positions = [page.index(f"<strong>{label}</strong>") for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Code Factory drafting", page)
        self.assertNotIn("ServiceOps triage", page)
