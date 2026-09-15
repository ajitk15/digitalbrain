# Code Graph integration plan

Status: implementation started. The first repository-to-Code-Factory slice is described below.

Implemented in the first slice:

- Application-scoped GitHub repository registration and explicit feature gating.
- Background indexing pinned to an immutable commit, with superseded-job protection.
- Bounded Python, JavaScript, JSX, TypeScript, and TSX file/symbol/import analysis.
- Immutable repository snapshots, retained source files, and explainable directed relationships.
- Desktop repository selector, file list, dependency graph, code search, and file inspector.
- Automatic repository visibility for GitHub documents and GitHub issue connectors.
- Sources links to the matching Code Graph repository and snapshot.
- Code Factory pins the matching snapshot after triage and includes its structural neighborhood in analysis.

Still planned: API seam and cross-repository analysis, Knowledge-to-symbol links, richer impact traversal, scoped code chat/tools, exports, additional languages, isolated test execution, and coordinated multi-repository delivery.

## Product outcome

Add **Code Graph** beside Knowledge, Chat, and Code Factory inside each application. It should explain how that application's repositories work, show relationships between repositories, and give Code Factory verifiable code context for planning and implementation.

The three connected views have distinct jobs:

| View | Question it answers |
|---|---|
| Knowledge | What should the application do, and what requirements or operational evidence support that? |
| Code Graph | Where is that behavior implemented, what depends on it, and which repository version are we looking at? |
| Code Factory | What change should we make, what else could it affect, and what evidence and checks support the proposed change? |

Use RECREATE.md as a feature reference. Its instructions to recreate a separate single-user FastAPI/React application, copy exact styling, and introduce a separate credential store are not requirements for this integration. Keep Digital Brain's application isolation, desktop layout, existing AI configuration, and review workflow.

## 1. What exists and what must change

Repository inspection shows:

- `github_sources.py`: a repository link imports a README, with a documentation fallback; directory imports select documentation extensions, bounded to 25 files and depth 4. A file link imports that one file. This is not a repository index.
- `link_sources.py`: link imports become privately stored Documents, then follow the conversion pipeline. Repository identity, original selection scope, and an immutable commit are not stored as first-class repository metadata.
- `connector_kinds.py`: the GitHub connector imports issues. Connecting an issue feed does not mean source files were indexed.
- `source_library.py`: the combined Sources inventory already distinguishes documents and records and matches Knowledge graph usage by source ID and digest. Extend this with repository and Code Graph provenance.
- `graphs.py`: saved Knowledge graph revisions support draft/published states. Code Factory records the published Knowledge graph number on a run.
- `code_factory.py`: six phases exist: triage, analysis, design, implementation, verification, delivery. Repository confirmation and delivery are separate from plan approval. Implementation reads approved target files; verification currently checks paths, elisions, and staleness, and explicitly does not run a build/test suite.
- `agent_runtime/tools.py`: tools close over the application and user and re-check access on every call. Extend that mechanism for code retrieval.

The principal work is therefore repository ingestion, immutable code evidence, static analysis, and Code Factory integration. A graph canvas alone would not deliver the intended result.

## 2. Desktop experience

Navigation: **Knowledge | Code Graph | Chat | Code Factory | Settings**.

Code Graph opens to an application overview when several repositories exist. With one repository, open that repository directly. Show registered repositories even before indexing, with an honest status such as **Documentation only**, **Queued**, **Indexing**, **Ready**, **Partial**, or **Failed**.

Repository view:

- Top bar: repository selector, branch/ref, short commit SHA, snapshot version, search, and Refresh.
- Left: collapsible file tree, organized by folders, with language and parse-status indicators.
- Center: graph, initially grouped by package/folder; selecting a group reveals its files. Large repositories do not open as thousands of overlapping nodes.
- Right: one inspector, opened on selection. Show file path, symbols and line numbers, then **Depends on**, **Used by**, and **Impact** sections. Offer **View code**, **Ask about this**, and **Use in Code Factory**.
- Toolbar: fit, zoom, relationship filters, and a small legend. Minimap only where useful. Preserve pan, zoom, selection, and settled node positions through refresh and panel changes.

Keep advanced information in expandable sections. No mandatory extra wizard, automatic AI summary on every click, or second settings area. A keyboard-accessible file/relationship list must remain usable when the canvas cannot load or JavaScript is disabled.

The application overview has repository nodes and service/package relationships. Selecting a relationship reveals both evidence locations and why they were linked. A repository selector includes **All repositories** without duplicating the entire file graph on that screen.

## 3. How code enters the application

One ingestion record must drive both Sources and Code Graph, with separate processing jobs for searchable documentation and structural code analysis.

| Input | Sources behavior | Code Graph behavior |
|---|---|---|
| GitHub repository added for code indexing | Repository source with README/docs and import status | Index selected branch at a resolved commit; group all included files under this repository |
| Existing README/repository documentation import | Keep current content and graph citations | Register identifiable repository as Documentation only; offer Index repository |
| GitHub file or folder link | Preserve selected content and relative path | Index selected code scope; clearly label partial coverage |
| GitHub issue connector | Keep imported issues | Associate configured repository as not yet indexed; offer source indexing |
| Uploaded source file | Keep original and searchable representation where supported | Place under an Uploaded code collection, without inventing a Git repository or commit |
| Uploaded source archive | Show archive and processing status | Preserve relative paths in a collection; index supported files after bounded extraction |
| Ordinary document | Existing conversion/search behavior | Link to relevant code evidence when available; do not pretend prose is source code |

For a new repository import, make **Code and documentation** the visible default, with a documentation-only alternative. A file/folder import stays limited to the selected scope. Existing README imports must not silently become full repository downloads.

Always analyze original source bytes, not the Markdown converter's rendering. Preserve relative paths: flattened filenames cannot reliably resolve imports or distinguish two `config.py` files. Keep unsupported files visible in coverage counts; use parse-failure indicators instead of dropping them silently.

Initial providers: GitHub and uploaded code. Define an adapter boundary for GitLab, Azure Repos, and operator-approved local checkouts later. A web request must not allow browsing arbitrary server filesystem paths. Archives need explicit file-count, expanded-byte, nesting, and path-containment limits; never follow symlinks or execute their contents.

## 4. Repository identity and versioning

Use application + provider + provider repository ID as identity, with owner/name as display metadata. Separate repository membership per application even if two applications use the same upstream repository.

Resolve a branch/tag to one commit before enumerating files. Read its tree and blobs against that commit; do not fetch different files against a moving branch. Store both the Git object identity and a content digest. Uploaded collections use an immutable manifest digest in place of a commit SHA.

Every successful or partial analysis creates a snapshot containing:

- Repository/collection, selected scope, branch/ref, commit or manifest digest.
- File manifest with paths, byte digests, parser results, and exclusions.
- Parser/resolver version and configuration fingerprint.
- Graph facts and relationship evidence.
- Completeness status, unresolved imports, warnings, timestamps, and initiating user.

Show two distinct provenance labels in Sources/details: **Knowledge graph v18** and **Code Graph v4 / repo / commit abc1234**. Do not reuse the existing graph version field for two different graph types.

For the application-wide graph, store a revision manifest mapping each repository to an exact code snapshot. It is a reproducible selection of commits, not a claim that all repositories deployed together.

Default browsing uses the latest usable snapshot and shows staleness. Code Factory pins the exact selected snapshots at run creation/design. Preserve the existing Knowledge publish gate; code indexing does not need another routine publish button. A changed snapshot, target commit, or substantive relationship selection after approval requires recomputing the affected plan and invalidating its approval as appropriate.

Historical code citations remain tied to retained immutable blobs and current access. When content is intentionally removed or access is revoked, historical references become unavailable rather than bypassing that removal. Establish a retention policy for snapshots referenced by plans and exports before enabling cleanup.

## 5. Analyzer and relationship accuracy

Adopt the attachment's useful capabilities: file and symbol extraction, imports, route declarations, outbound calls, dependency inspection, impact traversal, search, and documentation generation.

First languages: Python, JavaScript, JSX, TypeScript, TSX. Follow with Java and C/C++. Python can use the standard AST; prototype Tree-sitter for other grammars. Add Django URL patterns and `include()` prefix composition early: the attachment emphasizes FastAPI/Flask routes, which alone would miss this application's own routing.

Parse manifests and safe static configuration to improve resolution: Python package roots, TypeScript path aliases, workspace packages, Java packages, and declared include paths where available. Never execute build configuration to discover paths. Unknown aliases and dynamic imports stay unresolved or inferred.

Evidence classes:

| Label | Meaning |
|---|---|
| Static | A supported parser/resolver identifies the relationship in the selected snapshot |
| Inferred | A heuristic suggests a connection, such as a matching URL or conventional alias |
| Confirmed by user | A person confirmed a relationship, with attribution and snapshot provenance |

Static means supported by static analysis, not proof of runtime behavior. Keep heuristic import resolution out of the Static bucket. Store raw declaration, file, line range, method, path, and resolver reason.

Direction is explicit: **A imports B**. **Depends on** follows outgoing edges; **Used by / Impact** follows incoming edges. Inferred confidence must propagate through a path; traversing a later static edge does not turn that path into verified impact.

Use iterative strongly connected component analysis for **cyclic dependency groups**. An SCC is not the number of individual simple cycles, despite the attachment's shorthand. Report group count and affected files accurately, including self-loops. Likewise, label files **No detected links**, not unused/dead code.

Stable IDs incorporate repository identity and normalized path using a sufficiently strong digest/UUID; avoid the attachment's short path-only hash for a multi-repository service. A rename is a changed identity unless explicit rename evidence is available.

## 6. Relationships between repositories and Knowledge

Build in this order:

1. Declared internal packages/workspace dependencies and explicit configuration references.
2. API consumer/provider candidates using HTTP method, normalized path, service/base-URL identity, and route prefix composition.
3. OpenAPI/schema references and typed client links.
4. Message producer/consumer relationships using explicit broker, namespace, and topic identifiers.
5. Requirement/ticket/document links to files and symbols through citations, explicit paths, and reviewable suggestions.

Never match repositories solely because they both contain `/users` or a similarly named class. Preserve host/service identity; normalize route parameters only for comparison. Ambiguous candidates remain separate and show their evidence. Unknown HTTP methods cannot be presented as exact matches.

Example: an imported ticket requests a new payment status. Link the requirement to the backend endpoint, shared response schema, frontend consumer, and relevant tests. Code Factory can then propose coordinated changes and show which links are inferred.

Users can confirm or dismiss relationship suggestions within the application. Confirmation stores both endpoint snapshots, who confirmed it, and why. Re-indexing either side revalidates it; stale confirmations are not carried forward as facts.

Cross-application traversal is out of scope. The same repository appearing in two applications does not create a permission bridge.

## 7. Code Factory integration

| Phase | New capability | Reviewable output |
|---|---|---|
| Triage | Suggest repositories using indexed evidence and explicit ticket references | Candidate repository with reason; user chooses if ambiguous |
| Analysis | Retrieve symbols, callers, dependencies, API consumers, and related requirements | Evidence-backed affected-file list and uncertainty |
| Design | Use actual file paths, existing patterns, interfaces, and nearby tests | Proposed edits, dependency impact, and validation plan |
| Implementation | Retrieve bounded source windows from pinned snapshots; validate target content | Changes limited to the approved repository/files |
| Verification | Reparse changed files, compare structural facts, inspect affected contracts, check base commit | Separate static findings, executed tests, and unverified behavior |
| Delivery | Include snapshot references and impact evidence in the existing draft PR | Traceable proposal with explicit validation results |

Add application-bound read tools: `list_code_repositories`, `search_code`, `read_code_file`, `find_symbol`, `get_dependencies`, `get_dependents`, and `get_change_impact`. The server supplies allowed application, repositories, snapshots, and user; model arguments can only narrow that scope. Enforce source-window, graph-depth, result, and token budgets. Record code citations with snapshot, path, digest, and line range, verified against returned content.

Do not dump the whole repository into prompts. Retrieve the relevant code neighborhood plus linked requirements and tests. Track context completeness and token usage; measure whether this reduces unsupported paths and missed callers rather than promising a fixed quality improvement.

Before implementation, compare the approved baseline commit with the target branch. Before delivery, re-check the branch head and use an atomic/conditional Git update or equivalent GitHub operation so a race cannot silently move delivery onto an unreviewed base. A stale plan must stop for refresh/review, not quietly regenerate after approval.

Cross-repository analysis belongs in the first integration. Cross-repository writes are a later extension: one approved child plan and draft PR per repository, grouped under a coordination record with sequencing and partial-failure status. Keep the initial delivery path single-repository.

Running tests is separate infrastructure work. Add an isolated runner with explicitly configured commands, resource/time limits, network policy, dependency strategy, and no inherited host credentials. Repository content must not auto-authorize execution. Until that runner exists, continue saying **Static checks completed; tests not run**. Syntax/dependency analysis cannot establish correctness.

## 8. Technical implementation

Suggested models (final names may be adjusted during implementation):

| Model | Purpose |
|---|---|
| CodeRepository | Application membership, provider identity, default ref, source link and scope |
| CodeSnapshot | Immutable commit/manifest, analyzer version, completeness and job result |
| CodeFile / CodeSymbol | Snapshot-bound file metadata and symbol locations; source bytes in private blob storage |
| CodeRelationship | Typed edge with confidence and evidence ranges |
| ApplicationCodeRevision | Repository-to-snapshot manifest for a combined view |
| CodeKnowledgeLink | Typed references between code evidence and existing knowledge entries |
| FactoryCodeContext | Exact snapshots, approved targets and evidence pinned to a FactoryRun |

Prefer relational storage and bounded adjacency queries first; an external graph database is not required for the initial feature. Add indexes around application/repository/snapshot and source/target lookup. Use content-addressed parser caches scoped so they cannot leak source existence between applications.

Suggested code boundaries: `platform_core/code_graph/` with ingestion adapters, language parsers, resolution, snapshots, impact, queries, and jobs. Add Django routes under `/applications/<id>/code-graph/`, repository/snapshot detail routes, and scoped graph/search/file endpoints. Mutations use authenticated CSRF-protected POSTs; API-token access must reuse existing authorization.

Keep Django as the application server. Start the UI with a scoped canvas/SVG explorer integrated into the current shell and styles. Run a bounded renderer prototype before choosing a new frontend dependency. The attachment's React Flow implementation cannot simply be pasted in: evaluate its styling/build requirements against the current strict CSP without weakening that policy. No second FastAPI server, global single-user graph object, external fonts, or independent provider settings.

Background jobs use persisted states and leases: queued, fetching, parsing, resolving, ready/partial/failed/cancelled. Re-check access before credential use and publishing job output. Guard results by job ID so an older run cannot overwrite a newer one. Keep the last good snapshot readable during refresh and after failure. Cache unchanged file facts by digest but recompute relationships affected by manifest/resolver changes.

Repository enumeration must account for GitHub tree truncation, API rate limits, retries, deletion, repository rename, ambiguous branch names, LFS pointers, and submodule boundaries. Submodules are explicit related repositories, never silently followed. GitHub documents that recursive tree responses can be truncated and recommends fetching subtrees separately: https://docs.github.com/en/rest/git/trees .

Tree-sitter exposes syntax-error nodes; retain partial parse status and do not equate producing a tree with valid source: https://tree-sitter.github.io/tree-sitter/using-parsers/queries/1-syntax.html . Exact parser/package versions should be selected and locked after the compatibility prototype, not copied from the attachment's claimed installation table.

## 9. Chat and documentation

Reuse Chat with a visible scope chip: application, repository, or file. A file-scoped question cannot silently read other repositories. Broader dependency questions can offer an explicit scope change. Display code citations as repository/path/line/commit links alongside existing Knowledge citations.

Add asynchronous **Export technical reference** for a repository or selected application revision. Include architecture, file inventory, symbols/signatures where supported, dependencies, cyclic groups, API connections, coverage gaps, and snapshot provenance. Generate factual sections from parser results; optional narrative uses the configured application AI with normal billing records. If narrative generation fails, offer the factual report with its limitation stated.

Exports support Word and a graph image. Capture an undimmed graph without changing the user's current selection or camera. Bound export time, provide real progress, prevent stale jobs from overwriting results, and clean up private temporary artifacts. Validate rendered documents, not only successful file creation.

## 10. Rollout and acceptance

| Stage | Deliverable | Exit criterion |
|---|---|---|
| 1. Foundation and compatibility | Models, feature gate, repository adapter, immutable manifests, parser/renderer prototype | One bounded repo indexed at one commit; no execution; existing app tests pass |
| 2. Usable repository graph | Python/JS/TS analysis, desktop explorer, file inspector, search, versions, Sources linkage | Files and imports match fixtures; parse gaps visible; refresh preserves navigation |
| 3. Application linkage | Repository overview, API/package candidates, Knowledge links, impact and confirmation | No false exact edges for ambiguous services; all edges explainable |
| 4. Code Factory context | Scoped retrieval, pinned evidence, target-file validation, static comparison | Representative tickets produce existing paths and verified references; stale base prevents delivery |
| 5. Extended capability | Java/C/C++, scoped code chat, Word export, isolated test-runner pilot | Coverage matrix published; reports render correctly; executed checks accurately reported |
| 6. Coordinated delivery | Multi-repository child plans/PRs, broader provider adapters | Per-repository approvals, permissions, recovery, and partial-failure behavior tested |

Stages are delivery order, not separate products. The first release should include stages 1-4; otherwise Code Graph risks being a diagram with little Code Factory value. Extend languages in stage 5 based on the actual repository inventory.

Suggested initial capacity targets for benchmarking, not performance promises: 5 repositories/application, 5,000 included files/repository, and an initial canvas of at most 200 visible nodes with expansion/pagination. Configure explicit byte/time limits and show partial results when reached. Benchmark Windows and the production database before selecting final limits.

Regression coverage must include application/role isolation, feature disablement, grant revocation during indexing, private repositories, traversal and archive attacks, duplicate filenames across repositories, import direction, aliases, parse errors, unresolved modules, cyclic groups, inferred impact propagation, ambiguous API endpoints, mixed-version prevention, superseded jobs, deletion/retention, stale Code Factory plans, and exports.

Desktop acceptance: test 1280x720 and 1440x900; readable graph and inspector, keyboard navigation, no page-wide horizontal overflow, working Sources-to-file-to-Knowledge links, truthful empty/partial/error states, and no paid calls triggered by passive browsing. Preserve existing upload, conversion, search, graph publishing, Chat, and Code Factory behavior.

Roll out behind an application feature with an explicit disabled state for existing applications: missing feature rows currently mean enabled, so a new registry entry alone is not a safe opt-in rollout. Backfill only identifiable repository/source associations. Uncertain legacy origins stay unassigned and visible. Enable a pilot application, validate, then expand. Disabling the feature should hide the new entry point and stop new jobs while retaining existing Knowledge and Code Factory workflows.

Measure: repository/file coverage, unresolved and inferred relationship rates, indexing duration, navigation latency, Code Factory unsupported-path rate, evidence validity, stale-plan stops, reviewer-reported missed dependencies, and eventual build/test outcomes. These establish whether the integration improves generation rather than merely adding visualization.

## Recommended implementation starting point

Build one complete path first: add a GitHub repository to an application, index a pinned commit, explore Python/JS/TS dependencies, link its existing Knowledge sources, and use a selected file's dependency neighborhood in a Code Factory design. This validates the data model and user value before broadening languages, exports, providers, and coordinated writes.
