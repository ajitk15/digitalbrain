# Detailed graph generation and retrieval architecture

Design contract • Each application has an independent instance of this flow

[Generation diagram](graph-generation.html) · [Retrieval diagram](graph-retrieval.html)

## Graph generation: from a document to persistent, queryable knowledge

The upload is not the graph. The system produces several durable artifacts in sequence, retaining the ability to trace every extracted claim back to its source and to regenerate derived data. A background workflow tracks each stage and never exposes a half-built graph.

### Stage 1 — Bind the input to an application

The ingestion API verifies the user's organization membership, application grant and document-write permission. It creates an Upload/Document record with organization, application, document ID, owner, classification, intended specification status and upload limits. It issues a short-lived upload target within that application's quarantine container.

The client cannot change the application by editing the storage key or completion request. The service independently verifies the created record and received object. Retries preserve the same upload identity; duplicate detection is application-local and must not reveal that a document exists elsewhere.

### Stage 2 — Preserve and normalize evidence

A sandbox verifies content type, archive boundaries, malware status and checksum. MarkItDown converts accepted formats. OCR/vision runs only when required and permitted; the workflow flags unsupported, encrypted or incomplete inputs. The original remains immutable so the user can inspect extraction errors.

Output consists of the original version, normalized Markdown, extracted assets, conversion report and source map. A Markdown span points to the original page/section when that mapping is available; otherwise the UI truthfully cites the available document/heading reference. Do not invent page numbers. Store converter version and configuration with the artifact.

### Stage 3 — Create source units

Split Markdown using headings, paragraphs, tables and code-block boundaries. Assign stable version-specific chunk IDs. Retain heading ancestry and neighboring chunk links so a rule and its exception can be read together. Preserve table structure instead of separating headers from values. The required chunk sizes are benchmark choices, not hardcoded design claims.

A SourceChunk contains organization/application, document version, chunk ID, text object reference, source span, heading path, classification, effective ACL and checksum. The hierarchy also supports later NaviRAG evaluation. Changes create new versions; they do not mutate the evidence underlying an already approved factory plan.

### Stage 4 — Dispatch a scoped extraction job

The durable orchestrator selects a pinned corpus snapshot and ontology, applies application limits, and resolves the selected generation/coordinator model profile and dispatches a compatible SDK runtime. Its job contract contains input references, ontology/extractor/model versions, application capability, budget, timeout and permitted tool list. The SDK loop can coordinate semantic extraction but cannot change application context or override job policy.

The coordinator assigns a specialist with its own selected compatible model/runtime profile to propose entities and relationships from approved chunks. Requests use typed outputs: entity candidates, aliases, relationship candidates, exact evidence spans, uncertainty and unresolved ambiguities. Invalid output is rejected or repaired within a bounded retry budget. Unsupported claims do not become accepted facts.

### Stage 5 — Combine semantic and structural graph candidates

The Graphify adapter normalizes graph construction/extraction outputs into the SaaS schema. The desired integration combines document-derived concepts with structural code evidence from an allowed repository snapshot: symbols, imports, calls and tests. The adapter contract, including whether Graphify accepts supplied semantic candidates or instead owns extraction and exports them, must be verified against the selected release. Both paths must produce the same validated candidate format; the architecture does not assume an undocumented Graphify API.

The diagram shows the logical flow of claims through this boundary, not a claim that the two SDKs are natively built into Graphify. Structural parsing may execute alongside semantic extraction under the same application job. Only the application's code snapshot/path allowlist is available. Code parsing does not need to convert the source repository into Markdown first.

Example source sentence: “Refunds above the approved limit require a manager's authorization.” Candidate entities might be Refund and ApprovalRequirement. The extractor must retain the condition and source span; it must not invent the numeric limit, responsible service or implementing function. An implementation link requires separate code evidence and review.

### Stage 6 — Resolve identity without losing provenance

Map candidates to application-local canonical IDs using authoritative identifiers first, then approved aliases and conservative matching. Repository symbols use repository/path/symbol identity with commit-specific evidence. Do not merge similarly named systems using a confidence score alone.

Represent evidence assertions separately from canonical entities. An assertion has subject, predicate, object or literal condition, source spans, extraction method/version, confidence, review state and valid time. Conflicting assertions remain distinguishable and can be marked superseded or unresolved. Every endpoint, source and derived property must match the current application.

### Stage 7 — Verify quality and request improvements

Deterministic checks cover schema, endpoint existence, application identity, source references, ACL inheritance and forbidden relationships. Semantic checks assess whether the source supports the assertion and whether important qualifiers survived extraction. Compare against an application-specific labeled set to measure coverage and precision; sample human review by source type and risk.

Quality findings identify the failed rule, affected fact, evidence, responsible owner and requested input. Examples include an unreadable table, missing glossary, ambiguous alias, contradictory specification or absent test mapping. The owner supplies the missing source or correction; the workflow rebuilds affected assertions and reruns the checks. Unresolved security/provenance failures block publication.

A graph can be useful without every possible fact being extracted. Published quality reports expose known limitations instead of asserting completeness. Document approval and graph-quality approval are separate: a well-extracted draft specification is still a draft.

### Stage 8 — Persist application projections

Write a new private graph version containing canonical entities, evidence assertions and source references. Persist a reproducible graph export and its quality report in the application object store. The graph database serves bounded traversal; originals and Markdown remain in object storage. Metadata and release manifests live in the application-authorized metadata service.

Index the allowed evidence text and entity descriptions for lexical/vector discovery. These indexes point back to source IDs and graph entity IDs; embeddings do not become authoritative facts. An optional NaviRAG hierarchy is another versioned projection of the same source corpus. Vector indexing uses the source text, not just a stringified graph dump.

The diagram sequences graph and index readiness for clarity. Implementations may build projections concurrently, but publication waits for every enabled mandatory projection to be complete. If NaviRAG is not enabled, its absence cannot block the normal graph/vector path.

### Stage 9 — Publish atomically

A release manifest binds application, corpus versions, ontology, graph projection, index versions and quality report. After validating readiness, compare-and-swap the application's active version pointer. Readers pin the manifest for a request; they cannot mix a new graph with an old vector index.

There is no distributed transaction assumed across graph, object and vector stores. Build immutable versioned projections first, then switch one authoritative pointer. Failed candidates are marked failed and cleaned up according to retention. The previous valid version remains active.

Incremental updates follow source-to-assertion provenance. Remove assertions supported only by a retired source, preserve separately supported assertions, and refresh dependent embeddings/hierarchy summaries. Permission changes are enforced immediately by the access layer while projections are rebuilt. Rollback must not restore revoked access or deleted content.

## Graph retrieval: from a question to verified evidence

### Stage 1 — Establish a single application context

Chat or Code Factory submits a question/objective and the selected application. The gateway checks the principal, membership, explicit application grant and operation. It creates a short-lived read capability for that application and source permissions. The retriever never accepts an arbitrary database/collection name from the caller or model.

### Stage 2 — Pin the readable snapshot

Resolve the active manifest and check availability/freshness. A normal Chat query uses the active graph; a factory assessment uses the explicit approved specification and code baselines chosen for its run. The request logs which version it used. If a required source has been revoked, the live policy wins over the historical snapshot.

### Stage 3 — Plan the retrieval route

The router distinguishes exact lookup, explanatory passage retrieval, long-document navigation and relationship reasoning. Exact IDs go to exact/lexical lookup. Paraphrased questions can use vector search. Long-specification questions may use a configured NaviRAG navigator. Relationship questions start from matching entities and traverse the graph. These routes can combine, but they are budgeted and not all mandatory for every request.

Models may propose structured search arguments, not unrestricted database queries. A deterministic tool validates operation type, allowed relation types, application scope, result size and timeout. Do not execute arbitrary model-generated Cypher/SQL with broad credentials.

### Stage 4 — Find graph entry points

Resolve requirement IDs, ticket IDs, API names, aliases and code symbols. Vector search can rank matching entity descriptions or source chunks when names differ. Candidate results are restricted to the application and effective source ACL before they enter the model context. Ambiguous entity matches should be shown or resolved against evidence rather than silently choosing a similarly named component.

Traditional RAG may stop at relevant source chunks and bypass graph traversal. A NaviRAG route can navigate the application-private hierarchy to evidence and then use that evidence's entity links. Neither route can widen the application boundary.

### Stage 5 — Traverse typed relationships

A question about test coverage might follow Requirement ← IMPLEMENTS ← CodeSymbol and Requirement ← VERIFIES ← Test, using the ontology's canonical relation directions. The physical graph may materialize inverse indexes for efficient traversal; they do not change relationship meaning.

Apply a configured hop limit, maximum nodes/edges, relationship allowlist, cycle/visited handling, time budget, review/freshness policy and ACL check at every expansion. Bounded traversal protects relevance and cost. Empty permitted results mean “no supported answer found,” not permission to traverse another application.

A broad application audit must enumerate all in-scope approved requirements and record coverage, not inspect only the top-scoring graph neighborhood. Graph neighborhood retrieval provides evidence for each requirement; it cannot certify a complete audit by itself.

### Stage 6 — Hydrate source evidence

Graph assertions supply evidence IDs. The source service retrieves corresponding Markdown spans, original references and permitted code snippets at the pinned commit. Include nearby qualifiers when necessary without including inaccessible content. A graph label or confidence score alone is insufficient evidence for an answer.

Fetch only the minimum permitted excerpt for model context. A citation can link to a separately authorized source-view endpoint, which rechecks access when opened. The browser receives no permanent public storage URL.

### Stage 7 — Build a grounded context package

Deduplicate repeated passages, rerank by question relevance, and preserve separate contradictory evidence. Check that each assertion is supported, current enough and visible. Allocate a context budget across facts, source passages and paths; do not silently drop an exception that changes the answer.

A ContextBundle contains application, graph/source version tuple, permitted facts, paths, source spans, citation IDs, known conflicts, freshness and unanswered subquestions. This is the interface to Chat or Code Factory. It carries evidence rather than instructions copied from uploaded text.

### Stage 8 — Consume and verify

Chat generates an answer from the bundle, marks uncertainty and cites the original evidence. Code Factory uses the bundle to direct code inspection and tests, producing a deviation report with expected/observed behavior. A graph-only discrepancy remains a hypothesis until code/spec/test evidence supports it.

The final gate checks citation IDs resolve to accessible sources, current permissions still allow the response, and statements have support. Block or revise an unsupported answer. Cite the graph path when useful but also cite its underlying evidence. Record an audit event containing IDs and redacted metrics rather than raw confidential content in shared logs.

### Stage 9 — Explain and learn within the same application

Return the answer/findings, supporting sources, relevant relationship path, knowledge version and remaining gaps. A user can flag incorrect evidence or request missing information. This creates an application-local quality finding. It does not automatically alter authoritative requirements, approve code edits or change another application's knowledge.

## Illustrative evidence contract

For a refund feature, the UI could show: Requirement R-17 from specification version 3, section 4.2; implementation candidate authorizeRefund at the pinned repository commit; Test T-8; and an unresolved note that an exception table failed conversion. Chat can answer the supported part and state the missing exception evidence. Code Factory requests the corrected table before proposing a claim of full compliance or a fix that depends on it.

All names and IDs in this example are illustrative. No real document, graph or repository analysis has been performed.

## Model flexibility and developer access

[Model selection policy](model-selection.md) applies to every stage and individual agent: users select compatible profiles within application permissions, with explicit fallbacks and audited execution. Code Factory approval pins the model map; embedding changes rebuild the corresponding index. [Application REST and MCP access](developer-access.md) exposes the same authorized retrieval pipeline to developer code and coding assistants. External calls cannot cross application boundaries or bypass source permissions and Factory approval. The consolidated narrative includes both contracts in full.

