"""AI runs reuse unchanged sources; auto-refresh is opt-in, owner-only and self-stopping."""

import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import graph_ai
from platform_core.graphs import process_next_graph, rebuild, revision_added
from platform_core.models import (
    AIConfiguration,
    ApplicationGrant,
    AuditEvent,
    GraphRevision,
    KnowledgeEntry,
    KnowledgeGraph,
)
from platform_core.workbench import add_knowledge

from . import test_documents


def answer_for(entries_by_id):
    """A stand-in model: one typed relationship from each source's single line."""

    def extract(user, app_id, purpose, question, citations, **options):
        source_id = citations[0]["id"]
        text = entries_by_id()[source_id].content.rstrip(".")
        subject, _, obj = text.partition(" depends on ")
        return json.dumps(
            {
                "relationships": [
                    {
                        "subject": subject,
                        "subject_type": "Component",
                        "relation": "depends on",
                        "object": obj,
                        "object_type": "Component",
                        "source_id": source_id,
                        "quote": text,
                    }
                ]
            }
        )

    return extract


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class IncrementalTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.alpha = add_knowledge(self.owner, self.app.pk, "Alpha", "Alpha depends on Queue.")
        self.beta = add_knowledge(self.owner, self.app.pk, "Beta", "Beta depends on Store.")
        AIConfiguration.objects.create(
            application=self.app,
            purpose="graph_generation",
            provider="claude",
            model="claude-sonnet-5",
            input_rate=Decimal("2"),
            output_rate=Decimal("10"),
            configured_by=self.owner,
        )
        self.extract = answer_for(
            lambda: {str(e.pk): e for e in KnowledgeEntry.objects.filter(application=self.app)}
        )

    def request(self):
        graph, _ = KnowledgeGraph.objects.get_or_create(application=self.app)
        KnowledgeGraph.objects.filter(pk=graph.pk).update(
            status="queued",
            fingerprint="",
            requested_provider="claude",
            requested_model="claude-sonnet-5",
            requested_by=self.owner,
        )

    def run_ai(self):
        self.request()
        with patch("platform_core.ai.invoke_ai", side_effect=self.extract) as invoke:
            rebuild(self.app.pk)
        return invoke, GraphRevision.objects.order_by("-number").first()

    def change(self, entry, content):
        KnowledgeEntry.objects.filter(pk=entry.pk).update(
            content=content, digest=hashlib.sha256(content.encode()).hexdigest()
        )

    def test_an_unchanged_source_is_not_paid_for_twice(self):
        first, revision = self.run_ai()
        self.assertEqual(first.call_count, 2)
        self.assertEqual(set(revision.data["ai_sources"]), {str(self.alpha.pk), str(self.beta.pk)})
        self.change(self.beta, "Beta depends on Cache.")
        second, revision = self.run_ai()
        # Only the changed source went to the model.
        self.assertEqual(second.call_count, 1)
        self.assertEqual(second.call_args.args[4][0]["id"], str(self.beta.pk))
        self.assertEqual(revision.quality["reused_sources"], 1)
        self.assertEqual(revision.quality["reused_relationships"], 1)
        labels = {n["label"] for n in revision.data["nodes"] if n["kind"] == "entity"}
        self.assertEqual(labels, {"Alpha", "Queue", "Beta", "Cache"})

    def test_nothing_changed_means_no_model_call(self):
        self.run_ai()
        again, revision = self.run_ai()
        self.assertEqual(again.call_count, 0)
        self.assertEqual(revision.quality["reused_sources"], 2)

    def test_a_reused_relationship_is_still_verified_when_cited(self):
        _, revision = self.run_ai()
        revision.published_at = timezone.now()
        revision.save()
        cited = graph_ai.graph_citations(self.app.pk, "What does Alpha depend on?")
        self.assertIn(str(self.alpha.pk), {c["id"] for c in cited})

    def test_another_ontology_version_is_extracted_again(self):
        self.run_ai()
        with patch.object(graph_ai, "ONTOLOGY_VERSION", 99):
            again, _ = self.run_ai()
        self.assertEqual(again.call_count, 2)

    def test_a_source_cut_short_by_the_cap_is_asked_again(self):
        with patch.object(graph_ai, "MAX_RELATIONSHIPS", 1):
            _, revision = self.run_ai()
        self.assertEqual(len(revision.data["ai_sources"]), 1)
        again, revision = self.run_ai()
        self.assertEqual(again.call_count, 1)
        self.assertEqual(len(revision.data["ai_sources"]), 2)

    def test_sources_added_since_publishing_are_counted(self):
        _, revision = self.run_ai()
        self.assertEqual(revision_added(revision, self.app.pk), 0)
        add_knowledge(self.owner, self.app.pk, "Gamma", "Gamma depends on Alpha.")
        self.assertEqual(revision_added(revision, self.app.pk), 1)
        revision.published_at = timezone.now()
        revision.save()
        response = self.client.get(reverse("graph", args=[self.app.pk]) + "?tab=versions")
        self.assertContains(response, "1 source(s) were added")


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AutoRefreshTests(TestCase):
    setUp = IncrementalTests.setUp

    def switch(self, user=None, enabled=True):
        self.client.force_login(
            user or self.owner, backend="django.contrib.auth.backends.ModelBackend"
        )
        values = {"action": "auto_refresh"}
        if enabled:
            values["enabled"] = "on"
        return self.client.post(reverse("graph-generate", args=[self.app.pk]), values)

    def settle(self):
        """The background worker, run until it has nothing left to do."""
        with patch("platform_core.ai.invoke_ai", side_effect=self.extract) as invoke:
            for _ in range(10):
                if not process_next_graph():
                    break
            else:
                self.fail("The graph worker never settled.")
        return invoke

    def test_off_by_default_a_source_change_spends_nothing(self):
        self.settle()
        add_knowledge(self.owner, self.app.pk, "Gamma", "Gamma depends on Alpha.")
        self.assertEqual(self.settle().call_count, 0)

    def test_on_a_change_runs_an_incremental_ai_generation_as_its_owner(self):
        self.settle()
        self.assertEqual(self.switch().status_code, 302)
        add_knowledge(self.owner, self.app.pk, "Gamma", "Gamma depends on Alpha.")
        invoke = self.settle()
        self.assertGreaterEqual(invoke.call_count, 1)
        self.assertTrue(all(call.args[0] == self.owner for call in invoke.call_args_list))
        latest = GraphRevision.objects.order_by("-number").first()
        self.assertEqual(latest.model, "claude-sonnet-5")
        self.assertIsNone(latest.published_at)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="graph.generation_requested", details__automatic=True
            ).exists()
        )
        # Nothing changed since: the worker is quiet and spends nothing.
        self.assertEqual(self.settle().call_count, 0)

    def test_only_an_owner_switches_it(self):
        self.assertEqual(self.switch(user=self.viewer).status_code, 403)
        self.assertIsNone(KnowledgeGraph.objects.filter(application=self.app).first())

    def test_losing_access_stops_it_and_says_why(self):
        self.settle()
        self.switch()
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(role="viewer")
        KnowledgeEntry.objects.filter(pk=self.beta.pk).update(active=False)
        self.assertEqual(self.settle().call_count, 0)
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertIsNone(graph.auto_refresh_by)
        self.assertIn("may no longer change", graph.auto_refresh_note)

    def test_switching_off_clears_who_pays(self):
        self.switch()
        self.switch(enabled=False)
        self.assertIsNone(KnowledgeGraph.objects.get(application=self.app).auto_refresh_by)
