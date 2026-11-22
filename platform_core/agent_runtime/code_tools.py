"""Read-only tools over one Code Factory run's pinned code snapshot.

Phase 1 of `documentation/engineering/agentic-ai-plan.md`. A run's implementation,
test and review agents used to see only the files Python chose before they
started (`code_factory_agents.reference_files`: ten files, 64 KB, picked by
name). With these tools the agent looks for itself - lists a folder, reads a
file a window at a time, searches, finds where a symbol is defined, who imports
a file and which tests cover it.

What holds them, beside everything `tools.run_tool` holds every tool to:

* **One snapshot, closed over.** `ToolScope.snapshot_id` is set by the caller to
  the run's pinned `CodeSnapshot`, and every read is `CodeFile` rows of that
  snapshot *of a repository of this application*. A path is only ever a key
  looked up among those rows: nothing here opens a file, so no path a model
  writes can reach the disk, another repository, or another application. A
  snapshot that is not this application's reads as no files at all.
* **The purpose's rule, on every call.** The run's own purpose is authorized
  again before each read (`gateway.authorize`), so a grant revoked or Code
  Factory switched off mid-run refuses the next call, and the harness ends the
  run.
* **Reading, never writing.** What a run may change is still decided by the
  approved plan; these tools only read.
"""

import re

from .tools import ToolResult, ToolSpec

#: The purposes these tools serve: Code Factory's implementation chain, the AI
#: step of a code review (Phase 4), which reads the review's base snapshot, and
#: code documentation's AI step, which reads the documented snapshot.
CODE_PURPOSES = frozenset({"plan_drafting", "code_review", "code_docs"})
#: The purposes whose answers quote what the tools read, so a read is evidence
#: with an id the model is told, and the answer is checked against its live text.
QUOTING = frozenset({"code_review", "code_docs"})
#: Who read a window, as its evidence title says, for each quoting purpose.
READ_BY = {"code_review": "read by the reviewer", "code_docs": "read for the documentation"}
MAX_LISTED = 200
MAX_LINES = 400
LINE_CHARACTERS = 400
MAX_MATCHES = 20
LINES_PER_MATCH = 3
MAX_TERMS = 8
MAX_CALLERS = 30

NO_SNAPSHOT = "This run has no code snapshot to read."
NO_FILE = "No file with that path is in this run's snapshot."


def _files(scope):
    """The run's snapshot's files, after the purpose's rule is read again."""
    from .. import gateway
    from ..models import CodeFile, User

    user = User.objects.get(pk=scope.user_id)
    app, _ = gateway.authorize(user, scope.app_id, scope.purpose)
    if scope.snapshot_id is None:
        return None
    return CodeFile.objects.filter(
        snapshot_id=scope.snapshot_id, snapshot__repository__application=app
    )


def clean_path(value):
    """A path as the snapshot stores it: forward slashes, nothing in front."""
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith(("./", "/")):
        path = path[2:] if path.startswith("./") else path[1:]
    return path


def _text(value, limit=500):
    return str(value or "").strip()[:limit]


def list_files(scope, args):
    files = _files(scope)
    if files is None:
        return ToolResult(NO_SNAPSHOT)
    prefix = clean_path(args.get("prefix"))
    listed = files.filter(path__startswith=prefix) if prefix else files
    total = listed.count()
    rows = list(listed.order_by("path").values_list("path", "lines")[:MAX_LISTED])
    if not rows:
        return ToolResult(f"No files under {prefix!r} in this run's snapshot.", found=False)
    lines = [f"{total} file(s) under {prefix or 'the repository root'}:"]
    lines += [f"{path} ({count} lines)" for path, count in rows]
    if total > len(rows):
        lines.append(f"[{total - len(rows)} more; list a narrower prefix]")
    return ToolResult("\n".join(lines), found=True)


def read_file(scope, args):
    files = _files(scope)
    if files is None:
        return ToolResult(NO_SNAPSHOT)
    path = clean_path(args.get("path"))
    found = files.filter(path=path).only("id", "path", "content").first() if path else None
    if found is None:
        return ToolResult(NO_FILE, found=False)
    text = found.content.splitlines()
    start = args.get("start_line")
    start = start if isinstance(start, int) and start >= 1 else 1
    end = args.get("end_line")
    last = min(len(text), start + MAX_LINES - 1)
    end = min(end, last) if isinstance(end, int) and end >= start else last
    if start > len(text):
        return ToolResult(f"{path} has {len(text)} lines.", found=False)
    shown = [
        f"{number:>5} | {line[:LINE_CHARACTERS]}"
        for number, line in enumerate(text[start - 1 : end], start=start)
    ]
    quoting = scope.purpose in QUOTING
    head = (
        (f"Evidence id code:{found.pk} - " if quoting else "")
        + f"{path} (lines {start}-{end} of {len(text)}, as indexed):"
    )
    tail = (
        [f"[{len(text) - end} more lines; read again from start_line={end + 1}]"]
        if end < len(text)
        else []
    )
    # The window as it stands in the file, without the line numbers the model is
    # shown: what a quote must match, and `code_review.live_texts` checks again.
    citations = (
        [
            {
                "id": f"code:{found.pk}",
                "title": f"{path} lines {start}-{end} ({READ_BY[scope.purpose]})",
                "excerpt": "\n".join(text[start - 1 : end]),
            }
        ]
        if quoting
        else []
    )
    return ToolResult("\n".join([head, *shown, *tail]), citations, found=True)


def read_diff(scope, args):
    """The whole diff of one file the review's change touches, as stored."""
    from ..models import CodeReviewFile

    files = _files(scope)
    if files is None or scope.run_id is None:
        return ToolResult(NO_SNAPSHOT)
    path = clean_path(args.get("path"))
    row = (
        CodeReviewFile.objects.filter(
            review_id=scope.run_id, review__application_id=scope.app_id, path=path
        ).first()
        if path
        else None
    )
    if row is None or not row.diff:
        return ToolResult("That file is not one this change touches.", found=False)
    return ToolResult(
        f"Evidence id diff:{row.path} - the change to {row.path} ({row.change}):\n{row.diff}",
        [{"id": f"diff:{row.path}", "title": f"Change to {row.path}", "excerpt": row.diff}],
        found=True,
    )


def terms_of(query):
    """The query's words, at least two characters, at most `MAX_TERMS`."""
    seen = []
    for word in re.findall(r"[^\s\"'`()\[\]{},;]+", str(query or "")):
        word = word.lower()
        if len(word) >= 2 and word not in seen:
            seen.append(word)
    return seen[:MAX_TERMS]


def search_code(scope, args):
    files = _files(scope)
    if files is None:
        return ToolResult(NO_SNAPSHOT)
    terms = terms_of(args.get("query"))
    if not terms:
        return ToolResult("Provide words to search for.", found=False)
    limit = args.get("limit")
    limit = limit if isinstance(limit, int) and 1 <= limit <= MAX_MATCHES else 10
    ranked = []
    for path, content in files.values_list("path", "content").iterator():
        lowered = content.lower()
        present = [term for term in terms if term in lowered or term in path.lower()]
        if not present:
            continue
        hits = sum(lowered.count(term) for term in present)
        ranked.append((-len(present), -hits, path, content))
    ranked.sort()
    words = " ".join(terms)
    if not ranked:
        return ToolResult(f"Nothing in this run's snapshot mentions {words}.", found=False)
    lines = [f"{len(ranked)} file(s) mention {words}; the best {min(limit, len(ranked))}:"]
    for _, _, path, content in ranked[:limit]:
        scored = []
        for number, line in enumerate(content.splitlines(), start=1):
            lowered = line.lower()
            count = sum(term in lowered for term in terms)
            if count:
                scored.append((-count, number, line))
        scored.sort()
        lines.append(path)
        for _, number, line in sorted(scored[:LINES_PER_MATCH], key=lambda item: item[1]):
            lines.append(f"{number:>5} | {line.strip()[:LINE_CHARACTERS]}")
    return ToolResult("\n".join(lines), found=True)


def find_symbol(scope, args):
    files = _files(scope)
    if files is None:
        return ToolResult(NO_SNAPSHOT)
    name = _text(args.get("name"), 200)
    if not name:
        return ToolResult("Provide a symbol name.", found=False)
    exact, folded = [], []
    for path, symbols in files.values_list("path", "symbols").iterator():
        for symbol in symbols or []:
            if not isinstance(symbol, dict):
                continue
            found = str(symbol.get("name") or "")
            row = f"{path}:{symbol.get('line', '?')} {symbol.get('kind', 'symbol')} {found}"
            if found == name:
                exact.append(row)
            elif found.lower() == name.lower():
                folded.append(row)
    rows = (exact or folded)[:MAX_MATCHES]
    if not rows:
        return ToolResult(f"No definition of {name!r} in this run's snapshot.", found=False)
    return ToolResult("\n".join([f"{name} is defined at:", *rows]), found=True)


def callers(scope, args):
    from ..models import CodeRelationship

    files = _files(scope)
    if files is None:
        return ToolResult(NO_SNAPSHOT)
    path = clean_path(args.get("path"))
    target = files.filter(path=path).only("id").first() if path else None
    if target is None:
        return ToolResult(NO_FILE, found=False)
    edges = (
        CodeRelationship.objects.filter(snapshot_id=scope.snapshot_id, target=target)
        .select_related("source")
        .order_by("source__path")[:MAX_CALLERS]
    )
    rows = [
        f"{edge.source.path} ({edge.kind}{': ' + edge.detail[:120] if edge.detail else ''})"
        for edge in edges
    ]
    if not rows:
        return ToolResult(f"Nothing in this run's snapshot depends on {path}.", found=False)
    return ToolResult("\n".join([f"Files that depend on {path}:", *rows]), found=True)


def tests_for(scope, args):
    from ..code_factory_agents import covering_tests
    from ..models import CodeSnapshot

    files = _files(scope)
    if files is None:
        return ToolResult(NO_SNAPSHOT)
    path = clean_path(args.get("path"))
    if not path or not files.filter(path=path).exists():
        return ToolResult(NO_FILE, found=False)
    snapshot = CodeSnapshot.objects.get(pk=scope.snapshot_id)
    found = covering_tests(snapshot, [path])
    if not found:
        return ToolResult(f"No existing test in this run's snapshot covers {path}.", found=False)
    return ToolResult("\n".join([f"Tests that cover {path}:", *found]), found=True)


def _schema(**properties):
    """A schema both providers accept: every property required, none other allowed."""
    return {
        "type": "object",
        "properties": {
            name: {"type": kind, "description": description}
            for name, (kind, description) in properties.items()
        },
        "required": list(properties),
        "additionalProperties": False,
    }


LIST_FILES = ToolSpec(
    name="list_files",
    description=(
        "List the files in this run's code snapshot under a folder prefix, with their "
        "line counts. An empty prefix lists from the root."
    ),
    run=list_files,
    schema=_schema(prefix=("string", "Folder prefix, such as src/app/; empty for all.")),
    detail=("prefix", 120),
    purposes=CODE_PURPOSES,
)

READ_FILE = ToolSpec(
    name="read_file",
    description=(
        "Read numbered lines of one file in this run's code snapshot, as it was indexed. "
        f"At most {MAX_LINES} lines a call; read again from where it stopped."
    ),
    run=read_file,
    schema=_schema(
        path=("string", "The file's path, as list_files shows it."),
        start_line=("integer", "First line to read, from 1."),
        end_line=("integer", "Last line to read; 0 reads as far as one call allows."),
    ),
    detail=("path", 160),
    purposes=CODE_PURPOSES,
)

SEARCH_CODE = ToolSpec(
    name="search_code",
    description=(
        "Search this run's code snapshot for files containing the given words or "
        "identifiers. Returns the best files with their matching lines."
    ),
    run=search_code,
    schema=_schema(
        query=("string", "Words or identifiers to look for."),
        limit=("integer", f"Most files to return, 1-{MAX_MATCHES}."),
    ),
    detail=("query", 120),
    purposes=CODE_PURPOSES,
)

FIND_SYMBOL = ToolSpec(
    name="find_symbol",
    description="Where a function or class is defined in this run's code snapshot.",
    run=find_symbol,
    schema=_schema(name=("string", "The function or class name.")),
    detail=("name", 120),
    purposes=CODE_PURPOSES,
)

CALLERS = ToolSpec(
    name="callers",
    description="The files in this run's code snapshot that import or call one file.",
    run=callers,
    schema=_schema(path=("string", "The file's path, as list_files shows it.")),
    detail=("path", 160),
    purposes=CODE_PURPOSES,
)

TESTS_FOR = ToolSpec(
    name="tests_for",
    description="Existing test files in this run's code snapshot that exercise one file.",
    run=tests_for,
    schema=_schema(path=("string", "The file's path, as list_files shows it.")),
    detail=("path", 160),
    purposes=CODE_PURPOSES,
)

READ_DIFF = ToolSpec(
    name="read_diff",
    description=(
        "The whole unified diff of one file this change touches - the evidence shows "
        "each diff cut at a length; this is all of it."
    ),
    run=read_diff,
    schema=_schema(path=("string", "The changed file's path.")),
    detail=("path", 160),
    purposes=frozenset({"code_review"}),
)

CODE_TOOLS = (LIST_FILES, READ_FILE, SEARCH_CODE, FIND_SYMBOL, CALLERS, TESTS_FOR, READ_DIFF)
