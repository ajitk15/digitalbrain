"""The retrieval benchmark: known questions scored against graph versions, no model."""

import hashlib
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.benchmark import run, score
from platform_core.graph_ai import graph_citations
from platform_core.graphs import build_graph, rebuild
from platform_core.models import (
    BenchmarkQuestion,
    BenchmarkRun,
    GraphRevision,
    KnowledgeEntry,
)

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


@override_settings(**SETTINGS)
class BenchmarkTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.consent = self.entry(
            "CARE-401 consent export",
            "The FHIR export never checks patient consent before sharing.",
            source="https://team.atlassian.net/browse/KAN-4",
        )
        self.runbook = self.entry("Operations runbook", "Restart the export worker when it stalls.")
        self.url = reverse("graph-benchmark", args=[self.app.pk])

    def entry(self, title, content, source="", app=None):
        return KnowledgeEntry.objects.create(
            application=app or self.app,
            author=self.owner,
            title=title,
            content=content,
            source=source,
            digest=hashlib.sha256(content.encode()).hexdigest(),
        )

    def revision(self, number=1, published=False, app=None):
        app = app or self.app
        entries = list(KnowledgeEntry.objects.filter(application=app, active=True))
        data, quality = build_graph(entries, len(entries))
        return GraphRevision.objects.create(
            application=app,
            number=number,
            fingerprint="f" * 64,
            data=data,
            quality=quality,
            published_at="2026-10-01T00:00:00Z" if published else None,
        )

    def ask(self, question, expected, app=None):
        return BenchmarkQuestion.objects.create(
            application=app or self.app, question=question, expected=expected
        )

    def test_a_draft_is_scored_but_still_cannot_answer(self):
        """Measuring is not answering: the draft gate stays where it was."""
        draft = self.revision()
        self.ask("Is consent checked on export?", "KAN-4")
        result = run(self.app, draft)
        self.assertEqual((result.found, result.questions), (1, 1))
        with self.assertRaises(ValidationError):
            graph_citations(self.app.pk, "consent", version=draft.number)

    def test_expected_matches_title_or_link_and_ranks_by_source(self):
        draft = self.revision()
        results = score(
            self.app,
            draft,
            [
                self.ask("Is consent checked on export?", "kan-4"),
                self.ask("How do I restart the worker?", "OPERATIONS RUNBOOK"),
            ],
        )
        self.assertEqual([item["rank"] for item in results], [1, 1])
        # Each source once, however many of its relationships were cited.
        for item in results:
            self.assertEqual(len(item["cited"]), len(set(item["cited"])))

    def test_a_missed_question_lowers_the_score_and_says_what_was_cited(self):
        draft = self.revision()
        self.ask("Is consent checked on export?", "KAN-4")
        self.ask("How do I restart the worker?", "runbook")
        self.ask("What does billing cost?", "KAN-4")
        result = run(self.app, draft)
        self.assertEqual((result.found, result.top_three, result.questions), (2, 2, 3))
        # Questions made in the same instant can tie on created_at; find it by text.
        missed = next(item for item in result.results if "billing" in item["question"])
        self.assertIsNone(missed["rank"])
        self.assertEqual(missed["cited"], [])
        self.assertGreater(result.reciprocal_rank, 0)
        self.assertLess(result.reciprocal_rank, 1)

    def test_no_questions_means_no_run(self):
        self.assertIsNone(run(self.app, self.revision()))
        self.assertFalse(BenchmarkRun.objects.exists())

    def test_another_applications_sources_are_never_cited(self):
        """Scope holds: a question cannot reach across applications."""
        self.entry("CARE-401 elsewhere", "Consent export consent export.", app=self.other)
        draft = self.revision()
        results = score(self.app, draft, [self.ask("consent export", "elsewhere")])
        self.assertIsNone(results[0]["rank"])

    def test_every_saved_version_is_scored_automatically(self):
        self.ask("Is consent checked on export?", "KAN-4")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(rebuild(self.app.pk))
        revision = GraphRevision.objects.get(application=self.app)
        automatic = BenchmarkRun.objects.get(revision=revision)
        self.assertIsNone(automatic.created_by)
        self.assertEqual(automatic.trigger, "saved")
        self.assertEqual(automatic.found, 1)

    def test_a_failing_benchmark_never_costs_the_version(self):
        self.ask("Is consent checked on export?", "KAN-4")
        from unittest.mock import patch

        with patch("platform_core.benchmark.score", side_effect=RuntimeError("boom")):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(rebuild(self.app.pk))
        self.assertTrue(GraphRevision.objects.filter(application=self.app).exists())
        self.assertFalse(BenchmarkRun.objects.exists())

    def test_an_owner_adds_runs_and_removes_questions(self):
        draft = self.revision(number=3)
        response = self.client.post(
            self.url, {"action": "add", "question": "Is consent checked?", "expected": "KAN-4"}
        )
        self.assertRedirects(response, self.url)
        response = self.client.post(self.url, {"action": "run", "version": "3"})
        result = BenchmarkRun.objects.get(revision=draft)
        self.assertRedirects(response, f"{self.url}?run={result.pk}")
        page = self.client.get(f"{self.url}?run={result.pk}")
        self.assertContains(page, "Version 3 &middot; 1 of 1 found")
        self.assertContains(page, "Cited #1")
        self.assertContains(page, "run by owner")
        self.assertEqual(result.trigger, "button")
        question = BenchmarkQuestion.objects.get(application=self.app)
        self.client.post(self.url, {"action": "delete", "question": str(question.pk)})
        self.assertFalse(BenchmarkQuestion.objects.filter(application=self.app).exists())

    def test_a_short_expected_value_is_refused(self):
        response = self.client.post(
            self.url, {"action": "add", "question": "Anything?", "expected": "ab"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at least three characters")
        self.assertFalse(BenchmarkQuestion.objects.exists())

    def test_a_viewer_can_read_but_not_change(self):
        self.ask("Is consent checked on export?", "KAN-4")
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        page = self.client.get(self.url)
        self.assertContains(page, "Is consent checked on export?")
        self.assertNotContains(page, 'name="action" value="add"')
        response = self.client.post(
            self.url, {"action": "add", "question": "Sneaky?", "expected": "KAN-4"}
        )
        self.assertEqual(response.status_code, 403)

    def test_another_applications_question_cannot_be_removed(self):
        foreign = self.ask("Elsewhere?", "KAN-4", app=self.other)
        self.client.post(self.url, {"action": "delete", "question": str(foreign.pk)})
        self.assertTrue(BenchmarkQuestion.objects.filter(pk=foreign.pk).exists())

    def test_no_grant_is_not_found(self):
        self.assertEqual(
            self.client.get(reverse("graph-benchmark", args=[self.other.pk])).status_code, 404
        )

    def test_the_versions_tab_shows_each_score(self):
        draft = self.revision(number=2)
        self.ask("Is consent checked on export?", "KAN-4")
        result = run(self.app, draft)
        page = self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "versions"})
        self.assertContains(page, "<th>Benchmark</th>", html=False)
        self.assertContains(page, f"?run={result.pk}")
        self.assertContains(page, ">1/1</a>", html=False)

    def test_each_question_is_listed_once_with_its_result(self):
        """Questions and results used to be two tables naming the same questions."""
        draft = self.revision(number=2)
        removed = self.ask("Who approves a release?", "KAN-4")
        self.ask("Is consent checked on export?", "KAN-4")
        result = run(self.app, draft)
        removed.delete()
        self.ask("Is the export audited?", "KAN-4")
        page = self.client.get(f"{self.url}?run={result.pk}").content.decode()
        self.assertEqual(page.count("Is consent checked on export?"), 2)  # cell and aria-label
        self.assertIn("Not scored yet", page)
        self.assertIn("(removed since)", page)
        self.assertNotIn("<h3>Versions</h3>", page)

    def test_the_benchmark_opens_as_a_popup_not_a_tab(self):
        draft = self.revision(number=2, published=True)
        self.ask("Is consent checked on export?", "KAN-4")
        result = run(self.app, draft)
        versions = self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "versions"})
        self.assertNotContains(versions, "Benchmark</a></nav>", html=False)
        self.assertContains(versions, f'?run={result.pk}" data-modal', html=False)
        quality = self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "quality"})
        self.assertContains(quality, "Finds the right source")
        self.assertContains(quality, "1 of 1")
        self.assertContains(quality, f'?run={result.pk}" data-modal>See the questions', html=False)
        # The page still stands on its own, with Versions as its tab.
        page = self.client.get(self.url).content.decode()
        self.assertRegex(page, r'aria-current="page">[^<]*<svg[^>]*>.*?</svg>Versions')

    def test_the_command_loads_questions_once_and_scores_the_latest_version(self):
        self.revision(number=1)
        self.revision(number=2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "questions.json"
            path.write_text(
                json.dumps([{"question": "Is consent checked on export?", "expected": "KAN-4"}]),
                encoding="utf-8",
            )
            out = StringIO()
            call_command("benchmark", str(self.app.pk), "--load", str(path), stdout=out)
            call_command("benchmark", str(self.app.pk), "--load", str(path), stdout=StringIO())
        self.assertEqual(BenchmarkQuestion.objects.filter(application=self.app).count(), 1)
        self.assertIn("Version 2: found 1/1", out.getvalue())
        self.assertEqual(set(BenchmarkRun.objects.values_list("trigger", flat=True)), {"console"})
