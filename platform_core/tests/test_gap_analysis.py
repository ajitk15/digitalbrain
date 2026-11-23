"""Code Factory's gap analysis with tools (`gap_analysis_agent_tools`).

With the switch on, analysis is a harness run in the `gap_analysis` tool scope:
the source tools and the snapshot tools, each call held to `plan_drafting`'s
rule. A passage its searches return is numbered after the evidence it was handed,
and an item may cite it by that number - only once it verifies against its live
source, and labelled as found by search. Triage and design are one call each, as
before. A scripted model stands in for the provider.
"""

import json
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import TestCase, override_settings

from platform_core import code_factory_analysis
from platform_core.agent_runtime import code_tools, harness, registry
from platform_core.agent_runtime.scripted import Script
from platform_core.agent_runtime.tools import (
    FETCH_SOURCE,
    SEARCH_KNOWLEDGE,
    ToolScope,
    rule_for,
    run_tool,
)
from platform_core.code_factory import start_run
from platform_core.code_factory_analysis import execute
from platform_core.models import AgentStep, AIUsage, KnowledgeEntry, PlanItem, RunEvent
from platform_core.workbench import add_knowledge

from . import test_code_factory

QUOTE = test_code_factory.QUOTE

POLICY = "Retry policy: a client retries at most three times, then backs off (R-02)."


class Analyst:
    """The analysis agent: searches for the retry policy, then answers with an
    item citing the graph's passage and whatever number the search was shown at
    (`cite`, worked out from the evidence it was handed)."""

    def __init__(self, cite=None, between=()):
        self.cite = cite
        self.between = list(between)
        self.script = None

    def run(self, model, question, citations, *args, tools=None, **kwargs):
        number = self.cite or str(len(citations) + 1)
        item = {
            "category": "non_functional", "rubric": "availability",
            "title": "Retries are not capped", "explanation": "The policy caps retries.",
            "severity": "high",
            "evidence": [{"source_id": "1", "quote": QUOTE},
                         {"source_id": number, "quote": "at most three times"}],
        }
        self.script = Script(
            [("search_knowledge", {"query": "retry policy backs off"}), *self.between],
            json.dumps({"items": [item]}),
        )
        return self.script.run(model, question, citations, *args, tools=tools, **kwargs)


@override_settings(GAP_ANALYSIS_AGENT_TOOLS=True)
class GapAnalysisTests(TestCase):
    # Borrowed, not inherited or imported by name: either would run its tests here.
    setUp = test_code_factory.PipelineTests.setUp
    answers = test_code_factory.PipelineTests.answers

    def run_with(self, analyst):
        self.policy = add_knowledge(self.owner, self.app.pk, "Retry policy", POLICY)
        run = start_run(self.owner, self.app.pk, self.ticket)
        triage, _, design = self.answers()
        design = json.dumps({"items": [{"id": 0, "change_summary": "Cap retries",
                                        "targets": ["queue.py"]}]})
        with (
            patch("platform_core.ai.invoke_ai", side_effect=[triage, design]),
            patch("platform_core.ai.provider_credential", return_value="unused"),
            patch.object(harness, "runtime_for", return_value=analyst),
        ):
            execute(run)
        run.refresh_from_db()
        return run

    def test_an_item_may_cite_a_passage_its_search_found_once_it_verifies(self):
        analyst = Analyst()
        run = self.run_with(analyst)
        self.assertEqual(run.status, "awaiting_review")
        item = PlanItem.objects.get(plan=run.plan)
        graph, found = item.citations
        self.assertEqual(graph["origin"], "knowledge_graph")
        self.assertEqual((found["origin"], found["origin_label"], found["title"]),
                         ("knowledge_source", "Found by search", "Retry policy"))
        self.assertEqual(found["id"], str(self.policy.pk))
        self.assertIn("at most three times", found["excerpt"])
        # It was given the source tools and the snapshot's, never a review's diff.
        self.assertEqual(
            analyst.script.given,
            sorted(["search_knowledge", "fetch_source", "list_files", "read_file",
                    "search_code", "find_symbol", "callers", "tests_for"]),
        )
        phase = run.phases.get(name="analysis")
        self.assertEqual((phase.citations_verified, phase.citations_rejected), (2, 0))
        self.assertEqual(phase.status, "ok")
        # Each read is a note under the phase and a step of this run's analysis.
        self.assertTrue(RunEvent.objects.filter(
            run=run, phase="analysis", message__startswith="Gap analysis read: search_knowledge"
        ).exists())
        step = AgentStep.objects.get(run=run)
        self.assertEqual((step.phase, step.tool, step.outcome),
                         ("analysis", "search_knowledge", "found"))
        # Spent under plan_drafting: two analysis turns, triage and design.
        self.assertEqual(set(AIUsage.objects.values_list("purpose", flat=True)),
                         {"plan_drafting"})

    def test_a_found_passage_that_no_longer_verifies_cannot_be_cited(self):
        def restrict():
            KnowledgeEntry.objects.filter(title="Retry policy").update(sensitivity="restricted")

        run = self.run_with(Analyst(between=[restrict]))
        item = PlanItem.objects.get(plan=run.plan)
        self.assertEqual([c["origin"] for c in item.citations], ["knowledge_graph"])
        phase = run.phases.get(name="analysis")
        self.assertEqual((phase.citations_verified, phase.citations_rejected), (1, 1))

    def test_a_passage_found_after_the_first_twenty_four_may_be_cited(self):
        # `AgentResult.found` stops at 24 passages; a run that read more had every
        # later one refused although it verified. Six searches of five first.
        for group in range(6):
            for n in range(5):
                add_knowledge(self.owner, self.app.pk, f"Filler {group}-{n}",
                              f"Section {n} of folio{group} covers nothing in particular.")
        searches = [("search_knowledge", {"query": f"folio{group}", "limit": 8})
                    for group in range(6)]

        class Later(Analyst):
            def run(self, model, question, citations, *args, tools=None, **kwargs):
                self.cite = str(len(citations) + 31)
                return super().run(model, question, citations, *args, tools=tools, **kwargs)

        analyst = Later()
        original = Script.__init__

        def searches_first(script, steps, answer, *a, **k):
            original(script, [*searches, *steps], answer, *a, **k)

        with patch.object(Script, "__init__", searches_first):
            run = self.run_with(analyst)
        item = PlanItem.objects.get(plan=run.plan)
        self.assertEqual([c["origin"] for c in item.citations],
                         ["knowledge_graph", "knowledge_source"])
        self.assertEqual(item.citations[1]["id"], str(self.policy.pk))
        phase = run.phases.get(name="analysis")
        self.assertEqual((phase.citations_verified, phase.citations_rejected), (2, 0))

    def test_a_number_it_was_never_shown_is_refused(self):
        run = self.run_with(Analyst(cite="99"))
        phase = run.phases.get(name="analysis")
        self.assertEqual((phase.citations_verified, phase.citations_rejected), (1, 1))

    def test_off_analysis_is_one_call_as_before(self):
        with override_settings(GAP_ANALYSIS_AGENT_TOOLS=False):
            run = start_run(self.owner, self.app.pk, self.ticket)
            with (
                patch("platform_core.ai.invoke_ai", side_effect=self.answers()) as call,
                patch.object(harness, "runtime_for") as runtime,
            ):
                execute(run)
        runtime.assert_not_called()
        self.assertEqual(call.call_count, 3)
        self.assertFalse(AgentStep.objects.exists())


class ScopeTests(TestCase):
    setUp = test_code_factory.PipelineTests.setUp

    def test_gap_analysis_is_held_to_plan_draftings_rule_with_tools_of_its_own(self):
        self.assertEqual(rule_for("gap_analysis"), "plan_drafting")
        def names(purpose):
            scope = ToolScope(self.owner.pk, self.app.pk, purpose)
            return {spec.name for spec in registry.for_scope(scope)}

        # The implementation, test and review agents keep exactly their tools.
        self.assertNotIn(SEARCH_KNOWLEDGE.name, names("plan_drafting"))
        self.assertEqual(names("gap_analysis") - names("plan_drafting"),
                         {SEARCH_KNOWLEDGE.name, FETCH_SOURCE.name})
        self.assertNotIn(code_tools.READ_DIFF.name, names("gap_analysis"))

    def test_someone_who_may_not_draft_plans_cannot_search_through_it(self):
        scope = ToolScope(self.viewer.pk, self.app.pk, "gap_analysis")
        recorder = type("R", (), {"calls": 0, "budget": 5, "exhausted": False, "steps": [],
                                  "revoked": None, "record": lambda self, r: []})()
        with self.assertRaises((PermissionDenied, Http404)):
            run_tool(SEARCH_KNOWLEDGE, scope, {"query": "retry"}, recorder)

    def test_one_call_and_tools_answer_from_the_same_numbered_evidence(self):
        cited = [{"id": "x", "title": "Policy", "origin_label": "Knowledge graph v1",
                  "excerpt": "e"}]
        shown = code_factory_analysis.numbered_evidence(cited)
        self.assertEqual((shown[0]["id"], shown[0]["title"]),
                         ("1", "[Knowledge graph v1] Policy"))
