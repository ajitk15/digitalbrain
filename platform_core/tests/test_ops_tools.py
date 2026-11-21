"""ServiceOps triage's tools and agent (agentic plan, Phase 3), and the triage pack.

The tools read a real operations graph; the agent is the scripted model
(`agent_runtime.scripted`), so what triage reads, cites, keeps and is measured
against runs for real with no provider.
"""

import json
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from platform_core import ops_graph, serviceops_triage, triage_pack
from platform_core.agent_runtime import harness, ops_tools
from platform_core.agent_runtime.ops_tools import NOT_AN_INCIDENT
from platform_core.agent_runtime.scripted import Script
from platform_core.agent_runtime.tools import (
    FETCH_SOURCE,
    SEARCH_KNOWLEDGE,
    CitationRecorder,
    ToolScope,
    run_tool,
)
from platform_core.models import (
    AgentStep,
    AIConfiguration,
    AIUsage,
    ApplicationGrant,
    KnowledgeEntry,
    TriageHypothesis,
    TriageRun,
    TriageVerdict,
)
from platform_core.workbench import add_knowledge

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    AI_GATEWAY={
        "max_calls_per_application": 4,
        "max_calls_per_provider": 16,
        "slot_wait_seconds": 0,
        "admission_minutes": 15,
    },
)
CAUSE = "Worker pool exhaustion stalls the queue until the pool is resized."


@override_settings(**SETTINGS)
class Fixture(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.config = AIConfiguration.objects.create(
            application=self.app, purpose="serviceops_triage", provider="claude",
            model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
            configured_by=self.owner,
        )
        self.current = self.incident("Queue timeout")
        self.prior = self.incident(
            "Queue timeout earlier", state="Resolved", close_notes="Restarted worker safely."
        )
        self.note = add_knowledge(
            self.owner, self.app.pk, "Worker pool note", f"Capacity notes.\n{CAUSE}\n"
        )
        for patcher in (
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
            patch("platform_core.ai.provider_credential", return_value="sk-ant-test"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def incident(self, title, *, state="New", close_notes="", app=None, extra=""):
        content = (
            f"{title}\n\nType: Incident\nNumber: \nPriority: \nState: {state}\n"
            f"Service: Orders\nCI: api-1\nClose notes: {close_notes}\n\n"
            f"Queue workers time out.{extra}"
        )
        return add_knowledge(
            self.owner, (app or self.app).pk, title, content,
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D1",
        )

    def confirm(self, incident, cause, quote=CAUSE):
        run = TriageRun.objects.create(
            application=self.app, incident=incident, requested_by=self.owner,
            incident_digest=incident.digest, pack_digest="p", status="completed",
        )
        hypothesis = TriageHypothesis.objects.create(
            run=run, rank=1, statement="Pool", next_step="Inspect the pool",
            citations=[{"id": str(cause.pk), "quote": quote}],
        )
        verdict = TriageVerdict.objects.create(
            hypothesis=hypothesis, user=self.owner, verdict="accepted", actual_cause=cause,
            confirmed_at=timezone.now(),
        )
        ops_graph.confirm_cause(self.app, incident, cause, verdict, quote)
        return verdict

    def scope(self, user=None, purpose="serviceops_triage"):
        return ToolScope(user_id=(user or self.owner).pk, app_id=self.app.pk, purpose=purpose)

    def call(self, spec, args, scope=None):
        recorder = CitationRecorder(scope or self.scope())
        return run_tool(spec, recorder.scope, args, recorder), recorder


class OpsToolTests(Fixture):
    def test_another_incidents_neighbourhood_is_read_and_cited(self):
        text, recorder = self.call(ops_tools.INCIDENT_CONTEXT, {"incident_id": str(self.prior.pk)})
        self.assertIn("Around Queue timeout earlier", text)
        self.assertIn(str(self.current.pk), text)
        self.assertNotIn(f"id={self.prior.pk}", text)
        self.assertEqual(recorder.steps[0]["outcome"], "found")

    def test_only_this_applications_incidents_are_read(self):
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        foreign = self.incident("Foreign outage", app=self.other)
        for value in (str(foreign.pk), str(self.note.pk), "not-an-id", ""):
            with self.subTest(value=value):
                text, _ = self.call(ops_tools.INCIDENT_CONTEXT, {"incident_id": value})
                self.assertEqual(text, NOT_AN_INCIDENT)
        KnowledgeEntry.objects.filter(pk=self.prior.pk).update(sensitivity="restricted")
        text, _ = self.call(ops_tools.INCIDENT_CONTEXT, {"incident_id": str(self.prior.pk)})
        self.assertEqual(text, NOT_AN_INCIDENT)

    def test_an_incidents_own_confirmed_cause_is_never_shown_for_it(self):
        self.confirm(self.current, self.note)
        pack, _ = serviceops_triage.evidence_pack(self.app, self.current)
        self.assertIn("confirmed_cause", [row["kind"] for row in pack])
        withheld, _ = serviceops_triage.evidence_pack(self.app, self.current, own_confirmed=False)
        self.assertNotIn("confirmed_cause", [row["kind"] for row in withheld])
        text, _ = self.call(ops_tools.INCIDENT_CONTEXT, {"incident_id": str(self.current.pk)})
        self.assertNotIn("[confirmed_cause]", text)
        self.assertNotIn(CAUSE, text)

    def test_published_knowledge_is_searched_by_its_quotes_and_charges_nobody(self):
        found = [{"id": str(self.note.pk), "title": "Worker pool note", "digest": self.note.digest,
                  "excerpt": "Graph v1: pool stalls queue", "quote": CAUSE}]
        with patch("platform_core.graph_ai.graph_citations", return_value=found) as asked:
            text, recorder = self.call(ops_tools.SEARCH_PUBLISHED, {"query": "worker pool"})
        self.assertIn(CAUSE, text)
        self.assertFalse(asked.call_args.kwargs["spend"])
        self.assertEqual(recorder.found_citations()[0]["excerpt"], CAUSE)
        with patch("platform_core.graph_ai.graph_citations", side_effect=ValidationError("none")):
            text, _ = self.call(ops_tools.SEARCH_PUBLISHED, {"query": "worker pool"})
        self.assertIn("No knowledge is published", text)

    def test_what_triage_reads_is_minimized_and_what_it_cites_is_not(self):
        secret = add_knowledge(
            self.owner, self.app.pk, "Pager note",
            "Call ops on-call@example.com when the pool fills.",
        )
        text, recorder = self.call(FETCH_SOURCE, {"source_id": str(secret.pk)})
        self.assertIn("[email]", text)
        self.assertNotIn("on-call@example.com", text)
        self.assertIn("on-call@example.com", recorder.found_citations()[0]["excerpt"])
        # Chat's reading is unchanged.
        chat, _ = self.call(FETCH_SOURCE, {"source_id": str(secret.pk)}, self.scope(purpose="chat"))
        self.assertIn("on-call@example.com", chat)

    def test_a_record_id_is_never_redacted_as_a_telephone_number(self):
        """An id holding a run of digits read as a phone number, and could not be cited."""
        from platform_core.agent_runtime.tools import shown_to_model

        text = "id=a8dd5e5f-6133-4336-8417-d180b776055d call +44 118 496 0142 now"
        shown = shown_to_model("serviceops_triage", text)
        self.assertIn("id=a8dd5e5f-6133-4336-8417-d180b776055d", shown)
        self.assertNotIn("496 0142", shown)
        self.assertIn("[phone]", shown)
        self.assertEqual(shown_to_model("chat", text), text)

    def test_triage_tools_are_held_to_triages_rule(self):
        """A viewer may read knowledge in chat but may not triage, so may not read
        through triage's tools either."""
        with self.assertRaises(PermissionDenied):
            self.call(SEARCH_KNOWLEDGE, {"query": "pool"}, self.scope(user=self.viewer))
        text, _ = self.call(
            SEARCH_KNOWLEDGE, {"query": "pool"}, self.scope(user=self.viewer, purpose="chat")
        )
        self.assertIn("Worker pool note", text)


class TriageAgentTests(Fixture):
    def answer(self, cited, quote, also=None):
        citations = [{"id": str(cited.pk), "quote": quote}]
        if also is not None:
            citations.append({"id": str(also[0].pk), "quote": also[1]})
        return json.dumps(
            {
                "hypotheses": [
                    {
                        "title": "Worker pool exhausted",
                        "statement": "The pool may be too small.",
                        "next_step": "Inspect the pool size",
                        "counter_evidence": "",
                        "citations": citations,
                    }
                ]
            }
        )

    def triage(self, script=None, answer=None):
        run = serviceops_triage.queue_run(self.owner, self.app.pk, self.current)
        with (
            patch.object(harness, "runtime_for", return_value=script),
            patch("platform_core.ai.invoke_ai", return_value=answer) as one_shot,
        ):
            serviceops_triage.execute_run(run)
        run.refresh_from_db()
        return run, one_shot

    def test_off_by_default_triage_asks_once_over_its_pack(self):
        run, one_shot = self.triage(answer=self.answer(self.prior, "Restarted worker safely."))
        self.assertEqual(one_shot.call_count, 1)
        self.assertEqual(run.hypotheses.count(), 1)
        self.assertNotIn("agent_read", [row["kind"] for row in run.evidence])

    @override_settings(SERVICEOPS_TRIAGE_AGENT_TOOLS=True)
    def test_the_agent_reads_further_and_may_cite_what_it_read(self):
        # What it read, beside the precedent its pack already held: a note alone
        # is one uncorroborated source, which the scorer rightly withholds.
        script = Script(
            [("search_knowledge", {"query": "worker pool exhaustion"})],
            self.answer(self.note, CAUSE, also=(self.prior, "Restarted worker safely.")),
        )
        run, one_shot = self.triage(script)
        one_shot.assert_not_called()
        self.assertEqual(script.given, sorted(
            ["search_knowledge", "fetch_source", "incident_context", "search_published_knowledge"]
        ))
        self.assertTrue(script.instructions.endswith(serviceops_triage.TOOL_GUIDANCE))
        hypothesis = run.hypotheses.get()
        self.assertEqual(
            [citation["id"] for citation in hypothesis.citations],
            [str(self.note.pk), str(self.prior.pk)],
        )
        # Everything the search returned that the pack did not already hold.
        read = [row for row in run.evidence if row["kind"] == "agent_read"]
        self.assertIn(str(self.note.pk), [row["id"] for row in read])
        held = {(row["id"], row["excerpt"]) for row in run.evidence if row["kind"] != "agent_read"}
        self.assertFalse(held & {(row["id"], row["excerpt"]) for row in read})
        self.assertEqual(
            list(AgentStep.objects.filter(triage=run).values_list("tool", "outcome")),
            [("search_knowledge", "found")],
        )
        model = next(step for step in run.phases if step["name"] == "model")
        self.assertIn(
            f"1 read(s) over 2 turn(s), {len(read)} new evidence item(s)", model["detail"]
        )
        self.assertEqual(AIUsage.objects.filter(purpose="serviceops_triage").count(), 2)
        # The brief re-checks what the agent read, like every other source.
        self.assertTrue(serviceops_triage.run_still_verified(run, self.owner))
        KnowledgeEntry.objects.filter(pk=self.note.pk).update(content="Rewritten.")
        self.assertFalse(serviceops_triage.run_still_verified(run, self.owner))

    @override_settings(SERVICEOPS_TRIAGE_AGENT_TOOLS=True)
    def test_a_quote_the_agent_never_read_is_still_refused(self):
        script = Script(self.answer(self.note, CAUSE))
        run, _ = self.triage(script)
        self.assertEqual(run.hypotheses.count(), 0)

    @override_settings(SERVICEOPS_TRIAGE_AGENT_TOOLS=True)
    def test_a_next_step_that_would_change_something_is_still_dropped(self):
        answer = json.loads(self.answer(self.note, CAUSE))
        answer["hypotheses"][0]["next_step"] = "Restart the worker pool"
        script = Script([("search_knowledge", {"query": "worker pool"})], json.dumps(answer))
        run, _ = self.triage(script)
        self.assertEqual(run.hypotheses.count(), 0)

    def test_an_answer_that_read_further_is_not_reused_for_one_that_did_not(self):
        first, _ = self.triage(answer=self.answer(self.prior, "Restarted worker safely."))
        with override_settings(SERVICEOPS_TRIAGE_AGENT_TOOLS=True):
            script = Script(self.answer(self.prior, "Restarted worker safely."))
            second, _ = self.triage(script)
        self.assertNotEqual(first.pack_digest, second.pack_digest)
        self.assertEqual(script.max_turns, serviceops_triage.TOOL_TURNS)


class TriagePackTests(Fixture):
    def setUp(self):
        super().setUp()
        self.confirm(self.current, self.note)

    def test_a_case_withholds_the_incidents_own_confirmed_cause(self):
        self.assertEqual(triage_pack.cases(self.app), [(self.current, self.note)])
        prompt = {}

        def one_shot(user, app_id, purpose, question, citations, **options):
            prompt["evidence"] = " ".join(c["excerpt"] for c in citations)
            options["receipt"].update(prompt_tokens=1000, completion_tokens=200,
                                      provider="claude", model="claude-sonnet-5", request_id="r")
            return json.dumps({"hypotheses": []})

        with patch("platform_core.ai.invoke_ai", side_effect=one_shot):
            record = triage_pack.run(self.owner, self.app, ("one_shot",))
        self.assertNotIn(CAUSE, prompt["evidence"])
        row = record["details"][0]
        self.assertEqual((row["rank"], row["cause_in_pack"]), (None, False))
        self.assertEqual(Decimal(row["cost"]), Decimal("0.006"))

    def test_both_ways_are_scored_against_the_confirmed_cause(self):
        answer = TriageAgentTests.answer(self, self.note, CAUSE)
        script = Script([("search_knowledge", {"query": "worker pool exhaustion"})], answer)
        before = TriageRun.objects.count()
        with (
            patch("platform_core.ai.invoke_ai", return_value=json.dumps({"hypotheses": []})),
            patch.object(harness, "runtime_for", return_value=script),
        ):
            record = triage_pack.run(self.owner, self.app)
        by_mode = {row["mode"]: row for row in record["details"]}
        self.assertIsNone(by_mode["one_shot"]["rank"])
        self.assertEqual(by_mode["tools"]["rank"], 1)
        self.assertTrue(by_mode["tools"]["cause_read"])
        self.assertEqual(record["metrics"]["tools"]["cause_cited"], 1)
        self.assertEqual(record["metrics"]["tools"]["mrr"], 1.0)
        self.assertEqual(record["metrics"]["one_shot"]["cause_cited"], 0)
        # Nothing is written but receipts: no run, no hypothesis, no steps.
        self.assertEqual(TriageRun.objects.count(), before)
        self.assertFalse(AgentStep.objects.exists())

    def test_no_confirmed_cause_is_refused(self):
        TriageVerdict.objects.all().delete()
        with self.assertRaises(ValidationError):
            triage_pack.run(self.owner, self.app)

    def test_the_bar(self):
        def mode(cited, mrr, cost):
            return {"cause_cited": cited, "mrr": mrr, "cost": cost}

        for alone, tools, said in (
            (mode(2, 0.5, "1"), mode(3, 0.7, "1.5"), "clear the bar"),
            (mode(2, 0.5, "1"), mode(3, 0.7, "2.5"), "over 2x"),
            (mode(3, 0.5, "1"), mode(2, 0.7, "1"), "cited the cause 2 times against 3"),
            (mode(2, 0.5, "1"), mode(2, 0.5, "1"), "no better"),
        ):
            with self.subTest(said=said):
                self.assertIn(said, triage_pack.verdict({"one_shot": alone, "tools": tools}))

    def test_the_command_reports_and_saves(self):
        import tempfile
        from io import StringIO
        from pathlib import Path

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("triage_pack", str(self.app.pk), "--as", "owner", "--limit", "0")
        out = StringIO()
        with tempfile.TemporaryDirectory() as folder:
            with (
                patch("platform_core.ai.invoke_ai", return_value=json.dumps({"hypotheses": []})),
                patch.object(triage_pack, "RECORDS", Path(folder)),
            ):
                call_command(
                    "triage_pack", str(self.app.pk), "--as", "owner", "--only", "one_shot",
                    "--record", stdout=out,
                )
            self.assertEqual(len(list(Path(folder).iterdir())), 1)
        self.assertIn("-- Queue timeout (one_shot): cause not cited", out.getvalue())
        self.assertIn("one_shot: cause cited 0/1", out.getvalue())
