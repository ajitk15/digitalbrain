"""A graph version's elements are rows, held once across the versions they are in.

Phase 3 of the scale work: `GraphElement` beside `GraphRevision.data`, written
in the transaction that saves a version, with ranges of version numbers so a
rebuild after one source changes writes that source's rows and nothing else.
"""

import hashlib
import io
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import graph_store
from platform_core.graph_ai import citations_from
from platform_core.graphs import published_revision, rebuild, revision_added, revision_drift
from platform_core.models import (
    ApplicationGrant,
    GraphElement,
    GraphRevision,
    KnowledgeEntry,
    KnowledgeGraph,
)
from platform_core.workbench import add_knowledge

from . import test_documents

TABLE = "| Step | Owner |\n| --- | --- |\n| Restart | Ops |\n| Verify | QA |\n"


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class GraphStoreTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.first = add_knowledge(self.owner, self.app.pk, "Runbook", "# Restart\n" + TABLE)
        self.second = add_knowledge(
            self.owner, self.app.pk, "Guide", "# Verify\nCheck the queue.\n"
        )

    def latest(self):
        return GraphRevision.objects.filter(application=self.app).order_by("-number").first()

    def build(self):
        self.assertTrue(rebuild(self.app.pk))
        return self.latest()

    def test_a_saved_version_is_recorded_and_reads_back_exactly(self):
        revision = self.build()
        self.assertTrue(revision.stored)
        self.assertTrue(graph_store.verify(revision))
        for element in graph_store.ELEMENTS:
            with self.subTest(element=element):
                self.assertEqual(graph_store.read(revision, element), revision.data[element])

    def test_a_rebuild_after_one_change_writes_only_that_source(self):
        first = self.build()
        before = GraphElement.objects.count()
        KnowledgeEntry.objects.filter(pk=self.second.pk).update(
            content="# Verify\nCheck the queue twice.\n", digest="2" * 64
        )
        second = self.build()
        self.assertEqual(second.number, first.number + 1)
        added = GraphElement.objects.filter(first_number=second.number)
        self.assertTrue(added.exists())
        # Only the changed source's rows, and the source list entry naming it.
        self.assertEqual(set(added.values_list("knowledge_id", flat=True)), {str(self.second.pk)})
        self.assertLess(added.count(), before)
        self.assertTrue(graph_store.verify(first), "the older version still reads back")
        self.assertTrue(graph_store.verify(second))
        untouched = GraphElement.objects.filter(knowledge_id=str(self.first.pk))
        self.assertFalse(untouched.exclude(first_number=first.number).exists())
        self.assertFalse(untouched.filter(last_number__isnull=False).exists())

    def test_a_source_taken_out_closes_its_rows(self):
        first = self.build()
        KnowledgeEntry.objects.filter(pk=self.second.pk).update(active=False)
        second = self.build()
        gone = GraphElement.objects.filter(knowledge_id=str(self.second.pk))
        self.assertEqual(set(gone.values_list("last_number", flat=True)), {first.number})
        self.assertNotIn(
            str(self.second.pk), {s["id"] for s in graph_store.read(second, "sources")}
        )
        self.assertTrue(graph_store.verify(first))

    def test_a_repeated_element_is_counted_not_lost(self):
        revision = self.build()
        edge = revision.data["edges"][0]
        data = {**revision.data, "edges": [edge, edge]}
        twice = GraphRevision.objects.create(
            application=self.app, number=revision.number + 1, fingerprint="x", data=data
        )
        self.assertTrue(graph_store.record(twice))
        self.assertTrue(graph_store.verify(twice))
        self.assertEqual(graph_store.read(twice, "edges"), [edge, edge])

    def test_an_older_version_is_left_to_its_json(self):
        revision = self.build()
        old = GraphRevision.objects.create(
            application=self.app,
            number=revision.number + 5,
            fingerprint="old",
            data={"sources": [{"id": "x", "digest": "y"}]},
        )
        self.assertTrue(graph_store.record(old))
        late = GraphRevision.objects.create(
            application=self.app, number=revision.number + 1, fingerprint="late", data={}
        )
        with self.assertLogs("platform_core.graph_store", "WARNING"):
            self.assertFalse(graph_store.record(late))
        late.refresh_from_db()
        self.assertFalse(late.stored)
        self.assertEqual(graph_store.recorded_sources(late), [])
        self.assertTrue(graph_store.verify(revision), "ranges were not moved by it")

    def test_versions_saved_before_rows_are_backfilled_in_order(self):
        built = self.build()
        GraphElement.objects.all().delete()
        GraphRevision.objects.update(stored=False)
        extra = GraphRevision.objects.create(
            application=self.app, number=built.number + 1, fingerprint="b", data=built.data
        )
        self.assertEqual(graph_store.behind(), [built.pk, extra.pk])
        with self.assertRaises(CommandError):
            call_command("graph_store", check=True, stdout=io.StringIO())
        output = io.StringIO()
        call_command("graph_store", stdout=output)
        self.assertIn("2 graph versions", output.getvalue())
        call_command("graph_store", check=True, stdout=io.StringIO())
        # The same graph twice is one set of rows, open across both versions.
        self.assertFalse(GraphElement.objects.filter(first_number=extra.number).exists())
        for revision in (built, extra):
            revision.refresh_from_db()
            self.assertTrue(graph_store.verify(revision))

    def test_the_maintenance_step_says_when_it_worked(self):
        self.build()
        self.assertFalse(graph_store.record_step())
        GraphElement.objects.all().delete()
        GraphRevision.objects.update(stored=False)
        with self.assertLogs("platform_core.graph_store", "INFO"):
            self.assertTrue(graph_store.record_step())

    def test_rows_left_by_deleted_versions_do_not_join_the_next(self):
        self.build()
        GraphRevision.objects.all().delete()
        self.assertTrue(GraphElement.objects.exists())
        KnowledgeGraph.objects.update(fingerprint="")
        revision = self.build()
        self.assertEqual(revision.number, 1)
        self.assertTrue(graph_store.verify(revision))

    def test_another_applications_rows_are_not_read(self):
        mine = self.build()
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        add_knowledge(self.owner, self.other.pk, "Other", "# Theirs\nNot ours.\n")
        self.assertTrue(rebuild(self.other.pk))
        self.assertTrue(graph_store.verify(mine))
        theirs = {s["id"] for s in graph_store.read(mine, "sources")}
        self.assertEqual(theirs, {str(self.first.pk), str(self.second.pk)})

    def test_drift_is_read_from_rows_without_the_whole_graph(self):
        revision = self.build()
        revision.published_at = revision.created_at
        revision.save(update_fields=["published_at"])
        published = published_revision(self.app.pk)
        self.assertIn("data", published.get_deferred_fields())
        self.assertEqual(revision_drift(published, self.app.pk), (0, 2))
        self.assertEqual(revision_added(published, self.app.pk), 0)
        self.assertIn("data", published.get_deferred_fields(), "the graph was not read")
        add_knowledge(self.owner, self.app.pk, "New", "# New\nAdded later.\n")
        self.assertEqual(revision_added(published, self.app.pk), 1)

    def test_a_reordered_list_reads_back_in_its_new_order(self):
        revision = self.build()
        edges = revision.data["edges"]
        moved = edges[1:] + edges[:1]
        later = GraphRevision.objects.create(
            application=self.app,
            number=revision.number + 1,
            fingerprint="moved",
            data={**revision.data, "edges": moved},
        )
        self.assertTrue(graph_store.record(later))
        self.assertEqual(graph_store.read(later, "edges"), moved)
        self.assertEqual(graph_store.read(revision, "edges"), edges)
        # Only the one edge that lost its place was written again.
        self.assertEqual(
            GraphElement.objects.filter(element="edges", first_number=later.number).count(), 1
        )

    def test_a_list_with_no_room_left_is_written_afresh(self):
        revision = self.build()
        nodes = revision.data["nodes"]
        extra = {"id": "new", "kind": "section", "label": "Inserted"}
        crowded = nodes[:1] + [extra] + nodes[1:]
        later = GraphRevision.objects.create(
            application=self.app,
            number=revision.number + 1,
            fingerprint="crowded",
            data={**revision.data, "nodes": crowded},
        )
        keys = list(
            GraphElement.objects.filter(element="nodes").order_by("key").values_list("pk", "key")
        )
        # Squeeze the first two rows together so nothing fits between them.
        GraphElement.objects.filter(pk=keys[1][0]).update(key=keys[0][1] + 1)
        with self.assertLogs("platform_core.graph_store", "INFO"):
            self.assertTrue(graph_store.record(later))
        self.assertEqual(graph_store.read(later, "nodes"), crowded)
        self.assertEqual(
            GraphElement.objects.filter(element="nodes", first_number=later.number).count(),
            len(crowded),
        )


class PlacementTests(SimpleTestCase):
    def test_the_longest_run_in_order_is_kept(self):
        keys = [10, None, 30, 20, 40]
        kept = sorted(graph_store.increasing(keys))
        self.assertEqual(len(kept), 3)
        self.assertEqual([keys[i] for i in kept], sorted(keys[i] for i in kept))
        self.assertEqual(graph_store.increasing([None, None]), set())

    def test_new_items_take_keys_between_their_neighbours(self):
        opened = [(1, "a", 100), (2, "c", 200)]
        items = [(None, "a", {}), (None, "b", {}), (None, "c", {}), (None, "d", {})]
        keys, stays, kept = graph_store.placed(items, opened)
        self.assertEqual(stays, [True, False, True, False])
        self.assertEqual(kept, {1, 2})
        self.assertEqual(keys[0], 100)
        self.assertTrue(100 < keys[1] < 200 < keys[3])

    def test_no_room_is_said_rather_than_overlapping(self):
        opened = [(1, "a", 100), (2, "c", 101)]
        items = [(None, "a", {}), (None, "b", {}), (None, "c", {})]
        self.assertIsNone(graph_store.placed(items, opened))
        with patch.object(graph_store, "KEY_SPACE", 3):
            self.assertIsNone(graph_store.spaced(5))


QUOTE_A = "Service Alpha depends on Queue Beta, see C-09."
QUOTE_B = "Queue Beta depends on Store Gamma."
QUOTE_C = "Store Gamma implements requirement BR-05 for C-09."


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class RowsAnswerTests(TestCase):
    """Answers from rows are the answers from the JSON: the same citations, in order."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.sources = [
            add_knowledge(self.owner, self.app.pk, title, text)
            for title, text in (
                ("Architecture", QUOTE_A),
                ("Queues", QUOTE_B),
                ("Controls", QUOTE_C),
            )
        ]
        rebuild(self.app.pk)
        base = GraphRevision.objects.get(application=self.app)
        data = base.data
        entity = {
            name: hashlib.sha256(name.encode()).hexdigest()[:24]
            for name in ("Service Alpha", "Queue Beta", "Store Gamma", "BR-05")
        }
        nodes = data["nodes"] + [
            {"id": key, "kind": "entity", "label": name} for name, key in entity.items()
        ]
        typed = [
            {
                "source": entity[start],
                "target": entity[end],
                "relation": relation,
                "knowledge_id": str(source.pk),
                "digest": source.digest,
                "evidence": source.content,
                "line": 1,
                "inferred": True,
            }
            for source, start, relation, end in (
                (self.sources[0], "Service Alpha", "depends on", "Queue Beta"),
                (self.sources[1], "Queue Beta", "depends on", "Store Gamma"),
                (self.sources[2], "Store Gamma", "implements", "BR-05"),
            )
        ]
        self.revision = GraphRevision.objects.create(
            application=self.app,
            number=base.number + 1,
            fingerprint="typed",
            data={**data, "nodes": nodes, "edges": data["edges"] + typed},
        )
        self.assertTrue(graph_store.record(self.revision))
        self.revision.refresh_from_db()

    def answers(self, question, references=0):
        memory = citations_from(
            self.app.pk,
            self.revision.data,
            self.revision.number,
            question,
            viewer=self.owner,
            references=references,
        )
        rows = citations_from(
            self.app.pk,
            None,
            self.revision.number,
            question,
            viewer=self.owner,
            references=references,
            graph=graph_store.RowsGraph(self.revision),
        )
        return memory, rows

    def test_rows_answer_exactly_as_memory_does(self):
        for question, references in (
            ("What does Service Alpha depend on?", 0),
            ("Which store implements BR-05?", 0),
            ("Queue Beta outage, check C-09", 4),
            ("Nothing here matches", 0),
        ):
            with self.subTest(question=question):
                memory, rows = self.answers(question, references)
                self.assertEqual(rows, memory)

    def test_the_walk_reaches_the_same_sources(self):
        memory, rows = self.answers("Service Alpha")
        self.assertTrue(any(c["hops"] for c in memory), "the walk found something")
        self.assertEqual(rows, memory)

    @override_settings(GRAPH_CANDIDATES="rows")
    def test_the_setting_answers_from_rows_without_the_graph(self):
        from platform_core.graph_ai import graph_citations

        self.revision.published_at = self.revision.created_at
        self.revision.save(update_fields=["published_at"])
        with patch("platform_core.graph_ai.prepared") as loaded:
            found = graph_citations(self.app.pk, "What does Service Alpha depend on?")
        loaded.assert_not_called()
        self.assertTrue(found)
