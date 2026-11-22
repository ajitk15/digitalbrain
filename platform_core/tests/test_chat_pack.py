"""The chat agent's pack (`chat_pack`): with tools and with one call, on benchmark questions.

A scripted model stands in for the provider (`agent_runtime.scripted`), so each
case runs the harness a real chat answer runs - one call is a run allowed one
turn and given no tools - and the scoring, the receipts and the record are read
from what really happened.
"""

import json
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase

from platform_core import agent_measurements, chat_pack
from platform_core.agent_runtime import harness
from platform_core.agent_runtime.scripted import Script
from platform_core.models import AgentMeasurement, AIConfiguration, AIUsage, BenchmarkQuestion
from platform_core.workbench import add_knowledge

from . import test_documents


class Model:
    """One call answers from the seeded evidence; with tools it searches first.

    `alone` and `searching` are what each way writes; a fresh `Script` per run,
    because a script remembers the calls it was given.
    """

    def __init__(self, alone, searching):
        self.alone, self.searching = alone, searching

    def run(self, *args, tools=None, **kwargs):
        script = Script(*(self.searching if tools else [self.alone]))
        return script.run(*args, tools=tools, **kwargs)


class ChatPackTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.policy = add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )
        self.other_doc = add_knowledge(
            self.owner, self.app.pk, "Holiday rota", "Refund desk staff rota for the holidays."
        )
        AIConfiguration.objects.create(
            application=self.app, purpose="chat", provider="claude", model="claude-sonnet-5",
            input_rate=Decimal("3"), output_rate=Decimal("15"), configured_by=self.owner,
        )
        BenchmarkQuestion.objects.create(
            application=self.app, question="How long do refund requests last?",
            expected="Refund policy",
        )
        credential = patch("platform_core.ai.provider_credential", return_value="unused")
        credential.start()
        self.addCleanup(credential.stop)

    def evidence(self, *entries):
        return [
            {"id": str(e.pk), "title": e.title, "digest": e.digest, "excerpt": e.content}
            for e in entries
        ]

    def measured(self, model, evidence):
        with (
            patch.object(harness, "runtime_for", return_value=model),
            patch.object(chat_pack, "seeded", return_value=evidence),
        ):
            return chat_pack.run(self.owner, self.app)

    def test_tools_that_find_the_source_one_call_missed_clear_the_bar(self):
        search = [("search_knowledge", {"query": "refund requests expire"})]
        record = self.measured(
            Model("The rota does not say [1].", [search, "Thirty days [2]."]),
            self.evidence(self.other_doc),
        )
        alone, tools = record["metrics"]["one_shot"], record["metrics"]["tools"]
        self.assertEqual((alone["cited_expected"], tools["cited_expected"]), (0, 1))
        self.assertEqual((alone["shown_expected"], tools["shown_expected"]), (0, 1))
        self.assertEqual((alone["turns"], tools["turns"]), (1, 2))
        self.assertEqual(tools["tool_calls"], 1)
        # Cost is read from the receipts each way wrote, at the entered rates.
        self.assertEqual(Decimal(alone["cost"]), Decimal("0.00060000"))
        self.assertEqual(Decimal(tools["cost"]), Decimal("0.00120000"))
        self.assertEqual(AIUsage.objects.filter(purpose="chat").count(), 3)
        self.assertFalse(record["details"][0]["seeded_expected"])
        self.assertEqual(chat_pack.verdict(record["metrics"]).split(":")[0],
                         "Tools clear the bar")
        self.assertEqual(record["pack"], chat_pack.pack_digest(chat_pack.questions(self.app)))

    def test_a_marker_is_read_as_the_page_numbers_it_not_as_a_search_reply_did(self):
        """A search reply numbers its passages from [1]; the page numbers the run's
        whole list, seeded evidence first. An answer citing the search reply's [1]
        is shown beside the seeded source, and the pack counts what is shown."""
        search = [("search_knowledge", {"query": "refund requests expire"})]
        record = self.measured(
            Model("The rota [1].", [search, "Thirty days [1]."]),
            self.evidence(self.other_doc),
        )
        tools = record["metrics"]["tools"]
        self.assertEqual((tools["shown_expected"], tools["cited_expected"]), (1, 0))

    def test_a_marker_to_nothing_is_counted_and_a_wrong_first_citation_is_not_first(self):
        record = self.measured(
            Model("See [3] and [1].", ["The rota [2], and thirty days [1]."]),
            self.evidence(self.policy, self.other_doc),
        )
        alone, tools = record["metrics"]["one_shot"], record["metrics"]["tools"]
        self.assertEqual((alone["invented"], alone["cited_expected"], alone["cited_first"]),
                         (1, 1, 1))
        self.assertEqual((tools["invented"], tools["cited_expected"], tools["cited_first"]),
                         (0, 1, 0))

    def test_out_of_turns_the_turn_kept_back_answers_and_every_turn_costs(self):
        search = [("search_knowledge", {"query": "refund"})]
        record = self.measured(
            Model("Thirty days [1].", [search] * 6),
            self.evidence(self.policy),
        )
        tools = record["metrics"]["tools"]
        # The turn kept back answers from what was read: a wrap-up is an answer,
        # and the turns before it are counted from their receipts.
        self.assertEqual((tools["failed"], tools["cited_expected"]), (0, 1))
        self.assertEqual(tools["turns"], harness.TURNS + 1)

    def test_a_provider_failure_is_an_unanswered_question(self):
        class Down:
            def run(self, *args, **kwargs):
                raise RuntimeError("the prompt was secret")

        record = self.measured(Down(), self.evidence(self.policy))
        self.assertEqual(record["metrics"]["tools"]["failed"], 1)
        self.assertEqual(record["details"][0]["failure"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(record))

    def test_no_benchmark_questions_is_said_before_anything_is_spent(self):
        BenchmarkQuestion.objects.all().delete()
        with self.assertRaises(ValidationError):
            chat_pack.run(self.owner, self.app)
        self.assertFalse(AIUsage.objects.exists())

    def test_the_command_records_the_pack_for_the_chat_agents_page(self):
        with tempfile.TemporaryDirectory() as folder:
            records = Path(folder) / "chat"
            with (
                patch.object(chat_pack, "RECORDS", records),
                patch.object(harness, "runtime_for",
                             return_value=Model("Thirty days [1].", ["Thirty days [1]."])),
                patch.object(chat_pack, "seeded", return_value=self.evidence(self.policy)),
            ):
                out = StringIO()
                call_command("chat_pack", str(self.app.pk), "--as", "owner", "--record",
                             stdout=out)
            saved = json.loads(next(records.glob("*.json")).read_text(encoding="utf-8"))
        self.assertIn("Tools are no better than one call", out.getvalue())
        self.assertNotIn("Thirty days", json.dumps(saved["metrics"]))
        row = AgentMeasurement.objects.get()
        self.assertEqual((row.agent, row.pack, row.verdict), ("chat", "chat_pack", "no_better"))
        self.assertEqual(row.metrics["tools"]["questions"], 1)
        self.assertEqual(
            agent_measurements.main_share(agent_measurements.PACKS["chat_pack"],
                                          row.metrics["tools"]),
            1.0,
        )

    def test_the_command_refuses_a_limit_out_of_range_and_an_unknown_user(self):
        for args in (["--as", "owner", "--limit", "0"], ["--as", "nobody"]):
            with self.subTest(args=args), self.assertRaises(CommandError):
                call_command("chat_pack", str(self.app.pk), *args, stdout=StringIO())
