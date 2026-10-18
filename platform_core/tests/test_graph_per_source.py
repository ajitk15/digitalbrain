"""The structural graph is built a source at a time and takes every source.

It used to take the oldest `MAX_SOURCES` (100) and cap the whole graph at
6,000 nodes, so a large library answered from its oldest hundred documents
and whether a row was included depended on what came before it.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from platform_core import graphs
from platform_core.graphs import build_graph, rebuild, sources_for
from platform_core.models import GraphRevision, KnowledgeEntry
from platform_core.workbench import add_knowledge

from . import test_documents

TABLE = "| Requirement | Owner |\n| --- | --- |\n| {name} | {owner} |\n"


@override_settings(DOCUMENT_AUTO_CONVERT=False)
class PerSourceTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        graphs._fragments.clear()
        self.addCleanup(graphs._fragments.clear)

    def add(self, title, content, app=None):
        return add_knowledge(self.owner, (app or self.app).pk, title, content)

    def test_every_source_is_in_the_graph_past_the_old_hundred(self):
        for number in range(130):
            self.add(f"Requirement {number}", TABLE.format(name=f"BR-{number:03}", owner="Ops"))
        rebuild(self.app.pk)
        revision = GraphRevision.objects.filter(application=self.app).latest("number")
        self.assertEqual(revision.quality["processed_sources"], 130)
        self.assertEqual(revision.quality["source_count"], 130)
        self.assertFalse([w for w in revision.quality["warnings"] if "Only the first" in w])
        titles = {source["title"] for source in revision.data["sources"]}
        self.assertIn("Requirement 129", titles)

    def test_a_source_is_capped_by_its_own_size_never_by_what_came_before(self):
        rows = "".join(f"| BR-{n:03} | Ops |\n" for n in range(6))
        big = self.add("Big", "| Requirement | Owner |\n| --- | --- |\n" + rows)
        small = self.add("Small", TABLE.format(name="BR-900", owner="Ops"))
        with patch.object(graphs, "MAX_RECORDS_PER_SOURCE", 3):
            data, quality = build_graph([big, small])
            alone, _ = build_graph([small])
        self.assertEqual(quality["included_rows"], 3 + 1)
        self.assertIn("1 source(s) reached the row or node limit for one source.",
                      quality["warnings"])
        mine = [edge for edge in data["edges"] if edge["knowledge_id"] == str(small.pk)]
        self.assertEqual(mine, alone["edges"])

    def test_a_value_two_sources_share_is_one_node_labelled_by_the_first(self):
        first = self.add("First", TABLE.format(name="BR-001", owner="Ops"))
        second = self.add("Second", TABLE.format(name="BR-002", owner="Ops"))
        data, _ = build_graph([first, second])
        owners = [node for node in data["nodes"] if node.get("field") == "Owner"]
        self.assertEqual(len(owners), 1)
        joined = {edge["knowledge_id"] for edge in data["edges"]
                  if edge["target"] == owners[0]["id"]}
        self.assertEqual(joined, {str(first.pk), str(second.pk)})
        self.assertEqual(len({node["id"] for node in data["nodes"]}), len(data["nodes"]))

    def test_a_rebuild_reads_only_the_source_that_changed(self):
        for number in range(5):
            self.add(f"Requirement {number}", TABLE.format(name=f"BR-{number}", owner="Ops"))
        build_graph(list(sources_for(self.app.pk)))
        entries = list(sources_for(self.app.pk))
        entries[2].content = TABLE.format(name="BR-2", owner="Platform")
        entries[2].digest = "0" * 64
        with patch.object(graphs, "structure", wraps=graphs.structure) as read:
            first, _ = build_graph(entries)
        self.assertEqual([call.args[1].title for call in read.call_args_list], ["Requirement 2"])
        self.assertIn("Owner: Platform", {node["label"] for node in first["nodes"]})
        graphs._fragments.clear()
        again, _ = build_graph(entries)
        self.assertEqual(first, again)

    def test_what_a_caller_changes_in_a_graph_never_reaches_the_cache(self):
        entry = self.add("Policy", TABLE.format(name="BR-001", owner="Ops"))
        data, _ = build_graph([entry])
        data["nodes"][0]["label"] = "changed"
        data["edges"][0]["relation"] = "changed"
        again, _ = build_graph([entry])
        self.assertNotEqual(again["nodes"][0]["label"], "changed")
        self.assertNotEqual(again["edges"][0]["relation"], "changed")

    def test_one_application_never_reads_another_ones_cached_structure(self):
        mine = self.add("Policy", TABLE.format(name="BR-001", owner="Ops"))
        theirs = KnowledgeEntry(application=self.other, title=mine.title, content=mine.content,
                                digest=mine.digest)
        ours, _ = build_graph([mine])
        other, _ = build_graph([theirs])
        self.assertFalse({n["id"] for n in ours["nodes"]} & {n["id"] for n in other["nodes"]})

    def test_the_source_check_says_when_it_read_only_the_oldest(self):
        from django.urls import reverse

        from platform_core import source_map

        for number in range(3):
            self.add(f"Requirement {number}", TABLE.format(name=f"BR-{number}", owner="Ops"))
        source_map._cache.clear()
        self.addCleanup(source_map._cache.clear)
        with patch.object(source_map, "MAX_MAPPED", 2):
            page = self.client.get(reverse("source-check", args=[self.app.pk]))
        self.assertContains(page, "the oldest 2 of 3 sources")
