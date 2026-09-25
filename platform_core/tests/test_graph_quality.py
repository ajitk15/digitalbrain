from django.test import SimpleTestCase

from platform_core.graph_quality import (
    components,
    cross_document_links,
    document_share,
    health_report,
    provenance_bands,
    themes,
)


def node(node_id, kind="record", knowledge_id="doc-a"):
    return {"id": node_id, "kind": kind, "label": node_id, "knowledge_id": knowledge_id}


def edge(source, target, knowledge_id="doc-a", **extra):
    return {
        "source": source,
        "target": target,
        "relation": "contains",
        "knowledge_id": knowledge_id,
        "evidence": "a quoted line",
        **extra,
    }


class ComponentTests(SimpleTestCase):
    def test_an_empty_graph_has_no_components(self):
        self.assertEqual(components([], []), (0, 0))

    def test_unconnected_nodes_are_each_their_own_component(self):
        self.assertEqual(components([node("a"), node("b"), node("c")], []), (3, 1))

    def test_a_connected_chain_is_one_component(self):
        nodes = [node("a"), node("b"), node("c")]
        self.assertEqual(components(nodes, [edge("a", "b"), edge("b", "c")]), (1, 3))

    def test_two_clusters_report_the_larger_one(self):
        nodes = [node(x) for x in "abcde"]
        edges = [edge("a", "b"), edge("b", "c"), edge("d", "e")]
        self.assertEqual(components(nodes, edges), (2, 3))

    def test_edges_pointing_at_missing_nodes_are_ignored(self):
        """A revision can outlive nodes its edges referenced."""
        self.assertEqual(components([node("a")], [edge("a", "ghost")]), (1, 1))


class CrossDocumentTests(SimpleTestCase):
    def test_a_value_node_reached_from_two_documents_counts_once(self):
        nodes = [
            node("r1", knowledge_id="doc-a"),
            node("r2", knowledge_id="doc-b"),
            node("shared", kind="value", knowledge_id=None),
        ]
        edges = [edge("r1", "shared", "doc-a"), edge("r2", "shared", "doc-b")]
        self.assertEqual(cross_document_links(nodes, edges), 1)

    def test_a_value_node_reached_from_one_document_does_not_count(self):
        nodes = [node("r1"), node("shared", kind="value", knowledge_id=None)]
        self.assertEqual(cross_document_links(nodes, [edge("r1", "shared", "doc-a")]), 0)

    def test_document_owned_nodes_are_never_cross_document(self):
        """Only unattributed nodes can join documents; owned ones belong to one."""
        nodes = [node("r1", knowledge_id="doc-a"), node("r2", knowledge_id="doc-b")]
        edges = [edge("r1", "r2", "doc-a"), edge("r1", "r2", "doc-b")]
        self.assertEqual(cross_document_links(nodes, edges), 0)


class ProvenanceTests(SimpleTestCase):
    def test_bands_split_extracted_inferred_and_unquoted(self):
        edges = [
            edge("a", "b"),
            edge("c", "d", inferred=True),
            edge("e", "f", evidence=""),
        ]
        bands = provenance_bands(edges)
        self.assertEqual(
            {k: bands[k] for k in ("extracted", "inferred", "unquoted", "total")},
            {"extracted": 1, "inferred": 1, "unquoted": 1, "total": 3},
        )
        # Percentages are precomputed because CSP forbids inline style widths.
        self.assertEqual(bands["extracted_pct"], 33)

    def test_an_empty_graph_reports_zeroes_not_an_error(self):
        bands = provenance_bands([])
        self.assertEqual(
            {k: bands[k] for k in ("extracted", "inferred", "unquoted", "total")},
            {"extracted": 0, "inferred": 0, "unquoted": 0, "total": 0},
        )
        self.assertEqual(bands["extracted_pct"], 0)


class DocumentShareTests(SimpleTestCase):
    def test_shares_are_ordered_and_total_one_hundred(self):
        data = {
            "nodes": [
                node("a", knowledge_id="doc-a"),
                node("b", knowledge_id="doc-b"),
                node("c", knowledge_id="doc-b"),
                node("v", kind="value", knowledge_id=None),
            ],
            "sources": [{"id": "doc-a", "title": "A.csv"}, {"id": "doc-b", "title": "B.csv"}],
        }
        rows = document_share(data)
        self.assertEqual([r["title"] for r in rows], ["B.csv", "A.csv"])
        # Unattributed shared nodes are excluded, so shares total the attributed set.
        self.assertEqual(sum(r["percent"] for r in rows), 100.0)

    def test_a_source_missing_from_the_snapshot_is_labelled_not_dropped(self):
        data = {"nodes": [node("a", knowledge_id="gone")], "sources": []}
        self.assertEqual(document_share(data)[0]["title"], "Removed source")


class HealthReportTests(SimpleTestCase):
    def healthy(self):
        nodes = [node(x) for x in "abcd"]
        edges = [edge("a", "b"), edge("b", "c"), edge("c", "d")]
        return {"nodes": nodes, "edges": edges, "sources": [{"id": "doc-a", "title": "A"}]}

    def test_a_connected_evidenced_graph_grades_good(self):
        report = health_report(self.healthy(), {"nodes": 4, "edges": 3, "isolated_nodes": 0})
        self.assertEqual(report["grade"], "Good")
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["components"], 1)
        self.assertEqual(report["evidence_percent"], 100.0)

    def test_orphans_above_the_threshold_are_reported(self):
        data = self.healthy()
        data["nodes"].extend(node(x) for x in "efgh")
        report = health_report(data, {"nodes": 8, "edges": 3, "isolated_nodes": 4})
        self.assertNotEqual(report["grade"], "Good")
        self.assertTrue(any("orphans" in issue for issue in report["issues"]))
        self.assertEqual(report["orphan_percent"], 50.0)

    def test_fragmentation_is_reported_with_the_largest_share(self):
        nodes = [node(str(i)) for i in range(40)]
        report = health_report({"nodes": nodes, "edges": []}, {"nodes": 40, "edges": 0})
        self.assertEqual(report["grade"], "Poor")
        self.assertTrue(any("fragmented" in issue for issue in report["issues"]))

    def test_unquoted_relationships_are_surfaced(self):
        data = self.healthy()
        data["edges"].append(edge("a", "d", evidence=""))
        report = health_report(data, {"nodes": 4, "edges": 4, "isolated_nodes": 0})
        self.assertTrue(any("quote" in issue for issue in report["issues"]))
        self.assertEqual(report["bands"]["unquoted"], 1)

    def test_the_saved_orphan_count_is_reused_not_recomputed(self):
        """The panel must never contradict the warning list beside it."""
        report = health_report(self.healthy(), {"nodes": 4, "edges": 3, "isolated_nodes": 9})
        self.assertEqual(report["orphans"], 9)

    def test_an_empty_graph_does_not_divide_by_zero(self):
        report = health_report({"nodes": [], "edges": [], "sources": []}, {})
        self.assertEqual(report["nodes"], 0)
        self.assertEqual(report["orphan_percent"], 0.0)
        self.assertEqual(report["evidence_percent"], 0.0)
        self.assertEqual(report["documents"], [])

    def test_inferred_relationships_still_count_as_evidenced(self):
        """AI edges are quote-verified before storage; they are not low-confidence."""
        data = self.healthy()
        data["edges"] = [edge("a", "b", inferred=True), edge("b", "c", inferred=True)]
        report = health_report(data, {"nodes": 4, "edges": 2, "isolated_nodes": 0})
        self.assertEqual(report["evidence_percent"], 100.0)
        self.assertEqual(report["bands"]["inferred"], 2)


class ThemeTests(SimpleTestCase):
    """Leiden clusters from Graphify, named after a real node, never invented."""

    def two_topics(self):
        # Two dense groups of five, joined by one bridge.
        nodes, edges = [], []
        for topic, document in (("billing", "doc-a"), ("auth", "doc-b")):
            members = [f"{topic}-{i}" for i in range(5)]
            nodes += [node(member, knowledge_id=document) for member in members]
            edges += [
                edge(left, right, knowledge_id=document)
                for i, left in enumerate(members)
                for right in members[i + 1 :]
            ]
            # One node every other member points at, so the theme has a clear hub.
            nodes.append(node(topic, kind="value", knowledge_id=None))
            edges += [edge(member, topic, knowledge_id=document) for member in members]
        edges.append(edge("billing", "auth"))
        return nodes, edges

    def test_dense_groups_become_themes_named_after_their_hub(self):
        found = themes(*self.two_topics())
        self.assertEqual(found["count"], 2)
        labels = {row["label"] for row in found["rows"]}
        self.assertEqual(labels, {"billing", "auth"})
        for row in found["rows"]:
            self.assertEqual(row["nodes"], 6)
            self.assertEqual(row["documents"], 1)
            self.assertEqual(row["percent"], 50.0)
            self.assertEqual(row["cohesion"], 100)

    def test_the_same_graph_gives_the_same_themes(self):
        self.assertEqual(themes(*self.two_topics()), themes(*self.two_topics()))

    def test_stray_pairs_and_an_unlinked_graph_are_not_themes(self):
        self.assertEqual(themes([node("a"), node("b")], []), {"count": 0, "rows": []})
        found = themes([node("a"), node("b")], [edge("a", "b")])
        self.assertEqual(found["count"], 0)

    def test_the_health_report_carries_themes_without_changing_the_grade(self):
        nodes, edges = self.two_topics()
        report = health_report({"nodes": nodes, "edges": edges}, {})
        self.assertEqual(report["themes"]["count"], 2)
        self.assertEqual(report["grade"], "Good")

    def test_a_clustering_failure_hides_the_themes_not_the_panel(self):
        from unittest.mock import patch

        with patch("platform_core.graph_quality.themes", side_effect=RuntimeError):
            report = health_report({"nodes": [node("a")], "edges": []}, {})
        self.assertIsNone(report["themes"])
        self.assertEqual(report["nodes"], 1)


    def test_the_quality_panel_lists_the_themes(self):
        from django.template.loader import render_to_string

        nodes, edges = self.two_topics()
        report = health_report({"nodes": nodes, "edges": edges}, {})
        html = render_to_string("_kb_quality.html", {"health": report, "graph": {"quality": {}}})
        self.assertIn("Themes", html)
        self.assertIn("<td>billing</td>", html)
        self.assertIn("2 themes", html)
        # Nothing to cluster: no empty table, and the rest of the panel still renders.
        bare = health_report({"nodes": [node("a")], "edges": []}, {})
        html = render_to_string("_kb_quality.html", {"health": bare, "graph": {"quality": {}}})
        self.assertNotIn("Themes", html)
        self.assertIn("Edge provenance", html)
