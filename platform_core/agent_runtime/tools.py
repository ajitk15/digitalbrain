"""Knowledge tools the models may call, and the provenance they produce.

Two invariants hold this module together.

**Scope is closed over, never supplied by the model.** A ToolScope is built in the
request, after access has already been granted, and every query filters on
``scope.app_id``. Model arguments may only narrow a queryset that is already
scoped. An id belonging to another application therefore reads as "not found"
rather than leaking or raising.

**Citations come from what a tool returned and the database still agrees with.**
The recorder keeps what the tools produced; `verified_citations` re-checks each
one against live sources before anything is persisted or rendered. A citation the
model invents resolves to nothing.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

MAX_RESULTS = 8
MAX_CITATIONS = 8
MAX_TOOL_CALLS = 8
SOURCE_WINDOW = 4000

NOT_FOUND = "No source with that id is available in this application."
BUDGET_EXHAUSTED = (
    "The search budget for this answer is used up. Answer with the evidence you already have."
)


@dataclass(frozen=True)
class ToolScope:
    """Who is asking, and about which application. Never model-supplied."""

    user_id: uuid.UUID
    app_id: uuid.UUID
    purpose: str = "chat"


@dataclass(frozen=True)
class ToolResult:
    text: str
    citations: list = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    run: Callable


def _scoped_application(scope):
    """Re-resolve the application and re-check access on every call.

    Calls are spread over seconds; a grant revoked mid-answer must stop the next
    tool call rather than being trusted for the whole run.
    """
    from ..models import User
    from ..workbench import access

    user = User.objects.get(pk=scope.user_id)
    app, _ = access(user, scope.app_id, "knowledge")
    return app


def search_knowledge(scope, args):
    """Lexical passage search over this application's active sources."""
    from ..workbench import lexical_citations

    app = _scoped_application(scope)
    query = str(args.get("query") or "").strip()[:500]
    if not query:
        return ToolResult("Provide a search query.", [])
    limit = args.get("limit", 5)
    limit = limit if isinstance(limit, int) and 1 <= limit <= MAX_RESULTS else 5
    citations = lexical_citations(app, query)[:limit]
    if not citations:
        return ToolResult(f"No sources in this application matched {query!r}.", [])
    lines = [f"{len(citations)} matching passage(s) for {query!r}:"]
    for index, citation in enumerate(citations, 1):
        lines.append(
            f"[{index}] id={citation['id']} title={citation['title']}\n{citation['excerpt']}"
        )
    return ToolResult("\n\n".join(lines), citations)


def fetch_source(scope, args):
    """A bounded window of one source the model has already found."""
    from ..models import KnowledgeEntry

    app = _scoped_application(scope)
    try:
        entry_id = uuid.UUID(str(args.get("source_id", "")))
    except (ValueError, AttributeError, TypeError):
        return ToolResult(NOT_FOUND, [])
    # The application filter is not negotiable; the model only chooses which of
    # this application's sources to read.
    entry = KnowledgeEntry.objects.filter(application=app, active=True, pk=entry_id).first()
    if entry is None:
        return ToolResult(NOT_FOUND, [])
    offset = args.get("offset", 0)
    offset = offset if isinstance(offset, int) and offset >= 0 else 0
    offset = min(offset, max(len(entry.content) - 1, 0))
    excerpt = entry.content[offset : offset + SOURCE_WINDOW]
    if not excerpt:
        return ToolResult(f"{entry.title} has no content at offset {offset}.", [])
    remaining = max(len(entry.content) - (offset + len(excerpt)), 0)
    citation = {
        "id": str(entry.pk),
        "title": entry.title,
        "digest": entry.digest,
        "excerpt": excerpt,
    }
    text = f"{entry.title} (characters {offset}-{offset + len(excerpt)}):\n{excerpt}"
    if remaining:
        text += f"\n\n[{remaining} more characters; call again with offset={offset + len(excerpt)}]"
    return ToolResult(text, [citation])


SEARCH_KNOWLEDGE = ToolSpec(
    name="search_knowledge",
    description=(
        "Search this application's knowledge sources for passages relevant to a query. "
        "Returns numbered passages with their source ids."
    ),
    run=search_knowledge,
)

FETCH_SOURCE = ToolSpec(
    name="fetch_source",
    description=(
        "Read more of one knowledge source by its id, starting at a character offset. "
        "Use ids returned by search_knowledge."
    ),
    run=fetch_source,
)

TOOL_SPECS = [SEARCH_KNOWLEDGE, FETCH_SOURCE]

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to look for."},
        "limit": {"type": "integer", "description": "Maximum passages, 1-8."},
    },
    "required": ["query"],
}

FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "description": "Source id from search_knowledge."},
        "offset": {"type": "integer", "description": "Character offset to read from."},
    },
    "required": ["source_id"],
}

SCHEMAS = {"search_knowledge": SEARCH_SCHEMA, "fetch_source": FETCH_SCHEMA}


class CitationRecorder:
    """Collects tool provenance and enforces the per-answer tool budget."""

    def __init__(self, scope, budget=MAX_TOOL_CALLS):
        self.scope = scope
        self.budget = budget
        self.calls = 0
        self._citations = []

    @property
    def exhausted(self):
        return self.calls >= self.budget

    def record(self, result):
        self.calls += 1
        for citation in result.citations:
            key = (citation.get("id"), citation.get("excerpt"))
            if key not in {(c.get("id"), c.get("excerpt")) for c in self._citations}:
                self._citations.append(citation)

    def seed(self, citations):
        """Pre-load evidence computed before the run, without spending budget."""
        for citation in citations or []:
            self._citations.append(citation)

    def verified_citations(self):
        """Drop anything the database no longer backs, then renumber."""
        return verify_citations(self.scope.app_id, self._citations)[:MAX_CITATIONS]


def verify_citations(app_id, candidates):
    """Keep only citations whose source is still active, unchanged and quoting truly.

    Generalizes the check graph enrichment already applies to extracted quotes.
    """
    from ..models import KnowledgeEntry

    if not candidates:
        return []
    # Ids come from a model and are not necessarily ids at all. Anything that is
    # not a UUID cannot name a source, and passing it to the ORM raises rather
    # than returning nothing - which turned one bad citation into a failed run.
    ids = set()
    for candidate in candidates:
        value = candidate.get("id")
        if not value:
            continue
        try:
            uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            continue
        ids.add(str(value))
    if not ids:
        return []
    entries = {
        str(entry.pk): entry
        for entry in KnowledgeEntry.objects.filter(
            application_id=app_id, active=True, pk__in=ids
        ).only("id", "title", "content", "digest")
    }
    verified = []
    seen = set()
    for candidate in candidates:
        entry = entries.get(candidate.get("id"))
        if entry is None or entry.digest != candidate.get("digest"):
            continue
        excerpt = candidate.get("excerpt") or ""
        if not excerpt or excerpt not in entry.content:
            continue
        key = (str(entry.pk), excerpt)
        if key in seen:
            continue
        seen.add(key)
        verified.append(
            {
                "id": str(entry.pk),
                "title": entry.title,
                "digest": entry.digest,
                "excerpt": excerpt,
            }
        )
    return verified


def run_tool(spec, scope, args, recorder):
    """Execute one tool under the budget, returning text for the model."""
    if recorder.exhausted:
        return BUDGET_EXHAUSTED
    result = spec.run(scope, args or {})
    recorder.record(result)
    return result.text
