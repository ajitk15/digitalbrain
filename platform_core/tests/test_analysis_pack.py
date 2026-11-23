"""The gap analysis pack (`analysis_pack`): planted-bug identifiers a plan's items
cite, analysed with tools and with one call.

The answer key is the scorecard's own. A run here uses a patched provider for the
one call and a scripted model for the agent, so the evidence, `analyse`,
`collect_items` and the receipts are the ones a Code Factory run uses.
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import analysis_pack
from platform_core.agent_runtime import harness
from platform_core.agent_runtime.scripted import Script
from platform_core.models import AgentMeasurement, AIConfiguration
from platform_core.workbench import add_knowledge

from . import test_code_factory

KEY = {"CARE-431": ["C-08", "F-03"]}
MATRIX = "| C-08 | Exports honour a regulatory hold | Finding F-03 open |"


class KeyTests(SimpleTestCase):
    def test_an_identifier_counts_only_whole(self):
        self.assertTrue(analysis_pack.named("C-08", "control C-08 failed"))
        self.assertFalse(analysis_pack.named("C-08", "control C-080 and AC-08"))

    def test_the_key_is_the_scorecards_and_every_planted_ticket_has_identifiers(self):
        key = analysis_pack.load_key()
        self.assertIn("CARE-431", key)
        self.assertTrue(all(key.values()))


class Searcher:
    """With tools: searches the control matrix and cites what it found."""

    def run(self, model, question, citations, *args, tools=None, **kwargs):
        item = {"category": "functional", "rubric": "", "title": "Hold not enforced",
                "explanation": "C-08 says exports honour a hold.", "severity": "high",
                "evidence": [{"source_id": str(len(citations) + 1), "quote": "C-08"}]}
        script = Script([("search_knowledge", {"query": "regulatory hold control"})],
                        json.dumps({"items": [item]}))
        return script.run(model, question, citations, *args, tools=tools, **kwargs)


ONE_CALL = json.dumps({"items": [{
    "category": "stated", "rubric": "", "title": "Honour the hold",
    "explanation": "The ticket asks for it.", "severity": "medium", "evidence": [],
}]})


@override_settings(GAP_ANALYSIS_AGENT_TOOLS=True)
class RunTests(TestCase):
    setUp = test_code_factory.PipelineTests.setUp

    def measured(self):
        add_knowledge(self.owner, self.app.pk, "Control matrix", MATRIX)
        add_knowledge(
            self.owner, self.app.pk, "CARE-431 - Export ignores a regulatory hold",
            "An export went out while the record was under a regulatory hold.",
            source="https://team.atlassian.net/browse/KAN-8",
        )
        with (
            patch.object(analysis_pack, "load_key", return_value=KEY),
            patch("platform_core.ai.invoke_ai", return_value=ONE_CALL),
            patch("platform_core.ai.provider_credential", return_value="unused"),
            patch.object(harness, "runtime_for", return_value=Searcher()),
        ):
            return analysis_pack.run(self.owner, self.app)

    def test_one_call_cites_nothing_and_tools_cite_what_they_found(self):
        record = self.measured()
        alone, tools = record["metrics"]["one_shot"], record["metrics"]["tools"]
        self.assertEqual((alone["cited"], tools["cited"]), (0, 2))
        self.assertEqual((alone["evidenced"], tools["evidenced"]), (0, 1))
        # Neither identifier was in what it was handed: the tools found both.
        self.assertEqual((tools["offered"], tools["found_more"]), (0, 2))
        self.assertEqual(alone["expected"], 2)
        self.assertFalse(record["code"])
        self.assertTrue(analysis_pack.verdict(record["metrics"]).startswith(
            "Tools clear the bar"
        ))
        self.assertEqual(record["pack"], analysis_pack.pack_digest(KEY))

    def test_a_ticket_that_is_not_here_is_said_and_costs_nothing(self):
        with (
            patch.object(analysis_pack, "load_key", return_value={"CARE-999": ["X-01"]}),
            patch("platform_core.ai.invoke_ai") as call,
        ):
            record = analysis_pack.run(self.owner, self.app, ("one_shot",))
        call.assert_not_called()
        self.assertEqual(record["details"][0]["failure"],
                         "No imported ticket here is titled CARE-999.")

    def test_the_command_records_it_for_the_gap_analysis_agents_page(self):
        record = self.measured()
        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.object(analysis_pack, "RECORDS", Path(folder)),
                patch.object(analysis_pack, "run", return_value=record),
            ):
                out = StringIO()
                call_command("analysis_pack", str(self.app.pk), "--as", "owner", "--record",
                             stdout=out)
        self.assertIn("tools: cited 2/2 (100.0%)", out.getvalue())
        row = AgentMeasurement.objects.get()
        self.assertEqual((row.agent, row.pack, row.verdict),
                         ("gap-analysis", "analysis_pack", "clears"))

    def test_it_needs_code_factory_configured(self):
        AIConfiguration.objects.filter(purpose="plan_drafting").delete()
        with self.assertRaises(ValidationError):
            analysis_pack.run(self.owner, self.app)
