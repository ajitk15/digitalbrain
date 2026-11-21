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

`CLAUDE.md` used to say "Code Factory's agents still need the CLI's tools". The code did
not: they run with `max_turns=1` and no tools. Phase 0 corrected that line.

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
| 0 Agent harness | Shared tool registry, per-turn reservation, trajectory log, guardrail probes | 5-7 d | None | Scorecard "Agent guardrail probes" 100% | **Done** - chat runs on it |
| 1 Code Factory repository tools | Implementation, tests and review read the pinned snapshot themselves | 8-10 d | Yes, more turns per run | New `factory-pack.json` (paid) + planted-bug key | **Built, off** - waiting on a paid `factory_pack` run |
| 2 Evidence follow-through | Follow references from the evidence (F-03 via C-08): rules first, agent only if rules stall | 3-4 d rules, +4-5 d agent | None (rules), Yes (agent) | Scorecard `seeded_evidence_recall` | **Done by rule** - 92% -> 100%; agent not needed |
| 3 ServiceOps triage agent | Triage asks for neighbourhood, changes, runbooks one hypothesis at a time | 6-8 d | Yes | `triage_pack` against confirmed verdicts (paid) | **Built, off** - waiting on a paid `triage_pack` run |
| 4 Code review agent | AI review opens dependent files, tests and requirements as needed | 4-5 d | Yes | `review_pack` (paid, new) | **Built, off** - waiting on a paid `review_pack` run |
| 5 Chat deepening | Graph-walk and version-aware tools, turns sized by budget | 3-4 d | Yes, bounded | Benchmark + drift pack | On where it is measured equal or better |
| 6 Knowledge upkeep agent | Drafts a fix for a "gap" candidate as a `LearningCandidate` | 4-5 d | Yes, on a button | Accept rate in Learned | Off |

Phases 0-2 (rules) are **about 16-21 days** and make the largest difference on the
platform's main pipeline. With 3-6, **about 37-48 days**.

Order: 0 must come first (every later phase uses it). 1 before 4 (review reuses the
repository tools). 2's rule half is free and measured by the scorecard, so it can run
alongside 0.

## Phase 0 - Agent harness (done)

**What shipped:** `agent_runtime/registry.py`, `agent_runtime/harness.py`,
`agent_runtime/scripted.py`, `gateway.turn_costs` and per-turn admission, `AgentStep`
(migration 0107), the `gen_ai.tool.name` and `digitalbrain.turns` span attributes, the
scorecard's "Agent guardrail probes passing" (11 probes, 100%, in the baseline) and
`tests/test_agent_harness.py` (34 tests). Chat's answer is `run_agent`; its tools are shown
to both providers exactly as before (pinned by test), and its citations are verified on
the same path, so the benchmark is unchanged.

**What differs from the plan below, and why:**

- **The trajectory is not on a page yet.** The only agent is chat, which already shows each
  tool call live; the run page it was meant for is Code Factory's, which gets tools in
  Phase 1. `AgentStep` rows are written now so Phase 1 starts with history.
- **There is no clean stop at the turn limit.** Neither SDK can forbid tools on a run's
  last turn, so a model that ignores "answer with the evidence you have" still fails as the
  SDK fails it, as before. What the harness does instead: a run the budget pays one turn
  for is given no tools at all, and fewer turns means a proportionally smaller tool budget,
  so the model is told to answer while a turn is left.
- **Receipts are not linked to steps.** The Claude CLI reports usage once per run, not per
  tool call, so a step carries no receipt id; receipts stay one per turn.
- **A run's Stop is honoured before each tool**, not only between turns - the same thing for
  the SDKs, which run tools between turns.

The original plan for this phase follows.

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

## Phase 1 - Code Factory repository tools (built, switched off until measured)

**What shipped:** `agent_runtime/code_tools.py` (six tools: `list_files`, `read_file`,
`search_code`, `find_symbol`, `callers`, `tests_for`), registered for `plan_drafting`
only; `code_factory_agents.consult`, which sends the implementation, test and review
agents through `run_agent` when `code_factory_agent_tools = true` and the run has a
pinned snapshot, and through the one-shot `ask` otherwise; the harness and both runtimes
taking an agent's own instructions, timeout and tool-call limit; `AgentStep.run` and
`.phase` (migration 0108) and a note on the run per read; `benchmarks/factory-pack.json`
(four CarePath tickets, hand-labelled) and `manage.py factory_pack`; four more scorecard
probes (15 in all, 100%); `tests/test_code_tools.py` and `tests/test_factory_pack.py`.

**Not done yet, and why:** the paid pack has not been run - it needs an application with
the CarePath code indexed, a Code Factory model and its credential, and your go-ahead to
spend (four tickets, each sent both ways, up to 8 turns each with tools). Until it says tools clear the bar,
the switch stays off and nothing a run does has changed.

**What differs from the plan below:**

- **One snapshot for every run, GitHub or local.** A GitHub run reads its targets from the
  branch but its tools from the pinned snapshot, as `reference_files` already did; the
  tool descriptions say "as indexed".
- **Scoring is by rules, not by running CarePath's tests.** `expect`/`forbid` text inside
  a named symbol is a proxy, tested against the code so no label describes a bug that is
  not there. Running the suite per answer would execute model-written code, which needs
  the local-tests gates; it is the obvious next measure.
- **Code Factory's turns and tool calls are 8 and 16** (chat's are 4 and 8), each reserved
  before the first is sent.

The original plan for this phase follows.

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

## Phase 2 - Evidence follow-through (done, by rule)

**What shipped:** `graph_ai.evidence_references` and `follow=` on `citations_from` and
`graph_citations`; `code_factory.evidence_for` asks for `MAX_FOLLOWED` (1). After the
ticket's own cross-references, each reference the cited evidence names and the ticket does
not is followed on its own, in the order the evidence names it, to the best passage naming
it that is not already in front of the model. Another record's references are not followed.
The scorecard measures it as a run asks, and holds rows and memory identical with it on.

| Planted-bug evidence, 6 tickets | Recall | Passages added | Added with no planted identifier |
|---|---|---|---|
| Before (no follow) | 92% (23/25) | 0 | 0 |
| All evidence references as one query, 4 | 96% | 24 | 9 |
| One at a time, 1 | 92% | 6 | 2 |
| One at a time, skipping passages already read, **1 (shipped)** | **100% (25/25)** | 6 | 2 |
| Same, 2 | 100% | 12 | 5 |

The two noisy passages both follow `SEV-2`, a severity label. How many sources name a
reference does not tell a label from a reference (SEV-2 and T-04, which leads to R-02, are
each named by four), so no stoplist was added: one tuned to this corpus would be a rule
that only works here. `seeded_evidence_recall` in the baseline is now 100.

**The agent half was not built.** The plan said to build it only if rules stall; they
reached every planted identifier. It stays the option for a corpus where they do not -
measured with the factory pack, never by the scorecard, since it is paid.

The original plan for this phase follows.

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

## Phase 3 - ServiceOps triage agent (built, switched off until measured)

**What shipped:** `agent_runtime/ops_tools.py` - `incident_context` and
`search_published_knowledge` - with `search_knowledge` and `fetch_source` serving triage
too, each call held to triage's purpose; `serviceops_triage.ask_model`, which sends the
question through `run_agent` with those tools when `serviceops_triage_agent_tools = true`
and through the one-shot call otherwise; tool results `redact`ed before any model reads
them; what the agent read joining the pack (`agent_read`), so it can be cited and is
re-verified with the brief; `AgentStep.triage` (migration 0109); `manage.py triage_pack`;
four more scorecard probes (19 in all, 100%); `tests/test_ops_tools.py`.

**What differs from the plan below, and why:**

- **The tools are the readers triage already trusts, not new ones.** `incident_context`
  is `evidence_pack` on another incident, so "similar", "same event", changes, runbooks
  and automations mean exactly what they mean on the incident page. There is no separate
  `recent_changes` or `automation_for`: both are in that neighbourhood.
- **An incident's own confirmed cause is withheld from it** - from the tool always, and
  from the pack when measuring - or the measure would hand the model its answer.
- **The scorer is unchanged.** A hypothesis resting on one source the agent found, with
  nothing from the pack beside it, is withheld as too weak, as any single uncorroborated
  source is. If the pack shows that costs the agent real causes, the scorer is the next
  thing to measure - not something to loosen in passing.
- **Measured against confirmed verdicts, not `idea_confidence`**: whether a kept
  hypothesis cites the cause people confirmed, and at what rank. No paid run has been
  made; it needs an application with confirmed causes and your go-ahead.

The original plan for this phase follows.

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

## Phase 4 - Code review agent (built, switched off until measured)

**What shipped:** `code_review.review_answer` and `agent_review`, which send "Ask AI to
review" through `run_agent` when `code_review_agent_tools = true`; Phase 1's code tools
serving `code_review` over the review's base snapshot, with `read_file` handing back a
quotable `code:` window for a review only; `read_diff`; `search_published_knowledge`
serving reviews; what was read joining the material `judge` checks; `AgentStep.review`
(migration 0110); `benchmarks/review-pack.json` (four labelled CarePath changes) and
`manage.py review_pack`; three more scorecard probes (22 in all, 100%);
`tests/test_review_tools.py`.

**What differs from the plan below:**

- **The cache key is the material plus the way it was asked**, not "everything the agent
  read": the agent's reads are not known until it has been paid for, and an answer to the
  same material by the same way is the answer to keep.
- **The snapshot is the code before the change.** The tools say so, and the whole change is
  `read_diff`; reading a changed file's new text is the diff.
- **Noise is scored, not only recall.** A finding that quotes none of the labelled files is
  counted against the agent, so it cannot clear the bar by saying more.

No paid run has been made; it needs an application with the CarePath code indexed, a Code
review model and its credential, and your go-ahead.

The original plan for this phase follows.

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
