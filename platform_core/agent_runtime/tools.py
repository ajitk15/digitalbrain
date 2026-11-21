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

**Every call leaves a step, and a step holds no text** (`run_tool`). The tool's
name, the *shape* of its arguments (`shape`: numbers, ids and lengths, never a
string a model or a person wrote), what happened, how much came back and how
long it took. The harness keeps them as `AgentStep` rows. A refused call - a
grant revoked or a feature switched off since the run started - is remembered on
the recorder as well as raised, because a provider SDK turns a tool's exception
into text for the model and carries on; the harness raises it again once the run
ends, so the answer is never kept.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.http import Http404

MAX_RESULTS = 8
MAX_CITATIONS = 8
MAX_TOOL_CALLS = 8
SOURCE_WINDOW = 4000
#: The most text one tool call hands back to a model. Above anything today's tools
#: return - eight passages of `workbench.PASSAGE_LENGTH` with their titles, or one
#: `SOURCE_WINDOW` - so nothing is cut today; it is the bound the gateway's
#: reservation counts on for every turn after the first.
RESULT_CHARACTERS = 16000

NOT_FOUND = "No source with that id is available in this application."
BUDGET_EXHAUSTED = (
    "The search budget for this answer is used up. Answer with the evidence you already have."
)
STOPPED = "This answer was stopped. Do not call any more tools."
CUT = "\n\n[The rest of this result was cut.]"


@dataclass(frozen=True)
class ToolScope:
    """Who is asking, about which application, and for which run. Never model-supplied.

    `run_id` and `snapshot_id` are set by the caller for a run that reads one
    pinned thing - a Code Factory run's code snapshot - so a tool can hold every
    read to it. Chat sets neither.
    """

    user_id: uuid.UUID
    app_id: uuid.UUID
    purpose: str = "chat"
    run_id: uuid.UUID | None = None
    snapshot_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ToolResult:
    """What a tool hands back. `found` says whether it found anything, for a tool
    whose findings are not citations (code); unset, citations decide."""

    text: str
    citations: list = field(default_factory=list)
    found: bool | None = None


@dataclass(frozen=True)
class ToolSpec:
    """One tool, as `registry.TOOLS` lists it.

    `schema` is the JSON schema the Claude CLI is shown. `openai_description` and
    `openai_schema`, when set, are what the OpenAI Agents SDK is shown instead:
    the two providers were given differently worded tools before the registry
    existed, and changing what a model reads is measured for drift first, never
    renamed in passing. A new tool sets `schema` alone, written so the OpenAI
    strict mode accepts it. `detail` names the argument the live answer shows
    beside the tool's name, and how much of it.
    """

    name: str
    description: str
    run: Callable
    schema: dict = field(default_factory=dict)
    openai_description: str = ""
    openai_schema: dict | None = None
    detail: tuple = ()
    purposes: frozenset = field(default_factory=frozenset)

    def wire(self, provider):
        """(description, schema) as this provider is shown them."""
        if provider == "openai" and self.openai_schema is not None:
            return self.openai_description or self.description, self.openai_schema
        return self.description, self.schema

    def shown(self, args):
        """What the live answer says this call is about."""
        if not self.detail:
            return ""
        name, length = self.detail
        return str((args or {}).get(name, ""))[:length]


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
    from ..models import User
    from ..workbench import lexical_citations

    app = _scoped_application(scope)
    query = str(args.get("query") or "").strip()[:500]
    if not query:
        return ToolResult("Provide a search query.", [])
    limit = args.get("limit", 5)
    limit = limit if isinstance(limit, int) and 1 <= limit <= MAX_RESULTS else 5
    citations = lexical_citations(app, query, User.objects.get(pk=scope.user_id))[:limit]
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
    from ..models import KnowledgeEntry, User
    from ..permissions import permitted

    app = _scoped_application(scope)
    try:
        entry_id = uuid.UUID(str(args.get("source_id", "")))
    except (ValueError, AttributeError, TypeError):
        return ToolResult(NOT_FOUND, [])
    # The application filter is not negotiable; the model only chooses which of
    # this application's sources to read.
    entry = KnowledgeEntry.objects.filter(application=app, pk=entry_id).answerable().first()
    if entry is not None and not permitted(entry, User.objects.get(pk=scope.user_id)):
        entry = None
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

#: Exactly what `agents.function_tool` derived from the two functions the OpenAI
#: runtime used to declare; `test_agent_harness` holds them to it.
OPENAI_SEARCH_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "query": {"description": "What to look for.", "title": "Query", "type": "string"},
        "limit": {
            "default": 5,
            "description": "Maximum passages to return, 1-8.",
            "title": "Limit",
            "type": "integer",
        },
    },
    "required": ["query", "limit"],
    "title": "search_knowledge_args",
    "type": "object",
}

OPENAI_FETCH_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "source_id": {
            "description": "The source id to read.",
            "title": "Source Id",
            "type": "string",
        },
        "offset": {
            "default": 0,
            "description": "Character offset to start from.",
            "title": "Offset",
            "type": "integer",
        },
    },
    "required": ["source_id", "offset"],
    "title": "fetch_source_args",
    "type": "object",
}

#: The chat answer's tools serve both chat purposes: a graph conversation reads
#: knowledge the same way.
KNOWLEDGE_PURPOSES = frozenset({"chat", "graph_retrieval"})

SEARCH_KNOWLEDGE = ToolSpec(
    name="search_knowledge",
    description=(
        "Search this application's knowledge sources for passages relevant to a query. "
        "Returns numbered passages with their source ids."
    ),
    run=search_knowledge,
    schema=SEARCH_SCHEMA,
    openai_description="Search this application's knowledge sources for relevant passages.",
    openai_schema=OPENAI_SEARCH_SCHEMA,
    detail=("query", 120),
    purposes=KNOWLEDGE_PURPOSES,
)

FETCH_SOURCE = ToolSpec(
    name="fetch_source",
    description=(
        "Read more of one knowledge source by its id, starting at a character offset. "
        "Use ids returned by search_knowledge."
    ),
    run=fetch_source,
    schema=FETCH_SCHEMA,
    openai_description="Read more of one knowledge source returned by search_knowledge.",
    openai_schema=OPENAI_FETCH_SCHEMA,
    detail=("source_id", 64),
    purposes=KNOWLEDGE_PURPOSES,
)


class CitationRecorder:
    """Collects tool provenance, enforces the per-answer tool budget, and keeps
    the run's steps.

    `cancel` is the answer's Stop: once it is set no further tool runs. `revoked`
    holds the refusal a tool raised, for the harness to raise again.
    """

    def __init__(self, scope, budget=MAX_TOOL_CALLS, cancel=None):
        self.scope = scope
        self.budget = budget
        self.cancel = cancel
        self.calls = 0
        self.revoked = None
        self.steps = []
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
        from ..models import User

        viewer = User.objects.filter(pk=self.scope.user_id).first()
        return verify_citations(self.scope.app_id, self._citations, viewer)[:MAX_CITATIONS]


def verify_citations(app_id, candidates, viewer=None):
    """Keep only citations whose source is still active, unchanged and quoting truly.

    Generalizes the check graph enrichment already applies to extracted quotes.
    """
    from ..models import KnowledgeEntry
    from ..permissions import permitted

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
            application_id=app_id, pk__in=ids
        ).answerable().only("id", "title", "content", "digest", "access_principals")
        if permitted(entry, viewer)
    }
    verified = []
    seen = set()
    for candidate in candidates:
        entry = entries.get(candidate.get("id"))
        if entry is None or entry.digest != candidate.get("digest"):
            continue
        excerpt = candidate.get("excerpt") or ""
        # A graph citation's excerpt is "Graph vN: <relationship> ... Source
        # evidence: <quote>", which never appears in the source; its `quote` is
        # the source's own words. Checking the excerpt dropped every graph
        # citation from a streamed graph answer.
        quote = candidate.get("quote") or excerpt
        if not excerpt or not quote or quote not in entry.content:
            continue
        key = (str(entry.pk), excerpt)
        if key in seen:
            continue
        seen.add(key)
        citation = {
            "id": str(entry.pk),
            "title": entry.title,
            "digest": entry.digest,
            "excerpt": excerpt,
        }
        if candidate.get("quote"):
            citation["quote"] = quote
        verified.append(citation)
    return verified


def shape(spec, args):
    """The arguments of one call with no text in them, for its step.

    Only the arguments the tool declares; a number or a flag as it is, an id
    only when it really is one, anything else as its length.
    """
    declared = set((spec.schema or {}).get("properties", {})) | set(
        (spec.openai_schema or {}).get("properties", {})
    )
    shaped = {}
    for name in sorted(declared & set(args or {})):
        value = args[name]
        if isinstance(value, bool) or (isinstance(value, int) and abs(value) < 10**9):
            shaped[name] = value
            continue
        try:
            shaped[name] = {"id": str(uuid.UUID(str(value)))}
        except (ValueError, AttributeError, TypeError):
            shaped[name] = {"characters": len(str(value))}
    return shaped


def _step(recorder, spec, args, outcome, started, text="", citations=0):
    steps = getattr(recorder, "steps", None)
    if steps is None:
        return
    steps.append(
        {
            "sequence": len(steps) + 1,
            "tool": spec.name,
            "arguments": shape(spec, args),
            "outcome": outcome,
            "result_characters": len(text),
            "citations": citations,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    )


def run_tool(spec, scope, args, recorder):
    """Execute one tool under the budget, returning text for the model.

    Refused before it runs when the answer was stopped or the budget is spent; a
    grant revoked since the run began raises, as it always has, and is kept on
    the recorder too. Every outcome is a step.
    """
    from .. import telemetry

    started = time.monotonic()
    args = args or {}
    cancel = getattr(recorder, "cancel", None)
    if cancel is not None and cancel.is_set():
        _step(recorder, spec, args, "stopped", started)
        return STOPPED
    if recorder.exhausted:
        _step(recorder, spec, args, "budget", started)
        return BUDGET_EXHAUSTED
    with telemetry.span(
        "agent tool", tool=spec.name, purpose=scope.purpose, application_id=scope.app_id
    ) as traced:
        try:
            result = spec.run(scope, args)
        except (PermissionDenied, Http404) as refused:
            if hasattr(recorder, "revoked") and recorder.revoked is None:
                recorder.revoked = refused
            _step(recorder, spec, args, "refused", started)
            traced.set(outcome="refused")
            raise
        except Exception:
            _step(recorder, spec, args, "failed", started)
            raise
        text = result.text
        if len(text) > RESULT_CHARACTERS:
            text = text[: RESULT_CHARACTERS - len(CUT)] + CUT
        recorder.record(result)
        found = bool(result.citations) if result.found is None else result.found
        outcome = "found" if found else "nothing"
        traced.set(outcome=outcome)
        _step(recorder, spec, args, outcome, started, text, len(result.citations))
    return text
