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
import re

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


def communities(nodes, edges):
    """Leiden communities from Graphify, largest first: (graph, [(label, members)]).

    One partition for every place that shows themes - the Quality table and the
    canvas - so a theme is the same set of nodes, in the same position, on both.
    Graphify seeds its partitioner, so the answer is the same on every render.
    Each theme is named after its most-connected node, which is Graphify's own
    LLM-free labelling, so a name is always a real node label and never an
    invented summary. Clusters under `MIN_THEME` are stray pairs, not themes.
    """
    import networkx as nx
    from graphify.cluster import cluster, label_communities_by_hub

    graph = nx.Graph()
    for node in nodes:
        graph.add_node(node["id"], label=node.get("label", ""))
    for edge in edges:
        left, right = edge.get("source"), edge.get("target")
        if left in graph and right in graph and left != right:
            graph.add_edge(left, right)
    if not graph.number_of_edges():
        return graph, []
    found = {key: members for key, members in cluster(graph).items() if len(members) >= MIN_THEME}
    labels = label_communities_by_hub(graph, found)
    ordered = [(labels[key][:80], members) for key, members in found.items()]
    ordered.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    return graph, ordered


def themes(nodes, edges):
    """What the graph is about, as clusters of densely linked nodes.

    Cohesion is the share of possible links inside the theme that exist; it
    describes the structure, not correctness.
    """
    from graphify.cluster import cohesion_score

    graph, found = communities(nodes, edges)
    owner = {node["id"]: node.get("knowledge_id") for node in nodes}
    total = graph.number_of_nodes()
    rows = [
        {
            "rank": rank,
            "label": label,
            "nodes": len(members),
            "percent": _percent(len(members), total),
            "documents": len({owner[m] for m in members if owner.get(m)}),
            "cohesion": round(100 * cohesion_score(graph, members)),
        }
        for rank, (label, members) in enumerate(found, start=1)
    ]
    return {"count": len(rows), "rows": rows[:THEMES_SHOWN]}


def node_themes(data):
    """Each node's theme, for the canvas to colour, filter and group by.

    `membership` maps a node id to its theme's position in `themes`; a node in
    no theme is absent. The canvas colours the first `THEMES_SHOWN` - the same
    ones the Quality table lists - and folds the rest into "Other".
    """
    _, found = communities(data.get("nodes", []), data.get("edges", []))
    return {
        "themes": [{"label": label, "nodes": len(members)} for label, members in found],
        "membership": {
            member: index for index, (_, members) in enumerate(found) for member in members
        },
    }


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


#: At or above this a headline reads as good; at or above FAIR_AT, fair.
GOOD_AT = 90
FAIR_AT = 60


def _tone(percent):
    return "good" if percent >= GOOD_AT else "fair" if percent >= FAIR_AT else "poor"


def at_a_glance(health, quality, source_count, benchmark=None):
    """The four headline answers the Quality tab opens with, in plain words.

    Each is a question a reader actually has, one number, a bar and one line
    saying what it means - the detail stays in the tiles below. Nothing new is
    measured: every figure is one the panel already reported, gathered here.
    `benchmark` is this version's latest BenchmarkRun, or None if unscored.
    """
    processed = quality.get("processed_sources") or 0
    total = max(source_count or 0, processed)
    read = _percent(processed, total)
    connected = round(100 - health["orphan_percent"], 1)
    cards = [
        {
            "key": "sources",
            "question": "Sources in the graph",
            "figure": f"{processed} of {total}",
            "percent": read,
            "tone": _tone(read),
            "meaning": "Every source is read."
            if processed >= total
            else f"{total - processed} not read yet.",
        },
        {
            "key": "evidence",
            "question": "Links backed by a quote",
            "figure": f"{health['evidence_percent']:g}%",
            "percent": health["evidence_percent"],
            "tone": _tone(health["evidence_percent"]),
            "meaning": "Only quoted links can be cited in an answer.",
        },
        {
            "key": "connected",
            "question": "Nodes that connect",
            "figure": f"{connected:g}%",
            "percent": connected,
            "tone": _tone(connected),
            "meaning": "Isolated nodes never help an answer.",
        },
    ]
    if benchmark and benchmark.questions:
        found = _percent(benchmark.found, benchmark.questions)
        cards.append(
            {
                "key": "retrieval",
                "question": "Finds the right source",
                "figure": f"{benchmark.found} of {benchmark.questions}",
                "percent": found,
                "tone": _tone(found),
                "meaning": "Test questions that cite the source they should.",
                "run": benchmark,
            }
        )
    else:
        cards.append(
            {
                "key": "retrieval",
                "question": "Finds the right source",
                "figure": "Not measured",
                "percent": None,
                "tone": "none",
                "meaning": "Add test questions to measure it.",
            }
        )
    return cards


def quality_overview(health, cards, quality=None):
    """Prioritize existing signals without presenting structure as answer accuracy."""
    if not health.get("nodes"):
        return {
            "title": "Start with your source material",
            "detail": "There is not enough graph data to assess quality yet.",
            "actions": [{
                "title": "Add sources and generate a graph",
                "detail": "Quality signals appear when the graph contains knowledge.",
                "route": "documents", "label": "View sources", "tone": "none",
            }],
            "additional": 0, "tone": "none",
        }
    by_key = {card["key"]: card for card in cards}
    quality = quality or {}
    actions = []
    unquoted = health.get("bands", {}).get("unquoted", 0)
    if unquoted:
        actions.append({
            "title": f"{unquoted} links need source evidence",
            "detail": "Unquoted links cannot support a cited answer.",
            "route": "documents", "label": "Review sources", "tone": "poor",
        })
    coverage = quality.get("coverage_percent")
    incomplete = (
        coverage is not None and coverage < 100
        and (quality.get("table_rows", 0) or quality.get("sections", 0))
    )
    if incomplete:
        actions.append({
            "title": "Some source content was left out",
            "detail": f"{coverage:g}% of source rows and sections are represented.",
            "route": "documents", "label": "Review source coverage", "tone": "fair",
        })
    sources = by_key.get("sources", {})
    if sources.get("percent") is not None and sources["percent"] < 100:
        actions.append({
            "title": "Bring the remaining sources into the graph",
            "detail": sources["meaning"],
            "route": "documents", "label": "View sources", "tone": "fair",
        })
    retrieval = by_key.get("retrieval", {})
    if retrieval.get("percent") is None:
        actions.append({
            "title": "Measure how well answers find their sources",
            "detail": "Retrieval has not been tested for this version.",
            "route": "graph-benchmark", "label": "Add test questions", "tone": "none",
        })
    elif retrieval["percent"] < GOOD_AT:
        actions.append({
            "title": "Review the questions that missed their source",
            "detail": f"{retrieval['figure']} test questions found the expected source.",
            "route": "graph-benchmark", "label": "Review test results",
            "tone": retrieval["tone"], "run": retrieval.get("run"),
        })
    if health.get("orphan_percent", 0) > ORPHAN_LIMIT:
        actions.append({
            "title": f"Connect {health['orphans']} isolated nodes",
            "detail": "These nodes have no relationships to other knowledge.",
            "route": "graph", "label": "Explore graph", "tone": "fair",
        })
    elif (
        health.get("largest_percent", 100) < LARGEST_COMPONENT_FLOOR
        or health.get("components", 0) > max(3, health["nodes"] // 20)
    ):
        actions.append({
            "title": "Review disconnected clusters",
            "detail": f"The largest cluster holds {health['largest_percent']:g}% of nodes.",
            "route": "graph", "label": "Explore graph", "tone": "fair",
        })
    needs_review = any(action["tone"] in {"poor", "fair"} for action in actions)
    if unquoted:
        title = "Source evidence needs attention"
    elif incomplete:
        title = "Source coverage is incomplete"
    elif sources.get("percent") is not None and sources["percent"] < 100:
        title = "Some sources are missing"
    elif retrieval.get("percent") is not None and retrieval["percent"] < GOOD_AT:
        title = "Retrieval needs attention"
    elif needs_review:
        title = "Connections need attention"
    else:
        title = "Structure looks healthy"
    return {
        "title": title,
        "detail": "Measured signals, prioritized by impact."
        if needs_review else "Source evidence and graph connections are in good shape.",
        "actions": actions[:3],
        "additional": max(len(actions) - 3, 0),
        "tone": "fair" if needs_review else "good",
    }


#: Run notes the Quality tab already shows as a figure, so repeating them as a
#: notice says the same thing twice. Older versions stored them, so they are
#: recognised by their wording rather than stopped at the source.
_SHOWN_ELSEWHERE = (
    re.compile(r"^\d+ empty table cells were found\.?$"),
    re.compile(r"^\d+ repeated table rows were found\.?$"),
    re.compile(r"^\d+ unsupported or invalid AI relationship\(s\) were excluded\.?$"),
)


def notes(warnings):
    """A version's run notes, without the ones a tile or line already states."""
    return [
        warning
        for warning in warnings or []
        if not any(pattern.match(warning.strip()) for pattern in _SHOWN_ELSEWHERE)
    ]
