"""Multi-language symbols and a cross-file call graph, read by Graphify.

Graphify (https://graphify.net, the `graphifyy` package) parses code with
Tree-sitter. Only its AST pass is used here: `graphify.extract.extract` reads
files, resolves calls and inheritance across them, and calls no model -
`input_tokens` stays zero, and nothing in that path opens a socket. Its
semantic pass, which sends prose to a model under its own API key, is never
invoked: that would bypass per-application credentials and `AIUsage`.

It runs on text the clone already read and bounded. The sources are written
into a scratch directory of their own, so Graphify never sees the checkout,
its git metadata or anything the clone left out, and its cache lands in a
second scratch directory rather than beside the code.

What it adds is what the in-house analyser cannot: symbols for languages it
does not parse, and relationships between files that are not imports. Python
and JavaScript keep their existing symbols and import edges; Graphify's call
and inheritance edges are added for every language.
"""

import logging
import re
import tempfile
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

#: Graphify relation -> CodeRelationship.kind. Anything else (containment,
#: rationale, field references) is structure inside one file or too weak to
#: call a dependency, and is not stored.
KINDS = {
    "calls": "call",
    "inherits": "inherit",
    "implements": "inherit",
    "imports": "import",
    "imports_from": "import",
}
DETAIL = {
    "call": "Calls code in this file",
    "inherit": "Extends a type in this file",
    "import": "Imports project file",
}
#: The languages whose symbols and imports the in-house analyser already reads.
NATIVE = {"python", "javascript", "jsx", "typescript", "tsx"}
LINE = re.compile(r"L(\d+)")


def _line(location):
    match = LINE.match(location or "")
    return int(match.group(1)) if match else 0


def _safe(path):
    """A repository-relative path that cannot leave the scratch directory."""
    parts = PurePosixPath(path).parts
    return bool(parts) and not PurePosixPath(path).is_absolute() and ".." not in parts


def _symbol(label):
    """(name, kind) for a Graphify node label, or None for a file node.

    Graphify labels functions `name()` and methods `.name()`; a bare name is a
    type. Both function forms count as "function", which is what
    `code_graph.FUNCTION_KINDS` counts.
    """
    if label.endswith("()"):
        return label[:-2].lstrip("."), "function"
    return label, "class"


def _quiet(*args, **kwargs):
    logger.debug("graphify: %s", " ".join(str(arg) for arg in args).strip())


def analyse(files):
    """Read `files` (analysed dicts with path, language, content) with Graphify.

    Returns (symbols by path, edges, paths Graphify could not parse). Edges use
    the shape `code_graph_analysis.relationships` produces, so the two lists can
    be stored together.
    """
    by_path = {item["path"]: item for item in files if _safe(item["path"])}
    if not by_path:
        return {}, [], set()

    from graphify import extract as graphify_extract

    # Past 100 files it prints progress to stdout, which is not where this
    # process's structured logs go. A module global shadows the builtin for
    # that one module only - no stdout swap that another thread could feel.
    graphify_extract.print = _quiet
    extract = graphify_extract.extract

    with (
        tempfile.TemporaryDirectory(prefix="graphify-src-") as source_name,
        tempfile.TemporaryDirectory(prefix="graphify-cache-") as cache_name,
    ):
        root = Path(source_name).resolve()
        written = []
        for path, item in by_path.items():
            target = root.joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            written.append(target)
        # parallel=False: a process pool inside the worker thread would re-import
        # Django in every child on Windows, and the file cap keeps this small.
        result = extract(written, cache_root=Path(cache_name), root=root, parallel=False)

    # Reported as the paths it was given, which were built under `root`.
    failed = set()
    for raw in result.get("failed_sources") or []:
        try:
            failed.add(Path(str(raw)).relative_to(root).as_posix())
        except ValueError:
            continue

    nodes = {node["id"]: node for node in result.get("nodes", [])}
    path_of = {
        node_id: node.get("source_file")
        for node_id, node in nodes.items()
        if node.get("source_file") in by_path
    }

    symbols = {}
    for node_id, path in path_of.items():
        node = nodes[node_id]
        label = node.get("label") or ""
        if node.get("file_type") != "code" or label == PurePosixPath(path).name:
            continue
        if by_path[path]["language"] in NATIVE:
            continue
        name, kind = _symbol(label)
        if name:
            symbols.setdefault(path, []).append(
                {"name": name[:200], "line": _line(node.get("source_location")), "kind": kind}
            )
    for rows in symbols.values():
        rows.sort(key=lambda row: (row["line"], row["name"]))

    found = {}
    for edge in result.get("edges", []):
        kind = KINDS.get(edge.get("relation"))
        source, target = path_of.get(edge.get("source")), path_of.get(edge.get("target"))
        if not kind or not source or not target or source == target:
            continue
        # The in-house analyser owns imports for the languages it reads.
        if kind == "import" and by_path[source]["language"] in NATIVE:
            continue
        entry = found.setdefault(
            (source, target, kind), {"lines": set(), "names": [], "static": False}
        )
        entry["lines"].add(_line(edge.get("source_location")))
        name, _ = _symbol(nodes[edge["target"]].get("label") or "")
        if name and name != PurePosixPath(target).name:
            entry["names"].append(name)
        # One edge read straight from the source makes the pair static; an
        # inferred one must not displace it.
        entry["static"] = entry["static"] or edge.get("confidence") == "EXTRACTED"

    edges = [
        {
            "source": source,
            "target": target,
            "kind": kind,
            "confidence": "static" if entry["static"] else "inferred",
            "detail": DETAIL[kind],
            "evidence": {
                "lines": sorted(line for line in entry["lines"] if line),
                "names": list(dict.fromkeys(entry["names"]))[:6],
            },
        }
        for (source, target, kind), entry in sorted(found.items())
    ]
    return symbols, edges, failed
