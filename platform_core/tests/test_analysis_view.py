"""The Analysis stage as results first, and the warning for code the run cannot see.

The stage used to be the run log alone, where routine checks and findings read
the same. And a repository whose code is in a language Code Graph does not parse
was reasoned about without its structure, with nothing on the run to say so.
"""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from platform_core.code_factory import language_gap
from platform_core.code_graph_ingest import register
from platform_core.models import FactoryRun

from . import test_code_factory


class LanguageGapTests(SimpleTestCase):
    def snapshot(self, *rows):
        return type("Snapshot", (), {"languages": list(rows)})()

    def test_unparsed_code_is_named_with_its_share(self):
        gap = language_gap(
            self.snapshot(
                {"name": "Java", "share": 71.2, "analysed": False},
                {"name": "Python", "share": 28.8, "analysed": True},
            )
        )
        self.assertIn("Java (71.2%) is in this repository but not parsed", gap)
        self.assertNotIn("Python", gap)

    def test_fully_parsed_code_says_nothing(self):
        self.assertEqual(
            language_gap(self.snapshot({"name": "Python", "share": 100.0, "analysed": True})),
            "",
        )
        self.assertEqual(language_gap(None), "")


class AnalysisViewTests(TestCase):
    setUp = test_code_factory.PipelineTests.setUp
    answers = test_code_factory.PipelineTests.answers
    run_pipeline = test_code_factory.PipelineTests.run_pipeline

    def page(self, run):
        return self.client.get(reverse("run-detail", args=[self.app.pk, run.pk])).content.decode()

    def test_results_come_before_the_log(self):
        run = self.run_pipeline()
        body = self.page(run)
        stage = body.split('id="stage-2"')[1].split("</details>\n\n")[0]
        # Triage, evidence and the gap counts, from what the phases recorded.
        self.assertIn("Queue Beta overflows under retry load.", stage)
        self.assertIn("Stated in the ticket", stage)
        self.assertIn("Non-functional gap", stage)
        self.assertIn('class="analysis-step is-ok"', stage)
        # The log is still all there, behind its own disclosure, after the cards.
        self.assertIn("Show full log", stage)
        self.assertLess(stage.index("analysis-cards"), stage.index("Show full log"))
        self.assertIn("Gap analysis found 2 item(s)", stage)

    def test_problems_from_later_stages_stay_out_of_analysis(self):
        from platform_core.code_factory import note

        run = self.run_pipeline()
        note(run, "Implementation requested by owner.", level="check")
        note(run, "Change review failed. Later-stage trouble.", phase="review", level="problem")
        stage = self.page(run).split('id="stage-2"')[1].split("</details>\n\n")[0]
        problems = stage.split("analysis-problems")[1] if "analysis-problems" in stage else ""
        self.assertNotIn("Later-stage trouble", problems)

    def test_code_in_an_unparsed_language_is_a_failed_pre_check(self):
        run = self.run_pipeline()
        snapshot = register(self.owner, self.app.pk, "acme/widgets").snapshots.create(
            number=1,
            commit_sha="a" * 40,
            languages=[
                {"name": "Java", "files": 9, "bytes": 900, "share": 90.0, "analysed": False},
                {"name": "Python", "files": 1, "bytes": 100, "share": 10.0, "analysed": True},
            ],
        )
        FactoryRun.objects.filter(pk=run.pk).update(code_snapshot=snapshot)
        body = self.page(run)
        self.assertIn("Some code is in a language Code Graph does not parse", body)
        self.assertIn("Java (90.0%) is in this repository but not parsed", body)
