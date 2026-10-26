"""The Analysis stage as results first, and the warning for code the run cannot see.

The stage used to be the run log alone, where routine checks and findings read
the same. And a repository whose code is in a language Code Graph does not parse
was reasoned about without its structure, with nothing on the run to say so.
"""

from django.test import SimpleTestCase, TestCase, override_settings
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

    # As CI runs it: no host login, so the missing credential is a pre-check
    # problem and the problems list is on the page to be read.
    @override_settings(CLAUDE_USE_HOST_LOGIN=False)
    def test_problems_from_later_stages_stay_out_of_analysis(self):
        from platform_core.code_factory import note

        run = self.run_pipeline()
        note(run, "Implementation requested by owner.", level="check")
        note(run, "Change review failed. Later-stage trouble.", phase="review", level="problem")
        stage = self.page(run).split('id="stage-2"')[1].split("</details>\n\n")[0]
        # Only the problems list itself: the full log below it rightly carries
        # every line, later stages included. Reading past the list passed
        # locally only because no pre-check problem there made the list appear.
        problems = (
            stage.split('class="analysis-problems"')[1].split("</ul>")[0]
            if 'class="analysis-problems"' in stage
            else ""
        )
        self.assertNotIn("Later-stage trouble", problems)
        # The same line is still in the full log, where it belongs.
        self.assertIn("Later-stage trouble", stage.split('class="analysis-log"')[1])

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
        self.assertIn("Some code is in a language that is not parsed", body)
        self.assertIn("Java (90.0%) is in this repository but not parsed", body)


class TargetTagTests(TestCase):
    """The tag beside each gap target says what implementation will do with it."""

    setUp = test_code_factory.PipelineTests.setUp

    def test_only_a_path_is_a_file_and_a_defined_name_is_code(self):
        from types import SimpleNamespace

        from platform_core.factory_views import target_tags
        from platform_core.models import CodeFile, CodeRepository, CodeSnapshot

        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        snapshot = CodeSnapshot.objects.create(
            repository=repository, number=1, ref="main", commit_sha="c" * 40,
            manifest_digest="m" * 64,
        )
        CodeFile.objects.create(
            snapshot=snapshot, path="api/patients.py", language="python", digest="d" * 64,
            content="def search_patients(limit):\n    return limit\n", lines=2,
            symbols=[{"name": "search_patients", "kind": "function", "line": 1}],
        )
        tags = target_tags(
            SimpleNamespace(code_snapshot_id=snapshot.pk),
            [
                "api/patients.py",
                "api/patients.py::search_patients",
                "tests/test_bounds.py",
                "search_patients",
                "settings.max_page_size",
                "GET /patients",
                "NFR-11",
            ],
        )
        self.assertEqual(
            tags,
            {
                "api/patients.py": {"kind": "code", "path": "api/patients.py"},
                "api/patients.py::search_patients": {"kind": "code", "path": "api/patients.py"},
                "tests/test_bounds.py": {"kind": "new", "path": "tests/test_bounds.py"},
                "search_patients": {"kind": "symbol", "path": "api/patients.py"},
            },
        )
        self.assertEqual(target_tags(SimpleNamespace(code_snapshot_id=None), ["a.py"]), {})
