"""A code review's AI step with tools (agentic plan, Phase 4), and the review pack.

Reviews are built from the CarePath code as the pack builds them; the agent is
the scripted model, so what the review reads, quotes, keeps and is scored on runs
for real with no provider.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import code_review, review_pack
from platform_core.agent_runtime import code_tools, harness
from platform_core.agent_runtime.scripted import Script
from platform_core.agent_runtime.tools import CitationRecorder, ToolScope, run_tool
from platform_core.models import (
    AgentStep,
    AIConfiguration,
    AIUsage,
    ApplicationGrant,
    CodeReview,
    CodeReviewFile,
    DocumentAIResult,
)
from platform_core.scorecard import CAREPATH_CODE, code_snapshot

from . import test_documents
from .test_code_tools import SETTINGS

FHIR = "src/carepath/api/routes_fhir.py"
CALL = 'if not consent.current(connection, patient_id, "data-exchange"):'


class PackLabelTests(SimpleTestCase):
    """Labelled by hand; these hold each label to the code it describes."""

    def setUp(self):
        self.pack = review_pack.load_pack()

    def test_every_edit_applies_once_and_changes_its_file(self):
        for key, change in self.pack["changes"].items():
            for edit in change["edits"]:
                with self.subTest(change=key, path=edit["path"]):
                    before = (CAREPATH_CODE / edit["path"]).read_text(encoding="utf-8")
                    after = review_pack.apply(before, edit)
                    self.assertIsNotNone(after)
                    self.assertNotEqual(after, before)

    def test_every_file_a_finding_must_quote_makes_the_call_the_change_breaks(self):
        for key, change in self.pack["changes"].items():
            expect = change["expect"]
            if "category" in expect:
                self.assertEqual(expect["category"], "requirement")
                continue
            for path in expect["paths"]:
                with self.subTest(change=key, path=path):
                    text = (CAREPATH_CODE / path).read_text(encoding="utf-8")
                    self.assertIn(expect["uses"], text)
                    self.assertNotIn(path, [edit["path"] for edit in change["edits"]])

    def test_the_requirement_the_unbounded_change_breaks_is_written_down(self):
        documents = CAREPATH_CODE.parent / "docs"
        texts = " ".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in documents.rglob("*")
            if path.suffix in {".md", ".txt", ".html"}
        )
        self.assertIn("NFR-06", texts)

    def test_a_missing_pack_is_refused_never_rebuilt(self):
        with patch.object(review_pack, "PACK", CAREPATH_CODE / "no-such-pack.json"):
            with self.assertRaises(ValidationError):
                review_pack.load_pack()


@override_settings(**SETTINGS)
class Fixture(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.snapshot = code_snapshot(SimpleNamespace(app=self.app, people={"owner": self.owner}))
        self.config = AIConfiguration.objects.create(
            application=self.app, purpose="code_review", provider="claude",
            model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
            configured_by=self.owner,
        )
        for patcher in (
            patch("platform_core.ai.provider_credential", return_value="sk-ant-test"),
            patch("platform_core.secrets.application_secret", return_value="sk-ant-test"),
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.change = review_pack.load_pack()["changes"]["rename-consent-current"]
        self.fhir = self.snapshot.files.get(path=FHIR)

    def review(self):
        return review_pack.build(self.owner, self.app, self.snapshot, self.change)

    def scope(self, review, purpose="code_review"):
        return ToolScope(
            user_id=self.owner.pk, app_id=self.app.pk, purpose=purpose,
            run_id=review.pk, snapshot_id=self.snapshot.pk,
        )

    def window(self):
        """The routes_fhir lines around the call the rename breaks, as read_file returns them."""
        number = self.fhir.content.splitlines().index("    " + CALL) + 1
        return number - 2, number + 2

    def finding(self, source, text):
        return json.dumps(
            {
                "findings": [
                    {
                        "severity": "high", "category": "correctness",
                        "title": "The FHIR export calls a function that is gone",
                        "explanation": "consent.current was renamed.",
                        "quotes": [{"source": source, "text": text}],
                    }
                ]
            }
        )


class ReviewToolTests(Fixture):
    def test_a_review_reads_the_snapshot_as_quotable_evidence(self):
        review = self.review()
        start, end = self.window()
        recorder = CitationRecorder(self.scope(review))
        text = run_tool(
            code_tools.READ_FILE, recorder.scope,
            {"path": FHIR, "start_line": start, "end_line": end}, recorder,
        )
        self.assertIn(f"Evidence id code:{self.fhir.pk}", text)
        [read] = recorder.read_citations()
        self.assertEqual(read["id"], f"code:{self.fhir.pk}")
        self.assertIn(CALL, read["excerpt"])
        self.assertIn(read["excerpt"], self.fhir.content)
        # Code Factory reads the same file without being handed evidence to quote.
        factory = CitationRecorder(self.scope(review, purpose="plan_drafting"))
        plain = run_tool(
            code_tools.READ_FILE, factory.scope,
            {"path": FHIR, "start_line": start, "end_line": end}, factory,
        )
        self.assertNotIn("Evidence id", plain)
        self.assertEqual(factory.read_citations(), [])

    def test_read_diff_reads_only_what_this_review_changed(self):
        review = self.review()
        recorder = CitationRecorder(self.scope(review))
        text = run_tool(
            code_tools.READ_DIFF, recorder.scope,
            {"path": "src/carepath/services/consent.py"}, recorder,
        )
        self.assertIn("+def is_current(", text)
        self.assertEqual(
            recorder.read_citations()[0]["id"], "diff:src/carepath/services/consent.py"
        )
        self.assertEqual(
            run_tool(code_tools.READ_DIFF, recorder.scope, {"path": FHIR}, recorder),
            "That file is not one this change touches.",
        )
        # Another application's review of the same path is not this review.
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        elsewhere = ToolScope(
            user_id=self.owner.pk, app_id=self.other.pk, purpose="code_review",
            run_id=review.pk, snapshot_id=self.snapshot.pk,
        )
        other = CitationRecorder(elsewhere)
        self.assertEqual(
            run_tool(code_tools.READ_DIFF, elsewhere,
                     {"path": "src/carepath/services/consent.py"}, other),
            "That file is not one this change touches.",
        )
        self.assertEqual(other.read_citations(), [])


class ReviewAgentTests(Fixture):
    def ask(self, review, script=None, answer=None):
        def one_shot(config, question, citations, token, **options):
            usage = {"prompt_tokens": 1200, "completion_tokens": 80}
            return {"id": "req-1", "usage": usage}, answer

        CodeReview.objects.filter(pk=review.pk).update(
            ai_status="queued", ai_requested_by=self.owner
        )
        review.refresh_from_db()
        with (
            patch.object(harness, "runtime_for", return_value=script),
            patch("platform_core.claude_agents.completion", side_effect=one_shot) as called,
        ):
            code_review.run_ai(review)
        review.refresh_from_db()
        return review, called

    def test_off_by_default_the_review_asks_once_and_cannot_quote_what_it_never_had(self):
        review, called = self.ask(self.review(), answer=self.finding(f"code:{self.fhir.pk}", CALL))
        self.assertEqual(called.call_count, 1)
        self.assertEqual(review.ai_status, "ready")
        self.assertEqual(review.ai_result["findings"], [])
        self.assertEqual(review.ai_result["dropped_reasons"], {"quote not found": 1})

    @override_settings(CODE_REVIEW_AGENT_TOOLS=True)
    def test_the_agent_reads_the_caller_and_may_quote_it(self):
        start, end = self.window()
        script = Script(
            [("read_file", {"path": FHIR, "start_line": start, "end_line": end})],
            self.finding(f"code:{self.fhir.pk}", CALL),
        )
        review, called = self.ask(self.review(), script=script)
        called.assert_not_called()
        self.assertTrue(script.instructions.endswith(code_review.TOOL_GUIDANCE))
        self.assertIn("read_diff", script.given)
        self.assertNotIn("search_knowledge", script.given)
        [kept] = review.ai_result["findings"]
        self.assertEqual(kept["quotes"][0]["source"], f"code:{self.fhir.pk}")
        self.assertEqual(review.ai_result["read"], 1)
        self.assertEqual(
            list(AgentStep.objects.filter(review=review).values_list("tool", "outcome")),
            [("read_file", "found")],
        )
        self.assertEqual(AIUsage.objects.filter(purpose="code_review").count(), 2)
        # The page checks the quote against the live file, like any other.
        found, gone = code_review.checked_findings(review)
        self.assertEqual((len(found), gone), (1, 0))
        rubrics = list(DocumentAIResult.objects.values_list("rubric", flat=True))
        self.assertEqual(rubrics, [f"code-review-{code_review.REVIEW_VERSION}-agent"])

    @override_settings(CODE_REVIEW_AGENT_TOOLS=True)
    def test_a_quote_the_agent_never_read_is_still_refused(self):
        review, _ = self.ask(self.review(), script=Script(
            self.finding(f"code:{self.fhir.pk}", CALL)
        ))
        self.assertEqual(review.ai_result["findings"], [])


class ReviewPackTests(Fixture):
    def setUp(self):
        super().setUp()
        self.pack = {"repository": "documentation/demo/carepath",
                     "changes": {"rename-consent-current": self.change}}

    def test_both_ways_are_reviewed_scored_and_removed(self):
        start, end = self.window()
        script = Script(
            [("read_file", {"path": FHIR, "start_line": start, "end_line": end})],
            self.finding(f"code:{self.fhir.pk}", CALL),
        )

        def one_shot(config, question, citations, token, **options):
            # What the excerpts allow: the diff, which says nothing about the caller.
            diff = next(c for c in citations if c["id"].startswith("diff:"))
            line = next(row[1:] for row in diff["excerpt"].splitlines()
                        if row.startswith("+def is_current"))
            return (
                {"id": "req-1", "usage": {"prompt_tokens": 1000, "completion_tokens": 200}},
                self.finding(diff["id"].replace("diff:", "diff:", 1), line).replace(
                    '"correctness"', '"design"'),
            )

        with (
            patch.object(review_pack, "load_pack", return_value=self.pack),
            patch.object(harness, "runtime_for", return_value=script),
            patch("platform_core.claude_agents.completion", side_effect=one_shot),
        ):
            record = review_pack.run(self.owner, self.app)
        by_mode = {row["mode"]: row for row in record["details"]}
        self.assertEqual((by_mode["one_shot"]["found"], by_mode["one_shot"]["noise"]), (False, 1))
        self.assertEqual((by_mode["tools"]["found"], by_mode["tools"]["noise"]), (True, 0))
        self.assertEqual(by_mode["tools"]["read"], 1)
        self.assertEqual(Decimal(by_mode["one_shot"]["cost"]), Decimal("0.006"))
        self.assertIn("clear the bar", review_pack.verdict(record["metrics"]))
        # Nothing is left but the receipts.
        self.assertFalse(CodeReview.objects.exists())
        self.assertFalse(CodeReviewFile.objects.exists())
        self.assertFalse(AgentStep.objects.exists())
        self.assertFalse(DocumentAIResult.objects.exists())

    def test_a_refused_call_is_a_row_and_the_review_still_goes(self):
        with (
            patch.object(review_pack, "load_pack", return_value=self.pack),
            patch("platform_core.ai.invoke_ai", side_effect=ValidationError("Budget used.")),
        ):
            record = review_pack.run(self.owner, self.app, ("one_shot",))
        self.assertEqual(record["details"][0]["failure"], "Budget used.")
        self.assertFalse(CodeReview.objects.exists())

    def test_an_application_without_the_code_is_refused(self):
        self.snapshot.files.filter(path=FHIR).delete()
        with patch.object(review_pack, "load_pack", return_value=self.pack):
            with self.assertRaises(ValidationError):
                review_pack.run(self.owner, self.app)

    def test_the_bar(self):
        def mode(found, noise, cost):
            return {"found": found, "noise": noise, "cost": cost}

        for alone, tools, said in (
            (mode(1, 1, "1"), mode(3, 1, "1.5"), "clear the bar"),
            (mode(1, 1, "1"), mode(3, 1, "2.5"), "over 2x"),
            (mode(1, 1, "1"), mode(3, 4, "1"), "4 findings that are noise against 1"),
            (mode(3, 1, "1"), mode(2, 1, "1"), "found 2 changes against 3"),
            (mode(2, 1, "1"), mode(2, 1, "1"), "no better"),
        ):
            with self.subTest(said=said):
                self.assertIn(said, review_pack.verdict({"one_shot": alone, "tools": tools}))

    def test_the_command_reports(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with (
            patch.object(review_pack, "load_pack", return_value=self.pack),
            patch("platform_core.ai.invoke_ai", return_value=json.dumps({"findings": []})),
        ):
            call_command("review_pack", str(self.app.pk), "--as", "owner", "--only",
                         "one_shot", stdout=out)
        self.assertIn("-- rename-consent-current (one_shot): 0 finding(s)", out.getvalue())
        self.assertIn("one_shot: found 0/1", out.getvalue())
