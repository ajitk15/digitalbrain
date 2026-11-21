# Agentic AI - implementation plan

Turns the platform's fixed, one-shot model calls into real agents where letting the
model **choose what to read** measurably improves the result, and records what may
come after. Every phase can ship on its own, behind its own switch, and is measured
before and after like the retrieval roadmap was (`retrieval-roadmap-plan.md`).

Estimates are engineering days for one developer who knows the codebase, including
tests and the five verification gates in `CLAUDE.md`. They are not calendar time.

## Where we are

| Surface | Today | Agentic? |
|---|---|---|
| Chat answer | Claude Agent SDK / OpenAI Agents SDK, tools `search_knowledge` and `fetch_source`, `MAX_TURNS = 4`, `MAX_TOOL_CALLS = 8`, scope closed over in `ToolScope` | **Yes** - the only one |
| MCP server (`api.py:mcp`) | Serves graph search to outside agents; calls no model | Serves agents |
| Code Factory work order, implementation, tests, review, verification | `ask()` → `invoke_ai`, one turn, `tools=[]`, files chosen beforehand by `readable_targets` and `reference_files` | No - a Python pipeline of single calls |
| Code Factory triage, analysis, design, plan | Same, in `code_factory_analysis` | No |
| ServiceOps triage | One call over `serviceops_triage.evidence_pack` | No |
| Graph extraction, typing, document review, code review, code docs, reranking | One call each, JSON out, every quote verified | No |

`CLAUDE.md` says "Code Factory's agents still need the CLI's tools". The code does not:
they run with `max_turns=1` and no tools. Phase 0 corrects that line.

## The rule every phase keeps

**An agent chooses what to read. It never chooses what to write, who it acts as, or
what counts as evidence.**

- **Read-only tools, scope closed over.** Every tool takes a `ToolScope` built after
  access was granted. Model arguments only narrow a queryset or file set that is
  already scoped. A foreign id or path reads as "not found".
- **Grant checked again on every call**, as `tools._scoped_application` does now, so a
  revoked grant stops the next turn.
- **Writes stay where they are**: `apply_local`, `publish`, `add_knowledge`, verdicts,
  approvals, publishing a version. They run in Python after a person approves, never
  as a tool.
- **Evidence is verified, never trusted**: citations against live sources (active,
  unchanged digest, exact excerpt), quotes against text, exactly as today.
- **Every turn is paid for honestly**: through `gateway.admitted`, `budget.check`, one
  `AIUsage` receipt per turn, and never retried for the caller.
- **Restricted sources stay out**: tools read through `answerable()` and
  `permissions.permitted`.
- **Measured, not asserted**: no phase is turned on by default until its pack beats
  the one-shot baseline.

## Summary

| Phase | What | Effort | Model cost | Measured by | Default |
|---|---|---|---|---|---|
| 0 Agent harness | Shared tool registry, per-turn reservation, trajectory log, guardrail probes | 5-7 d | None | Scorecard "Agent guardrail probes" 100% | - |
| 1 Code Factory repository tools | Implementation, tests and review read the pinned snapshot themselves | 8-10 d | Yes, more turns per run | New `factory-pack.json` (paid) + planted-bug key | Off, then on once it beats one-shot |
| 2 Evidence follow-through | Follow references from the evidence (F-03 via C-08): rules first, agent only if rules stall | 3-4 d rules, +4-5 d agent | None (rules), Yes (agent) | Scorecard `seeded_evidence_recall` (92% today) | Rules on; agent off |
| 3 ServiceOps triage agent | Triage asks for neighbourhood, changes, runbooks one hypothesis at a time | 6-8 d | Yes | Triage replay + `idea_confidence` against confirmed verdicts | Off |
| 4 Code review agent | AI review opens dependent files, tests and requirements as needed | 4-5 d | Yes | Labelled review pack (new) | Off |
| 5 Chat deepening | Graph-walk and version-aware tools, turns sized by budget | 3-4 d | Yes, bounded | Benchmark + drift pack | On where it is measured equal or better |
| 6 Knowledge upkeep agent | Drafts a fix for a "gap" candidate as a `LearningCandidate` | 4-5 d | Yes, on a button | Accept rate in Learned | Off |

Phases 0-2 (rules) are **about 16-21 days** and make the largest difference on the
platform's main pipeline. With 3-6, **about 37-48 days**.

Order: 0 must come first (every later phase uses it). 1 before 4 (review reuses the
repository tools). 2's rule half is free and measured by the scorecard, so it can run
alongside 0.

## Phase 0 - Agent harness

One place that builds an agent run, so phases 1-6 add tools and do not each rebuild
the sandbox, the receipts and the limits.

- **`agent_runtime/registry.py`**: a closed registry of tools (`ToolSpec` with name,
  schema, the purposes it serves, the feature and role it needs), the same pattern as
  `services.FEATURES` and `gateway.PURPOSES`. A purpose's tool list is read from it.
  The tool list a model sees cannot drift from what the validator allows, because
  both are written from the registry.
- **Generalise `ToolScope`**: add an optional `run_id` and `snapshot_id`, both set by
  the caller and never by the model. Chat keeps its current tools unchanged.
- **`agent_runtime/harness.py`**: one `run_agent(purpose, scope, instructions, prompt,
  tools, max_turns, max_tool_calls)` over both SDKs, drained to the end (never
  `break` after `ResultMessage`), cancellable between turns.
- **Per-turn reservation**: today a streamed answer takes one slot and one
  reservation for the whole run. An agent's worst case is `max_turns` times one
  turn's estimate plus tool output, so admission reserves that amount up front, and
  the run stops cleanly with what it has once the reservation is used. This is a
  `gateway` change and needs its own scorecard probe ("past a reservation or a full
  gateway" stays 0).
- **Trajectory log** (`AgentStep`, a new table secured by migration 0091's `secure`
  and listed in `SECURED`): turn number, tool name, argument *shape* (counts and ids,
  never text), result size, duration, receipt id. Shown on the run page under the
  phase that made it. Telemetry gets `agent.turn` and `agent.tool` attributes. Both
  are written by code, so they are safe for `telemetry.ATTRIBUTES`.
- **Scripted model for tests**: a fake model that replays a planned list of tool
  calls, so every guardrail is tested without a provider: foreign id, path outside
  the snapshot, a restricted source, a grant revoked mid-run, the budget spent
  mid-run, Stop between turns, too many tool calls.
- **Scorecard "Agent guardrail probes passing"** must be 100%, like the release-check
  and learning-loop probes.
- Correct the Code Factory line in `CLAUDE.md`.

**Done when:** chat runs on the harness with byte-identical citations on the
benchmark, and the probes pass on SQLite and PostgreSQL.

## Phase 1 - Code Factory repository tools

Today `reference_files` guesses up to `REFERENCE_FILES` (10) files and 64 KB of
reference before implementation starts. A live run already missed the consent service
a change depended on. Letting the agent look closes that gap.

**Tools** (read-only, over the run's pinned `CodeSnapshot`; for a local run, the
snapshot as indexed, never the live folder and never git):

| Tool | Returns | Bound |
|---|---|---|
| `list_files(prefix)` | Paths under a folder in the snapshot | `SKIP_DIRS` and hidden folders excluded |
| `read_file(path, start, end)` | Numbered lines | Path must be in the snapshot; `REFERENCE_FILE_BYTES` a read |
| `search_code(terms)` | Paths and line numbers | BM25 over the snapshot's files, top N |
| `find_symbol(name)` | Definition sites | From the indexed symbols |
| `callers(path_or_symbol)` | Files importing or calling it | From the import graph Code Graph keeps |
| `tests_for(path)` | Covering tests | `covering_tests` as it is now |

**What does not change:**
- The files it may **write** are still the approved plan's targets (`target_paths`).
  An agent that decides another file must change says so in `notes`, and the run
  stops for a person to approve a revised plan. It is never written.
- Each answer still names files by their number, never by a path.
- A local run is never handed the GitHub token (`code_factory_build.gate`), and its
  tools touch nothing outside the snapshot.
- `verification` and `delivery` stay one-shot and rule-checked.

**Which agents get tools:** implementation, tests and review. The work order stays
one-shot, because its job is to split approved intent, not to explore.

**Measurement:** a paid `benchmarks/factory-pack.json` (`manage.py factory_pack <app>
--as <user> --record`, the same shape as `typing_pack` and `llm_drift`): the CarePath
planted-bug tickets with an approved plan each, scored on whether the patch touches
the files the answer key names, whether CarePath's tests pass under
`run_local_tests`, cost and turns. Agent against one-shot, same model.

**Done when:** the agent beats one-shot on the pack at no more than twice the cost,
and the guardrail probes cover every new tool.

## Phase 2 - Evidence follow-through

`CLAUDE.md` already names the next step: F-03 is named only by evidence (C-08), not
by the ticket, and following references *from the evidence* "would need measuring for
noise first".

1. **Rules first (free).** `graph_ai.referenced` gets a second hop: references named
   by the cited evidence, at most `MAX_REFERENCED` more, after everything the ticket
   itself names. Measured by the scorecard's `seeded_evidence_recall` (92% today) and
   by the share of extra citations that no answer-key item needed (noise). It ships
   only if recall rises and noise stays within a stated bound.
2. **Agent only if rules stall.** An evidence agent with `search_knowledge`,
   `fetch_source` and a `follow_reference(id)` tool, its citations verified like
   chat's. It is paid, so it is measured by the factory pack, not the scorecard.

`evidence_for` still refuses without a published graph (`NO_GRAPH`). Citations still
come from `graph_snapshot`, never a draft.

## Phase 3 - ServiceOps triage agent

Triage is the most natural agent here: an on-call engineer asks "what changed?",
"have we seen this before?", "what does the runbook say?" in turn, and the answer to
one decides the next.

**Tools**, all wrapping readers that already exist so the page and the agent cannot
disagree:

| Tool | Wraps |
|---|---|
| `neighbourhood(incident)` | `ops_graph.neighbourhood` |
| `similar_incidents(incident)` | `score_pair` / `same_event` |
| `recent_changes(component, hours)` | Change records joined in the operations graph |
| `runbook_passages(component)` | Latest published revision only, as now |
| `automation_for(component)` | Linked automations - **description only** |

**Never a tool:** launching an automation (`simulate_automation` stays a person's
button), confirming a cause, closing an incident. The hypotheses still go through
`_parsed_hypotheses`, `_score` and `idea_confidence`, and only a person's verdict
makes a `confirmed` edge.

**Measurement:** triage replay against incidents with a confirmed verdict - was the
confirmed cause among the hypotheses, and at what rank - agent against one-shot,
plus cost.

## Phase 4 - Code review agent

The rule checks stay as they are (reach, routes, symbols, source with no test,
requirement passages). The AI review (purpose `code_review`, on a button) gets Phase
1's tools over the **head** text kept in `CodeReviewFile` and the base snapshot, plus
`requirement_passages` from `code_knowledge`. Every finding still quotes the diff, a
dependent file or a requirement; a quote not found exactly drops the finding; quotes
are checked again against live text when the page is drawn. Nothing is posted to
GitHub. Cache key: the digest of everything the agent read, not just the diff.

**Measurement:** a labelled review pack (diffs with known problems in dependent
files) - findings recalled, findings with no label (noise), cost.

## Phase 5 - Chat deepening

Chat is already an agent; this widens it carefully.

- A `graph_walk(entity)` tool over typed AI relationships (`graph_ai.walk`'s rules:
  `MAX_HOPS`, `MAX_FANOUT`, never structural edges), on the conversation's pinned or
  latest published version.
- `MAX_TURNS` worked out from the per-turn reservation and the application's
  remaining budget, never above a fixed ceiling, instead of a constant 4.
- The answer is still rendered through `chat_text` on the server; the browser still
  never renders model Markdown.

**Measurement:** benchmark questions through the agent path, scored on found / top 3
/ MRR against the current path, plus `llm_drift` (the drift pack is frozen; a change
of tools is exactly what drift exists to measure).

## Phase 6 - Knowledge upkeep agent

For a Learned "gap" (a complete answer that cited nothing), an owner or contributor
presses **Draft a fix**. The agent searches sources and proposes either a benchmark
question with the source it should cite, or a note of what is missing. The output is
a `LearningCandidate`, accepted or rejected by a person (`learning.accepted` /
`.rejected`). Questions people asked stay without who asked, and a question the
personal-data scan flags is never copied.

**Measurement:** accept rate in Learned over a month, against candidates built by
rules alone.

## Future enhancements

Not in scope for the phases above. Each is listed with the invariant it touches, so
whoever picks it up knows what has to be decided first.

| Enhancement | What it would add | What must be decided first |
|---|---|---|
| **Run tests as a tool** | Implementation sees a failing test and tries again inside one run | It breaks "never runs the code it writes" beyond the two opt-in exceptions. It would need the sandbox container, `code_factory_local_tests`, refusal in production, and a per-run attempt cap |
| **Supervisor and sub-agents** | A Python supervisor hands sub-tasks (explore, write, test, review) to specialised agents that run in parallel | One receipt per sub-agent turn; the reservation is the sum; the trajectory nests. The supervisor stays code, never a model |
| **"Propose" tools** | An agent creates *pending* records: a draft plan item, a draft runbook edit, a draft benchmark question | Pending only, audited, accepted by a person - the learning-loop pattern. Never a direct write |
| **Real automation launch** | Triage proposes an automation; a person approves; a launcher runs it (`serviceops-real-automation.md`) | One approval per run, the launcher's own credential, the audit row `simulate_automation` already writes. The agent itself is never able to execute anything |
| **Scheduled agents** | Nightly knowledge hygiene, stale-runbook detection, weekly access summaries | Runs as the owner who switched it on, re-checked each time, stopped by revocation - the connector-schedule pattern. Budgeted like automatic AI |
| **Wider MCP and agent-to-agent** | Outside agents (IDE, Slack, other platforms) call more read tools: `code_search`, `incident_neighbourhood` | Token = user, no token roles, `spend=False` unless gated like `chat_api` |
| **Agent memory** | What an agent learned on one run helps the next | Only as `LearningCandidate`s a person accepted - never a hidden memory a model writes |
| **Model routing** | A small model picks tools, a large one writes the answer | Each model priced and receipted; drift measured per model |
| **Prompt caching** | Cheaper repeated turns over the same snapshot | Receipts must still read cached tokens honestly (`usage.py`) |
| **Trajectory evaluation** | Score *how* an agent got there (wasted calls, loops), not just the result | A frozen trajectory pack, like the drift pack - a new pack starts a new reference |
| **Agents on other providers** | Gemini, local models through the same harness | Per-application credential, no shared keys, `AIUsage` parsing that fails closed |

**Never agentic, by design:** publishing a version, approving a plan, changing a
grant or a role, deleting anything, lifting a restriction, masking, citation and quote
verification, writing a secret. These are decisions or checks, and each is the
reason a person can trust the output.

## Risks

| Risk | Mitigation |
|---|---|
| Cost grows with turns | Worst-case reservation up front; turn and tool-call caps; budget stops a run cleanly; every phase measured against one-shot cost |
| Results vary run to run | Frozen packs per phase, `llm_drift` after any tool or prompt change, default off until measured |
| A tool leaks across applications | `ToolScope` closed over; row security under it on PostgreSQL; guardrail probes with foreign ids |
| A tool reads a restricted source | Tools read only through `answerable()` and `permissions.permitted`; a probe for each |
| Prompt injection from a source or repository file | Tools are read-only and writes are fixed by an approved plan, so injected text can at worst make the agent read more or answer worse - which the packs catch. Never add a write tool |
| Slower runs | Trajectory durations on the run page; `slo` objectives for agent runs; Stop between turns |
| A dead worker strands a run | Lane steps keep their `keepalive`; a lost claim writes nothing |

## Open decisions

1. Phase 1's cost bound - is "no more than twice one-shot" the right bar?
2. Whether the trajectory log is shown to viewers or to owners and contributors only.
3. Whether Phase 2's agent half is worth building if the rule half gets close to the
   answer key.
