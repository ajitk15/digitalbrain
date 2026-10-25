"""The scorecard's regression check, the drift check's scoring, and why extraction refuses."""

import json
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from platform_core import drift, scorecard
from platform_core.graph_ai import judge


def card(**values):
    return {
        "commit": "abc",
        "corpus": {"sources": 1, "questions": 1},
        "details": {"hipaa": []},
        "metrics": {
            name: {
                "aspect": aspect,
                "label": label,
                "unit": unit,
                "better": better,
                "tolerance": tolerance,
                "value": values.get(name, 1),
            }
            for name, (aspect, label, unit, better, tolerance) in scorecard.METRICS.items()
        },
    }


class TicketQuestionTests(SimpleTestCase):
    """The agentic evidence figure is only worth anything if it is a run's question."""

    def entry(self, content):
        from types import SimpleNamespace

        return SimpleNamespace(
            source="https://carepath.atlassian.net/browse/CARE-401",
            title="Refund window off by one",
            content=content,
        )

    def test_it_is_built_by_code_factory_itself(self):
        with patch(
            "platform_core.code_factory_analysis.evidence_question", return_value="asked"
        ) as asked:
            self.assertEqual(scorecard.ticket_question(self.entry("text")), "asked")
        run, triage, body = asked.call_args.args
        self.assertEqual(run.ticket_external_id, "CARE-401")
        self.assertEqual(run.ticket_title, "Refund window off by one")
        self.assertEqual(run.ticket_body, "")
        self.assertEqual(triage, {"summary": "", "requirements": []})
        self.assertEqual(body, "text")

    def test_it_carries_the_ticket_id_and_its_own_text_as_a_run_does(self):
        question = scorecard.ticket_question(
            self.entry(
                "Refund window off by one\n\nType: Bug\nKey: CARE-401\n\nRefunds close a day early."
                + " x" * 5000
            )
        )
        self.assertTrue(question.startswith("Ticket CARE-401: Refund window off by one"))
        self.assertIn("Refunds close a day early.", question)
        self.assertNotIn("Type: Bug", question)
        self.assertEqual(len(question), 8000)


class EvidenceQuestionTests(SimpleTestCase):
    """Gap analysis asks with what a ticket says, not the header a connector wrote."""

    def run_for(self, body=""):
        from types import SimpleNamespace

        return SimpleNamespace(ticket_external_id="KAN-4", ticket_title="Consent", ticket_body=body)

    def ask(self, run, body):
        from platform_core.code_factory_analysis import evidence_question

        triage = {"summary": "Export skips consent", "requirements": ["BR-05"]}
        return evidence_question(run, triage, body)

    def test_an_imported_ticket_is_asked_without_its_title_and_header(self):
        content = (
            "Consent\n\nKey: KAN-4\nType: Bug\nPriority: Highest\nUpdated: 2026-09-14\n\n"
            "BR-05 requires consent before export.\n\nSee C-09."
        )
        question = self.ask(self.run_for(), content)
        self.assertEqual(
            question,
            "Ticket KAN-4: Consent\n\nExport skips consent\n\nAsked for:\nBR-05\n\n"
            "BR-05 requires consent before export.\n\nSee C-09.",
        )

    def test_text_typed_by_hand_is_asked_whole(self):
        typed = "Status: we think it is broken\n\nThe export skips consent."
        self.assertIn(typed, self.ask(self.run_for(typed), typed))

    def test_a_record_with_no_header_is_asked_whole(self):
        from platform_core.connector_kinds import record_text

        self.assertEqual(record_text("Plain prose.\n\nMore prose."), "Plain prose.\n\nMore prose.")
        self.assertEqual(record_text("Key: KAN-9\n\nBody"), "Body")


class RegressionTests(SimpleTestCase):
    def test_each_direction_is_judged_its_own_way(self):
        base = card(graph_found=15, graph_p50_ms=8.0, restricted_leaks=0)
        self.assertEqual(scorecard.regressions(base, base), [])
        worse = card(graph_found=14, graph_p50_ms=8.0, restricted_leaks=0)
        self.assertEqual(scorecard.regressions(worse, base), [("graph_found", 15, 14)])
        slower = card(graph_found=15, graph_p50_ms=30.0, restricted_leaks=0)
        self.assertEqual(scorecard.regressions(slower, base), [("graph_p50_ms", 8.0, 30.0)])
        leaky = card(graph_found=15, graph_p50_ms=8.0, restricted_leaks=1)
        self.assertEqual(scorecard.regressions(leaky, base), [("restricted_leaks", 0, 1)])

    def test_a_better_number_and_noise_within_tolerance_pass(self):
        base = card(graph_found=15, graph_mrr=0.700, graph_p50_ms=8.0)
        now = card(graph_found=16, graph_mrr=0.696, graph_p50_ms=20.0)
        self.assertEqual(scorecard.regressions(now, base), [])

    def test_a_metric_the_baseline_lacks_is_not_a_regression(self):
        base = card()
        del base["metrics"]["merge_recall"]
        self.assertEqual(scorecard.regressions(card(merge_recall=0), base), [])

    def test_the_ontology_aspect_scores_its_labelled_cases(self):
        values, details = scorecard.ontology_quality()
        self.assertEqual(values["ontology_prompt_coverage"], 100.0)
        self.assertEqual(details["typing_errors"], [])
        self.assertEqual(details["merge_errors"], [])

    def test_every_fixture_the_scorecard_reads_is_in_the_repository(self):
        for name in ("seeded-evidence.json", "phi-samples.json", "ontology-samples.json"):
            self.assertTrue(json.loads((scorecard.BENCH / name).read_text(encoding="utf-8")))
        self.assertTrue((scorecard.KIT / "jira-export.json").is_file())


class CommandTests(TestCase):
    def test_check_fails_on_a_regression_and_passes_without_one(self):
        baseline = card(graph_found=15)
        with patch.object(scorecard, "BENCH") as bench:
            bench.__truediv__.return_value.is_file.return_value = True
            bench.__truediv__.return_value.read_text.return_value = json.dumps(baseline)
            with patch.object(scorecard, "measure", return_value=card(graph_found=15)):
                call_command("scorecard", "--check", stdout=StringIO())
            with patch.object(scorecard, "measure", return_value=card(graph_found=13)):
                with self.assertRaises(CommandError) as failed:
                    call_command("scorecard", "--check", stdout=StringIO())
        self.assertIn("graph_found: 15 -> 13", str(failed.exception))

    def test_a_replay_cannot_become_the_baseline(self):
        replayed = {**card(), "replayed": True}
        with patch.object(scorecard, "replay", return_value=replayed):
            with self.assertRaises(CommandError):
                call_command("scorecard", "--replay", "abc", "--set-baseline", stdout=StringIO())

    def test_drift_needs_someone_to_charge(self):
        with self.assertRaises(CommandError):
            call_command("llm_drift", "00000000-0000-0000-0000-000000000000", stdout=StringIO())


class DriftScoringTests(SimpleTestCase):
    def test_citation_markers_in_every_form(self):
        self.assertEqual(drift.markers("A [1], B [2][3] and C [4, 5]."), [1, 2, 3, 4, 5])
        self.assertEqual(drift.markers("No citations here."), [])

    def record(self, **values):
        return {"metrics": {name: values.get(name, 50) for name in drift.METRICS}}

    def test_points_and_proportions(self):
        first = self.record(extraction_acceptance=60, tokens_per_call=4000)
        self.assertEqual(
            drift.drifted(self.record(extraction_acceptance=50, tokens_per_call=4000), first), []
        )
        self.assertEqual(
            drift.drifted(self.record(extraction_acceptance=40, tokens_per_call=4000), first),
            [("extraction_acceptance", 60, 40)],
        )
        self.assertEqual(
            drift.drifted(self.record(extraction_acceptance=60, tokens_per_call=6500), first),
            [("tokens_per_call", 4000, 6500)],
        )

    def test_an_invented_citation_is_drift_at_once(self):
        first = self.record(answer_invented_citations=0)
        self.assertEqual(
            drift.drifted(self.record(answer_invented_citations=1), first),
            [("answer_invented_citations", 0, 1)],
        )

    def test_the_frozen_pack_is_in_the_repository(self):
        pack = json.loads(drift.PACK.read_text(encoding="utf-8"))
        self.assertEqual([t["id"] for t in pack["extraction"]], list(drift.EXTRACTION_TICKETS))
        self.assertTrue(all("source" in e for a in pack["answers"] for e in a["evidence"]))

    def test_a_missing_pack_is_refused_never_rebuilt(self):
        """A rebuilt pack is a new reference; comparing it with old records is not drift."""
        import tempfile
        from pathlib import Path

        from django.core.exceptions import ImproperlyConfigured

        with tempfile.TemporaryDirectory() as scratch:
            missing = Path(scratch) / "drift-pack.json"
            with (
                patch.object(drift, "PACK", missing),
                patch.object(drift, "build_pack") as build,
                self.assertRaises(ImproperlyConfigured) as raised,
            ):
                drift.load_pack()
            build.assert_not_called()
            self.assertFalse(missing.exists())
        self.assertIn("--build-pack", str(raised.exception))


class JudgeTests(SimpleTestCase):
    source = {"s": {"excerpt": "The referrals service depends on the audit log."}}

    def item(self, **changes):
        base = {
            "subject": "referrals service",
            "subject_type": "Component",
            "relation": "depends on",
            "object": "audit log",
            "object_type": "Component",
            "source_id": "s",
            "quote": "The referrals service depends on the audit log",
        }
        return {**base, **changes}

    def test_each_refusal_names_its_reason(self):
        self.assertEqual(judge(self.item(), self.source)[1], None)
        cases = {
            "malformed": self.item(subject=""),
            "unknown source": self.item(source_id="x"),
            "quote too long": self.item(quote="x" * 400),
            "quote not in source": self.item(quote="The referrals service needs the audit log"),
            "subject not in quote": self.item(subject="Referrals API"),
            "object not in quote": self.item(object="audit trail"),
            "outside the ontology": self.item(relation="loves"),
        }
        for reason, item in cases.items():
            self.assertEqual(judge(item, self.source), (None, reason), reason)

    def test_a_source_may_be_the_subject_of_what_it_says(self):
        ticket = {
            "t": {
                "title": "CARE-401 - Consent is not checked before export",
                "excerpt": "CARE-401 - Consent is not checked before export\n\nKey: KAN-4\n"
                "Type: Bug\n\nBR-05 requires that consent is honoured before export.",
            }
        }
        item = {
            "subject": "CARE-401",
            "subject_type": "Ticket",
            "relation": "violates",
            "object": "BR-05",
            "object_type": "Requirement",
            "source_id": "t",
            "quote": "BR-05 requires that consent is honoured before export.",
        }
        self.assertEqual(judge(item, ticket), (("Ticket", "violates", "Requirement"), None))
        # Named by its whole title, or by the key its imported header states.
        for name in ("CARE-401 - Consent is not checked before export", "KAN-4", "care-401"):
            self.assertIsNone(judge({**item, "subject": name}, ticket)[1], name)
        # Not another ticket, not a type a source cannot be, and the object still counts.
        refused = {
            "subject not in quote": [
                {**item, "subject": "CARE-402"},
                {**item, "subject_type": "Component"},
            ],
            "object not in quote": [{**item, "object": "NFR-03"}],
        }
        for reason, items in refused.items():
            for refused_item in items:
                self.assertEqual(judge(refused_item, ticket), (None, reason), refused_item)

    def test_offline_rescoring_uses_the_same_judge(self):
        pack = drift.load_pack()["extraction"][0]
        record = {
            "details": {
                "proposals": [
                    {
                        "task": pack["id"],
                        "subject": pack["id"],
                        "subject_type": "Ticket",
                        "relation": "references",
                        "object": pack["text"].split()[-1],
                        "object_type": "Component",
                        "quote": pack["text"].rstrip().split("\n")[-1][-100:],
                    },
                    {"task": pack["id"], "raw": "'not a dict'"},
                ]
            }
        }
        acceptance, reasons = drift.rescore(record)
        self.assertEqual((acceptance, reasons), (50.0, {"malformed": 1}))
