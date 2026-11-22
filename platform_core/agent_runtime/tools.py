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

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.http import Http404

MAX_RESULTS = 8
MAX_CITATIONS = 8
#: Sources a prose answer keeps when it cites more than `MAX_CITATIONS`: every one
#: it cites, up to this, before any it read and did not cite.
MAX_CITED = 16
#: An answer's [n] or [2, 3] citation markers, as `chat_text` reads them.
MARKER = re.compile(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]")
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
    whose findings are not citations (code); unset, citations decide.

    `numbered`, when set, writes the text again with each citation under the
    number the run gives it (`CitationRecorder.record`): its place in the run's
    whole list, seeded evidence first - the number the page shows beside the
    answer. A reply numbered from 1 on every call let an answer cite a passage
    it searched for as [1] and be shown beside the run's first source instead.
    """

    text: str
    citations: list = field(default_factory=list)
    found: bool | None = None
    numbered: Callable | None = None


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
    purpose = getattr(scope, "purpose", "chat")
    if purpose not in KNOWLEDGE_PURPOSES:
        # A tool serving another purpose - triage - is held to that purpose's
        # rule on every call, as Code Factory's code tools are to theirs. A scope
        # that names no purpose is chat's, as `ToolScope`'s default says.
        from .. import gateway

        app, _ = gateway.authorize(user, scope.app_id, purpose)
        return app
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

    def text(numbers):
        lines = [f"{len(citations)} matching passage(s) for {query!r}:"]
        for number, citation in zip(numbers, citations, strict=True):
            lines.append(
                f"[{number}] id={citation['id']} title={citation['title']}\n{citation['excerpt']}"
            )
        return "\n\n".join(lines)

    return ToolResult(text(range(1, len(citations) + 1)), citations, numbered=text)


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
    # Cited by the number the run gives this window, as a searched passage is.
    return ToolResult(text, [citation], numbered=lambda numbers: f"[{numbers[0]}] {text}")


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
#: knowledge the same way. Triage reads sources with the same two
#: (`ops_tools.py` adds its own), held to its own purpose's rule.
KNOWLEDGE_PURPOSES = frozenset({"chat", "graph_retrieval"})
#: Purposes the source tools also serve, each held to its own rule on every call:
#: triage (Phase 3), drafting a fix for a Learned question (Phase 6) and a
#: document's AI review, which looks for what other sources say.
SOURCE_PURPOSES = KNOWLEDGE_PURPOSES | {
    "serviceops_triage", "knowledge_upkeep", "document_review"
}

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
    purposes=SOURCE_PURPOSES,
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
    purposes=SOURCE_PURPOSES,
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
        self.seeded = 0

    @property
    def exhausted(self):
        return self.calls >= self.budget

    def record(self, result):
        """Keep a tool's citations; returns the number each has in the run's list
        (from 1, seeded evidence first) - one already kept keeps its number."""
        self.calls += 1
        numbers = []
        for citation in result.citations:
            key = (citation.get("id"), citation.get("excerpt"))
            kept = [(c.get("id"), c.get("excerpt")) for c in self._citations]
            if key in kept:
                numbers.append(kept.index(key) + 1)
                continue
            self._citations.append(citation)
            numbers.append(len(self._citations))
        return numbers

    def seed(self, citations):
        """Pre-load evidence computed before the run, without spending budget."""
        for citation in citations or []:
            self._citations.append(citation)
        self.seeded = len(self._citations)

    def read_citations(self):
        """What the tools returned beyond the seeded evidence, unverified, in the
        order read - for a caller that verifies its own kinds of evidence (a code
        review's diffs and snapshot files, which are not knowledge sources)."""
        return list(self._citations[self.seeded :])

    def found_citations(self, limit=24):
        """What the tools found, beyond the evidence the run was seeded with,
        verified against live sources: an agent's own reading, in the order read."""
        from ..models import User

        viewer = User.objects.filter(pk=self.scope.user_id).first()
        return verify_citations(self.scope.app_id, self._citations[self.seeded :], viewer)[
            :limit
        ]

    def verified_citations(self):
        """Drop anything the database no longer backs, then renumber."""
        from ..models import User

        viewer = User.objects.filter(pk=self.scope.user_id).first()
        return verify_citations(self.scope.app_id, self._citations, viewer)[:MAX_CITATIONS]

    def numbered(self, answer):
        """(answer, citations) as the page will show them, for a prose answer.

        The answer cites by the run's numbers (`record`). What it ends with is
        what still verifies, at most `MAX_CITATIONS` - every source the answer
        cites first (up to `MAX_CITED`), then the rest in the run's order - and
        a source that is dropped renumbers every one after it. So the list is
        kept in the run's order, numbered from 1, and each `[n]` the answer wrote
        is moved onto its source's new number; one to a source not kept (no
        longer verifying, or past the cap) is taken out rather than left to
        point at another source.
        """
        from ..models import User

        viewer = User.objects.filter(pk=self.scope.user_id).first()
        good = {
            (citation["id"], citation["excerpt"]): citation
            for citation in verify_citations(self.scope.app_id, self._citations, viewer)
        }
        held, seen = [], set()
        for number, candidate in enumerate(self._citations, 1):
            key = (str(candidate.get("id")), candidate.get("excerpt") or "")
            if key in good and key not in seen:
                seen.add(key)
                held.append((number, good[key]))
        cited = {int(n) for group in MARKER.findall(answer or "") for n in group.split(",")}
        keep = [pair for pair in held if pair[0] in cited][:MAX_CITED]
        keep += [pair for pair in held if pair[0] not in cited][: max(MAX_CITATIONS - len(keep), 0)]
        keep.sort(key=lambda pair: pair[0])
        moved = {number: index for index, (number, _) in enumerate(keep, 1)}

        def renumber(match):
            numbers = [moved[int(n)] for n in match[1].split(",") if int(n) in moved]
            return "[" + ", ".join(str(n) for n in numbers) + "]" if numbers else ""

        return MARKER.sub(renumber, answer or ""), [citation for _, citation in keep]


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


#: A record id as the tools write it. Not personal data, and the one thing a model
#: must be able to repeat exactly: it is how a citation names its source.
RECORD_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def shown_to_model(purpose, text):
    """A tool's text as the model may read it.

    Triage minimizes identifiers and secret shapes in everything it hands a model
    (`serviceops_triage.redact`), its evidence pack included, so what its tools
    read is minimized the same way - everything but the record ids between it,
    which the pack hands over outside the redacted text: `redact` reads a run of
    digits and hyphens as a telephone number, and some ids hold one, so an id
    came back as "a8dd5e5f-[phone]-d180b776055d" and could not be cited. The
    citations keep the source's own words: they are verified against it, never
    shown.
    """
    if purpose == "serviceops_triage":
        from ..serviceops_triage import redact

        pieces, last = [], 0
        for match in RECORD_ID.finditer(text):
            pieces += [redact(text[last : match.start()]), match.group()]
            last = match.end()
        return "".join([*pieces, redact(text[last:])])
    return text


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
        "agent tool",
        tool=spec.name,
        purpose=getattr(scope, "purpose", "chat"),
        application_id=scope.app_id,
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
        numbers = recorder.record(result)
        text = result.text
        if result.numbered is not None and numbers and len(numbers) == len(result.citations):
            text = result.numbered(numbers)
        text = shown_to_model(getattr(scope, "purpose", "chat"), text)
        if len(text) > RESULT_CHARACTERS:
            text = text[: RESULT_CHARACTERS - len(CUT)] + CUT
        found = bool(result.citations) if result.found is None else result.found
        outcome = "found" if found else "nothing"
        traced.set(outcome=outcome)
        _step(recorder, spec, args, outcome, started, text, len(result.citations))
    return text
