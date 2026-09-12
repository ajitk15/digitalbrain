import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.graphs import build_graph, process_next_graph, rebuild
from platform_core.models import ApplicationGrant, FeatureSwitch, KnowledgeGraph
from platform_core.workbench import add_knowledge

from . import test_documents


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DOCUMENT_AUTO_CONVERT=False,
)
class GraphTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def source(self, content=None, app=None):
        return add_knowledge(
            self.owner,
            (app or self.app).pk,
            "Configuration",
            content or "| Node | Queue |\n| --- | --- |\n| NODE1 | Q1 |\n| NODE2 | Q1 |",
        )

    def test_tables_create_shared_values_and_evidenced_relationships(self):
        entry = self.source()
        data, quality = build_graph([entry], 1)
        self.assertEqual(quality["included_rows"], 2)
        self.assertEqual(quality["coverage_percent"], 100)
        self.assertEqual(quality["provenance_percent"], 100)
        shared = [n for n in data["nodes"] if n.get("field") == "Queue"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(len([e for e in data["edges"] if e["target"] == shared[0]["id"]]), 2)
        self.assertTrue(all(e["knowledge_id"] == str(entry.pk) for e in data["edges"]))
        self.assertTrue(all(e["digest"] == entry.digest for e in data["edges"]))
        ids = {n["id"] for n in data["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in data["edges"]))

    @patch("platform_core.graphs.MAX_RECORDS_PER_SOURCE", 150)
    def test_quality_reports_missing_values_duplicate_rows_and_limits(self):
        entry = self.source("| Node | Queue |\n| --- | --- |\n" + "| N |  |\n" * 160)
        data, quality = build_graph([entry], 1)
        self.assertEqual(quality["table_rows"], 160)
        self.assertEqual(quality["included_rows"], 150)
        self.assertEqual(quality["duplicate_rows"], 159)
        self.assertEqual(quality["empty_cells"], 160)
        self.assertLess(quality["coverage_percent"], 100)
        self.assertEqual(quality["result"], "Needs attention")

    def test_graph_worker_builds_and_rebuilds_changed_sources(self):
        entry = self.source()
        self.assertTrue(process_next_graph())
        graph = KnowledgeGraph.objects.get(application=self.app)
        old_version = graph.version
        self.assertFalse(process_next_graph())
        entry.active = False
        entry.save()
        self.assertTrue(process_next_graph())
        graph.refresh_from_db()
        self.assertNotEqual(graph.version, old_version)
        self.assertEqual(graph.data["nodes"], [])

    def test_graph_page_has_view_and_quality_and_export_is_scoped(self):
        self.source()
        rebuild(self.app.pk)
        response = self.client.get(reverse("graph", args=[self.app.pk]))
        self.assertContains(response, 'id="graph-canvas"')
        self.assertContains(response, ">Quality</a>")
        self.assertContains(
            self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "quality"}),
            "Graph quality",
        )
        response = self.client.get(reverse("graph", args=[self.app.pk]), {"format": "json"})
        payload = json.loads(response.content)
        self.assertEqual(payload["application_id"], str(self.app.pk))
        self.assertEqual(payload["quality"]["table_rows"], 2)

    def test_stale_graph_is_not_exposed_after_source_removed(self):
        entry = self.source()
        rebuild(self.app.pk)
        entry.delete()
        response = self.client.get(reverse("graph", args=[self.app.pk]), {"format": "json"})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("NODE1", response.content.decode())
        response = self.client.get(reverse("graph", args=[self.app.pk]))
        self.assertNotContains(response, 'id="graph-canvas"')

    def test_other_application_and_platform_admin_cannot_read_graph(self):
        self.source()
        rebuild(self.app.pk)
        self.assertEqual(self.client.get(reverse("graph", args=[self.other.pk])).status_code, 404)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("graph", args=[self.app.pk])).status_code, 404)

    def test_graph_feature_enforcement_and_revocation(self):
        self.source()
        rebuild(self.app.pk)
        FeatureSwitch.objects.create(key="knowledge", enabled=False)
        self.assertEqual(self.client.get(reverse("graph", args=[self.app.pk])).status_code, 403)
        FeatureSwitch.objects.all().delete()
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        self.assertEqual(self.client.get(reverse("graph", args=[self.app.pk])).status_code, 404)

    def test_application_values_never_merge_across_graphs(self):
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.source()
        self.source("| Private |\n| --- |\n| FOREIGN |", self.other)
        rebuild(self.app.pk)
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertNotIn("FOREIGN", json.dumps(graph.data))

    def test_untrusted_graph_text_is_safely_embedded(self):
        self.source("| Node |\n| --- |\n| </script><img src=x onerror=alert(1)> |")
        rebuild(self.app.pk)
        response = self.client.get(reverse("graph", args=[self.app.pk]))
        self.assertNotContains(response, "</script><img")
        self.assertContains(response, "\\u003C/script\\u003E")

    def test_prose_structure_is_not_presented_as_semantic_inference(self):
        entry = self.source("# Requirements\nRefunds need approval.")
        data, quality = build_graph([entry], 1)
        self.assertEqual({e["relation"] for e in data["edges"]}, {"contains section"})
        self.assertIn("Not measured", quality["semantic_accuracy"])

    def test_graph_scripts_allowed_without_inline_execution(self):
        self.source()
        rebuild(self.app.pk)
        response = self.client.get(reverse("graph", args=[self.app.pk]))
        policy = response["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertContains(response, "/static/graph.js")

    def test_versions_are_saved_and_unchanged_rebuild_does_not_duplicate(self):
        from platform_core.models import GraphRevision

        self.source()
        rebuild(self.app.pk)
        first = GraphRevision.objects.get(application=self.app)
        original = json.dumps(first.data, sort_keys=True)
        rebuild(self.app.pk)
        self.assertEqual(GraphRevision.objects.count(), 1)
        self.source("# Additional source\nAnother requirement.")
        rebuild(self.app.pk)
        self.assertEqual(list(GraphRevision.objects.values_list("number", flat=True)), [2, 1])
        first.refresh_from_db()
        self.assertEqual(json.dumps(first.data, sort_keys=True), original)
        response = self.client.get(
            reverse("graph", args=[self.app.pk]), {"version": "1", "format": "json"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["version_number"], 1)
        self.assertEqual(json.loads(response.content)["graph"], first.data)
        response = self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "versions"})
        self.assertContains(response, ">v1</strong>")
        self.assertContains(response, ">v2</strong>")

    def test_historical_graph_cannot_restore_deleted_source_content(self):
        entry = self.source()
        rebuild(self.app.pk)
        entry.delete()
        rebuild(self.app.pk)
        response = self.client.get(
            reverse("graph", args=[self.app.pk]), {"version": "1", "format": "json"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("NODE1", response.content.decode())
        response = self.client.get(reverse("graph", args=[self.app.pk]), {"version": "1"})
        self.assertContains(response, "source evidence is no longer available")
        self.assertNotContains(response, 'id="graph-canvas"')

    def test_version_lookup_is_application_scoped_and_validated(self):
        self.source()
        rebuild(self.app.pk)
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.assertEqual(
            self.client.get(reverse("graph", args=[self.other.pk]), {"version": "1"}).status_code,
            404,
        )
        for value in ["abc", "-1", "9999999999999999"]:
            self.assertEqual(
                self.client.get(
                    reverse("graph", args=[self.app.pk]), {"version": value}
                ).status_code,
                400,
            )

    def test_one_knowledge_menu_with_sources_graph_quality_and_versions(self):
        self.source()
        rebuild(self.app.pk)
        response = self.client.get(reverse("graph", args=[self.app.pk]))
        self.assertNotContains(response, ">Graph &amp; quality</a>")
        self.assertContains(response, 'aria-label="Knowledge views"')
        for title in ["Graph", "Sources", "Quality", "Versions"]:
            self.assertContains(response, f">{title}</a>")
