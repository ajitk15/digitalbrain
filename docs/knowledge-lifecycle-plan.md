# The knowledge lifecycle: long-term plan

Phases are marked as they land in the roadmap below. This is the target the platform grows towards. It is ordered so
that each phase ships on its own and is measured before the next one starts.
`docs/knowledge-code-repos-plan.md` is Phase 1 of it.

## The cycle

```
 1 Set up ──► 2 Feed ──► 3 Build ──► 4 Check ──► 5 Publish ──► 6 Use
    ▲                                                            │
    └────────────── 8 Keep current ◄──── 7 Learn ◄───────────────┘
```

1. **Set up**: create the application and choose its purpose, its features, its
   industry ontology and the systems it reads from.
2. **Feed**: add the sources of truth: documents of every kind, code
   repositories, tickets, articles, automations.
3. **Build**: generate a graph version from them, with AI and checked by rules.
4. **Check**: judge the version: source quality, retrieval benchmark, what AI
   added compared with structure alone, citation integrity.
5. **Publish**: a person makes a checked version the one that answers.
6. **Use**: Code Factory, ServiceOps, chat and the API answer from the published
   version, and only from it.
7. **Learn**: what using it produces (delivered fixes, confirmed incident causes,
   questions it could not answer, answers someone marked wrong) is offered back as
   *candidate* knowledge.
8. **Keep current**: sources that moved at their origin, a published version that
   has fallen behind, and a draft that is ready are each surfaced to the person
   who can act on them. The cycle then runs again.

Three rules hold at every stage:

- **What is trusted is decided by a person, from evidence.** Nothing a model says
  becomes knowledge by saying it. Learning produces candidates, a person accepts
  them, and they then pass the same Build → Check → Publish as everything else.
- **Every answer knows who is asking.** Grants, source-system permissions,
  restricted sources and masking decide what each person's answer may contain.
- **Doing no harm is structural, not a prompt.** Deny by default, scope closed over
  rather than supplied by the model, row security, budgets, the gateway, never
  executing an automation, never running written code outside the opt-in local
  tests. These stay invariants and no stage may weaken them.

## Where the platform stands, stage by stage

| Stage | Exists today | Gap |
|---|---|---|
| 1 Set up | Create form: purpose → feature switches (`services.PURPOSES`); ontology on the form and on Generate (`Application.ontology`, `ontology.PACKS`: core, US insurance, healthcare); connector question in onboarding; checklist (`readiness.all_steps`) | The checklist is organised by Code Factory's gates (Analysis, Delivery), not by the cycle; Operations has its own list. Nothing shows the cycle as one path |
| 2 Feed | Documents (upload, folder, link, SharePoint, GitHub docs), tickets (Jira, GitHub, ServiceNow), articles and automations (ServiceNow KB, AWX, Rundeck, Azure, CSV), code (Code Graph, its own menu item) | Code sits outside Knowledge. Each kind has its own list code (`source_library`, `ticket_library`, `code_graph`), and a new kind means a new page |
| 3 Build | Per-source fragments, AI on every version unless switched off, incremental extraction with every answer cached, ontology-typed, quote-verified, versions stored as rows | None structural |
| 4 Check | Source check and content check per document, ticket readiness, benchmark per version, with/without AI per version, Quality tab | All advisory and spread over tabs. Nothing compares a draft with what is published |
| 5 Publish | `graphs.publish_revision`: refuses only a version whose evidence changed | **No release check.** A version that answers fewer benchmark questions than the published one can be published with one click and no warning |
| 6 Use | Chat, Code Factory, ServiceOps, API, MCP all read `graph_snapshot` (published only); each records which version answered | None structural |
| 7 Learn | ServiceOps verdicts write a `confirmed` edge; plan items are accepted or rejected; runs record delivery and CI | **No loop back into knowledge.** A delivered fix, a confirmed cause, an unanswered question, a rejected plan item with its reason: none of them reaches Sources. Chat answers have no feedback |
| 8 Keep current | Connector schedules with high-water marks; link drift detected (`KnowledgeSource.Status.STALE`); repository head checks; Code Factory's Refresh stage; the Graph page notes "N sources changed since" | Detection exists, but nothing **tells** anyone: the Attention list has no item for "published version is behind", "a draft is ready", or "sources drifted at their origin". It is only visible to someone who opens the Graph page |

The core (Build, Use and the safety invariants) is in place and strong. The gaps
are at the edges of the cycle: Check → Publish, Learn and Keep current. That is
where this plan spends its effort.

## Target design

### A. One source registry

Today each kind of source has its own listing code. Long term, the different kinds
should sit behind one contract, the same pattern as `services.FEATURES`,
`ontology.PACKS` and `connector_kinds.KINDS`:

```
SOURCE_KINDS = {
  "documents":  Kind(label, feature, list_rows, freshness, quality, add_url, ...),
  "code":       Kind(...),     # CodeRepository
  "tickets":    Kind(...),     # connector-claimed KnowledgeEntry
  "articles":   Kind(...),     # later: KB articles and automations get their own tab, if wanted
  "learned":    Kind(...),     # Phase 5: accepted candidates
}
```

Each kind answers the same questions: what rows it has, whether each row is fresh
at its origin, how good it is, where it is used (graph versions or code
snapshots), whether it is sensitive, and how to add one. The Knowledge tabs, the
status strip, the Attention list and onboarding are all drawn from the registry.
Adding a kind is one entry plus its row code, with no new navigation code. Storage
does not change: `Document`/`KnowledgeEntry`, `CodeRepository` and connector
records stay where they are. The registry is a way of reading them, the way
`ticket_library` already is.

**Code stays its own graph**, with its own versions (snapshots), linked to
knowledge by `code_knowledge`. Merging code into the knowledge graph would break
citation verification, which runs against `KnowledgeEntry`, and the rule that text
derived from code is never evidence that the code meets a requirement. Code is fed
in through Knowledge, read in Code Repos, and used by Code Factory and code
review.

### B. The lifecycle tracker (onboarding, and after it)

Today's checklist is replaced by the cycle itself, shown in one place:

```
Set up ✓ · Feed (3 kinds, 41 sources) ✓ · Build v14 ✓ · Check v14 ⚠ 1 regression · Publish (v12 answering) · Use
```

- **Onboarding** is the first time through: the same steps as today
  (`readiness`), regrouped as Set up / Feed / Build / Check / Publish, then each
  consumer's own gates (Code Factory's Analysis and Delivery, ServiceOps' gates)
  under **Use**. `readiness.steps` keeps its Code Factory list because `prevalidate`
  narrates it. Only the grouping and the page change.
- **After setup**, the same tracker becomes the Knowledge status strip, one line on
  every Knowledge page: what answers, what is waiting, what is behind.
- **The Attention list** gains three items, each with a link to the screen that
  fixes it:
  - the published version is behind its sources;
  - a draft passed its release check and is waiting to be published;
  - N sources moved at their origin.

### C. Release check before publish

Publishing is where an application decides what is trusted, so it is checked the
way the scorecard checks a commit: a **draft compared with the published
version**, worked out by rules, with no model:

| Check | Hard (refuses) | Soft (warns; owner may publish anyway, audited) |
|---|---|---|
| Evidence still readable | ✓ (today's only check) | |
| Every citation verifies (active, digest, quote) | ✓ | |
| No restricted source among its sources | ✓ | |
| Benchmark questions found / MRR | | worse than published |
| Structure-only questions found, and what AI added | | AI added nothing, or took away |
| Sources lost since published | | any |
| Source checks: documents below Ready | | count went up |
| Ontology: proposals outside it, refused quotes | | share went up |

The Publish button shows the check; a soft failure needs a typed reason, stored on
the revision and in the audit event. A version that passes everything can be
published in one click, as now.

**Optional auto-publish** (off by default, owner-set per application): a version
that passes every check, hard and soft, publishes itself, and the audit event names
the owner who switched the policy on. This is what makes the cycle continuous
without trading away the check. Without the release check it must not exist, which
is why it comes after it.

### D. Learning loop

What using the knowledge produces comes back as **candidates**, in a **Learned**
inbox under Knowledge, each built by rules from records the platform already keeps:

| Signal | Built from (all already stored) | Becomes, when accepted |
|---|---|---|
| A delivered fix | `FactoryRun` delivered, plan approved, `checks` passing; ticket key, files changed (`ProposedChange` paths), PR or folder, approver | A **change record** source: what changed for which ticket, where and when, and who approved it |
| A confirmed incident cause | `TriageVerdict` with the cause chosen from the idea's own citations | An **incident learning** source: incident, cause, the evidence it was confirmed from |
| A question nothing answered | Chat and API answers with no citation, or every citation from one weak source | A **gap**: an owner adds a source, or promotes the question into the benchmark |
| An answer marked wrong | New: a "this answer is wrong / cites the wrong source" control on a chat answer | A gap or a benchmark question (the expected source supplied by the person) |
| A rejected plan item | `PlanItem.status = rejected` and the review note | Shown to the next run's reviewers as context. **Never** a source on its own: a rejection is a judgement, not a fact |

Rules that keep this from contaminating the knowledge it learns from:

- **Rules write it, never a model.** A candidate's text is put together from stored
  fields. A model's plan or explanation is quoted only as "the plan said", never
  stated as fact.
- **A person accepts it.** Owner or contributor, audited. Rejecting a candidate is
  remembered so it is not offered again.
- **It is derived knowledge.** Learned sources get a prefix of their own (like
  `code-graph://` for code docs) that `code_knowledge.references` excludes. A
  change record can tell chat and the next run "CARE-401 was fixed in these files"
  but can never be the evidence that code meets a requirement. Otherwise the
  platform would be citing its own output as proof of itself.
- **Same path as every other source**: masking, the personal-data scan, restriction
  and source permissions apply. It then goes through Build → Check → Publish.
- **The benchmark grows from use**: promoted questions make the release check
  harder to pass over time, which is what makes "learning" measurable rather than
  claimed.

### E. Keeping it current

- **Freshness per source kind**, from signals that already exist: connector
  `last_attempt_at` and schedule, `KnowledgeSource.status`, `CodeRepository.drifted`,
  a document's age against an optional review date per document type (a runbook
  reviewed yearly, an ADR never).
- **Detect automatically, act on a person's word.** This is the rule
  `KnowledgeSource` already follows. Connector imports run on their schedules as
  today. Links and repositories are re-read on Resync or Refresh. Code Factory's
  Refresh stage stays a question.
- **Drift of the models themselves**: `llm_drift` and `typing_pack` on a schedule,
  shown in the release check when the model changed since the published version.

### F. Who is asking, and what must never happen

Already enforced, and this plan builds on rather than adds to it: grants and roles
(`policy.CAPABILITIES`), source-system permissions on answers, restricted sources,
masking before storage, per-application credentials and budgets, the gateway, and
review before any change is written.

Later, measured, and only if wanted: **application rules**. An owner marks
documents of an authoritative type (policy, standard, ADR) as rules that every
consumer must check its output against: Code Factory's analysis, code review,
ServiceOps triage. Each finding quotes the rule. Rules can only add checks; they
can never relax an invariant or grant access. This extends what Code Factory's
non-functional rubric already does, and it needs its own benchmark before it is
trusted.

Built first for Code Factory's plan review only (`rules.py`): statements are read
by rules and quoted from one line, an owner marks the source, and the scorecard
holds the ranking to `benchmarks/rules-pack.json`. Code review and triage come
after the pack has more than three labelled tickets.

## Information architecture

```
Application
├─ Knowledge        Documents · Code Repos · Tickets · Graph · Quality · Versions   (+ Learned, Phase 5)
│                   status strip: answering vN · draft vM checked · K sources behind
├─ Code Factory     uses the published knowledge and the code snapshots
├─ ServiceOps       uses the published knowledge and the operations graph
├─ Chat
└─ Settings
```

The menu follows the cycle: feed and build first, then the consumers. Onboarding
walks it once, and the status strip keeps it visible afterwards.

## Roadmap

| Phase | What | Effort | Risk | Measured by |
|---|---|---|---|---|
| 1 ✓ | Code Repos under Knowledge, Code Graph folded in (`knowledge-code-repos-plan.md`) | 4.5-5 d | Low-medium | Gates unchanged; tests for gating and onboarding routes |
| 2 ✓ | Lifecycle tracker (`lifecycle.py`): the cycle on onboarding, Knowledge status strip, Attention items (draft ready, published behind, origins and repositories moved) | 3-4 d | Low | Every step reachable and correct on Engineering, Operations, Both |
| 3 ✓ | Release check before publish (`release.py`); reason recorded on override | 4-5 d | Medium (changes a habit) | Scorecard probe: a regressed draft is refused or warned, as designed |
| 4 ✓ | Source kind registry (`source_kinds.py`): Documents, Code, Tickets behind one contract; tabs and Feed drawn from it (rows, freshness and quality stay on each kind's page) | 5-8 d | Medium (refactor) | Same pages byte-for-byte where nothing changed; query counts held flat |
| 5 ✓ | Learning loop (`learning.py`): Learned inbox, change records, incident learnings, gaps, answer feedback, benchmark promotion | 8-10 d | Medium-high (new trust path) | Benchmark grows; derived-prefix exclusion tested; scorecard evidence recall unchanged |
| 6 ✓ | Optional auto-publish policy (`release.auto_publish`) | 1-2 d | Medium | Only on all-green release checks; audited |
| 7 ✓ | Application rules (`rules.py`): Code Factory's plan review first | measured first | High | Its own pack, `benchmarks/rules-pack.json`, held by the scorecard |

Phases 1 and 2 can be built in either order, and together they make the cycle
visible. Phase 3 is the most valuable single safety step. Phase 4 should come
before Phase 5, so that "Learned" is a registry entry rather than a sixth
hand-written page. New tables (Phase 5: candidates, answer feedback) need a
row-security policy in their migration, a `test_tenancy` entry, and nullable or
`db_default` columns, as the expand/contract rule requires.

## Risks

| Risk | Mitigation |
|---|---|
| The learning loop cites the platform's own output as evidence | Built by rules from stored fields; person accepts; derived prefix excluded from requirement evidence; release check |
| A release check that warns on everything trains people to override it | Hard checks only for integrity and safety; soft checks compare with the published version, not with an ideal |
| Auto-publish ships a bad version | Off by default; only on all-green; the benchmark grows from real questions, so all-green gets harder to reach |
| Answer feedback is noisy or abused | It creates candidates and benchmark proposals, never changes ranking by itself; an owner promotes |
| Every version is AI-built, so the cycle costs money | Unchanged: cached answers per window, budget, gateway; the release check costs nothing |
| A registry refactor regresses pages | Phase 4 is behaviour-preserving, held by existing tests and query counts, shipped alone |
| Scope creep into "the model decides" | The three rules at the top are the review question for every phase |

## Decisions for the owner

1. **Can a version ever publish itself?** Recommended: yes, as an opt-in policy,
   and only after the release check exists.
2. **Can anything become knowledge without a person accepting it?** Recommended:
   no, never. Connector imports are the only automatic intake, as today.
3. **Should viewers be able to mark answers wrong?** Recommended: yes. It only
   creates candidates.
4. **Should soft release-check failures need a typed reason?** Recommended: yes,
   stored on the version.
5. **Should KB articles and automations get their own Knowledge tab, or stay under
   Documents?** Recommended: stay under Documents until Phase 4's registry makes a
   tab one entry.
