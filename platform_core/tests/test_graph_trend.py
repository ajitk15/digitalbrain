"""Quality by version: each version measured as built and as structure alone, no model."""

import hashlib
import re

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import graph_trend
from platform_core.benchmark import run
from platform_core.graphs import build_graph
from platform_core.models import (
    BenchmarkQuestion,
    BenchmarkRun,
    GraphRevision,
    KnowledgeEntry,
)

from . import test_documents
from .test_benchmark import SETTINGS

QUOTE = "The FHIR export never checks patient consent before sharing."


@override_settings(**SETTINGS)
class GraphTrendTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.consent = self.entry("CARE-401", QUOTE, source="https://team.atlassian.net/browse/KAN-4")
        self.entry(
            "Release plan",
            "| Step | Owner |\n| --- | --- |\n| Deploy carepath-api | Platform team |\n",
        )
        BenchmarkQuestion.objects.create(
            application=self.app, question="Which requirement is violated?",
            expected="KAN-4",
        )

    def entry(self, title, content, source=""):
        return KnowledgeEntry.objects.create(
            application=self.app, author=self.owner, title=title, content=content,
            source=source, digest=hashlib.sha256(content.encode()).hexdigest(),
        )

    def revision(self, number, ai=False, model=""):
        """A version as `build_graph` makes it, and - with `ai` - one verified AI relationship
        in the shape `graph_ai.enrich_graph` writes, the only thing that names a requirement."""
        entries = list(KnowledgeEntry.objects.filter(application=self.app, active=True))
        data, quality = build_graph(entries, len(entries))
        if ai:
            ids = ["e" * 24, "f" * 24]
            for node_id, label, kind in zip(
                ids, ("partner export", "BR-05 consent requirement"), ("Component", "Requirement"),
                strict=True,
            ):
                data["nodes"].append({"id": node_id, "kind": "entity", "type": kind,
                                      "label": label, "knowledge_id": str(self.consent.pk),
                                      "line": 1})
            data["edges"].append({"source": ids[0], "target": ids[1], "relation": "violates",
                                  "knowledge_id": str(self.consent.pk),
                                  "digest": self.consent.digest, "line": 1, "evidence": QUOTE,
                                  "inferred": True})
            quality.update({"semantic_relationships": 1, "rejected_relationships": 3,
                            "outside_ontology": 1, "merged_entities": 0})
        return GraphRevision.objects.create(
            application=self.app, number=number, fingerprint="f" * 64, data=data,
            quality=quality, model=model,
        )

    def test_without_ai_removes_entities_and_every_relationship_touching_them(self):
        data = self.revision(1, ai=True).data
        plain = graph_trend.without_ai(data)
        self.assertTrue(graph_trend.has_ai(data))
        self.assertFalse(graph_trend.has_ai(plain))
        self.assertFalse([node for node in plain["nodes"] if node.get("kind") == "entity"])
        self.assertEqual(len(plain["edges"]), len(data["edges"]) - 1)
        # The structure is what the rules read, untouched.
        structural = self.revision(2).data
        self.assertEqual(plain["edges"], structural["edges"])

    def test_a_version_with_ai_is_also_scored_as_structure_alone(self):
        version = self.revision(1, ai=True, model="claude-sonnet-5")
        result = run(self.app, version)
        # Only the AI relationship names a requirement, so only it finds the ticket.
        self.assertEqual(result.found, 1)
        self.assertEqual(result.structure["found"], 0)
        self.assertEqual(result.structure["ranks"], [None])
        self.assertGreater(result.reciprocal_rank, result.structure["reciprocal_rank"])

    def test_a_version_without_ai_is_its_own_structure_and_is_not_scored_twice(self):
        result = run(self.app, self.revision(1))
        self.assertIsNone(result.structure)
        row = graph_trend.trend(self.app)["rows"][0]
        self.assertEqual(row["retrieval"]["structure"], row["retrieval"]["with_ai"])

    def test_a_reranked_run_is_never_scored_twice(self):
        """The second scoring would be a second paid rerank."""
        from unittest.mock import patch

        version = self.revision(1, ai=True)
        with patch("platform_core.rerank.rerank", side_effect=lambda user, app, q, c, n: c[:n]):
            result = run(self.app, version, reranker=self.owner)
        self.assertIsNone(result.structure)

    def test_each_version_says_whether_it_is_better_or_worse_and_what_ai_adds(self):
        for number, ai in ((1, False), (2, True)):
            run(self.app, self.revision(number, ai=ai, model="claude-sonnet-5" if ai else ""))
        found = graph_trend.trend(self.app)
        first, second = found["rows"]
        self.assertEqual(first["changes"], {})
        self.assertEqual(second["changes"]["reciprocal_rank"], "better")
        self.assertEqual(second["changes"]["found"], "better")
        self.assertEqual(second["lift"]["reciprocal_rank"]["direction"], "better")
        self.assertEqual(second["ai_run"]["acceptance_percent"], 25.0)
        self.assertEqual(second["ai_run"]["outside_ontology"], 1)
        self.assertIsNone(first["ai_run"]["semantic_relationships"])
        tones = [line["tone"] for line in found["verdict"]]
        self.assertEqual(tones, ["good", "good"])
        self.assertIn("MRR 0.00 → 1.00", found["verdict"][0]["text"])
        # Two scored versions draw both lines.
        self.assertEqual(len(found["chart"]["with_ai"]["points"]), 2)
        self.assertEqual(len(found["chart"]["structure"]["points"]), 2)

    def test_a_version_built_without_the_model_is_called_out(self):
        run(self.app, self.revision(1))
        verdict = graph_trend.trend(self.app)["verdict"]
        self.assertEqual(verdict[-1]["tone"], "poor")
        self.assertIn("structure alone", verdict[-1]["text"])

    def test_measures_are_stored_once_and_the_version_itself_is_never_written(self):
        version = self.revision(1, ai=True)
        before = GraphRevision.objects.get(pk=version.pk).data
        graph_trend.trend(self.app)
        stored = GraphRevision.objects.get(pk=version.pk)
        self.assertEqual(stored.measures["version"], graph_trend.MEASURES_VERSION)
        self.assertTrue(stored.measures["ai"])
        self.assertEqual(stored.measures["with_ai"]["edges"],
                         stored.measures["structure"]["edges"] + 1)
        self.assertEqual(stored.data, before)
        with self.assertNumQueries(2):
            # Read, never measured again: the revisions and their benchmark runs.
            graph_trend.trend(self.app)

    def test_saving_a_version_measures_it(self):
        from platform_core.benchmark import run_after_save

        version = self.revision(1, ai=True)
        run_after_save(version.pk)
        self.assertTrue(GraphRevision.objects.get(pk=version.pk).measures["ai"])
        self.assertIsNotNone(BenchmarkRun.objects.get(revision=version).structure)

    def test_the_quality_tab_draws_the_chart_with_no_script_or_style(self):
        for number, ai in ((1, False), (2, True)):
            run(self.app, self.revision(number, ai=ai, model="claude-sonnet-5" if ai else ""))
        self.client.force_login(self.owner)
        response = self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "quality",
                                                                          "version": 2})
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        section = page[page.index('class="card quality-trend"'):]
        section = section[: section.index("</section>")]
        self.assertIn("<polyline", section)
        self.assertIn("Structure alone", section)
        self.assertIn("v2", section)
        self.assertNotIn("<script", section)
        self.assertIsNone(re.search(r"\sstyle=", section))

    def test_other_tabs_do_not_measure_anything(self):
        self.revision(1, ai=True)
        self.client.force_login(self.owner)
        self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "versions"})
        self.assertIsNone(GraphRevision.objects.get(number=1).measures)
