# Agentic AI - what the first measurements say, and what to build next

Follows `agentic-ai-plan.md`, whose phases 0-9, the Messages runtime, model routing and
prompt caching are all built. This plan starts from the first paid pack runs, recorded on
CarePathDev on 2026-11-23 (`benchmarks/<pack>/2026-11-23T*.json`, claude-sonnet-5-5, the
Claude CLI on the host login), and turns what they show into work.

Estimates are engineering days for one developer who knows the codebase, including tests
and the five gates in `CLAUDE.md`. "Paid" means a step needs your go-ahead to spend.

## Where every agent stands

Verdicts are each pack's own `verdict` over its newest recorded file.

| Agent | One call | With tools | Cost × | Verdict |
|---|---|---|---|---|
| Chat (`chat_pack`, 16 questions) | 16 cited expected, 10 first | 16 cited, **11 first**, 9 tool calls | 1.25 | Clears the bar - by one question |
| Chat routed (`--routed`) | - | 15 cited, 10 first, **21** tool calls | 1.33 | Fails: not cheaper, one citation lost |
| Code Factory (`factory_pack`, 4 tickets) | 2 fixed, 60% checks | **4 fixed, 100% checks** | **3.58** | Fails on cost alone |
| Code review (`review_pack`, 4 changes) | 2 found, 7 noise | 2 found, **3 noise** | 2.19 | Fails on cost alone |
| Code documentation (`docs_pack`) | 13 paragraphs | 12 paragraphs, **0 tool calls** | 0.93 | "Fails" - but tools were never used |
| Document review (`document_review_pack`, 5 items) | 2-3 found, 0 linked | **4 found, 3 linked** | 1.96 | **Clears the bar** (two runs of two that compared) |
| Gap analysis (`analysis_pack`, 6 tickets) | 22/25 cited | 22/25 cited, 0 found more | 1.23 | No better: leave off |
| Triage (`triage_pack`) | - | - | - | **Never run**: no confirmed causes in the application |
| Knowledge upkeep (`learning_upkeep`) | - | - | - | Measured by use; too few decided |
| `llm_drift` | 100% valid extraction, 94.1% accepted, 100% answers cite expected | | | First reference on sonnet-5-5 |

## What the runs reveal

These are the findings the plan is built on. Each names its evidence.

1. **Turn counts are wrong on the CLI path.** Every pack reports `turns: 1` for a run with
   tools (`factory_pack` CARE-431: 7 tool calls, "1 turn"). The CLI reports usage once per
   run (`claude_runtime.run` returns one `claude_turn`), and callers count turns as
   `len(result.usages)` (`code_review.agent_review`, the packs, `AgentResult.usages`). The
   receipt is right - one summed usage - but `digitalbrain.turns`, the pack figures and the
   agent pages under-report. The CLI's `ResultMessage` carries `num_turns`; nothing reads it.
2. **Code Factory's cost is mostly repeated reading.** CARE-431 with tools sent 174,970
   prompt tokens against 18,570 one call, and read `routes_patients.py` three times - a
   target already shown whole in the prompt. CARE-412 sent 84,052. Every later turn
   re-sends every earlier result, so a repeated read costs once per remaining turn. Nothing
   in `code_tools.read_file` knows what the run has already been shown, and the CLI path has
   no prompt caching (`claude_prompt_caching` applies to the Messages runtime only).
3. **The cost bar judges a run, not a result.** One call left CARE-412 and CARE-431
   unanswered. Per ticket fixed, one call cost $0.091 and tools $0.162 - **1.8×**, inside
   the 2× bar the per-run figure (3.58×) fails. This is open decision 1 of the first plan.
4. **A pack whose tools were never called measured nothing about tools.** `docs_pack`'s
   tools way made 0 calls and 1 turn: it was one call twice, and its "do not clear the bar"
   (12 paragraphs against 13) is noise. `nothing_compared` catches failed and empty runs but
   not this. The uncommitted change to `code_docs.judge` (quotes naming evidence by number)
   is what took the pack from 0 kept paragraphs to 13; it is not committed yet.
5. **Gap analysis has almost no headroom on this pack.** Planted-evidence recall is already
   100% by rule, both ways cited 22 of 25 expected identifiers, and tools found nothing more
   for 23% more cost. `analysis_pack` also records no tool-call count, so it cannot say how
   much the agent searched.
6. **Code review's tools cut noise, not misses.** Found 2 of 4 both ways; noise 7 → 3; 0 new
   windows read (its tool calls were searches and `read_diff`). Four changes cannot separate
   a real gain from luck, and its verdict reads cost only - noise is a figure the bar
   ignores unless it rises.
7. **Routing made the small model search more.** 21 tool calls against 9, cost up, one
   expected citation lost. The searching model gets the same `tool_budget` as chat's own;
   a cheaper model that searches twice as much is not cheaper.
8. **Chat's "clears" rests on one question** (cited first 11 against 10). The first plan's
   rule - three runs on one model, two of three clear - has not been applied to any pack.
9. **Triage cannot be measured on the demo.** `triage_pack` needs incidents whose cause a
   person confirmed; a fresh demo has none, and seeding verdicts would make the platform's
   own data look like a person's judgement.
10. **The Messages runtime and prompt caching have never run live.** CarePathDev answers
    through the host login, which only the CLI accepts. The two biggest cost levers built -
    per-turn receipts and the cache - are unmeasured.

## Phase A - Measure honestly (free, 3-4 d) - built

**What shipped:** `AIUsage.turns` and `TurnUsage.turns` (migration 0117) carry the CLI's
`num_turns`, and every turn count reads `usage.turns_of` or `Sum("turns")`;
`agent_measurements.tools_unused` makes a tools way with no tool call `NOTHING_COMPARED`
(`TOOLS_UNUSED`), in every pack's verdict and `--record`; gap analysis and document review
count tool calls; `carried_by`/`stamp` write the runtime into every record and
`AgentMeasurement.runtime`, a new reference on the trend when it changes;
`measure_agents --run --repeat N` with `combine` (most runs must clear).

**Found on the way:** packs that total a case's receipts by time counted a receipt from
the case before when both fell in one clock tick - a test's verdict flipped one run in
three. `case_receipts` counts each receipt once. Two records in one minute wrote one file;
the second is now `-2`.

**Re-judged:** `docs_pack`'s recorded run now reads "nothing compared" (it made no tool
call); the other recorded verdicts are unchanged. Its `AgentMeasurement` row, kept before
the rule, still says "Does not clear" until it is forgotten (`agent_measurements --forget`)
or the pack is run again.

**Not done here:** committing the `code_docs.judge` numbering fix (it is in the working
tree with its tests) and the paid re-runs, which wait on Phase B.

The original plan for this phase follows.

Before any switch is decided, the figures it is decided on have to be right.

- **Turns from the run, not the receipts.** `claude_runtime` reads `ResultMessage.num_turns`
  and returns it beside the usages (a `TurnCount`, or `AgentResult.turns_sent`); the
  Messages and OpenAI runtimes already know theirs. `harness.measured`, `AgentResult`, the
  packs and `code_review`/`code_docs`/`document_ai` receipts use it. Receipts are unchanged:
  still one per provider request we can key. Test: a scripted CLI result with `num_turns=5`
  reports 5 everywhere.
- **"Tools never called" is not a comparison.** Extend `agent_measurements.nothing_compared`:
  when the tools way made no tool call in any case, the verdict is `NOTHING_COMPARED` with
  that reason and `--record` keeps nothing. Every pack's `verdict` already goes through it.
- **Every pack records tool calls and the runtime.** Add `tool_calls` to `analysis_pack`;
  write `runtime` (`cli`/`messages`/`openai`) and `prompt_caching` into every pack file's
  header and `AgentMeasurement`, and draw a new-reference break on a runtime change as the
  page already does for a model or pack digest.
- **Repeat runs.** `measure_agents --run --repeat 3` runs each ready pack three times and
  reports "clears in N of 3"; a pack is called clear only at 2 of 3. One shared helper
  (`pack_verdicts.combine`) rather than eight copies.
- **Commit the `code_docs.judge` numbering fix** with its tests, then re-run `docs_pack`.

Done when: the gates pass, the agent pages show real turn counts, and every pack file says
how it was run.

## Phase B - Spend less per run (2-4 d build, then paid measurement)

**Built, not yet measured:** a code tool's exact repeat is answered with a note
(`ToolSpec.once`), `read_file` points at a target handed over whole (`SHOWN_WHOLE`) and at
lines already returned (`ALREADY_READ`), each a `repeat` step (migration 0118) - the tool is
still run first, so every repeat is authorized and budgeted like any call. Code Factory's
`TOOL_GUIDANCE` says the numbered files are in front of the model and to read one function by
`find_symbol` and a line range. A scorecard probe holds it (34 agent probes, 100%).

**Left as it was, on purpose:** the default window stays `MAX_LINES` (400) - the guard
removes the re-reads that drove the cost, and a smaller window is a second change to what the
model reads, to measure on its own. Chat's and triage's tools do not take the repeat rule.

**Waiting on your go-ahead:** `factory_pack --record` three times (about $2 at the last
run's figures; `measure_agents --run --repeat 3` would run every ready pack), then the Messages runtime and
prompt caching on an application with an API-key credential. The cost bar (decision 1) is
still open.

The original plan for this phase follows.

Addresses finding 2 directly. None of it changes what may be written or cited.

- **A run remembers what it was shown** (`code_tools`, on `CitationRecorder` or the scope's
  run state). `read_file` of a file the prompt already held whole answers
  "`<path>` is shown in full above as file N" - a few tokens, not the file again. A window
  already returned in this run answers "already read above (lines a-b)". The answer still
  counts as a step (`outcome="repeat"`), so trajectories show it. The same guard on
  `fetch_source` for a window already fetched.
- **Smaller default windows.** `read_file` with no range returns `MAX_LINES` (400). Measure
  200 with a "read again from" tail; most reads in the factory pack were to find one
  function, which `find_symbol` then `read_file` with a range does better. `TOOL_GUIDANCE`
  says so.
- **Measure the Messages runtime with caching.** On an application with an API-key
  credential (not the host login): `chat_pack` and `factory_pack` with
  `claude_messages_agents` off, then on, then with `claude_prompt_caching` on. Paid: about
  the cost of three chat packs and three factory packs at today's figures (~$3).
- **Decide the cost bar** (decision 1 below). If it becomes cost per success, it is one
  `Figure` in each pack's `verdict`, the per-run cap kept as a ceiling (proposed 4×).

Done when: CARE-431 with tools sends under half its 174,970 prompt tokens, and the factory
pack is re-run three times.

## Phase C - Decide the switches (operator, paid re-runs) - decided

Judged only on runs that are **clean** - billed by their receipts, each case by its own
receipts, started cold (no prompt cache warm from a run inside the hour) - on CarePathDev,
claude-sonnet-5-5 through the CLI on the host login. Runs that were not are left out and
named.

| Agent | Clean runs | Result | Decision |
|---|---|---|---|
| **Code Factory** | `2026-11-24T1057`, `T1204` | Both clear: fixed 3/4 against 2/4, checks met 100% against 60%, at 1.13x and 1.15x the cost | **Clears** - two clean runs of two, a majority of three whatever a third says. An operator may write `code_factory_agent_tools = true` |
| **Document review** | `2026-11-23T0532`, `T1647` | Both clear: 4/5 found and 3/5 linked against 2-3 found and none linked, at about 2x | **Clears**. An operator may write `document_review_agent_tools = true`. Its cost sits at the 2x bar: re-check it when the bar is decided |
| Code review | `2026-11-24T1205` | Found 2/4 both ways; noise 3 against 6, at 1.43x | **Leave off.** No more found; less noise is not yet something the verdict credits - Phase E |
| Code documentation | `2026-11-24T1206` | With the new guidance the agent read (5 reads) but kept 11 paragraphs against 14, at 1.39x | **Leave off.** It now uses its tools and still describes less |
| Gap analysis | `2026-11-23T1644` | 22/25 cited both ways, nothing more found | **Leave off** |
| Chat | `2026-11-23T1710` | Clears by one question; chat always answers with tools | **No switch.** Re-measure with the next `chat_pack` |
| Model routing | `2026-11-23T1710 --routed` | More searches (21 against 9), one citation fewer, not cheaper | **Stays seeded off.** The searching model's tool budget was not changed |
| Triage | none | No confirmed causes | Phase D |

**Left out:** `2026-11-24T0331` (priced by tokens, before the bill fix - one call read at
$0.18 against a $0.32 bill), `T1058` (inside the hour of `T1057`, so one call read its
prompt from the cache at a tenth, and three of the person's own Code Factory calls were
counted before cases were billed by id), and the 24 November rounds of document review and
chat, which the provider refused with HTTP 429 - the host login's usage limit - and which
compared nothing and recorded nothing. Rounds 2 and 3 never ran: the background job ended
in its hour's pause.

**Found on the way:** CARE-431 with tools ran out of its 8 turns in both clean factory runs
(`TurnLimit`; its turns now have a receipt). The one run where it did not (`T1058`) fixed
4/4. A turn kept back to answer in, as chat's `wrap_up` keeps one, is the next thing to try
for Code Factory - measured with `factory_pack`, since it changes what the model is asked.

**Nothing was switched.** No production configuration names these switches today, the
hosted demo already turns all six on, and a measurement switches nothing on its own.

The original plan for this phase follows.

| Agent | Proposed action | After |
|---|---|---|
| Document review | One more run; at 3 of 3 (or 2 of 3) write `document_review_agent_tools = true` in production | Phase A's repeat |
| Code Factory | Switch on in production if it clears after Phase B (or on the cost-per-success bar) | Phase B |
| Code review | Grow the pack (Phase E) and add noise to its bar; re-run | Phase E |
| Code documentation | Add guidance naming when to read (a component's files beyond the opening lines); re-run. If tools are still unused, leave off and say why on its page | Phase A |
| Gap analysis | Leave off in production. Revisit only with a pack whose answer key the rules miss | - |
| Chat | Chat already answers with tools; three runs confirm it. No switch | Phase A |
| Model routing | Give the searching model the tool budget chat's own run *used* on the same question class, not the ceiling; re-run once. If still not cheaper, keep it seeded off and say on its page that it measured more expensive | Phase A |

Every switch stays an operator's line in the configuration. A measurement switches nothing.

## Phase D - Make triage measurable (3-4 d, then paid)

`triage_pack` scores against causes people confirmed, which a demo does not have. Two ways:

- **Recommended: a labelled pack like the others.** `benchmarks/triage-pack.json` names
  CarePath incidents (from `documentation/demo/`) and, for each, the record that is its
  cause - a change, a known error, a runbook passage - with the label tested against the
  imported records so it cannot name something that is not there. `triage_pack --labelled`
  scores against the label instead of a verdict. No verdict and no `confirmed` edge is
  written, so "only a person's verdict makes a confirmed edge" is untouched.
- Rejected: seeding verdicts into the demo. It would make the platform's own data read as
  a person's judgement in the audit trail and on the incident page.

Done when: triage has a recorded measurement and a verdict on the demo.

## Phase E - Grow the packs (4-6 d, labels by a person)

Four tickets or four changes cannot tell a real gain from one lucky answer (finding 8).

- `factory-pack.json` 4 → 10 tickets, `review-pack.json` 4 → 10 changes,
  `document-review-pack.json` 5 → 10, each label tested against CarePath as today.
- A new pack digest is a new reference, which the agent pages already draw.
- Review's verdict gains noise as a figure that must not rise *and* a "fewer noise" credit,
  so an agent that finds the same and says less can clear at the cost bar.

## Phase F - Show and score the trajectory (free, 3-4 d)

`AgentStep` rows exist for every agent; nothing reads them per run.

- **Steps on the run's own page**: Code Factory under each phase, a review's AI card, a
  triage run, a drafted fix - tool, shape, outcome, size, duration. Never what was asked or
  read; those are never kept. Who sees it is decision 3.
- **Trajectory figures from rows already kept**, free: repeated reads (same tool, same
  shape in one run - the Phase B guard's `repeat` outcome), calls that found nothing, calls
  past the budget. Shown in the agent page's activity and as a `harness.measured` counter by
  outcome, so finding 2 is watched in production rather than discovered in a pack.

## Phase G - Next capabilities (after A-F; each its own decision)

The first plan's "Future enhancements" stand. In the order the measurements argue for:

1. **Run tests as a tool, development and the demo only.** Code Factory's tools way fixed
   4 of 4 by rule labels; CarePath's own suite is the proof the first plan named. A
   `run_tests(paths)` tool on the sandbox lane, refused in production by `load_config`,
   behind `code_factory_local_tests`, an attempt cap per run, and the same environment
   trimming `run_local_tests` uses. It breaks "never runs the code it writes" a third time,
   so it needs its own decision.
2. **Propose tools**: an agent creates a *pending* record (a plan-item note, a runbook edit,
   a benchmark question) - the learning-loop pattern, accepted by a person.
3. **Scheduled agents** (stale runbooks, knowledge hygiene), as the owner who switched them
   on, stopped by revocation, budgeted like automatic AI.
4. `graph_walk` for chat stays deferred until a benchmark and drift run say choice helps.

## Clean-up in the agent layer (2 d, any time)

- `run_agent` takes seven link ids (`message_id`, `run_id`, `triage_id`, `review_id`,
  `candidate_id`, `docs_id`, `entry_id`) and `keep_steps` mirrors them; each new agent adds a
  kwarg, a column and a branch. Replace the kwargs with one `links` mapping validated
  against `AgentStep`'s link fields. Columns stay - no migration.
- The eight packs each carry their own `COST_BAR`, cost check and `nothing_compared` call.
  Move the shared bar into the helper Phase A adds; each pack keeps only its own figures.

## Order and effort

| Phase | Effort | Paid | Depends on |
|---|---|---|---|
| A Measure honestly | 3-4 d | No | - |
| B Spend less per run | 2-4 d | Yes (~$3) | A |
| C Decide the switches | 1 d + re-runs | Yes | A, B |
| D Triage labelled pack | 3-4 d | Yes | A |
| E Grow the packs | 4-6 d | Yes | A |
| F Trajectory | 3-4 d | No | B's `repeat` outcome |
| Clean-up | 2 d | No | - |
| G Next capabilities | per item | Yes | A-F |

A, B and C are **about 6-9 days** and decide every switch that can be decided today.

## Decisions needed

1. **The cost bar.** Keep "at most 2× per run", or judge cost per success with a per-run
   ceiling (proposed: ≤ 2× per success, ≤ 4× per run)? Code Factory clears the second and
   fails the first today.
2. **Document review in production.** Switch it on after one more run clears, or wait for
   the 10-item pack?
3. **Who sees a run's trajectory**: viewers, or owners and contributors only?
4. **Triage**: build the labelled pack (recommended), or wait for confirmed causes from use?
5. **An API-key application for measuring** the Messages runtime and caching - which
   application, and its budget.
6. **Running tests as an agent tool** in development and the demo - worth the third
   exception to "never runs the code it writes"?
