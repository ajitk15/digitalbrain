"""The factory pack (`factory_pack.py`): its labels, its scoring and a run, offline.

A real run is paid and deliberate; here the one-shot call is patched and the
agent is the scripted model, so everything but the provider runs for real.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import factory_pack
from platform_core.agent_runtime import harness
from platform_core.agent_runtime.scripted import Script
from platform_core.models import AIConfiguration, AIUsage, FactoryRun, RunEvent
from platform_core.scorecard import CAREPATH_CODE, code_snapshot
from platform_core.services import record_ai_usage

from . import test_documents
from .test_code_tools import SETTINGS


class PackLabelTests(SimpleTestCase):
    """Labelled by hand; these hold the labels to the code they describe."""

    def setUp(self):
        self.pack = factory_pack.load_pack()

    def test_every_file_the_pack_names_is_in_carepath(self):
        self.assertEqual(self.pack["repository"], "documentation/demo/carepath")
        for path in factory_pack.pack_paths(self.pack):
            with self.subTest(path=path):
                self.assertTrue((CAREPATH_CODE / path).is_file())

    def test_each_check_names_a_symbol_the_target_defines_and_the_bug_is_there(self):
        for key, item in self.pack["tickets"].items():
            for kind in ("expect", "forbid"):
                for path, symbol, text in item[kind]:
                    with self.subTest(ticket=key, kind=kind, symbol=symbol):
                        self.assertIn(path, item["targets"])
                        body = factory_pack.region(
                            (CAREPATH_CODE / path).read_text(encoding="utf-8"), symbol
                        )
                        self.assertIsNotNone(body)
                        # Unfixed code fails every check: an expectation is not met
                        # yet, and what is forbidden is still there.
                        self.assertEqual(text in body, kind == "forbid")

    def test_every_ticket_says_what_a_reviewer_approved(self):
        for key, item in self.pack["tickets"].items():
            with self.subTest(ticket=key):
                self.assertTrue(item["title"] and item["change"] and item["targets"])
                self.assertTrue(item["expect"] or item["forbid"])
                self.assertFalse(set(item["needs"]) & set(item["targets"]))

    def test_a_missing_pack_is_refused_never_rebuilt(self):
        with patch.object(factory_pack, "PACK", CAREPATH_CODE / "no-such-pack.json"):
            with self.assertRaises(ValidationError):
                factory_pack.load_pack()


class ScoreTests(SimpleTestCase):
    def setUp(self):
        self.item = factory_pack.load_pack()["tickets"]["CARE-412"]
        self.path = self.item["targets"][0]
        self.original = (CAREPATH_CODE / self.path).read_text(encoding="utf-8")

    def answer(self, content):
        return json.dumps({"files": [{"file": "1", "content": content}]})

    def test_a_fix_meets_its_checks(self):
        fixed = self.original.replace(
            "limit: int = Query(default=50),",
            "limit: int = Query(default=50, le=settings.max_page_size),",
            1,
        )
        self.assertNotEqual(fixed, self.original)
        found = factory_pack.score(self.item, {self.path: self.original}, self.answer(fixed))
        self.assertTrue(found["answered"] and found["fixed"])
        self.assertEqual(found["changed"], [self.path])

    def test_an_answer_that_changes_nothing_fixes_nothing(self):
        same = factory_pack.score(
            self.item, {self.path: self.original}, self.answer(self.original)
        )
        self.assertFalse(same["fixed"])
        self.assertFalse(same["answered"])

    def test_code_that_does_not_parse_meets_no_check(self):
        broken = self.original.replace("def search_patients(", "def search_patients((")
        found = factory_pack.score(self.item, {self.path: self.original}, self.answer(broken))
        self.assertTrue(found["answered"])
        self.assertFalse(found["fixed"])
        self.assertEqual([check["met"] for check in found["expect"]], [False])

    def test_prose_is_not_an_answer(self):
        found = factory_pack.score(self.item, {self.path: self.original}, "I would clamp it.")
        self.assertFalse(found["answered"] or found["fixed"])


class VerdictTests(SimpleTestCase):
    def mode(self, fixed, checks, cost):
        return {"fixed": fixed, "checks_met": checks, "cost": cost}

    def test_the_bar(self):
        cases = [
            (self.mode(2, 50.0, "1.00"), self.mode(3, 75.0, "1.80"), "clear the bar"),
            (self.mode(2, 50.0, "1.00"), self.mode(3, 75.0, "2.50"), "over 2x"),
            (self.mode(3, 75.0, "1.00"), self.mode(2, 75.0, "1.00"), "fixed 2 tickets against 3"),
            (self.mode(2, 50.0, "1.00"), self.mode(2, 50.0, "1.20"), "no better"),
        ]
        for alone, tools, said in cases:
            with self.subTest(said=said):
                self.assertIn(said, factory_pack.verdict({"one_shot": alone, "tools": tools}))
        self.assertIn("both ways", factory_pack.verdict({"tools": cases[0][1]}))


@override_settings(**SETTINGS)
class PackRunTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.snapshot = code_snapshot(SimpleNamespace(app=self.app, people={"owner": self.owner}))
        AIConfiguration.objects.create(
            application=self.app, purpose="plan_drafting", provider="claude",
            model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
            configured_by=self.owner,
        )
        credential = patch("platform_core.ai.provider_credential", return_value="sk-ant-test")
        credential.start()
        self.addCleanup(credential.stop)
        self.pack = {
            "repository": "documentation/demo/carepath",
            "tickets": {"CARE-412": factory_pack.load_pack()["tickets"]["CARE-412"]},
        }
        self.original = self.snapshot.files.get(path="src/carepath/api/routes_patients.py").content

    def fixed(self):
        return json.dumps(
            {
                "files": [
                    {
                        "file": "1",
                        "content": self.original.replace(
                            "limit: int = Query(default=50),",
                            "limit: int = Query(default=50, le=settings.max_page_size),",
                            1,
                        ),
                    }
                ]
            }
        )

    def test_both_ways_are_asked_what_a_run_asks_and_scored(self):
        asked = {}

        def one_shot(user, app_id, phase, instructions, question, citations, receipt):
            asked.update(question=question, citations=citations, phase=phase)
            receipt.update(provider="claude", model="claude-sonnet-5", prompt_tokens=1000,
                           completion_tokens=200, request_id="r")
            # As a real call does: a receipt, which is what the pack prices from.
            record_ai_usage(
                actor=user, application_id=app_id, provider="Anthropic",
                model="claude-sonnet-5", request_id="r", purpose="plan_drafting",
                input_tokens=1000, output_tokens=200, cache_write_tokens=1000,
                amount=Decimal("0.006"),
            )
            return json.dumps({"files": []})

        script = Script(
            [("read_file", {"path": "src/carepath/api/routes_consent.py", "start_line": 1,
                            "end_line": 0}),
             ("read_file", {"path": "src/carepath/config.py", "start_line": 1, "end_line": 0})],
            self.fixed(),
        )
        with (
            patch.object(factory_pack, "load_pack", return_value=self.pack),
            patch("platform_core.code_factory_analysis.ask", side_effect=one_shot),
            patch.object(harness, "runtime_for", return_value=script),
        ):
            record = factory_pack.run(self.owner, self.app)
        self.assertEqual(asked["phase"], "implementation")
        self.assertTrue(asked["question"].startswith("Approved changes:\nClamp the page size"))
        self.assertEqual([c["title"] for c in asked["citations"]],
                         ["src/carepath/api/routes_patients.py"])
        by_mode = {row["mode"]: row for row in record["details"]}
        self.assertFalse(by_mode["one_shot"]["fixed"])
        self.assertTrue(by_mode["tools"]["fixed"])
        self.assertEqual(by_mode["tools"]["read"],
                         ["src/carepath/api/routes_consent.py", "src/carepath/config.py"])
        self.assertEqual(by_mode["tools"]["needs_seen"],
                         ["src/carepath/api/routes_consent.py", "src/carepath/config.py"])
        self.assertEqual((by_mode["tools"]["turns"], by_mode["tools"]["tool_calls"]), (2, 2))
        self.assertEqual(record["metrics"]["tools"]["fixed"], 1)
        self.assertEqual(record["metrics"]["one_shot"]["fixed"], 0)
        # Each way is what its receipts billed, not its tokens priced here.
        self.assertEqual(Decimal(by_mode["one_shot"]["cost"]), Decimal("0.006"))
        tools_bill = sum(receipt.amount for receipt in AIUsage.objects.filter(
            purpose="plan_drafting").exclude(request_id="r"))
        self.assertEqual(Decimal(by_mode["tools"]["cost"]), tools_bill)
        self.assertIn("clear the bar", factory_pack.verdict(record["metrics"]))
        # Nothing is written but the receipts: no run, no notes.
        self.assertFalse(FactoryRun.objects.exists())
        self.assertFalse(RunEvent.objects.exists())
        self.assertEqual(AIUsage.objects.filter(purpose="plan_drafting").count(), 3)
        self.assertEqual(record["snapshot"], {"repository": "carepath", "number": 1})

    def test_a_refused_call_is_a_row_not_a_crash(self):
        with (
            patch.object(factory_pack, "load_pack", return_value=self.pack),
            patch("platform_core.code_factory_analysis.ask",
                  side_effect=ValidationError("Budget used.")),
        ):
            record = factory_pack.run(self.owner, self.app, ("one_shot",))
        self.assertEqual(record["details"][0]["failure"], "Budget used.")
        self.assertEqual(record["metrics"]["one_shot"]["fixed"], 0)

    def test_an_application_without_the_code_is_refused(self):
        self.snapshot.files.filter(path="src/carepath/config.py").delete()
        with patch.object(factory_pack, "load_pack", return_value=self.pack):
            with self.assertRaises(ValidationError) as raised:
                factory_pack.run(self.owner, self.app)
        self.assertIn("Code Repos", " ".join(raised.exception.messages))


@override_settings(**SETTINGS)
class CommandTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def record(self):
        row = {
            "ticket": "CARE-412", "mode": "tools", "fixed": True, "failure": "",
            "changed": ["src/carepath/api/routes_patients.py"],
            "needs_seen": ["src/carepath/config.py"], "needs": ["a", "b"],
        }
        found = {"tickets": 1, "fixed": 1, "checks_met": 100.0, "needs_seen": 50.0,
                 "turns": 2, "tool_calls": 1, "cost": "0.01", "seconds": 1.0}
        return {"generated_at": "2026-11-21T12:00:00+00:00", "model": "claude-sonnet-5",
                "details": [row], "metrics": {"tools": found}}

    def call(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("factory_pack", *args, stdout=out)
        return out.getvalue()

    def test_it_needs_an_application_and_someone_to_charge(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.call(str(self.app.pk), "--as", "nobody")

    def test_it_reports_each_ticket_and_saves_when_asked(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.object(factory_pack, "run", return_value=self.record()) as run,
                patch.object(factory_pack, "RECORDS", Path(folder)),
            ):
                out = self.call(str(self.app.pk), "--as", "owner", "--only", "tools", "--record")
            saved = list(Path(folder).iterdir())
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].name, "2026-11-21T1200-claude-sonnet-5.json")
            self.assertEqual(json.loads(saved[0].read_text())["model"], "claude-sonnet-5")
        self.assertEqual(run.call_args.args[2], ("tools",))
        self.assertIn("ok CARE-412 tools: changed src/carepath/api/routes_patients.py", out)
        self.assertIn("tools: fixed 1/1, checks met 100.0%", out)
        self.assertIn("Run both ways to compare.", out)

    def test_a_refusal_is_a_command_error(self):
        from django.core.management.base import CommandError

        with patch.object(factory_pack, "run", side_effect=ValidationError("No snapshot.")):
            with self.assertRaises(CommandError) as raised:
                self.call(str(self.app.pk), "--as", "owner")
        self.assertIn("No snapshot.", str(raised.exception))
