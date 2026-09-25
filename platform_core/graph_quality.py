"""Structural health signals derived from a stored graph.

Pure functions over the graph dict: no database, no AI, no provider calls. Every
number here is recomputed from `data["nodes"]` / `data["edges"]` at render time,
so it works identically for the live graph and for any saved revision.

Two deliberate departures from the reference design this panel is modelled on:

* There is no "average confidence" score. This platform stores no per-edge
  confidence, and inventing one would contradict the standing rule against
  assigned-but-unmeasured quality numbers. What is reported instead is the
  evidence rate: the share of relationships that quote their source, which is a
  fact about the data rather than a judgement about it.
* Provenance bands are Extracted / Inferred / Unquoted, not the reference's
  Extracted / Inferred / Ambiguous / Unknown. The latter two would be permanently
  zero here and would imply a scoring system that does not exist. Note that
  "Inferred" is not a synonym for low quality: AI relationships are accepted only
  after their quote and both entity names are verified against the source.
"""

import logging

logger = logging.getLogger(__name__)

#: Themes the Health panel lists; the rest are counted, not shown.
THEMES_SHOWN = 6
#: A cluster smaller than this is a stray pair, not a theme.
MIN_THEME = 3

#: An orphan share above this reads as a fragmented extraction.
ORPHAN_LIMIT = 15.0
#: Below this, the graph has no dominant connected body.
LARGEST_COMPONENT_FLOOR = 60.0


def _percent(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0


def components(nodes, edges):
    """Connected-component count and the size of the largest, via union-find."""
    parent = {node["id"]: node["id"] for node in nodes}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for edge in edges:
        left, right = edge.get("source"), edge.get("target")
        if left not in parent or right not in parent:
            continue
        a, b = find(left), find(right)
        if a != b:
            parent[a] = b

    sizes = {}
    for key in parent:
        root = find(key)
        sizes[root] = sizes.get(root, 0) + 1
    return len(sizes), (max(sizes.values()) if sizes else 0)


def cross_document_links(nodes, edges):
    """Nodes that genuinely join two or more documents.

    Value nodes are keyed on the field/value pair and carry no `knowledge_id`, so
    the same value appearing in two documents collapses into one node. Those
    shared nodes are the only real cross-document join the structural graph makes.
    """
    owner = {node["id"]: node.get("knowledge_id") for node in nodes}
    reach = {}
    for edge in edges:
        source_document = edge.get("knowledge_id")
        if not source_document:
            continue
        for endpoint in (edge.get("source"), edge.get("target")):
            if endpoint in owner and not owner[endpoint]:
                reach.setdefault(endpoint, set()).add(source_document)
    return sum(1 for documents in reach.values() if len(documents) > 1)


def provenance_bands(edges):
    """How relationships are evidenced, in the three categories the data supports.

    Integer percentages are computed here rather than in the template because the
    stacked bar is sized by CSS classes: the Content-Security-Policy forbids inline
    style attributes, so a width cannot be interpolated into the markup.
    """
    inferred = sum(1 for edge in edges if edge.get("inferred"))
    unquoted = sum(1 for edge in edges if not edge.get("inferred") and not edge.get("evidence"))
    extracted = len(edges) - inferred - unquoted
    bands = {
        "extracted": extracted,
        "inferred": inferred,
        "unquoted": unquoted,
        "total": len(edges),
    }
    for name in ("extracted", "inferred", "unquoted"):
        bands[f"{name}_pct"] = int(round(_percent(bands[name], len(edges))))
    return bands


def document_share(data):
    """Each source's share of the graph, by nodes attributed to it.

    Labelled as a share of the graph rather than as coverage of the document: the
    stored quality report aggregates rows across all sources, so what fraction of
    one document made it into the graph is not recoverable from saved data.
    """
    titles = {str(source["id"]): source.get("title", "") for source in data.get("sources", [])}
    counts = {}
    for node in data.get("nodes", []):
        key = node.get("knowledge_id")
        if key:
            counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    rows = [
        {
            "id": key,
            "title": titles.get(key, "Removed source"),
            "nodes": value,
            "percent": _percent(value, total),
        }
        for key, value in counts.items()
    ]
    rows.sort(key=lambda row: row["nodes"], reverse=True)
    return rows


def themes(nodes, edges):
    """What the graph is about, as clusters of densely linked nodes.

    Leiden community detection from Graphify (`graphify.cluster`), over the
    relationships already stored - no model, no provider, the same on every
    render. Each theme is named after its most-connected node, which is
    Graphify's own LLM-free labelling, so a name is always a real node label
    and never an invented summary. Cohesion is the share of possible links
    inside the theme that exist; it describes the structure, not correctness.
    """
    import networkx as nx
    from graphify.cluster import cluster, cohesion_score, label_communities_by_hub

    graph = nx.Graph()
    for node in nodes:
        graph.add_node(node["id"], label=node.get("label", ""))
    for edge in edges:
        left, right = edge.get("source"), edge.get("target")
        if left in graph and right in graph and left != right:
            graph.add_edge(left, right)
    if not graph.number_of_edges():
        return {"count": 0, "rows": []}
    found = {key: members for key, members in cluster(graph).items() if len(members) >= MIN_THEME}
    labels = label_communities_by_hub(graph, found)
    owner = {node["id"]: node.get("knowledge_id") for node in nodes}
    total = graph.number_of_nodes()
    rows = [
        {
            "label": labels[key][:80],
            "nodes": len(members),
            "percent": _percent(len(members), total),
            "documents": len({owner[m] for m in members if owner.get(m)}),
            "cohesion": round(100 * cohesion_score(graph, members)),
        }
        for key, members in found.items()
    ]
    rows.sort(key=lambda row: (-row["nodes"], row["label"]))
    return {"count": len(rows), "rows": rows[:THEMES_SHOWN]}


def health_report(data, quality):
    """Structural signals for the Health panel.

    `quality` is the report saved with the graph; its counts are reused rather
    than recomputed wherever they exist, so this panel can never contradict the
    warnings shown beside it.
    """
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    node_count = quality.get("nodes") or len(nodes)
    edge_count = quality.get("edges") or len(edges)
    orphans = quality.get("isolated_nodes")
    if orphans is None:
        connected = {e for edge in edges for e in (edge.get("source"), edge.get("target"))}
        orphans = sum(1 for node in nodes if node["id"] not in connected)

    total_components, largest = components(nodes, edges)
    orphan_percent = _percent(orphans, node_count)
    largest_percent = _percent(largest, node_count)
    bands = provenance_bands(edges)
    evidenced = bands["extracted"] + bands["inferred"]

    issues = []
    if orphan_percent > ORPHAN_LIMIT:
        issues.append(
            f"{orphans} node(s) ({orphan_percent}%) are orphans with no relationships."
        )
    if total_components > max(3, node_count // 20):
        issues.append(
            f"The graph is fragmented into {total_components} disconnected pieces "
            f"(largest holds {largest_percent}% of nodes)."
        )
    if node_count and largest_percent < LARGEST_COMPONENT_FLOOR:
        issues.append(
            f"No dominant cluster: the largest piece holds only {largest_percent}% of nodes."
        )
    if edge_count and bands["unquoted"]:
        issues.append(f"{bands['unquoted']} relationship(s) do not quote a source.")

    # Informational, so it never changes the grade, and a clustering failure
    # hides the table rather than the whole panel.
    try:
        found_themes = themes(nodes, edges)
    except Exception:
        logger.warning("graph_themes_failed", extra={"event": "graph_themes_failed"})
        found_themes = None

    return {
        "themes": found_themes,
        "grade": "Good" if not issues else ("Fair" if len(issues) == 1 else "Poor"),
        "issues": issues,
        "nodes": node_count,
        "edges": edge_count,
        "orphans": orphans,
        "orphan_percent": orphan_percent,
        "components": total_components,
        "largest_percent": largest_percent,
        "cross_document": cross_document_links(nodes, edges),
        "evidence_percent": _percent(evidenced, edge_count),
        "evidenced_edges": evidenced,
        "bands": bands,
        "documents": document_share(data),
    }
