import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.graphs import STALL_MINUTES, build_graph, process_next_graph, rebuild
from platform_core.models import (
    AIConfiguration,
    ApplicationGrant,
    FeatureSwitch,
    KnowledgeGraph,
)
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


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class GraphRunTests(TestCase):
    """One run at a time, and never a paid call nobody asked for."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        GraphTests.source(self)
        self.url = reverse("graph", args=[self.app.pk])
        AIConfiguration.objects.create(
            application=self.app,
            purpose="graph_generation",
            enabled=True,
            provider="claude",
            model="claude-sonnet-5",
            input_rate=Decimal("3"),
            output_rate=Decimal("15"),
            configured_by=self.owner,
        )

    def generate(self, choice="claude:claude-sonnet-5"):
        return self.client.post(self.url, {"action": "generate", "model_choice": choice})

    def graph(self):
        return KnowledgeGraph.objects.get(application=self.app)

    def test_a_second_generation_is_refused_while_one_is_running(self):
        self.generate()
        first = self.graph()
        self.assertEqual(first.status, "queued")
        self.generate("claude:claude-opus-5")
        second = self.graph()
        # The live run keeps its own model and its own start time.
        self.assertEqual(second.requested_model, "claude-sonnet-5")
        self.assertEqual(second.started_at, first.started_at)

    def test_retry_is_refused_while_the_run_is_still_live(self):
        self.generate()
        KnowledgeGraph.objects.filter(application=self.app).update(status="building")
        self.client.post(self.url, {"action": "retry"})
        self.assertEqual(self.graph().status, "building")

    def test_a_stalled_run_can_be_retried_so_a_crash_cannot_wedge_it(self):
        self.generate()
        KnowledgeGraph.objects.filter(application=self.app).update(
            status="building", updated_at=timezone.now() - timedelta(minutes=STALL_MINUTES + 1)
        )
        self.client.post(self.url, {"action": "retry"})
        self.assertEqual(self.graph().status, "queued")

    def test_a_failed_paid_run_is_not_repeated_when_sources_change(self):
        """The invariant: only a run someone queued may spend money."""
        self.generate()
        with patch("platform_core.graph_ai.enrich_graph", side_effect=RuntimeError("provider")):
            with self.assertRaises(RuntimeError):
                rebuild(self.app.pk)
        self.assertEqual(self.graph().status, "failed")
        # A source changes, so the worker rebuilds - structurally, with no AI call.
        GraphTests.source(self, "# Another\nA second requirement.")
        with patch("platform_core.graph_ai.enrich_graph") as enrich:
            rebuild(self.app.pk)
        enrich.assert_not_called()
        self.assertEqual(self.graph().requested_model, "")

    def test_an_explicit_retry_does_repeat_the_requested_model(self):
        self.generate()
        with patch("platform_core.graph_ai.enrich_graph", side_effect=RuntimeError("provider")):
            with self.assertRaises(RuntimeError):
                rebuild(self.app.pk)
        self.client.post(self.url, {"action": "retry"})
        self.assertEqual(self.graph().status, "queued")
        self.assertEqual(self.graph().requested_model, "claude-sonnet-5")

    def test_the_page_reports_the_stage_the_run_is_actually_in(self):
        self.generate()
        KnowledgeGraph.objects.filter(application=self.app).update(
            status="building", stage="Extracting relationships with claude-sonnet-5"
        )
        response = self.client.get(self.url)
        self.assertContains(response, "Extracting relationships with claude-sonnet-5")
        self.assertContains(response, "data-document-pending")
        # No button that would start a second paid run while this one is live.
        self.assertNotContains(response, 'value="retry"')

    def test_a_regenerate_keeps_the_last_good_graph_on_screen(self):
        """Requesting a rebuild must not blank the workspace, or a failure strands you."""
        from platform_core.models import GraphRevision

        rebuild(self.app.pk)
        revision = GraphRevision.objects.get(application=self.app)
        revision.published_at = timezone.now()
        revision.save(update_fields=["published_at"])
        self.generate()
        response = self.client.get(self.url)
        # The run is reported in a banner...
        self.assertContains(response, "run-banner")
        self.assertContains(response, "claude-sonnet-5")
        # ...and the published graph is still rendered beneath it, not replaced.
        self.assertContains(response, "Generated graph")
        self.assertContains(response, "Latest available version")
        self.assertNotContains(response, "Return to current")

    def test_a_failed_run_still_shows_the_previous_version_and_offers_retry(self):
        from platform_core.models import GraphRevision

        rebuild(self.app.pk)
        GraphRevision.objects.filter(application=self.app).update(published_at=timezone.now())
        self.generate()
        KnowledgeGraph.objects.filter(application=self.app).update(
            status="failed", started_at=timezone.now() - timedelta(minutes=STALL_MINUTES + 1)
        )
        response = self.client.get(self.url)
        self.assertContains(response, "The last graph generation failed")
        self.assertContains(response, "nothing was lost")
        self.assertContains(response, 'value="retry"')
        self.assertContains(response, "Generated graph")

    def test_a_failure_says_what_to_do_about_it(self):
        """"It failed" is not actionable; the reason is recorded and shown."""
        from django.core.exceptions import ValidationError as Invalid

        self.generate()
        with patch(
            "platform_core.graph_ai.enrich_graph",
            side_effect=Invalid("Claude rejected the credentials for this application."),
        ):
            with self.assertRaises(Invalid):
                rebuild(self.app.pk)
        self.assertIn("rejected the credentials", self.graph().failure_reason)
        KnowledgeGraph.objects.filter(application=self.app).update(
            started_at=timezone.now() - timedelta(minutes=STALL_MINUTES + 1)
        )
        self.assertContains(self.client.get(self.url), "rejected the credentials")

    def test_an_internal_fault_is_not_echoed_to_the_page(self):
        self.generate()
        with patch(
            "platform_core.graph_ai.enrich_graph", side_effect=RuntimeError("/srv/secret/path")
        ):
            with self.assertRaises(RuntimeError):
                rebuild(self.app.pk)
        reason = self.graph().failure_reason
        self.assertNotIn("/srv/secret/path", reason)
        self.assertIn("stopped unexpectedly", reason)


class ProviderDiagnosisTests(SimpleTestCase):
    """A failure category is safe to show; the provider's own text is not."""

    def diagnose(self, failure):
        from platform_core.claude_agents import diagnosis

        return diagnosis(failure)

    def test_an_expired_login_is_named_as_such(self):
        from claude_agent_sdk._errors import ResultError

        message = self.diagnose(
            ResultError("Failed to authenticate: OAuth session expired and could not be refreshed")
        )
        self.assertIn("claude /login", message)
        # The provider's own wording never reaches the user.
        self.assertNotIn("OAuth session expired", message)

    def test_a_timeout_says_so(self):
        self.assertIn("did not respond in time", self.diagnose(TimeoutError("timed out")))

    def test_anything_unrecognised_falls_back_to_the_generic_message(self):
        self.assertIn("response unavailable", self.diagnose(ValueError("weird internal state")))
