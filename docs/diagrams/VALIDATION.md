# Diagram documentation audit

Validated against the application source on 2026-09-28. Scope: the five JSON specifications, their generated HTML diagrams, and `index.html`. Application code was not changed. `Digital-Brain.pdf` was explicitly excluded by the user and remains unchanged; it may contain the earlier descriptions.

## Deviations corrected

| Area | Corrected behavior | Application evidence |
| --- | --- | --- |
| Evidence paths | Sources/AI chat starts from active-source retrieval; Graph answer and Code Factory use published graph revisions. Code snapshots remain separate. | [workbench.py](../../platform_core/workbench.py): `answer_question`, `lexical_citations`; [code_factory.py](../../platform_core/code_factory.py): `start_run`; [graph_ai.py](../../platform_core/graph_ai.py): `graph_snapshot` |
| Publication and drift | Structural builds wait for in-flight intake and save drafts. Failed paid calls are not repeated automatically. Publication validates sources recorded in the revision, not newly added sources. The Knowledge UI shows recorded-source drift. | [graphs.py](../../platform_core/graphs.py): `rebuild`, `process_next_graph`, `publish_revision`, `revision_drift`, `graph_view` |
| Machine AI spend | Graph-search REST and MCP are model-free; chat REST and triage POST can invoke AI. Full triage paths include the application reference. | [urls.py](../../digitalbrain/urls.py); [api.py](../../platform_core/api.py); [api_chat.py](../../platform_core/utility/api_chat.py); [api_triage.py](../../platform_core/utility/api_triage.py) |
| Connector scheduling | Manual-only, 15-minute, hourly, four-hourly and daily imports exist. Scheduled imports recheck the connector creator's access. They do not start AI triage automatically. | [connectors.py](../../platform_core/connectors.py): `SYNC_INTERVALS`, `process_next_connector` |
| Code Factory | Manual briefs live on FactoryRun, not in Knowledge. Plan approval precedes work order, implementation, test authoring, model review and mechanical verification. Prepared files are local until a separate PR request; preparation can use snapshot files without a write credential. Delivery requires repository confirmation and a write credential, and verifies again. | [code_factory.py](../../platform_core/code_factory.py): `start_run`; [code_factory_build.py](../../platform_core/code_factory_build.py): `gate`, `publish_gate`, `prepare`, `publish` |
| Testing and refresh | Code Factory authors tests but does not execute repository tests. GitHub checks are polled after delivery. Refresh of documents, graph and repository is explicitly requested; a new knowledge draft still requires publication. | [code_factory_build.py](../../platform_core/code_factory_build.py): `run_delivery`, `refresh_checks`, `refresh_after` |
| ServiceOps implementation | Triage, caching, fallback briefs, verdicts, confirmed causes and operations-graph exploration already exist. UI requests are queued; REST POST executes synchronously. | [serviceops.py](../../platform_core/serviceops.py); [serviceops_triage.py](../../platform_core/serviceops_triage.py): `queue_run`, `_execute`, `create_run`; [document_worker.py](../../platform_core/document_worker.py): `serviceops_steps` |
| Operations graph | Rules connect live incidents, changes and shared entities with published document passages. Reads ensure freshness. Confirmed causes feed future evidence without statistical calibration. No published graph means document passages are absent, not that incident triage is blocked. | [ops_graph.py](../../platform_core/ops_graph.py): `ensure_current`, `neighbourhood`, `confirm_cause`; [serviceops_triage.py](../../platform_core/serviceops_triage.py): `evidence_pack` |
| Scoring | Scorer v2 uses eight components with weights 0.22, 0.18, 0.18, 0.14, 0.10, 0.09, 0.05 and 0.04. Insufficient below 0.35 (or without a supported hypothesis), Low below 0.55, otherwise Medium. No High band or calibrated probability. | [serviceops_triage.py](../../platform_core/serviceops_triage.py): `SCORE_WEIGHTS`, `_score` |
| Data model | IncidentProfile, TriageRun, TriageHypothesis, TriageVerdict, OperationsGraph, OpsNode and OpsEdge are implemented. Run steps are JSON; separate TriagePhase and Calibration tables do not exist. | [models.py](../../platform_core/models.py) |
| Code analysis | Native Python/JavaScript-family analysis is supplemented by Graphify's model-free Tree-sitter AST extraction for multi-language symbols and relationships. | [code_graph_ingest.py](../../platform_core/code_graph_ingest.py); [code_graph_graphify.py](../../platform_core/code_graph_graphify.py) |
| Production uploads | Upload availability depends on scanner readiness when scanning is required, including production; it is not a permanent development-only feature. | [services.py](../../platform_core/services.py): `_uploads_available`; [processing.py](../../platform_core/processing.py): `scanning_ready`, `process_document` |
| Security wording | Source/quote checks establish provenance, not correctness of every inference. Owner-managed credentials and controlled SDK credential injection are supported. Fetching has explicit allow-list/credential options; conversion's Python socket restriction is not a general OS sandbox. | [agent tools](../../platform_core/agent_runtime/tools.py); [secrets.py](../../platform_core/secrets.py); [fetching.py](../../platform_core/fetching.py); [processing.py](../../platform_core/processing.py); [chat_text.py](../../platform_core/templatetags/chat_text.py) |
| Roadmap versus guarantees | Auto-triage, daily budgets, High-band calibration, nightly fitting and proposed performance/accuracy targets are clearly separated from current behavior. The replay command measures a retrieval baseline, not calibrated model accuracy. | [serviceops_replay.py](../../platform_core/management/commands/serviceops_replay.py); current worker lanes, models and scorer listed above |

## Validation

- All five specifications and generated diagrams passed Archify showcase validation: 9/9 checks, zero errors and zero warnings.
- All five artifacts passed the packaged automated browser check at 1440x900, 1600x1000, 1920x1080 and 2048x1320, including containment and readability checks.
- Light and dark screenshot samples of each final diagram were visually inspected. Automated browser evidence and visual review are separate checks.
- Index section links, local resource links, unique IDs, iframe viewBox dimensions, JSON-to-HTML regeneration hashes and embedded script syntax were checked from source.
- Interactive index review was blocked by the in-app browser's policy against `file:` URLs. No index interaction or theme-switch test is claimed.
- This was a source/documentation audit. No live provider, external connector, application database, deployment, load test or application test suite was exercised.

## Artifact receipts

Hashes bind the exact UTF-8 bytes validated in this workspace. Line-ending conversion will change the hashes. `browser_evidence: passed`; `visual_review: passed` for the five standalone diagrams only.

### 01_overview.html

- Type: `dataflow`
- Specification SHA-256: `b768a31b4c2e3181e57157454b5b7e14ed71bd34de46901b42a9a6c297ebb608`
- HTML SHA-256: `755f901df4b37d4c151e631aab183ae4a5b39d3f9eb1369c0c69e52a33962a5c`
- Validation: 9/9 showcase; browser evidence passed; visual review passed.

### 02_knowledge-graph-management.html

- Type: `workflow`
- Specification SHA-256: `11f198fceda2b43e1ddad8958aae7221adf674a27aab958a69a0c149d9a5ed15`
- HTML SHA-256: `bdf4b570bad08f40e3874c96a9ee8ba2862c7ca7cc871735d87de49359b8f3bd`
- Validation: 9/9 showcase; browser evidence passed; visual review passed.

### 03_sdlc-bug-fix.html

- Type: `workflow`
- Specification SHA-256: `4c5026aed4d2ebcee9d22216ec4ebb5ff4aec7a86442c92abf60d54d0c7a355d`
- HTML SHA-256: `ca80f8ae48612e7bc813a8e3b6bdffea1a3ea67609abb8ba49dad61d5906e96f`
- Validation: 9/9 showcase; browser evidence passed; visual review passed.

### 04_sdlc-incident-management.html

- Type: `workflow`
- Specification SHA-256: `e0d432a1db1c73a917373f9076766885b5964ff8a2895cb9d05eb4c88c594f89`
- HTML SHA-256: `66c01a9074a29d4f2e8fb968c4dd6b55d7a23c1a7c44f9cd864c0ae5b4bec85f`
- Validation: 9/9 showcase; browser evidence passed; visual review passed.

### 05_serviceops-triage.html

- Type: `workflow`
- Specification SHA-256: `4c5158a162e4dc6a6cff7de92ea19ca33520b8ae5300789ad1c02258b7aa42e0`
- HTML SHA-256: `df3de28080fd6fc0ede1fe0e647bcad15600c2e95346237da5bc902f49f2c990`
- Validation: 9/9 showcase; browser evidence passed; visual review passed.


## Bug-fix follow-up verification

The follow-up audit found omissions in the earlier high-level mapping. They are now corrected in the bug-fix diagram and its index section:

- Triage receives ticket text and no graph citations; `run_analysis` retrieves the published graph evidence.
- Code snapshots are optional context and are used in analysis as well as design.
- `factory_views.self_approval_allowed` permits audited self-approval when `ALLOW_SELF_APPROVAL` is explicitly enabled (including production); the default is false. `review_plan` also supports selecting plan items and requires a review note.
- Preparation saves local files only; snapshot-only verification cannot check live staleness. Repository tests run through external CI, not preparation.
- Delivery performs sequential remote writes, so a delivery failure can leave a branch or partial commits. Pre-write refusal and preparation are the stages with the no-repository-write guarantee.
- The index now lists the full ten-step lifecycle, including plan creation, preparation, the separate PR request and post-delivery checks/refresh. The diagram remains an intentionally grouped view.

The updated diagram passed 9/9 showcase checks and all four automated desktop checks; light/dark screenshots were reviewed. Application code and the PDF were not changed.

## Incident-triage follow-up verification

Checked the orchestration, operations-graph walk, parser, scoring, brief reads, feedback handler and API against the documentation. The index now gives the full sequence around the six recorded phases: profile, evidence, model, verify, score, save.

- UI requests queue; API POST queues and executes synchronously. Atomic claiming, access rechecks and incident-digest validation precede execution.
- Only verified completed runs qualify for cache reuse; incident/digest, pack, prompt/scorer and provider/model must match. Evidence-only and failed runs are not reused as completed answers.
- Exact quote checks and a next-step keyword deny-list do not verify the truth of a cause or guarantee semantic read-only safety. The diagram now says checked quotes, not verified claims.
- Up to three surviving hypotheses retain model order. Scorer v2 computes one band for the run, selecting supporting pack rows by cited source ID, not one score per hypothesis.
- Handled AI errors yield an evidence-only brief; access loss, changed intake evidence and unexpected exceptions fail the run. Worker-stalled runs are failed after the configured 300-second threshold and are not automatically retried.
- Saved-brief verification covers the incident, every evidence-pack row and hypothesis citations. Source changes may invalidate it even if the changed pack item was not cited. Publishing a new graph alone does not invalidate a historically verified brief.
- A verdict alone neither confirms a cause nor updates a saved score. Accepted/partial verdicts can select a cited, active source other than the incident. When the incident graph node exists, a confirmed edge is created for future retrieval; the history component contributes only if future hypotheses cite that evidence. Authors may retract their verdicts and associated edges.
- Resolution imports remain independent from feedback; neither connector sync nor feedback automatically requests triage.

Evidence: [serviceops_triage.py](../../platform_core/serviceops_triage.py) (`queue_run`, `execute_run`, `_execute`, `_parsed_hypotheses`, `_score`, `run_still_verified`, `process_next_triage`); [ops_graph.py](../../platform_core/ops_graph.py) (`neighbourhood`, `passage_rows`, `confirm_cause`); [serviceops.py](../../platform_core/serviceops.py) (`serviceops`, `triage_verdict`); [api_triage.py](../../platform_core/utility/api_triage.py).

The revised diagram passed 9/9 showcase checks with zero errors/warnings, four desktop browser checks and light/dark visual review. Documentation links, iframe dimensions and documented constants were checked from source. No application runtime tests or provider calls were run. Application code and PDF remain unchanged.
