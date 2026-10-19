"""Whether each graph version is better or worse than the one before, with AI and without.

Every version an AI run builds is two graphs at once: the structure read from
the sources by rule, and the relationships a model proposed and a quote
verified. "Is the AI helping?" and "is this version better than the last?" are
both questions about the difference, so each version is measured twice - as
built, and as structure alone (`without_ai`: its AI entities and every
relationship that is inferred or touches one removed) - by the same code: the
benchmark's ranking and verification (`benchmark.score`) and the Quality
panel's shape figures (`graph_quality`). No model is called and nothing is
charged, so it runs for every version as it is saved.

What is stored is a report about a version, never the version: the shape
figures on `GraphRevision.measures` and the structure-alone benchmark on
`BenchmarkRun.structure`, both filled after the save and, for a version saved
before they existed, the first time the trend is drawn. The version's `data`
is never written.
"""

from .graph_quality import components, cross_document_links, provenance_bands

#: The trend reads this many of the newest versions. Each one's measures are
#: computed once, so this bounds only how wide the chart is.
VERSIONS = 20
#: Bumped when a measure is added or defined differently, so stored ones are
#: worked out again rather than compared with the new definition.
MEASURES_VERSION = 1

#: (key, label, higher is better, kind). Kind decides formatting: "score" is
#: 0-1, "percent" 0-100, "count" a whole number.
RETRIEVAL = (
    ("reciprocal_rank", "Mean reciprocal rank", True, "score"),
    ("found", "Expected source cited", True, "count"),
    ("top_three", "In the top three", True, "count"),
)
SHAPE = (
    ("cross_document", "Cross-document links", True, "count"),
    ("largest_percent", "Largest connected piece", True, "percent"),
    ("components", "Disconnected pieces", False, "count"),
    ("orphan_percent", "Orphan nodes", False, "percent"),
    ("evidence_percent", "Relationships quoting a source", True, "percent"),
)
AI = (
    ("semantic_relationships", "AI relationships kept", True, "count"),
    ("acceptance_percent", "AI proposals verified", True, "percent"),
    ("rejected_relationships", "AI proposals refused", False, "count"),
    ("outside_ontology", "Outside the ontology", False, "count"),
    ("merged_entities", "Entities merged across sources", True, "count"),
)
#: A change smaller than this is "no change", so rounding never reads as a trend.
SAME = {"score": 0.005, "percent": 0.5, "count": 0.5}


def without_ai(data):
    """This version as structure alone: what the rules read, nothing a model proposed."""
    entities = {node["id"] for node in data.get("nodes", []) if node.get("kind") == "entity"}
    return {
        **data,
        "nodes": [node for node in data.get("nodes", []) if node["id"] not in entities],
        "edges": [
            edge
            for edge in data.get("edges", [])
            if not edge.get("inferred")
            and edge.get("source") not in entities
            and edge.get("target") not in entities
        ],
    }


def has_ai(data):
    return any(edge.get("inferred") for edge in data.get("edges", [])) or any(
        node.get("kind") == "entity" for node in data.get("nodes", [])
    )


def _percent(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0


def shape(data):
    """The Quality panel's own shape figures for one graph, themes left out (they are slow)."""
    nodes, edges = data.get("nodes", []), data.get("edges", [])
    pieces, largest = components(nodes, edges)
    touched = {end for edge in edges for end in (edge.get("source"), edge.get("target"))}
    bands = provenance_bands(edges)
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "cross_document": cross_document_links(nodes, edges),
        "components": pieces,
        "largest_percent": _percent(largest, len(nodes)),
        "orphan_percent": _percent(sum(node["id"] not in touched for node in nodes), len(nodes)),
        "evidence_percent": _percent(bands["extracted"] + bands["inferred"], len(edges)),
    }


def measure(revision):
    """Both graphs of one version, measured and stored on the revision. Returns them."""
    from .models import GraphRevision

    data = revision.data
    built = shape(data)
    measures = {
        "version": MEASURES_VERSION,
        "ai": has_ai(data),
        "with_ai": built,
        "structure": shape(without_ai(data)) if has_ai(data) else built,
    }
    GraphRevision.objects.filter(pk=revision.pk).update(measures=measures)
    revision.measures = measures
    return measures


def summary(results):
    """A benchmark's three figures from its per-question results."""
    ranks = [result["rank"] for result in results]
    return {
        "found": sum(rank is not None for rank in ranks),
        "top_three": sum(rank is not None and rank <= 3 for rank in ranks),
        "reciprocal_rank": round(sum(1 / rank for rank in ranks if rank) / len(ranks), 3)
        if ranks
        else 0.0,
        "ranks": ranks,
    }


def score_structure(app, revision, questions):
    """The same questions against this version without its AI: None when it has none."""
    from .benchmark import score

    if not has_ai(revision.data):
        return None
    return summary(score(app, revision, questions, data=without_ai(revision.data)))


def direction(kind, higher_is_better, now, before):
    """"better", "worse" or "same" for one measure between two versions."""
    if now is None or before is None:
        return None
    change = now - before
    if abs(change) < SAME[kind]:
        return "same"
    return "better" if (change > 0) == higher_is_better else "worse"


def _ai_figures(quality):
    kept = quality.get("semantic_relationships")
    refused = quality.get("rejected_relationships")
    if kept is None:
        return {key: None for key, *_ in AI}
    return {
        "semantic_relationships": kept,
        "acceptance_percent": _percent(kept, kept + (refused or 0)) if kept or refused else None,
        "rejected_relationships": refused,
        "outside_ontology": quality.get("outside_ontology"),
        "merged_entities": quality.get("merged_entities"),
    }


def trend(app):
    """The newest `VERSIONS` versions, oldest first, each measured with AI and without.

    Each row: the version, its model, retrieval with AI and as structure alone
    (None where no questions were scored), shape both ways, the AI run's own
    counts, and for every figure whether it moved better or worse than the
    version before.
    """
    from .benchmark import latest_runs
    from .models import GraphRevision

    revisions = list(
        GraphRevision.objects.filter(application=app).order_by("-number")[:VERSIONS]
    )[::-1]
    if not revisions:
        return None
    runs = latest_runs(app, revisions)
    rows = []
    for revision in revisions:
        stored = revision.measures or {}
        measures = stored if stored.get("version") == MEASURES_VERSION else measure(revision)
        run = runs.get(revision.pk)
        with_ai = (
            {
                "found": run.found,
                "top_three": run.top_three,
                "reciprocal_rank": run.reciprocal_rank,
                "questions": run.questions,
            }
            if run
            else None
        )
        if run is None:
            structure = None
        elif not measures["ai"]:
            structure = dict(with_ai)
        elif run.structure:
            structure = {**run.structure, "questions": run.questions}
        else:
            structure = None
        rows.append(
            {
                "revision": revision,
                "number": revision.number,
                "model": revision.model,
                "ai": measures["ai"],
                "published": revision.published_at is not None,
                "retrieval": {"with_ai": with_ai, "structure": structure},
                "shape": {"with_ai": measures["with_ai"], "structure": measures["structure"]},
                "ai_run": _ai_figures(revision.quality or {}),
            }
        )
    for before, row in zip([None, *rows], rows, strict=False):
        row["changes"] = changes(before, row)
        row["lift"] = lift(row)
    return {"rows": rows, "chart": chart(rows), "verdict": verdict(rows)}


def _value(row, group, variant, key):
    if group == "ai_run":
        return row["ai_run"].get(key)
    figures = row[group][variant]
    return figures.get(key) if figures else None


def changes(before, row):
    """Better / worse / same against the version before, for every figure with AI."""
    found = {}
    if before is None:
        return found
    for group, measures in (("retrieval", RETRIEVAL), ("shape", SHAPE), ("ai_run", AI)):
        for key, _, higher, kind in measures:
            now = _value(row, group, "with_ai", key)
            found[key] = direction(kind, higher, now, _value(before, group, "with_ai", key))
    return found


def lift(row):
    """What the AI added to this version over its own structure, figure by figure."""
    found = {}
    for group, measures in (("retrieval", RETRIEVAL), ("shape", SHAPE)):
        for key, _, higher, kind in measures:
            now = _value(row, group, "with_ai", key)
            base = _value(row, group, "structure", key)
            if now is None or base is None:
                found[key] = None
                continue
            change = round(now - base, 3 if kind == "score" else 1)
            found[key] = {
                "change": change,
                "direction": direction(kind, higher, now, base),
            }
    return found


#: The chart's drawing box, in SVG user units; the SVG scales to its container.
WIDTH, HEIGHT, LEFT, RIGHT, TOP, BOTTOM = 640, 220, 44, 16, 16, 34


def chart(rows):
    """Mean reciprocal rank per version, with AI and as structure alone, as SVG coordinates.

    Drawn on the server so it needs no script and survives the CSP: points and
    labels, no style. A version with no benchmark run leaves a gap.
    """
    scored = [row for row in rows if row["retrieval"]["with_ai"]]
    if not scored:
        return None
    span = max(len(rows) - 1, 1)
    inner_w, inner_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

    def x(index):
        return round(LEFT + inner_w * (index / span if len(rows) > 1 else 0.5), 1)

    def y(value):
        return round(TOP + inner_h * (1 - value), 1)

    lines = {}
    for variant in ("with_ai", "structure"):
        points = [
            {
                "x": x(index),
                "y": y(row["retrieval"][variant]["reciprocal_rank"]),
                "value": row["retrieval"][variant]["reciprocal_rank"],
                "number": row["number"],
            }
            for index, row in enumerate(rows)
            if row["retrieval"][variant]
        ]
        lines[variant] = {
            "points": points,
            "path": " ".join(f"{p['x']},{p['y']}" for p in points),
        }
    return {
        "width": WIDTH,
        "height": HEIGHT,
        "left": LEFT,
        "right": WIDTH - RIGHT,
        "bottom": HEIGHT - BOTTOM,
        "label_y": HEIGHT - BOTTOM + 18,
        "grid": [{"y": y(value), "label": f"{value:.2f}"} for value in (0, 0.25, 0.5, 0.75, 1)],
        "ticks": [{"x": x(index), "label": f"v{row['number']}"} for index, row in enumerate(rows)],
        "with_ai": lines["with_ai"],
        "structure": lines["structure"],
    }


def verdict(rows):
    """One line each: newest against the version before, and what AI adds to the newest."""
    newest = rows[-1]
    said = []
    if len(rows) > 1:
        before = rows[-2]
        now_score = _value(newest, "retrieval", "with_ai", "reciprocal_rank")
        then_score = _value(before, "retrieval", "with_ai", "reciprocal_rank")
        moved = newest["changes"]
        better = [label for key, label, *_ in RETRIEVAL + SHAPE + AI if moved.get(key) == "better"]
        worse = [label for key, label, *_ in RETRIEVAL + SHAPE + AI if moved.get(key) == "worse"]
        if now_score is not None and then_score is not None:
            way = moved.get("reciprocal_rank")
            said.append(
                {
                    "tone": {"better": "good", "worse": "poor"}.get(way, "same"),
                    "text": f"Version {newest['number']} against {before['number']}: retrieval "
                    + {"better": "better", "worse": "worse", "same": "unchanged"}[way]
                    + f" (MRR {then_score:.2f} → {now_score:.2f}); "
                    f"{len(better)} figure(s) better, {len(worse)} worse.",
                }
            )
        else:
            said.append(
                {
                    "tone": "same",
                    "text": f"Version {newest['number']} against {before['number']}: "
                    f"{len(better)} figure(s) better, {len(worse)} worse. Add benchmark "
                    "questions to compare retrieval.",
                }
            )
    if not newest["ai"]:
        said.append(
            {
                "tone": "poor",
                "text": f"Version {newest['number']} has no AI relationships - it is structure "
                "alone, so there is nothing to compare. Generate it with the model.",
            }
        )
    else:
        gain = newest["lift"].get("reciprocal_rank")
        links = newest["lift"].get("cross_document")
        parts = []
        if gain:
            parts.append(f"MRR {gain['change']:+.3f}")
        if links:
            parts.append(f"{links['change']:+.0f} cross-document links")
        way = gain["direction"] if gain else (links["direction"] if links else "same")
        said.append(
            {
                "tone": {"better": "good", "worse": "poor"}.get(way, "same"),
                "text": f"AI on version {newest['number']}, against its own structure: "
                + (", ".join(parts) if parts else "no measured difference")
                + ".",
            }
        )
    return said
