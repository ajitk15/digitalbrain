"""How each agent measured against one call, shown on its page (`agent_measurements`).

Pins that a pack record is kept as numbers only and once, that its verdict is the
pack's own `verdict` read into a state, that a record made elsewhere loads only from
the folder its pack records into, that Knowledge upkeep is judged by use with its
floor, and that the agent page and the index draw the measurement - paired bars,
a trend that marks a new reference, the last days' use - for this application only,
with spend only where usage reports are on.
"""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import agent_catalog, agent_measurements
from platform_core.models import (
    MEASUREMENT_VERDICTS,
    AgentMeasurement,
    AgentStep,
    AIConfiguration,
    AIUsage,
    ApplicationFeature,
    ChatConversation,
    ChatMessage,
)

from . import test_documents


def factory_totals(fixed, checks, cost="0.10", seconds=10.0):
    return {
        "tickets": 4, "fixed": fixed, "answered": 4, "checks_met": checks,
        "needs_seen": 50.0, "cost": cost, "turns": 4, "tool_calls": 0, "seconds": seconds,
    }


def factory_record(when, one_shot, tools, model="claude-sonnet-5", pack="a" * 64):
    return {
        "generated_at": when.isoformat(timespec="seconds"),
        "provider": "claude",
        "model": model,
        "pack": pack,
        "snapshot": {"repository": "carepath", "number": 1},
        "metrics": {"one_shot": one_shot, "tools": tools},
        "details": [{"ticket": "CARE-401", "mode": "tools", "read": ["app/models.py"]}],
    }


WHEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
)
class MeasurementTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        for purpose in ("chat", "knowledge_upkeep", "code_review", "plan_drafting",
                        "serviceops_triage"):
            AIConfiguration.objects.create(
                application=self.app, purpose=purpose, provider="claude",
                model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
                configured_by=self.owner,
            )
        reachable = patch("platform_core.ai.credential_problem", return_value=None)
        reachable.start()
        self.addCleanup(reachable.stop)

    def keep(self, one_shot, tools, when=WHEN, app=None, **kwargs):
        row, _ = agent_measurements.record(
            app or self.app, "factory_pack", factory_record(when, one_shot, tools, **kwargs),
            self.owner,
        )
        return row

    # The registry and its verdicts ---------------------------------------------

    def test_every_pack_measures_an_agent_that_exists_once(self):
        keys = {agent.key for agent in agent_catalog.agents()}
        measured = [pack.agent for pack in agent_measurements.PACKS.values()]
        self.assertEqual(len(measured), len(set(measured)))
        # Every agent has a pack now: chat's was the last (chat_pack).
        self.assertEqual(set(measured), keys)
        states = {key for key, _ in MEASUREMENT_VERDICTS}
        for pack in agent_measurements.PACKS.values():
            self.assertEqual(len(pack.modes), 2)
            self.assertTrue(pack.command.startswith(f"manage.py {pack.key} "))
            self.assertEqual(bool(pack.judge), pack.paid)
        self.assertEqual(states, {"clears", "no_better", "fails", "incomplete", "too_few"})

    def test_each_packs_own_verdict_reads_as_its_state(self):
        cases = {
            "factory_pack": (
                factory_totals(1, 50.0), factory_totals(2, 60.0),
                factory_totals(1, 50.0), factory_totals(0, 40.0),
            ),
            "review_pack": tuple(
                {"changes": 4, "found": found, "noise": 0, "cost": "0.1", "turns": 4,
                 "tool_calls": 0, "seconds": 1.0}
                for found in (2, 3, 2, 1)
            ),
            "triage_pack": tuple(
                {"cases": 10, "cause_cited": cited, "cause_first": 1, "mrr": mrr,
                 "cause_in_evidence": 5, "cost": "0.1", "turns": 4, "tool_calls": 0,
                 "seconds": 1.0}
                for cited, mrr in ((5, 0.4), (6, 0.5), (5, 0.4), (4, 0.3))
            ),
        }
        cases["chat_pack"] = tuple(
            {"questions": 16, "cited_expected": cited, "cited_first": cited, "shown_expected": 16,
             "with_citation": 16, "invented": 0, "failed": 0, "cost": "0.1", "turns": 16,
             "tool_calls": 0, "seconds": 1.0}
            for cited in (10, 12, 10, 8)
        )
        cases["docs_pack"] = tuple(
            {"asked": 6, "components": components, "paragraphs": components * 2,
             "quotes": components * 2, "files_quoted": components, "dropped": 0,
             "kept_share": 100.0, "failed": 0, "cost": "0.1", "turns": 1, "tool_calls": 0,
             "seconds": 1.0}
            for components in (3, 5, 3, 2)
        )
        cases["document_review_pack"] = tuple(
            {"items": 5, "found": found, "linked": linked, "findings": 6, "dropped": 0,
             "failed": 0, "cost": "0.1", "turns": 5, "seconds": 1.0}
            for found, linked in ((2, 0), (3, 2), (2, 0), (1, 1))
        )
        for key, (base, better, same, worse) in cases.items():
            pack = agent_measurements.PACKS[key]
            with self.subTest(pack=key):
                for tools, state in ((better, "clears"), (same, "no_better"), (worse, "fails")):
                    found, text = agent_measurements.judged(
                        pack, {"one_shot": base, "tools": tools}
                    )
                    self.assertEqual(found, state, text)
                found, _ = agent_measurements.judged(pack, {"tools": better})
                self.assertEqual(found, "incomplete")

    def test_tools_that_cost_over_the_bar_do_not_clear_it(self):
        row = self.keep(factory_totals(1, 50.0, cost="0.10"),
                        factory_totals(2, 60.0, cost="0.30"))
        self.assertEqual(row.verdict, "fails")
        self.assertIn("over 2x", row.verdict_text)

    # Keeping a record -------------------------------------------------------------

    def test_a_record_is_kept_as_numbers_only_and_once(self):
        tools = {**factory_totals(2, 60.0), "note": "a model wrote this", "flag": True}
        row = self.keep(factory_totals(1, 50.0), tools)
        self.assertEqual(row.agent, "code-factory")
        self.assertEqual(row.verdict, "clears")
        self.assertNotIn("note", row.metrics["tools"])
        self.assertNotIn("flag", row.metrics["tools"])
        self.assertNotIn("details", json.dumps(row.metrics))
        self.assertEqual(row.metrics["tools"]["cost"], "0.10")
        again = self.keep(factory_totals(1, 50.0), factory_totals(2, 60.0))
        self.assertEqual(again.pk, row.pk)
        self.assertEqual(AgentMeasurement.objects.count(), 1)

    def test_a_record_made_elsewhere_loads_from_its_packs_folder_only(self):
        with tempfile.TemporaryDirectory() as folder:
            made = Path(folder) / "factory" / "2026-11-01T1200-claude-sonnet-5.json"
            made.parent.mkdir()
            made.write_text(json.dumps(factory_record(
                WHEN, factory_totals(1, 50.0), factory_totals(1, 50.0)
            )), encoding="utf-8")
            row, created = agent_measurements.load(self.app, made)
            self.assertTrue(created)
            self.assertEqual((row.verdict, row.file), ("no_better", made.name))
            self.assertFalse(agent_measurements.load(self.app, made)[1])
            stray = Path(folder) / "elsewhere.json"
            stray.write_text(made.read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaises(ValidationError):
                agent_measurements.load(self.app, stray)
            broken = Path(folder) / "factory" / "broken.json"
            broken.write_text("{", encoding="utf-8")
            with self.assertRaises(ValidationError):
                agent_measurements.load(self.app, broken)

    def test_record_keeps_what_the_command_recorded(self):
        record = factory_record(WHEN, factory_totals(1, 50.0), factory_totals(2, 60.0))
        record["details"] = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "factory" / "made.json"
            with (
                patch("platform_core.factory_pack.run", return_value=record),
                patch("platform_core.factory_pack.save", return_value=path),
            ):
                out = StringIO()
                call_command(
                    "factory_pack", str(self.app.pk), "--as", "owner", "--record", stdout=out
                )
        self.assertIn("Kept for its agent's page", out.getvalue())
        row = AgentMeasurement.objects.get()
        self.assertEqual(
            (row.agent, row.verdict, row.file), ("code-factory", "clears", "made.json")
        )
        self.assertEqual(row.recorded_by, self.owner)

    def test_the_list_command_loads_and_lists(self):
        self.keep(factory_totals(1, 50.0), factory_totals(2, 60.0))
        out = StringIO()
        call_command("agent_measurements", str(self.app.pk), stdout=out)
        self.assertIn("code-factory", out.getvalue())
        self.assertIn("Clears the bar", out.getvalue())

    # Knowledge upkeep, by use -------------------------------------------------------

    def upkeep(self, drafted, rules):
        found = {
            "drafted": {"decided": drafted[0], "accepted": drafted[1], "as_drafted": drafted[2]},
            "rules": {"decided": rules[0], "accepted": rules[1], "as_drafted": 0},
        }
        with patch("platform_core.learning_draft.report", return_value=found):
            return agent_measurements.upkeep(self.app, 30)

    def test_upkeep_is_too_few_under_its_floor_and_judged_above_it(self):
        self.assertEqual(self.upkeep((9, 9, 9), (10, 1, 0))["state"], "too_few")
        self.assertEqual(self.upkeep((10, 6, 5), (10, 4, 0))["state"], "clears")
        self.assertEqual(self.upkeep((10, 4, 3), (10, 4, 0))["state"], "no_better")
        self.assertEqual(self.upkeep((10, 2, 1), (10, 4, 0))["state"], "fails")
        self.assertEqual(self.upkeep((10, 2, 1), (0, 0, 0))["state"], "incomplete")
        measured = self.upkeep((10, 6, 5), (10, 4, 0))
        row, _ = agent_measurements.record(self.app, "learning_upkeep", measured)
        self.assertEqual((row.agent, row.verdict), ("draft-a-fix", "clears"))
        self.assertEqual(row.metrics["drafted"]["accept_rate"], 60.0)
        self.assertIn("60.0%", row.verdict_text)

    # Drawing ------------------------------------------------------------------------

    def test_the_bars_are_scaled_judged_and_leave_cost_out_when_asked(self):
        row = self.keep(factory_totals(1, 50.0, seconds=10.0),
                        factory_totals(2, 75.0, seconds=30.0))
        chart = agent_measurements.comparison(row)
        rows = {item["label"]: item for item in chart["rows"]}
        fixed = rows["Tickets fixed"]
        self.assertEqual([bar["length"] for bar in fixed["bars"]],
                         [agent_measurements.BAR_SPAN / 4, agent_measurements.BAR_SPAN / 2])
        self.assertEqual(fixed["bars"][1]["shown"], "2 of 4")
        self.assertEqual(fixed["direction"], "better")
        self.assertEqual(rows["Checks met"]["bars"][1]["length"],
                         agent_measurements.BAR_SPAN * 0.75)
        self.assertEqual(rows["Seconds"]["direction"], "worse")
        self.assertEqual(rows["Turns"]["direction"], "")
        self.assertIn("Cost", rows)
        hidden = agent_measurements.comparison(row, show_cost=False)
        self.assertNotIn("Cost", {item["label"] for item in hidden["rows"]})

    def test_the_trend_needs_two_runs_and_marks_a_new_reference(self):
        first = self.keep(factory_totals(1, 50.0), factory_totals(2, 60.0))
        self.assertIsNone(agent_measurements.trend([first]))
        second = self.keep(factory_totals(1, 40.0), factory_totals(1, 80.0),
                           when=WHEN + timedelta(days=1))
        third = self.keep(factory_totals(1, 40.0), factory_totals(1, 80.0),
                          when=WHEN + timedelta(days=2), model="claude-opus-5")
        chart = agent_measurements.trend([first, second, third])
        self.assertEqual([p["value"] for p in chart["agent"]["points"]], [0.6, 0.8, 0.8])
        self.assertEqual([p["value"] for p in chart["base"]["points"]], [0.5, 0.4, 0.4])
        self.assertEqual(len(chart["references"]), 1)
        self.assertGreater(chart["references"][0]["x"], chart["agent"]["points"][1]["x"])

    def test_use_counts_runs_calls_and_problems_from_this_agents_steps(self):
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="t"
        )
        message = ChatMessage.objects.create(
            application=self.app, user=self.owner, conversation=conversation, role="assistant",
            body="", sequence=2,
        )
        for sequence, outcome, ms in ((1, "found", 10), (2, "budget", 30), (3, "found", 20)):
            AgentStep.objects.create(
                application=self.app, message=message, purpose="chat", sequence=sequence,
                tool="search_knowledge", outcome=outcome, duration_ms=ms,
            )
        found = agent_measurements.use(agent_catalog.get("chat"), self.app, 30)
        self.assertEqual(found["runs"], 1)
        self.assertEqual(found["calls_per_run"], 3.0)
        self.assertEqual(found["problem_share"], 33.3)
        self.assertEqual(found["median_ms"], 20)
        self.assertIsNone(found["spend"])

    # Pages --------------------------------------------------------------------------

    def page(self, key):
        return self.client.get(reverse("agent", args=[self.app.pk, key]))

    def test_an_agent_not_measured_here_says_how_to_measure_it(self):
        page = self.page("code-factory")
        self.assertContains(page, "Not measured here yet")
        self.assertContains(page, "manage.py factory_pack &lt;application id&gt;")
        self.assertContains(self.page("chat"), "manage.py chat_pack &lt;application id&gt;")
        self.assertContains(self.page("draft-a-fix"), "manage.py learning_upkeep")

    def test_a_measured_agent_draws_its_verdict_bars_and_trend_for_this_application_only(self):
        self.keep(factory_totals(0, 10.0), factory_totals(0, 10.0), app=self.other)
        self.keep(factory_totals(1, 50.0), factory_totals(2, 60.0))
        self.keep(factory_totals(1, 50.0), factory_totals(3, 70.0), when=WHEN + timedelta(days=1))
        page = self.page("code-factory")
        self.assertContains(page, "Clears the bar.")
        self.assertContains(page, 'class="measure-bar measure-agent"')
        self.assertContains(page, "3 of 4")
        self.assertContains(page, "Checks met by recorded run")
        self.assertContains(page, "2 recorded runs")
        self.assertNotContains(page, "style=")
        index = self.client.get(reverse("agents", args=[self.app.pk]))
        self.assertContains(index, "Measured: Clears the bar")
        self.assertNotContains(index, "No pack yet")
        self.assertContains(index, "Not measured yet")

    def test_spend_is_shown_only_where_usage_reports_are_on(self):
        AIUsage.objects.create(
            application=self.app, actor=self.owner, purpose="plan_drafting", provider="claude",
            model="claude-sonnet-5", request_id="r1", input_tokens=10, output_tokens=10,
            amount=Decimal("0.25"),
        )
        self.keep(factory_totals(1, 50.0), factory_totals(2, 60.0))
        page = self.page("code-factory")
        self.assertContains(page, "$0.2500 over 1 receipt")
        self.assertContains(page, ">Cost</text>")
        ApplicationFeature.objects.create(application=self.app, key="usage_reports", enabled=False)
        page = self.page("code-factory")
        self.assertNotContains(page, "over 1 receipt")
        self.assertNotContains(page, ">Cost</text>")

    def test_a_viewer_sees_the_measurement_and_a_stranger_learns_nothing(self):
        self.keep(factory_totals(1, 50.0), factory_totals(2, 60.0))
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertContains(self.page("code-factory"), "Clears the bar.")
