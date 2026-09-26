# ServiceOps on an operations graph — plan

Draft for review • 26 September 2026 • Plan only; nothing here is built yet

## 1. Why

ServiceOps triage should work the way responders think: start at the incident, look at the thing it is
on, what changed there, what broke there before and how it was fixed, what the runbooks say about it,
and what people concluded last time. That is a walk through a graph.

Today it is not built that way. The knowledge graph holds each imported ticket as an isolated
*document* with its header lines as loose text nodes: `CI: carepath-api-green` appears once per
incident, as separate text, so nothing in the graph joins `INC0010009` to `CHG0030001` or to
`INC0010002`. Triage works around the graph with four separate queries (precedents, changes, related
incidents, graph passages) and uses the graph only for text search over documents, and only when a
version is published.

The goal: **one operations graph per application that holds incidents, changes and the things that
connect them, links in the documents that explain them, is kept current as records arrive, learns from
people's verdicts — and that triage walks to gather its evidence.**

## 2. What stays true

These are existing invariants (CLAUDE.md) and this plan keeps every one of them. Section 9 lists the
one deliberate change.

| Invariant | How the plan keeps it |
|---|---|
| Application isolation, deny by default | One graph per application. Every node, edge and read is filtered by application; a foreign id reads as not found. No cross-application edge can exist because the builder only ever reads one application's records. |
| Citations verified against live sources | Every node points at the record it came from, every edge at the records that justify it. Whatever reads an edge re-verifies those records (active, digest unchanged, quoted text present) before it becomes evidence. |
| The model never writes facts | The graph is built by rules only. The AI proposes hypotheses from evidence the graph supplied; nothing it says is written back. The only non-rule edges come from a person's verdict. |
| Usage is never silently zero | The build makes no model call. Optional AI extras (section 8) record usage like every other call. |
| Documents are publish-gated | Unchanged: runbook and design passages still come only from the published knowledge graph. |

## 3. Two layers

| | Operations layer | Documents layer |
|---|---|---|
| Holds | Incidents, changes, services, components, symptoms, assignment groups | Runbooks, ADRs, design documents |
| Built from | Imported ServiceNow / Jira records, hand-added incidents | Uploaded documents, folders, links |
| Built by | Fixed rules, no AI | Existing graph generation (structural + optional verified AI enrichment) |
| Freshness | **Live**: rebuilt whenever its records change | **Publish-gated**: a person publishes a version |
| Joined by | Name matches between a passage and a component or service (section 4.3) | |

## 4. The graph

### 4.1 Nodes

| Kind | One per | Key | Carries |
|---|---|---|---|
| Incident | imported or hand-added incident record | `incident:<entry id>` | number, title, state, priority, opened, resolved, close code, open/closed |
| Change | imported change request | `change:<entry id>` | number, title, state, type, start, end |
| Component (CI) | distinct CI name in the application | `component:<name, case-folded>` | name |
| Service | distinct service name | `service:<name>` | name |
| Symptom | fault concept found in a record (timeout, auth failure, db lock…) | `symptom:<concept>` | concept |
| Assignment group | distinct group name | `group:<name>` | name |
| Passage | published knowledge-graph passage that names a component or service | `passage:<entry id>:<line>` | title, excerpt, graph version |

Incident, change and passage nodes point at their source record; shared nodes (component, service,
symptom, group) exist because records name them and disappear when none does.

### 4.2 Edges

Each edge has a relation, a weight where one makes sense, a `detail` explaining it, and the records
that justify it.

| Relation | From → to | Rule | Weight / detail |
|---|---|---|---|
| on | incident, change → component | the record's `CI:` line | — |
| in | incident, change → service | the record's `Service:` line | — |
| assigned | incident → group | the record's `Assignment group:` line | — |
| has symptom | incident → symptom | a concept matched in title or description | — |
| before | change → incident | same component, change started 0–48 h before the incident opened | hours before |
| similar | incident → resolved incident | `score_pair` > 0: same CI (+4), same service (+3), same environment (+1), same signature (+4), weighted symptom similarity (up to +8); top 8 per incident | score, reasons, differences, similarity |
| same event | open incident ↔ open incident | same service (when both have one) and identical fingerprint or similarity ≥ 0.35 | similarity, shared terms |
| mentions | passage → component, service | the passage text names the component or service exactly | quote |
| confirmed cause | incident → change, incident or passage | a person's verdict named it as the actual cause (section 6) | who, when, note |

"Similar", "same event" and "before" are the rules triage already uses (precedent scoring, related
incidents, change timing), moved into one place so the graph, the page, triage and the replay command
share a single definition.

### 4.3 Documents joined in

The published knowledge graph already holds verified passages. A deterministic pass looks for exact
component and service names in those passages and adds `mentions` edges, so incident → component →
runbook is a path. Only the latest *published* revision is used, re-verified at read time exactly as
graph citations are today. AI entity extraction (existing `graph_ai`, verified quotes) can add more
`mentions` edges later without changing anything else.

### 4.4 Storage

Two tables, `OpsNode` and `OpsEdge`, plus `OperationsGraph` (one row per application: fingerprint,
built at, counts). Tables rather than one JSON blob because triage reads a neighbourhood, not the
whole graph, and edges need unique constraints and indexes. Nodes cascade with their source record, so
retiring a record removes it from the graph.

## 5. Keeping it current

| Trigger | Action |
|---|---|
| Connector import finishes | rebuild |
| Incident added by hand | rebuild |
| Verdict names a cause | add one `confirmed cause` edge (no rebuild) |
| Knowledge graph version published | rebuild the passage links |
| Page or triage reads the graph | if the fingerprint (ids + digests of every incident and change) differs from the stored one, rebuild first |
| Demo reset | clear the application's operations graph |
| `serviceops_rebuild` command | rebuild |

**Full rebuild, not incremental, to start.** It replaces every machine-built node and edge in one
transaction and keeps person-made edges. Cost is dominated by pair scoring: at the current cap of 500
incidents that is ~125k comparisons of small term sets, estimated at 1–2 seconds. Incremental updates
are a later optimisation if an application outgrows the cap; the fingerprint check means a stale graph
is never read either way.

## 6. The learning loop

Today a verdict records Useful / Partly useful / Not right and a note. The `actual_cause` field exists
but nothing fills it.

1. When a responder marks an idea **Useful** or **Partly useful**, the form offers "What was the actual
   cause?" with the idea's cited records as choices (for example `CHG0030001`), plus "something else".
2. Choosing one adds a `confirmed cause` edge, with who and when, and records it in the audit log.
3. The next incident that shares the component or symptom finds that edge in its neighbourhood and it
   ranks first: *last time a timeout hit carepath-api-green, the confirmed cause was a deployment*.
4. Retracting a verdict removes the edge. Only people create these edges; the AI never does.
5. Confirmed causes are the outcome history that later lets the strength rating be calibrated and a
   High band introduced (a separate, later piece of work).

## 7. Triage on the graph

### 7.1 Walk

From the incident node, bounded and deterministic:

| Hop | Collects |
|---|---|
| 1 | its component, service, symptoms, group; changes `before` it; `similar` resolved incidents; `same event` open incidents; `confirmed cause` edges of similar incidents |
| 2 | passages that `mention` its component or service |

Caps per kind (5 changes, 5 precedents, 5 related, 5 passages, 3 confirmed causes) keep the evidence
pack within today's 40-item bound.

### 7.2 Evidence with paths

Each evidence item carries the path that found it, shown on the page and sent to the model:

- `INC0010009 → carepath-api-green → CHG0030001 (2.2 h before)`
- `INC0010009 → carepath-api-green → INC0010002 (similar: timeout, bulk, fhir)`
- `INC0010009 → carepath-api-green → Deployment runbook §4`
- `INC0010009 → timeout → confirmed cause of INC0010002: CHG0029990`

The prompt tells the model how each item is connected, which should improve hypotheses without giving
it anything it could not already cite.

### 7.3 Steps and scoring

The six steps stay; step 2 becomes **Walk the graph** and reports what it found per path. Scoring
keeps its seven parts and gains one, **confirmed history** (a similar incident's confirmed cause is
cited), weighted from the existing parts' share. The existing `Why …` table shows it like the others.

### 7.4 One definition

The incident page's Evidence section and triage's evidence pack both read `neighbourhood()`, so they
cannot disagree. The replay command keeps its point-in-time scoring (`as_of`) through the same
`score_pair`.

## 8. AI — where it could be added later

Not part of this plan's core; listed so the design leaves room.

| Option | Where | Trade-off |
|---|---|---|
| Embedding similarity | an extra signal in `score_pair` | needs an embedding source: OpenAI credential per application, or a local model (one-time download) |
| Claude re-ranking | after the walk, before step 3 | second paid call, +10–20 s per run |
| Support check | after citation verification | can only drop ideas; +1 call |
| Entity extraction from documents | more `mentions` edges | existing `graph_ai` path, verified quotes, publish-gated |

## 9. The one invariant that changes

CLAUDE.md says answers come from the **latest published** graph and never a draft. The operations layer
would be read **live**. Proposed wording for CLAUDE.md:

> The operations layer (incidents, changes and what connects them) is live, not publish-gated: it is
> rebuilt by rules from imported records whenever they change, and every edge is re-verified against
> its records when read. Documents stay publish-gated. Nothing a model says is written to either.

## 10. What people see

| Where | What |
|---|---|
| Incident page, Triage | evidence grouped as today, each item with its path |
| Incident page, new "Around this incident" | a map: the incident in the middle, its component, service and symptoms around it, changes, similar and related incidents and runbook passages outside, each joined to what it was reached through; click any node to open it |
| Run details | the walk: which paths produced which evidence |
| Verdict form | "What was the actual cause?" |
| Knowledge → Graph | an **Operations** layer next to documents in the existing explorer (graph.js), filterable by kind |
| How triage works | step 2 rewritten as "Walk the graph", plus the learning loop |

## 11. Phases

Each phase ships working and tested on its own.

| Phase | Delivers | Acceptance on CareOps | Estimate |
|---|---|---|---|
| **1. Build the graph** | models + migration, rule builder, triggers, demo reset, rebuild command, shared `score_pair` / `same_event` | 13 incident/change nodes; `INC0010009` on `carepath-api-green`; `CHG0030001` before it (2.2 h); similar edges to `INC0010002/3`; same-event edge to `INC0010010`; rebuild < 2 s | ½ day |
| **2. Triage walks it** | `neighbourhood()`, page and evidence pack switched to it, paths on evidence and in the prompt, step 2 renamed | same evidence as today plus paths; page and pack agree; run shows paths | ½ day |
| **3. See it** | incident neighbourhood map; Operations layer in Knowledge → Graph | map shows `INC0010009` → component → change / precedents / related | ½ day |
| **4. Learn** | actual-cause capture, confirmed edges, confirmed-history evidence and score part | confirming `CHG0030001` for `INC0010009` makes it the first evidence for a new similar incident | ½ day |
| **5. Documents** | `mentions` edges from the published graph, passages in the walk | after uploading `demo-artifacts/docs` to CareOps and publishing, the deployment runbook appears for `INC0010009` | ½ day |

Tests for every phase: isolation (foreign application never appears), verification at read (a retired
or edited record drops out), rebuild idempotence, person-made edges survive rebuild, demo reset clears.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Rebuild cost grows with history | cap (500, as today); fingerprint avoids needless rebuilds; incremental later |
| Name matching links the wrong component (e.g. a CI named "api") | exact, case-folded, whole-word match; minimum length; shown with its quote so a person can judge |
| Confirmed-cause edges encode a wrong human judgement | who and when on every edge; retractable; shown as "confirmed by …", never as fact |
| Live layer read while a rebuild runs | rebuild in one transaction; readers see the old or the new graph, never half |
| Graph and page drift | one `neighbourhood()` for both; tests pin it |

## 13. Decisions needed

1. **Live operations layer, publish-gated documents** (section 9). Recommended.
2. **Tables, full rebuild** (4.4, 5). Recommended; incremental only if needed.
3. **Documents joined by exact name match first**, AI extraction later (4.3). Recommended.
4. **Learning loop in scope now** (phase 4) or later.
5. **Where the whole graph is shown**: Knowledge → Graph as a layer (recommended, one explorer) or a new ServiceOps tab.
6. **Order**: 1 → 2 → 3 → 4 → 5 as above, or bring documents (5) forward for the demo.
