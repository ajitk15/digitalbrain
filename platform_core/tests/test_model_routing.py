"""Model routing: chat's searches on a cheaper model, its answer on chat's own.

With Chat searching set up and switched on (`ai.routing_config`), chat's agent
runs its tool turns on that model, its words never shown, and chat's model then
answers in one turn more over the evidence that still verifies. Each turn is
receipted at its own model's price under its own task; admission reserves every
turn at the dearer rates. What is streamed is in `test_messages_runtime`.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core import ai, gateway
from platform_core.agent_runtime import harness
from platform_core.agent_runtime.harness import WRAP_UP, run_agent
from platform_core.agent_runtime.scripted import Script
from platform_core.agent_runtime.tools import ToolScope
from platform_core.models import AIConfiguration, AIUsage
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_agent_harness import GATEWAY

QUESTION = "How long do refunds take?"


def searching(app, owner, model="claude-haiku-5-5", provider="claude", enabled=True):
    return AIConfiguration.objects.create(
        application=app, purpose="chat_search", provider=provider, model=model,
        enabled=enabled, input_rate=Decimal("0.10"), output_rate=Decimal("0.50"),
        configured_by=owner,
    )


@override_settings(AI_GATEWAY=GATEWAY)
class RoutingTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )
        self.config = AIConfiguration.objects.create(
            application=self.app, purpose="chat", provider="claude", model="claude-sonnet-5",
            input_rate=Decimal("3"), output_rate=Decimal("15"), configured_by=self.owner,
        )

    def run_with(self, script, turns=4, **kwargs):
        with patch.object(harness, "runtime_for", return_value=script):
            return run_agent(
                user=self.owner, app=self.app, config=self.config, token="unused",
                purpose="chat", scope=ToolScope(user_id=self.owner.pk, app_id=self.app.pk),
                question=QUESTION, citations=[], history=[], emit=lambda *args: None,
                max_tokens=1024, prompt_characters=200, turns=turns, **kwargs,
            )

    def script(self):
        return Script(
            [("search_knowledge", {"query": "refund deadline"})],
            "The searching model's own answer, never shown.",
            then=["Thirty days [1]."],
        )

    def test_the_searches_run_on_the_cheaper_model_and_chats_model_answers(self):
        searching(self.app, self.owner)
        script = self.script()
        result = self.run_with(script)
        searched, answered = script.calls
        self.assertEqual((searched["model"], answered["model"]),
                         ("claude-haiku-5-5", "claude-sonnet-5"))
        self.assertTrue(searched["given"])
        # The answer turn has no tools, is asked the question as it was asked, and
        # reads what the searches found, verified.
        self.assertEqual(answered["given"], [])
        self.assertEqual(answered["question"], QUESTION)
        self.assertNotIn(WRAP_UP, answered["question"])
        self.assertIn(str(self.source.pk), answered["evidence"])
        self.assertEqual(result.answer, "Thirty days [1].")
        self.assertEqual(result.routed, "claude-haiku-5-5")
        self.assertEqual([c["id"] for c in result.citations], [str(self.source.pk)])

    def test_each_turn_is_receipted_at_its_own_models_price_under_its_own_task(self):
        searching(self.app, self.owner)
        self.run_with(self.script())
        receipts = AIUsage.objects.order_by("created_at")
        self.assertEqual(
            sorted((r.purpose, r.model, r.amount) for r in receipts),
            sorted([
                # 100 in at $0.10/M, 20 out at $0.50/M, for each of the two searching turns.
                ("chat_search", "claude-haiku-5-5", Decimal("0.00002000")),
                ("chat_search", "claude-haiku-5-5", Decimal("0.00002000")),
                # 100 in at $3/M, 20 out at $15/M.
                ("chat", "claude-sonnet-5", Decimal("0.00060000")),
            ]),
        )

    def test_admission_reserves_every_turn_at_the_dearer_rates(self):
        route = searching(self.app, self.owner)
        route.input_rate, route.output_rate = Decimal("5"), Decimal("1")
        route.save()
        reserved = []
        original = gateway.admitted

        def admitted(user, app, config, *args, **kwargs):
            reserved.append((config.model, config.input_rate, config.output_rate))
            return original(user, app, config, *args, **kwargs)

        with patch.object(gateway, "admitted", admitted):
            self.run_with(self.script())
        self.assertEqual(reserved, [("claude-sonnet-5", Decimal("5"), Decimal("15"))])
        # Nothing stored changed: the dearer rates are a copy.
        self.config.refresh_from_db()
        self.assertEqual(self.config.input_rate, Decimal("3"))

    def test_off_or_not_asked_for_one_model_does_both(self):
        searching(self.app, self.owner, enabled=False)
        script = Script([("search_knowledge", {"query": "refund"})], "Thirty days [1].")
        self.run_with(script)
        self.assertEqual([c["model"] for c in script.calls], ["claude-sonnet-5"])
        AIConfiguration.objects.filter(purpose="chat_search").update(enabled=True)
        script = Script([("search_knowledge", {"query": "refund"})], "Thirty days [1].")
        result = self.run_with(script, routing=False)
        self.assertEqual([c["model"] for c in script.calls], ["claude-sonnet-5"])
        self.assertEqual(result.routed, "")

    def test_too_few_turns_for_a_kept_answer_turn_is_not_routed(self):
        searching(self.app, self.owner)
        script = Script("From the evidence.")
        result = self.run_with(script, turns=1)
        self.assertEqual([c["model"] for c in script.calls], ["claude-sonnet-5"])
        self.assertEqual(result.routed, "")

    def test_another_provider_or_the_same_model_routes_nothing_and_says_why(self):
        route = searching(self.app, self.owner, model="gpt-5.6-luna", provider="openai")
        self.assertIsNone(ai.routing_config(self.app, self.config))
        self.assertIn("same provider", ai.routing_problem(self.app))
        route.provider, route.model = "claude", "claude-sonnet-5"
        route.save()
        self.assertIsNone(ai.routing_config(self.app, self.config))
        self.assertIn("same model", ai.routing_problem(self.app))
        route.model = "claude-haiku-5-5"
        route.save()
        self.assertEqual(ai.routing_config(self.app, self.config), route)
        self.assertEqual(ai.routing_problem(self.app), "")

    def test_an_agent_with_its_own_instructions_is_never_routed(self):
        searching(self.app, self.owner)
        script = Script([("search_knowledge", {"query": "refund"})], '{"answer": 1}')
        self.run_with(script, instructions="Answer in JSON.")
        self.assertEqual([c["model"] for c in script.calls], ["claude-sonnet-5"])

    def test_the_task_page_says_why_it_routes_nothing(self):
        searching(self.app, self.owner, model="claude-sonnet-5")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("ai-task", args=[self.app.pk, "chat_search"]))
        self.assertContains(page, "same model as Chat conversation")


class DearerTests(SimpleTestCase):
    def test_the_higher_of_each_rate(self):
        config = AIConfiguration(model="a", input_rate=Decimal("3"), output_rate=Decimal("15"))
        other = AIConfiguration(model="b", input_rate=Decimal("4"), output_rate=Decimal("1"))
        found = ai.dearer(config, other)
        self.assertEqual((found.model, found.input_rate, found.output_rate),
                         ("a", Decimal("4"), Decimal("15")))
        self.assertEqual(config.input_rate, Decimal("3"))
