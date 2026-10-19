"""An application with no documents and no tickets has no graph version, and says so."""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.graphs import fingerprint, rebuild
from platform_core.models import GraphRevision, KnowledgeGraph

from . import test_documents
from .test_benchmark import SETTINGS


@override_settings(**SETTINGS)
class EmptyGraphTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.client.force_login(self.owner)

    def test_a_rebuild_with_no_sources_saves_no_version(self):
        self.assertTrue(rebuild(self.app.pk))
        self.assertFalse(GraphRevision.objects.filter(application=self.app).exists())
        graph = KnowledgeGraph.objects.get(application=self.app)
        # Idle on the fingerprint of no sources: the worker leaves it alone.
        self.assertEqual((graph.status, graph.fingerprint), ("idle", fingerprint(self.app.pk)))
        self.assertTrue(rebuild(self.app.pk))
        self.assertFalse(GraphRevision.objects.filter(application=self.app).exists())

    def test_a_requested_run_with_no_sources_is_dropped_not_kept_for_later(self):
        KnowledgeGraph.objects.create(application=self.app, status="queued",
                                      requested_provider="claude", requested_model="m")
        rebuild(self.app.pk)
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertEqual((graph.status, graph.requested_model), ("idle", ""))

    def test_the_graph_and_versions_say_there_is_nothing_to_build_from(self):
        rebuild(self.app.pk)
        url = reverse("graph", args=[self.app.pk])
        graph = self.client.get(url)
        self.assertContains(graph, "There are no documents or tickets yet")
        self.assertNotContains(graph, "Version 1")
        versions = self.client.get(url, {"tab": "versions"})
        self.assertContains(versions, "No versions yet: there are no documents or tickets")
