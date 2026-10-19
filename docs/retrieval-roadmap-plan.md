# Retrieval roadmap - implementation plan

Turns the retrieval roadmap (the former `docs/diagrams/index.html` section 07, now
in git history; current behaviour is `docs/index.html#components-retrieval-and-chat`) into
phases that can each ship on their own. Every phase is measured with the benchmark
(a version's score under Knowledge → Versions, `manage.py benchmark`) before and after, and none of them
touches the three invariants: the publish gate, citation checks against live sources,
and published-version-only answers.

Estimates are engineering days for one developer who knows the codebase, including
tests and the four verification gates in `CLAUDE.md`. They are not calendar time.

## Summary

| Phase | Roadmap items | Effort | New infrastructure | Model cost | Status |
|---|---|---|---|---|---|
| 0 Measure and rank | 1 Benchmark, 2 BM25 | 3-4 d | None | None | **Done** |
| 1 Ontology | 3 Typed graph | 2-3 d | None | None (same call) | **Done** |
| 2 Merge names | 4 Merged names, runbook aliases | 2-3 d | None | None | **Done** |
| 3 Multi-hop | 6 Traversal (2 hops, typed) | 2-3 d | None | None | **Done** |
| 4 Speed | Cached published graphs, Postgres full-text | 2-3 d | None (index migration) | None | **Done** - run on PostgreSQL 16 in CI since; stored columns in 0088 |
| 5 Embeddings | 5 Vector starting points | 6-8 d | OpenAI credential (pgvector later) | **Yes** | **Done** - not yet measured |
| 6 Spend and rerank | Spend caps, reranker | 3-5 d | None (reranker uses the app's model) | Yes (reranker, opt-in) | **Done** |
| 7 Enterprise | Labels, incremental updates, source permissions, shared streams | 20-29 d | None (database lease; Redis came later, optional) | Auto-refresh only, opt-in | **Done** - Jira permissions only; not run against live Jira |

Phases 0-4 need no new infrastructure and no new spend: **about 11-16 days**. With 5 and 6,
**about 20-29 days**. Phase 7 is a separate project of roughly another month.

Order is the roadmap's: measure first, then the free wins, then the paid one. Phase 1 must
precede 2 (merging is type-aware) and 3 (traversal follows typed relations).

## Phase 0 - Measure and rank (done)

- `benchmark.py`, `BenchmarkQuestion`/`BenchmarkRun` (migration 0067), Knowledge → Benchmark,
  `manage.py benchmark`. Every saved version is scored after commit, free.
- `ranking.py`: coverage first, BM25 second, then `spread` (each source's best citation
  before any source's second). Used by graph citations and `workbench.retrieve`.
- Measured on CarePathDev v4: 11/16 → 15/16 found, MRR 0.53 → 0.61.
- Follow-up fix (2026-10-09). The first version ranked three questions lower than the old
  word count (graph KAN-5 1→2, regulatory-requirements 2→4; keyword KAN-9 3→4). Causes and
  fixes: an identifier was one word (`max_page_size`), now its words; "requirement" and
  "require" were different terms, now one (`ranking.term`, outside the fingerprint stemmer);
  "against" counted as a subject word, now filler. Keyword search was 2.2× slower than HEAD
  because every passage was split and stemmed on every question; terms are now cached per
  source and per word.

  | Measured vs HEAD 18dee62, 16 questions | HEAD | BM25 first cut | Now |
  |---|---|---|---|
  | Graph: found / top 3 / MRR | 11 / 9 / 0.528 | 15 / 10 / 0.614 | **15 / 11 / 0.693** |
  | Keyword: found / top 3 / MRR | 8 / 6 / 0.257 | 14 / 11 / 0.614 | **14 / 12 / 0.674** |
  | Questions ranked worse than HEAD | - | 3 | **0** |
  | Graph ms per question (median) | 22.2 | 59.9 (6.8 cached) | **6.8** |
  | Keyword ms per question (median) | 10.4 | 22.9 | **4.4** (27.8 first search) |

## Phase 1 - Ontology (done)

`platform_core/ontology.py` is a closed registry, the same pattern as `services.FEATURES`:
eight types (Ticket, Requirement, Control, Component, Code, Runbook, Incident, Change)
and twelve relations, each with the free-text phrasings it absorbs ("uses", "relies on",
"depends on" → `depends on`).

- Extraction instructions list the allowed types and relations; the prompt and the
  validator read the same registry, so they cannot drift.
- A relationship whose relation is not in the registry, or whose types are not, is rejected
  and counted, exactly like an unverified quote. The quote check is unchanged and still
  comes first.
- Entity nodes carry `type`; edges carry the canonical relation. `ONTOLOGY_VERSION` is
  saved on the revision's data and quality report. It is deliberately not part of the
  fingerprint: that would make every application rebuild a new structural draft on
  deploy, and a requested AI run always saves a new version anyway.

Value: relations stop drifting; merging and traversal can trust types.

## Phase 2 - Merge names (done)

`ontology.merge_entities` runs after extraction. Two AI entity nodes merge only when they
have the **same type** and their names normalise to the same key, or are near-identical
word by word: the same number of words, each equal after stemming or a long word (6+
letters) misspelt (Jaro-Winkler ≥ 0.93), numbers exact. A Component never merges with a
Requirement, CARE-401 never with CARE-412, `carepath-api` never with `carepath-api-green`
(whole-string similarity was tried first and did exactly that), blue never with green.

- Every edge keeps its own `knowledge_id`, digest and quote, so verification is per edge
  exactly as before; the merged node lists its `aliases`.
- Graphify's `dedup` was evaluated and not used for this step: it only merges cross-file
  nodes typed `concept`, keys behaviour on `source_file` paths and prints to stderr. The
  rules above are the same idea in 40 lines we control. Its LLM tie-break stays off either way.
- ServiceOps runbook linking (`ops_graph.passages`) now matches a component name however it
  is punctuated ("CarePath API green" reaches `carepath-api-green`; `carepath-api` still does
  not), and also under any name the published knowledge graph merged with it.
  `RULES_VERSION` in the operations fingerprint rebuilds every operations graph under the
  new rule. Free and works on structural graphs too.
- Not done, deliberately: "green deployment" → `carepath-api-green`. That needs meaning, not
  spelling - phase 5.

## Phase 3 - Multi-hop (done)

`graph_ai.walk` goes breadth-first, up to `MAX_HOPS = 2`, from the endpoints of the five
best-ranked AI relationships, along other AI relationships.

- **Typed (AI) relationships only.** Structural edges are not walked: a value like
  "Status: Done" is shared by hundreds of records, and walking through it connects
  everything to everything. The existing one-hop structural expansion is unchanged.
- A hub with more than `MAX_FANOUT = 25` typed relationships is not walked through.
- Walked finds (at most `MAX_WALKED = 6`, hop 1 before hop 2) come after every direct
  match, carry `hops`, and say "Reached by: CARE-425 affects Referrals API -> ...". Every
  one is verified exactly as before, so a further hop can only add verified evidence.
- Corrected 2026-10-10 after the live test. The first version let walked finds take up to
  three places ahead of weaker direct matches. On the first AI-enriched CarePath versions
  that pushed three expected sources down and one out of the eight: v4 14/16, MRR 0.678.
  After the direct matches instead: v2 15/16 (0.693), v3 15/16 (0.692), v4 15/16 (0.691).
  The walk now fills only places a direct match left empty; the benchmark's single-document
  questions cannot show its gain, relationship questions ("which requirement does CARE-425
  break?") are where it adds a source.
- Measured: CarePathDev v4 (structural only) unchanged at 15/16 found, top-3 10/16,
  MRR 0.61 - no regression. The gain needs an AI-enriched version; the test fixture shows
  CARE-425 → Referrals API → referrals.py ← NFR-03 cited with no word shared with the
  question, and not cited with one hop.
- ServiceOps "change → component → runbook" needed no new walk: a change is linked to an
  incident only on the same CI, so its runbooks are already the incident's. A change on a
  component the incident's component *depends on* is the real third hop, and needs typed
  `depends on` edges in a published graph - a follow-up once AI-enriched graphs exist.

## Phase 4 - Speed

Done: `graph_ai.prepared` keeps a published revision's parsed data, relationship texts and
BM25 index in process (LRU of 8), keyed by revision id and creation time; a `post_save`
drops an entry. Safe because a revision's data never changes once saved. The benchmark
builds the same structure once per run instead of once per question. Measured on
CarePathDev v4 (2,974 relationships): **54 ms → 6.6 ms per question**, plus the ~60 ms read
and parse of the graph that is no longer repeated.

Keyword search also caches each source's passage terms: 22.9 ms → 4.4 ms per question.
Postgres full-text search is phase 4b below.

## Phase 5 - Embeddings (done: OpenAI `text-embedding-3-small`)

Decided: OpenAI embeddings through each application's own OpenAI credential. Built as
**starting points only** - a vector picks candidates, and what is cited is still a
relationship or passage verified against its live source.

- **Setting.** A seventh AI purpose, *Search embeddings* (`embeddings`), OpenAI-only,
  `text-embedding-3-small` or `-large` or a custom `text-embedding-*` id. **Off by
  default** and seeded off on new applications: it needs an OpenAI credential and spends on
  every new question once on.
- **What is embedded, and when.** Relationship texts of the graph version, passages of its
  sources (the same 1,400-character windows keyword search uses) and the benchmark
  questions - only in an **AI graph run someone requested** (`graphs.rebuild`), or by an
  operator with `manage.py embed <app> --as <user>`. A background rebuild never spends.
  Each new question is embedded when asked and charged to the person asking; Code Factory
  charges the run's requester. Capped at 12,000 texts per run.
- **Storage** (`models.Embedding`, migration 0068). Content-addressed by the SHA-256 of
  the exact text, per application and model, 512-dimension unit float32: unchanged text is
  never paid for twice, so a rebuilt version reuses every vector. A passage also records
  its entry and offset and is re-read and re-hashed at search time - changed text is
  skipped.
- **Search.** Exact cosine in process with numpy over the stored vectors (top 30, floor
  0.2), fused with the BM25 order by reciprocal rank fusion (k = 60). With no vector the
  order is exactly the word ranking, so every application that leaves it off behaves as
  before. Used by Chat graph mode, Chat AI mode and the `search_knowledge` tool, the
  API/MCP search, Code Factory evidence and ServiceOps' graph passages (stored question
  vectors only there: nothing is charged from a page view).
- **Billing.** One `AIUsage` receipt per request, purpose `embeddings`, provider OpenAI,
  the request id from `x-request-id`; a missing token count or malformed vector is an
  error, never a free request. A failed question embedding logs `embedding_unavailable`
  and answers by words; a failed indexing step warns on the version and never loses the
  relationships already paid for.
- **Benchmark.** Uses stored question vectors only, so a run still calls no model; each
  row records whether meaning took part ("words + meaning").
- **pgvector deliberately not used yet.** A version's vectors fit in memory (3,000 × 512
  float32 ≈ 6 MB) and exact search over them is sub-millisecond, so the production
  database image and its extension privileges do not change. pgvector replaces the
  in-memory matrices once a library outgrows that - nothing else changes.
- Not measured on CarePath yet: this machine has no OpenAI credential. Set
  `openai_<app id>`, switch Search embeddings on, then
  `manage.py embed <app> --as <owner>` and `manage.py benchmark <app>` give the before/after
  (cost on CarePathDev is a fraction of a cent).

## Phase 6 - Spend caps and reranking (done)

- **Monthly budget** (`budget.py`, `AIBudget`, migration 0069). An owner sets it in AI
  settings; Usage shows it. Checked before every paid call at the three choke points
  (`invoke_ai`, `chat_configuration`, `embeddings.embed`), refusing with the month's figures.
  It caps *starting* spend from receipts at the entered rates: a call in flight finishes, so a
  month can end one call over, and a purpose priced at 0 counts as free. Graph extraction
  that hits the cap keeps what it already paid for and says why it stopped.
- **Reranker** (`rerank.py`, purpose "Answer reranking", migration 0072, seeded off). Reorders
  the top 20 verified citations to the best 8 (keyword: 5) through the application's own
  Claude or OpenAI model. It can only reorder: unknown, repeated or non-numeric answers are
  dropped, and any failure keeps the original order. `manage.py benchmark --rerank --as
  <user>` is the one benchmark that spends, so it is switched on because it was measured.
  Not measured on CarePath (paid).

## Phase 4b - Postgres full-text (done, now run on PostgreSQL in CI)

`workbench.candidate_sources` asks PostgreSQL's full-text index (GIN, migration 0073,
`to_tsvector('english', left(title || ' ' || content, 200000))`) for the 100 best-matching
sources by `ts_rank`, instead of the first 100 that contain any word. Questions become
prefix tsqueries from letters and digits only, so nothing typed becomes tsquery syntax.
SQLite keeps the substring scan. The PostgreSQL test skips on SQLite and says so; CI
now runs the suite and the scorecard on PostgreSQL 16 too. The first PostgreSQL
scorecard measured `ts_rank` re-tokenizing every matching row at 51 ms a keyword
answer, so migration 0088 made the `tsvector` a stored generated column with the GIN
index on it, for sources and passages both: 10 ms after.

## Phase 7 - Enterprise hardening (done)

| Item | What was built | Not covered |
|---|---|---|
| Sensitivity labels | `sensitivity.py`, `KnowledgeEntry.sensitivity`/`personal_data` (0070). Restricted sources are out of every AI path via `answerable()`, including already-published versions. Personal-data scan (email, phone, SSN, Luhn-valid cards, dates of birth) is a suggestion on the source page; `manage.py scan_personal_data` backfills. | No automatic labelling - deliberate. |
| Incremental updates + stale alert | `graph_ai.reusable`: AI runs reuse unchanged sources and work through large libraries run by run. Opt-in auto-refresh (0071) charged to the owner who enabled it, grant re-checked. Notices count sources *added* since the published version, not only changed ones. | Publishing stays manual - the gate is an invariant. |
| Source-system permissions | `permissions.py` (0075): Jira "can browse" per member and issue, fail-closed, enforced on every answer path; `manage.py link_identity` for hidden emails. | ServiceNow and SharePoint (need delegated per-user access). Sources list and graph explorer still show titles to members. Not run against a live Jira. |
| Multiple server processes | `StreamLease` (0074), `shared_streams = true`: Stop and `is_live` across processes through a polled lease, no Redis. | One process remains the default and needs nothing. |

Measured after phases 4b-7 on a migrated copy of the local database (CarePathDev v4, 16
questions, no model): graph 15/16 found, MRR 0.693, and keyword 14/16, MRR 0.674 -
identical to before them, since nothing there is restricted or permission-checked. The
added checks cost 2-3 ms a question: graph 6.8 -> 8.8 ms, keyword 4.4 -> 7.1 ms median,
still under the original 22.2 and 10.4 ms.

## Since this plan

Built after the roadmap closed, each measured by the scorecard
(`manage.py scorecard`) rather than this plan's benchmark runs:

| Item | What was built |
|---|---|
| Passages as rows | `KnowledgeChunk` (`chunks.py`): each active source's passages, written with the source; keyword answers come from them once every source has current ones (`retrieval_candidates = "auto"`). Same 14/16, MRR 0.674. |
| Graph versions as rows | `GraphElement` (`graph_store.py`): each element stored once for every version it is in, and graph questions answered from the rows (`graph_candidates = "rows"`), held by the scorecard to identical citations. |
| Graph built a source at a time | `MAX_SOURCES` removed: every usable source is in the graph, each held to its own 2,000 rows and 6,000 nodes, and a rebuild reads only the sources that changed. |
| References followed | `graph_ai.referenced`: Code Factory's evidence follows the references its ticket names; planted-bug evidence recall 76% -> 96%. |
| Redis streams | an answer outlives its connection and resumes on any web process; uvicorn serves stream readers without a thread each. |

How retrieval works today, stage by stage, is `docs/graph-retrieval-end-to-end.md`.
