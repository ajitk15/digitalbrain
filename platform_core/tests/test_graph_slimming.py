"""Old graph versions keep their sources, nodes and edges only as rows.

Phase 5 (F12): each version's JSON was the whole graph again, so storage grew
with versions rather than with change. Once a version's lists are rows that
read back exactly, `graph_store.slim` drops them from its JSON - except for the
newest few and the latest published - and loading the version puts them back.
"""

from django.test import TestCase, override_settings
from django.utils import timezone

from platform_core import graph_store
from platform_core.graphs import rebuild
from platform_core.models import GraphRevision, KnowledgeEntry
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_graph_store import TABLE


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SlimmingTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.entry = add_knowledge(self.owner, self.app.pk, "Runbook", "# Restart\n" + TABLE)
        self.versions = []
        for step in range(graph_store.KEEP_WHOLE + 3):
            KnowledgeEntry.objects.filter(pk=self.entry.pk).update(
                content=f"# Restart\nStep {step}.\n" + TABLE, digest=f"{step:064d}"
            )
            self.assertTrue(rebuild(self.app.pk))
            self.versions.append(
                GraphRevision.objects.filter(application=self.app).order_by("-number").first()
            )
        self.whole = {v.pk: v.data for v in self.versions}

    def raw(self, revision):
        """The JSON as stored, without the load putting anything back."""
        return GraphRevision.objects.filter(pk=revision.pk).values_list("data", flat=True)[0]

    def test_old_versions_give_up_their_lists_and_read_back_whole(self):
        oldest = self.versions[0]
        self.assertTrue(graph_store.slim_step())
        stored = self.raw(oldest)
        self.assertTrue(stored["slimmed"])
        for element in graph_store.ELEMENTS:
            self.assertNotIn(element, stored)
        # Every reader of `data` sees the version exactly as it was saved.
        self.assertEqual(GraphRevision.objects.get(pk=oldest.pk).data, self.whole[oldest.pk])
        deferred = GraphRevision.objects.defer("data").get(pk=oldest.pk)
        self.assertEqual(deferred.data, self.whole[oldest.pk])

    def test_the_newest_and_the_latest_published_stay_whole(self):
        published = self.versions[1]
        GraphRevision.objects.filter(pk=published.pk).update(published_at=timezone.now())
        while graph_store.slim_step():
            pass
        newest = self.versions[-graph_store.KEEP_WHOLE:]
        for revision in [*newest, published]:
            self.assertNotIn("slimmed", self.raw(revision), revision.number)
        slimmed = [v for v in self.versions if v not in newest and v != published]
        self.assertTrue(slimmed)
        for revision in slimmed:
            self.assertTrue(self.raw(revision)["slimmed"], revision.number)
            self.assertEqual(GraphRevision.objects.get(pk=revision.pk).data,
                             self.whole[revision.pk])

    def test_a_version_whose_rows_do_not_read_back_keeps_its_json(self):
        oldest = self.versions[0]
        GraphRevision.objects.filter(pk=oldest.pk).update(
            data={**self.whole[oldest.pk], "nodes": []}
        )
        with self.assertLogs("platform_core.graph_store", "WARNING"):
            self.assertFalse(graph_store.slim(oldest))
        self.assertNotIn("slimmed", self.raw(oldest))

    def test_an_unrecorded_version_is_never_slimmed(self):
        GraphRevision.objects.filter(pk=self.versions[0].pk).update(stored=False)
        self.assertFalse(graph_store.slim(self.versions[0]))
        self.assertNotIn(
            self.versions[0].pk, graph_store.slimmable(self.app.pk).values_list("pk", flat=True)
        )

    def test_readers_that_skip_the_model_still_see_the_whole_version(self):
        from types import SimpleNamespace

        from platform_core import graph_ai
        from platform_core.source_library import graph_usage

        oldest = self.versions[0]
        source = self.whole[oldest.pk]["sources"][0]
        entry = SimpleNamespace(pk=source["id"], digest=source["digest"])
        before = graph_usage(self.app, [entry])
        self.assertTrue(before[source["id"]])
        self.assertTrue(graph_store.slim(oldest))
        # A pinned conversation answers from `prepared`, which reads raw JSON.
        with graph_ai._prepared_lock:
            graph_ai._prepared.clear()
        revision = GraphRevision.objects.defer("data").get(pk=oldest.pk)
        self.assertEqual(graph_ai.prepared(revision).data, self.whole[oldest.pk])
        # The Documents page lists which versions read each source.
        self.assertEqual(graph_usage(self.app, [entry]), before)

    def test_it_is_a_maintenance_step(self):
        from platform_core.document_worker import maintenance_steps

        self.assertIn(graph_store.slim_step, maintenance_steps())
