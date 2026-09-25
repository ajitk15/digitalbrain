"""Bounded, deterministic static analysis for immutable repository snapshots."""

import ast
import hashlib
import posixpath
import re

SUPPORTED = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    # Parsed by Graphify's Tree-sitter pass (`code_graph_graphify`), not here.
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
}
#: The languages whose symbols and imports this module reads itself.
JS_FAMILY = {"javascript", "jsx", "typescript", "tsx"}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    ".nuxt",
    "target",
    ".gradle",
    "build",
    "dist",
    "vendor",
    "coverage",
}
#: Every language a repository is counted in, parsed or not. The graph parses
#: only `SUPPORTED`; this says what else is there, so a Java service with one
#: Python script does not read as a Python repository. A closed set on purpose:
#: these names reach Code Factory's prompts, and a repository must not be able
#: to put words there by naming its files. Data and prose formats (JSON, YAML,
#: Markdown) are not languages a change is written in and are not counted.
LANGUAGE_NAMES = {
    ".py": "Python",
    ".pyi": "Python",
    ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".groovy": "Groovy",
    ".gradle": "Groovy",
    ".go": "Go",
    ".rs": "Rust",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hh": "C++",
    ".hpp": "C++",
    ".hxx": "C++",
    ".cs": "C#",
    ".fs": "F#",
    ".fsx": "F#",
    ".vb": "Visual Basic",
    ".swift": "Swift",
    ".m": "Objective-C",
    ".mm": "Objective-C",
    ".rb": "Ruby",
    ".php": "PHP",
    ".pl": "Perl",
    ".pm": "Perl",
    ".lua": "Lua",
    ".r": "R",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".clj": "Clojure",
    ".cljs": "Clojure",
    ".ml": "OCaml",
    ".jl": "Julia",
    ".cob": "COBOL",
    ".cbl": "COBOL",
    ".f90": "Fortran",
    ".sol": "Solidity",
    ".zig": "Zig",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Shell",
    ".ps1": "PowerShell",
    ".psm1": "PowerShell",
    ".bat": "Batchfile",
    ".cmd": "Batchfile",
    ".tf": "HCL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".sass": "CSS",
    ".less": "CSS",
    ".vue": "Vue",
    ".svelte": "Svelte",
}
LANGUAGE_FILENAMES = {"dockerfile": "Dockerfile", "makefile": "Makefile"}
#: The languages the graph parses, by the name the census gives them.
ANALYSED_LANGUAGES = {
    "Python",
    "JavaScript",
    "TypeScript",
    "Java",
    "Kotlin",
    "Scala",
    "Go",
    "Rust",
    "C",
    "C++",
    "C#",
    "Swift",
    "Ruby",
    "PHP",
}

MAX_FILES = 500
MAX_FILE_BYTES = 400_000
MAX_TOTAL_BYTES = 20 * 1024 * 1024
#: Bump when the facts a snapshot stores change shape or meaning. The snapshot
#: reuse check keys on this, so a bump is what makes a re-index recompute.
ANALYZER_VERSION = "structural-v5-graphify"

JS_IMPORT = re.compile(
    r"(?:import\s+(?:(?P<clause>[^;\"']*?)\s+from\s+)?"
    r"|export\s+(?P<exported>[^;\"']*?)\s+from\s+"
    r"|require\s*\(|import\s*\()"
    r"[\"'](?P<module>[^\"']+)[\"']"
)
JS_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
#: An import's binding clause, so an edge can say which names crossed it.
JS_CLAUSE = re.compile(r"[{,]?\s*([A-Za-z_$][\w$]*)(?:\s+as\s+[A-Za-z_$][\w$]*)?")
#: `const Thing = () => {}` and `const Thing = function ...`. Modern JavaScript
#: declares most of its functions this way, and a declaration-keyword-only scan
#: reports a React component file as having no symbols at all.
JS_ASSIGNED = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*"
    r"(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
    re.MULTILINE,
)


# --------------------------------------------------------------- API seams
#
# A route declared in one file and a call made in another are the same seam seen
# from both ends. Matching them is a heuristic - the path is compared, not the
# running service - so every edge it produces is labelled inferred and never
# static. Unknown methods stay "*" rather than being presented as an exact match.

#: Django's own routing, which the FastAPI/Flask patterns alone would miss.
DJANGO_ROUTE = re.compile(
    r"""\b(?:re_)?path\(\s*r?["']([^"']*)["']""",
    re.VERBOSE,
)
#: Decorator routing: @app.get("/x"), @router.post("/x"), @app.route("/x").
DECORATED_ROUTE = re.compile(
    r"""@\w+\.(get|post|put|patch|delete|route)\(\s*["']([^"']+)["']"""
)
#: fetch("/api/x"), axios.get("/api/x"), api.post(`/api/x/${id}`).
JS_CALL = re.compile(
    r"""(?:fetch\(\s*|\.(get|post|put|patch|delete)\(\s*)["'`]([^"'`]*/[^"'`]*)["'`]"""
)

#: A path parameter, in every spelling the four frameworks use.
PARAMETER = re.compile(r"<[^>]+>|\{[^}]+\}|:[A-Za-z_][\w]*|\$\{[^}]*\}")


def normalise_route(value):
    """One comparable shape for a URL path.

    Parameters collapse to `*` so `/users/<int:pk>` and `/users/${id}` compare
    equal, and a trailing slash stops mattering. Host and query are dropped:
    they say where a call went, not which route answered it.
    """
    value = (value or "").split("?", 1)[0].split("#", 1)[0]
    value = re.sub(r"^https?://[^/]+", "", value)
    value = PARAMETER.sub("*", value)
    value = re.sub(r"\^|\$", "", value)
    value = "/" + value.strip("/")
    return re.sub(r"/{2,}", "/", value)


def python_routes(text):
    found = []
    for match in DJANGO_ROUTE.finditer(text):
        found.append(
            {"method": "*", "path": normalise_route(match.group(1)), "raw": match.group(1)}
        )
    for match in DECORATED_ROUTE.finditer(text):
        verb = match.group(1).upper()
        found.append(
            {
                "method": "*" if verb == "ROUTE" else verb,
                "path": normalise_route(match.group(2)),
                "raw": match.group(2),
            }
        )
    return [item for item in found if item["path"] != "/"]


def js_calls(text):
    found = []
    for match in JS_CALL.finditer(text):
        verb = (match.group(1) or "*").upper()
        path = normalise_route(match.group(2))
        if path != "/":
            found.append({"method": verb, "path": path, "raw": match.group(2)})
    return found


def language_for(path):
    lower = path.lower()
    if lower.endswith(".d.ts"):
        return "typescript"
    dot = lower.rfind(".")
    return SUPPORTED.get(lower[dot:] if dot >= 0 else "")


def included(path, size):
    parts = path.replace("\\", "/").split("/")
    return (
        bool(language_for(path))
        and not any(part in SKIP_DIRS or part.startswith("cmake-build-") for part in parts)
        and 0 <= size <= MAX_FILE_BYTES
        and not parts[-1].endswith((".min.js", ".bundle.js"))
    )


def language_name(path):
    """The census name for one path, or None when it is not a counted language."""
    name = path.rsplit("/", 1)[-1].lower()
    if name in LANGUAGE_FILENAMES:
        return LANGUAGE_FILENAMES[name]
    if name.endswith((".min.js", ".bundle.js")):
        return None
    dot = name.rfind(".")
    return LANGUAGE_NAMES.get(name[dot:]) if dot > 0 else None


def census(entries):
    """Which languages a repository is written in, over every file it holds.

    `entries` is (path, size) for every file in the checkout, not only the ones
    the graph indexes: the point is to describe the repository, and the file
    and byte caps exist to bound parsing, not counting. Skipped directories are
    skipped here too - a vendored node_modules is not the repository's language.

    Returns one row per language, largest first, as
    {"name", "files", "bytes", "share", "analysed"}; `share` is a percentage of
    the counted bytes, to one decimal place.
    """
    totals = {}
    for path, size in entries:
        parts = path.replace("\\", "/").split("/")
        if any(part in SKIP_DIRS or part.startswith("cmake-build-") for part in parts):
            continue
        name = language_name("/".join(parts))
        if name is None:
            continue
        files, total = totals.get(name, (0, 0))
        totals[name] = (files + 1, total + max(size, 0))
    counted = sum(total for _, total in totals.values()) or 1
    rows = [
        {
            "name": name,
            "files": files,
            "bytes": total,
            "share": round(100 * total / counted, 1),
            "analysed": name in ANALYSED_LANGUAGES,
        }
        for name, (files, total) in totals.items()
    ]
    rows.sort(key=lambda row: (-row["bytes"], -row["files"], row["name"]))
    return rows


def describe_languages(languages, limit=6):
    """"Java 71.2%, TypeScript 22.0%, Shell 6.8%" - the census in one line."""
    shown = [f"{row['name']} {row['share']:.1f}%" for row in languages[:limit]]
    rest = len(languages) - limit
    if rest > 0:
        shown.append(f"{rest} more")
    return ", ".join(shown)


def python_facts(path, text):
    symbols, imports = [], []
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return symbols, imports, False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                }
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                {"module": alias.name, "names": [], "line": node.lineno, "level": 0}
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                {
                    "module": node.module or "",
                    "names": [alias.name for alias in node.names],
                    "line": node.lineno,
                    "level": node.level,
                }
            )
    return symbols, imports, True


def _clause_names(clause):
    """The names bound by an import clause, for the label on its edge."""
    if not clause:
        return []
    clause = clause.strip()
    if clause.startswith("*"):
        return []
    return [match.group(1) for match in JS_CLAUSE.finditer(clause)][:8]


def js_facts(text):
    symbols = [
        {
            "name": match.group(2),
            "line": text.count("\n", 0, match.start()) + 1,
            "kind": match.group(1),
        }
        for match in JS_SYMBOL.finditer(text)
    ]
    seen = {item["name"] for item in symbols}
    for match in JS_ASSIGNED.finditer(text):
        if match.group(1) in seen:
            continue
        seen.add(match.group(1))
        symbols.append(
            {
                "name": match.group(1),
                "line": text.count("\n", 0, match.start()) + 1,
                "kind": "function",
            }
        )
    symbols.sort(key=lambda item: item["line"])
    imports = [
        {
            "module": match.group("module"),
            "names": _clause_names(match.group("clause") or match.group("exported")),
            "line": text.count("\n", 0, match.start()) + 1,
            "level": 0,
        }
        for match in JS_IMPORT.finditer(text)
    ]
    return symbols, imports, True


def facts(path, text):
    language = language_for(path)
    if language == "python":
        symbols, imports, parse_ok = python_facts(path, text)
    elif language in JS_FAMILY:
        symbols, imports, parse_ok = js_facts(text)
    else:
        # Every other language is read by Graphify over the whole snapshot at
        # once (`code_graph_graphify.analyse`), which fills these in.
        symbols, imports, parse_ok = [], [], True
    return {
        "path": path,
        "language": language,
        "routes": python_routes(text) if language == "python" else [],
        "calls": js_calls(text) if language in JS_FAMILY else [],
        "digest": hashlib.sha256(text.encode()).hexdigest(),
        "content": text,
        "parse_ok": parse_ok,
        "symbols": symbols,
        "imports": imports,
        "lines": len(text.splitlines()),
    }


def _python_modules(paths):
    modules = {}
    for path in paths:
        if not path.endswith((".py", ".pyi")):
            continue
        module = re.sub(r"\.pyi?$", "", path).replace("/", ".")
        modules[module] = path
        if module.endswith(".__init__"):
            modules.setdefault(module[:-9], path)
    return modules


def _resolve_python(source, item, modules):
    directories = source.split("/")[:-1]
    level = item.get("level", 0)
    prefixes = (
        [".".join(directories[: max(0, len(directories) - (level - 1))])]
        if level
        else [".".join(directories[:depth]) for depth in range(len(directories), -1, -1)]
    )
    names = item.get("names") or []
    candidates = [f"{item['module']}.{name}".strip(".") for name in names]
    candidates.append(item["module"])
    for candidate in candidates:
        for prefix in prefixes:
            target = modules.get(f"{prefix}.{candidate}".strip("."))
            if target:
                return target
    return None


def _resolve_js(source, specifier, paths):
    if not specifier.startswith((".", "/", "@/", "~/")):
        return None
    if specifier.startswith(("@/", "~/")):
        bases = [posixpath.join(root, specifier[2:]) for root in ("src", "app", "lib", "")]
    else:
        bases = [posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))]
    extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts")
    for base in bases:
        for candidate in [
            base,
            *(base + ext for ext in extensions),
            *(posixpath.join(base, "index") + ext for ext in extensions),
        ]:
            if candidate in paths:
                return candidate
    return None


def api_edges(files):
    """Candidate seams between a call and the route whose path it matches.

    Never "static": this compares strings, not services. Two files that both
    mention /users are not connected by that alone, so an unmatched call simply
    produces nothing rather than an invented edge.
    """
    routes = {}
    for item in files:
        for route in item.get("routes") or []:
            routes.setdefault(route["path"], []).append((item["path"], route))

    found = {}
    for item in files:
        for call in item.get("calls") or []:
            for target, route in routes.get(call["path"], ()):
                if target == item["path"]:
                    continue
                methods = {route["method"], call["method"]}
                # An unknown method on either side cannot contradict the other,
                # but two different known methods are different endpoints.
                if "*" not in methods and len(methods) > 1:
                    continue
                key = (item["path"], target)
                entry = found.setdefault(key, set())
                verb = call["method"] if call["method"] != "*" else route["method"]
                entry.add(f"{verb} {call['path']}")
    return [
        {
            "source": source,
            "target": target,
            "kind": "api",
            "confidence": "inferred",
            "detail": "Calls a path this file declares",
            "evidence": {"lines": [], "names": [], "endpoints": sorted(entry)[:6]},
        }
        for (source, target), entry in sorted(found.items())
    ]


def relationships(files):
    paths = {item["path"] for item in files}
    modules = _python_modules(paths)
    found = {}
    for item in files:
        for imported in item["imports"]:
            target = (
                _resolve_python(item["path"], imported, modules)
                if item["language"] == "python"
                else _resolve_js(item["path"], imported["module"], paths)
            )
            if target and target != item["path"]:
                entry = found.setdefault((item["path"], target), {"lines": [], "names": []})
                entry["lines"].append(imported.get("line", 0))
                entry["names"].extend(imported.get("names") or [])
    imports = [
        {
            "source": source,
            "target": target,
            "kind": "import",
            "confidence": "static",
            "detail": "Imports project file",
            "evidence": {
                "lines": sorted(set(entry["lines"])),
                # What crossed the edge, in first-seen order, so the label on it
                # says more than "there is a dependency here".
                "names": list(dict.fromkeys(entry["names"]))[:6],
            },
        }
        for (source, target), entry in sorted(found.items())
    ]
    # Import edges first; a seam never overrides a real import between the same
    # pair, because "inferred" must not displace "static".
    seen = {(edge["source"], edge["target"]) for edge in imports}
    return imports + [
        edge for edge in api_edges(files) if (edge["source"], edge["target"]) not in seen
    ]


#: What a file is within its own repository, decided by what points at it, with
#: the short label the graph legend shows for each. `classify` below is the only
#: thing that assigns these, and the legend renders from this list rather than
#: spelling the five out again in the template. `static/code_graph.js` carries a
#: longer wording of its own for card tooltips, where there is room to explain.
ROLES = [
    ("entry", "Entry point"),
    ("service", "Module"),
    ("leaf", "Leaf"),
    ("orphan", "No links"),
    ("circular", "Circular"),
]


def cycles(paths, edges):
    """Strongly connected components of more than one file, plus self-imports.

    Tarjan, iterative. The recursive form descends as deep as the longest import
    chain, which on a real repository is enough to hit Python's recursion limit
    and take the whole index down with it.

    A cycle is a *group*, not a file: five files in one loop is one problem, not
    five. The cheaper "walk depth-first and mark both ends of a back edge"
    version reports only the two ends - on a -> b -> c -> a it marks a and c and
    leaves b looking clean - so it is not used here.

    Returns a list of groups, each a sorted list of paths. A file that imports
    itself is a group of one, which no amount of component splitting surfaces,
    so it is added separately.
    """
    adjacency = {path: [] for path in paths}
    loops = set()
    for edge in edges:
        if edge["source"] == edge["target"]:
            loops.add(edge["source"])
        elif edge["source"] in adjacency and edge["target"] in adjacency:
            adjacency[edge["source"]].append(edge["target"])

    order, low, on_stack, stack = {}, {}, set(), []
    counter = 0
    groups = []
    for root in sorted(adjacency):
        if root in order:
            continue
        work = [(root, 0)]
        while work:
            node, step = work[-1]
            if step == 0:
                order[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            links = adjacency[node]
            if step < len(links):
                work[-1] = (node, step + 1)
                nxt = links[step]
                if nxt not in order:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], order[nxt])
                continue
            if low[node] == order[node]:
                members = []
                while True:
                    popped = stack.pop()
                    on_stack.discard(popped)
                    members.append(popped)
                    if popped == node:
                        break
                if len(members) > 1:
                    groups.append(sorted(members))
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    groups.extend([path] for path in sorted(loops))
    return sorted(groups)


def classify(paths, edges):
    """(role by path, cycle group number by path, number of cycle groups).

    Computed over the whole snapshot, not over whatever subset a page happens to
    draw: a count taken from the drawn nodes is not approximate, it is wrong, and
    it appears beside the file count with the same authority.
    """
    incoming = {path: 0 for path in paths}
    outgoing = {path: 0 for path in paths}
    for edge in edges:
        if edge["source"] in outgoing:
            outgoing[edge["source"]] += 1
        if edge["target"] in incoming:
            incoming[edge["target"]] += 1

    groups = cycles(paths, edges)
    group_of = {}
    for number, members in enumerate(groups, start=1):
        for member in members:
            group_of[member] = number

    roles = {}
    for path in paths:
        if path in group_of:
            roles[path] = "circular"
        elif not incoming[path] and not outgoing[path]:
            roles[path] = "orphan"
        elif not incoming[path]:
            roles[path] = "entry"
        elif not outgoing[path]:
            roles[path] = "leaf"
        else:
            roles[path] = "service"
    return roles, group_of, len(groups)
