# Digital Brain: graph generation and security
Prepared 12 October 2026. Repository-based teaching material. All DEMO examples are synthetic.
Open `index.html` for the offline presentation. Arrow keys navigate; N toggles speaker notes; T toggles technical detail. The two companion diagrams support exploration and export.

## 01. From documents to a graph you can explain
Digital Brain connects information, keeps the supporting evidence, and checks it again when someone asks a question.

**Say this:** Imagine a college library. A document is a book; the graph is an index connecting ideas across books. The index helps us find a passage, but the passage is still the evidence. Today we will follow one source from intake to a published graph and then to a cited answer. This walkthrough covers the Knowledge graph; the Code Graph and ServiceOps maps are separate features.

**Technical detail:**
- Repository-grounded walkthrough, prepared 12 October 2026. The examples are synthetic teaching data.
- Suggested delivery: 25–30 minutes plus a 5-minute application demo. The walkthrough works offline; no account or provider key is required.

## 02. Three graphs, with different jobs
Knowledge, Code, and Operations graphs organize different evidence. Graphify contributes selected algorithms; it does not generate every graph.

**Say this:** Start with the product tabs, then name the modules. The Knowledge graph answers where a document claim came from. The Code Graph answers which files and symbols connect. The Operations graph organizes incident and change records. These are distinct implementations with different update behavior. The bridge records evidence-backed references; a matching file path does not prove that a requirement is correctly implemented.

**Technical detail:**
- KnowledgeGraph/GraphRevision persist application-scoped graph JSON and quality data; publication gates graph answers.
- CodeRepository/CodeSnapshot/CodeFile/CodeRelationship preserve repository snapshots. Ingestion combines the in-house analyzer and selected Graphify AST results.
- OperationsGraph/OpsNode/OpsEdge are built from records by rules and refresh when their input fingerprint changes. They do not use an AI extractor to construct those links.
- code_knowledge.links derives a read-only projection from explicit file paths and shared identifiers; it does not mutate either stored graph.

**Repository evidence:**
- [platform_core/graphs.py:66](C:/Workspace/digitalbrian/platform_core/graphs.py:66) — `build_graph`
- [platform_core/code_graph_ingest.py:1](C:/Workspace/digitalbrian/platform_core/code_graph_ingest.py:1)
- [platform_core/ops_graph.py:527](C:/Workspace/digitalbrian/platform_core/ops_graph.py:527) — `rebuild`
- [platform_core/code_knowledge.py:64](C:/Workspace/digitalbrian/platform_core/code_knowledge.py:64) — `links`

## 03. Graphify has two specific jobs here
The installed Python distribution is graphifyy; application code imports it as graphify. Its use is local analysis and clustering in these paths.

**Say this:** Say the package and import names distinctly: install graphifyy, import graphify. The Code Graph uses Graphify's Tree-sitter pass on already-read source text in two temporary directories. It does not hand Graphify the entire checkout or git metadata. Knowledge graph themes use a different Graphify module for clustering. Clustering groups existing links; it does not create new facts or call the extraction model.

**Technical detail:**
- code_graph_graphify.analyse calls graphify.extract.extract with parallel=False, maps accepted relation kinds, and keeps scratch sources/cache separate from the checkout.
- The native analyzer keeps Python and JS-family symbol/import handling; Graphify supplies additional language symbols and cross-file call/inheritance edges. Failure marks the code snapshot partial rather than discarding all native results.
- graph_quality.communities calls NetworkX Graph, graphify.cluster.cluster, and label_communities_by_hub; themes also calls cohesion_score. The Leiden backend dependency is graspologic-native.
- Observed local versions on 12 October 2026: graphifyy 0.9.67, NetworkX 3.7, graspologic-native 1.3.1. These are environment observations, not permanent version guarantees.

**Repository evidence:**
- [platform_core/code_graph_graphify.py:75](C:/Workspace/digitalbrian/platform_core/code_graph_graphify.py:75) — `analyse`
- [platform_core/code_graph_ingest.py:1](C:/Workspace/digitalbrian/platform_core/code_graph_ingest.py:1)
- [platform_core/graph_quality.py:133](C:/Workspace/digitalbrian/platform_core/graph_quality.py:133) — `communities`
- [platform_core/graph_quality.py:162](C:/Workspace/digitalbrian/platform_core/graph_quality.py:162) — `themes`
- [pyproject.toml:1](C:/Workspace/digitalbrian/pyproject.toml:1)

## 04. Graph RAG describes the answer pipeline
Retrieve evidence from a published graph, verify it, then give the verified context to the configured answer model.

**Say this:** RAG means retrieval-augmented generation. Here Graph RAG is the application's own graph-backed retrieval and answer flow. It does not mean that Microsoft GraphRAG, Neo4j, or LangChain is powering this code path. A structural-only graph can still support Graph answer mode if graph retrieval is configured; semantic enrichment and answer generation are different settings. In streaming mode the provider runtime can use scoped tools, and final citations are checked again.

**Technical detail:**
- workbench.answer_question is the synchronous path: Sources shows readable evidence, AI calls answer_with_ai, and Graph answer obtains graph_citations then invokes the graph_retrieval purpose.
- workbench.answer_worker seeds streaming with graph citations in graph mode; ai.stream_chat_answer dispatches to agent_runtime/openai_runtime.py or claude_runtime.py with a user/application-bound ToolScope and CitationRecorder.
- graph_ai.citations_from uses ranking.py for BM25, embeddings.py for cosine similarity and rank fusion, and typed walk expansion. Optional rerank.py reorders already-verified evidence and falls back on failure.
- The inspected paths implement the Graph RAG pattern directly. No dedicated Microsoft GraphRAG pipeline or graph database query is invoked here; graph JSON is stored through Django models.

**Repository evidence:**
- [platform_core/workbench.py:686](C:/Workspace/digitalbrian/platform_core/workbench.py:686) — `answer_question`
- [platform_core/workbench.py:1153](C:/Workspace/digitalbrian/platform_core/workbench.py:1153) — `answer_worker`
- [platform_core/ai.py:491](C:/Workspace/digitalbrian/platform_core/ai.py:491) — `stream_chat_answer`
- [platform_core/graph_ai.py:693](C:/Workspace/digitalbrian/platform_core/graph_ai.py:693) — `citations_from`
- [platform_core/rerank.py:42](C:/Workspace/digitalbrian/platform_core/rerank.py:42) — `configuration`
- [platform_core/agent_runtime/tools.py:196](C:/Workspace/digitalbrian/platform_core/agent_runtime/tools.py:196) — `verify_citations`

## 05. Name the technology beside its purpose
The implementation combines Python/Django modules, selected graph algorithms, optional provider SDKs, and a browser canvas.

**Say this:** Explain each technology through a job, not just a logo. Django provides server-side application structure and authorization hooks; it is not the graph algorithm. Graphify supplies selected code extraction and clustering. NumPy performs vector math; vectors are ranking signals. The graph canvas is application JavaScript implementing a d3-force-style model, not a claim that the D3 JavaScript package is loaded. Security modules enforce the boundaries described later in the talk.

**Technical detail:**
- pyproject.toml declares the direct package requirements. NetworkX and Tree-sitter are reached through Graphify's dependency stack; installed local versions are documented in the technology map.
- Graph visuals use static/graph.js with Canvas, Barnes-Hut repulsion, link forces, and collision handling. The stored graph determines edges; layout creates no new evidence.
- policy.py: application access; permissions.py: optional source-system answer permissions; sensitivity.py: restricted source eligibility; secrets.py: credential files; budget.py: configured spend checks.
- The technology map distinguishes direct dependencies, transitive algorithms, built-in Python modules, optional provider features, and deployment components.

**Repository evidence:**
- [pyproject.toml:1](C:/Workspace/digitalbrian/pyproject.toml:1)
- [digitalbrain/settings.py:1](C:/Workspace/digitalbrian/digitalbrain/settings.py:1)
- [static/graph.js:1](C:/Workspace/digitalbrian/static/graph.js:1)
- [platform_core/ranking.py:1](C:/Workspace/digitalbrian/platform_core/ranking.py:1)
- [platform_core/embeddings.py:1](C:/Workspace/digitalbrian/platform_core/embeddings.py:1)
- [platform_core/policy.py:1](C:/Workspace/digitalbrian/platform_core/policy.py:1)

## 06. A graph is a set of things and relationships
A node names a thing. An edge says how two things connect. Provenance explains where that edge came from.

**Say this:** Read this aloud: ticket DEMO-101 violates requirement REQ-7; REQ-7 is implemented in a code file. This is a teaching example of allowed types and relationships. An arrow does not prove the implementation is correct. It records a claim backed by a source quote. Distinguish the real-world fact from the graph's representation of the source's statement.

**Technical detail:**
- Nodes have stable application-scoped IDs. Structural IDs derive from SHA-256 over application namespace, kind, and identity.
- Edges retain source and target IDs, relation, knowledge_id, digest, line, and evidence. AI-derived relationship edges also carry inferred=True.
- The graph is persisted as JSON data in GraphRevision in the relational database; this implementation does not require a dedicated graph database.

**Repository evidence:**
- [platform_core/graphs.py:66](C:/Workspace/digitalbrian/platform_core/graphs.py:66) — `build_graph`
- [platform_core/models.py:1647](C:/Workspace/digitalbrian/platform_core/models.py:1647) — `GraphRevision`

## 07. Generation has a deliberate release step
Building a graph creates a draft. Review and publication decide which snapshot graph-based answers can use.

**Say this:** Use the diagram to tell the whole story before opening the code. The structural-only path skips paid AI extraction. A successful save is not the same as publication. Graph answers read a deliberately published snapshot, while live source checks prevent old or removed evidence from being cited.

**Technical detail:**
- Generation status: queued → building → ready, with stage updates for reading, optional extraction, optional embeddings, and saving. Failures set failed with a sanitized reason.
- claim_generation uses a conditional update to avoid duplicate claims. rebuild checks source/configuration fingerprint before committing the revision.
- Generated graph versions start unpublished. Publication is an explicit write action and refuses versions whose recorded source evidence has drifted.

**Repository evidence:**
- [platform_core/graphs.py:586](C:/Workspace/digitalbrian/platform_core/graphs.py:586) — `claim_generation`
- [platform_core/graphs.py:222](C:/Workspace/digitalbrian/platform_core/graphs.py:222) — `rebuild`
- [platform_core/graphs.py:377](C:/Workspace/digitalbrian/platform_core/graphs.py:377) — `publish_revision`

## 08. Files become local, bounded Markdown
The generator reads normalized knowledge entries. It does not ask an AI model to open arbitrary uploaded files.

**Say this:** Demonstrate an uploaded text document moving through the Sources screen. The original bytes are stored privately. Before conversion the code checks whether those bytes still match the recorded hash. Production requires a working scanner with current signatures. Conversion gets a local stream, not a URL, and plugins are disabled. Image-only PDFs need OCR; this pipeline reports that limitation rather than pretending it extracted the page.

**Technical detail:**
- Upload limit: 20 MiB. Conversion timeout: 45 seconds. Maximum accepted Markdown: 1,000,000 characters. Archive member/expanded-size and PDF page/encryption checks are implemented.
- ClamAV invocation has a 30-second timeout and rejects stale signatures; scanner failures leave the stored file private.
- The converter disables plugins and patches Python socket connect/connect_ex. This is a bounded subprocess, not a claim of complete OS/container isolation.
- KnowledgeEntry stores a content SHA-256 and suggested personal-data findings. Sensitivity classification remains a person's decision.

**Repository evidence:**
- [platform_core/documents.py:236](C:/Workspace/digitalbrian/platform_core/documents.py:236) — `store_document`
- [platform_core/processing.py:59](C:/Workspace/digitalbrian/platform_core/processing.py:59) — `process_document`
- [scripts/extract_text.py:15](C:/Workspace/digitalbrian/scripts/extract_text.py:15) — `extract`
- [platform_core/workbench.py:62](C:/Workspace/digitalbrian/platform_core/workbench.py:62) — `add_knowledge`

## 09. A shared value creates a visible connection
No model is needed to connect a document to its rows, or a row to its field values.

**Say this:** Show the two-row table. The graph builds one document node, two record nodes, and three value nodes: Node:NODE1, Node:NODE2, and Queue:Q1. It creates six edges: two containment links plus two field-value links per row. Q1 is reused because its field and value identity match. The diagram lists the queue links for clarity; the Node field links are also stored. A prose line becomes a section node attached to its document.

**Technical detail:**
- build_graph parses Markdown table headers and rows. Each record is keyed by source and line; value identity includes the case-folded field name and value.
- Nonempty non-table lines produce section nodes. The label is bounded; the edge retains its source evidence and line number.
- Empty cells, duplicate rows, omitted content, and isolated nodes are counted. The structural parser does not infer domain relationships.

**Repository evidence:**
- [platform_core/graphs.py:60](C:/Workspace/digitalbrian/platform_core/graphs.py:60) — `cells`
- [platform_core/graphs.py:66](C:/Workspace/digitalbrian/platform_core/graphs.py:66) — `build_graph`

## 10. The model proposes; the server decides
A requested AI run adds typed relationships to the existing structural graph. The model's JSON is treated as a proposal.

**Say this:** First show the valid relationship. Then change the quote to a sentence that never appeared in the source: it is rejected. Then change the subject type to Person: it is outside the ontology. This is a teaching simulation of the validation rules, not an actual provider call. Passing the checks proves quote and type consistency; it does not establish that the model understood every nuance of the sentence.

**Technical detail:**
- Only explicitly requested AI generation uses the selected provider/model. Automatic structural maintenance does not pick up a stale paid-model request.
- Extraction happens per source, with a bounded JSON relationships array. The prompt explicitly treats source text as untrusted data and forbids inferring missing facts.
- Malformed JSON or provider failure can stop the generation run. The existing published version remains available; the UI surfaces the sanitized failure.

**Repository evidence:**
- [platform_core/graphs.py:222](C:/Workspace/digitalbrian/platform_core/graphs.py:222) — `rebuild`
- [platform_core/graph_ai.py:75](C:/Workspace/digitalbrian/platform_core/graph_ai.py:75) — `extract_relationships`
- [platform_core/graph_ai.py:154](C:/Workspace/digitalbrian/platform_core/graph_ai.py:154) — `judge`

## 11. Six questions before an AI edge is kept
These checks are code, not just instructions in a prompt.

**Say this:** Ask students why each check exists. An unknown source is a provenance failure; an absent quote is unsupported evidence; a bad type pair is an invalid relationship in our vocabulary. The subject has one controlled exception: a Ticket, Runbook, Incident, or Change may name the source itself by its verified title or key. The object still has to appear in the quote. Quote matching is case-sensitive; entity presence checks are case-folded.

**Technical detail:**
- judge returns either canonical (subject type, relation, object type), or an explicit rejection reason. Maximum quote length is 300 characters; subject/relation/object names are bounded to 120 characters.
- Rejection counts and reasons are stored in quality data. A discarded proposal is not silently presented as a valid edge.
- Exact-quote presence is not a semantic entailment proof: polarity, ambiguity, and incorrect statements in the source still require review.

**Repository evidence:**
- [platform_core/graph_ai.py:154](C:/Workspace/digitalbrian/platform_core/graph_ai.py:154) — `judge`
- [platform_core/graph_ai.py:251](C:/Workspace/digitalbrian/platform_core/graph_ai.py:251) — `enrich_graph`

## 12. A small vocabulary makes paths understandable
Consistent types and relationship names let a ticket connect to a requirement and then to code.

**Say this:** An ontology is a shared vocabulary, like a course glossary with grammar rules. We do not allow any relation between any two types. The same registry writes the extraction prompt and checks the response. Name merging is conservative: same type and normalized or sufficiently similar word-by-word names, with numeric identifiers preserved. Similar names are a heuristic, not absolute proof of identity.

**Technical detail:**
- ontology.py defines eight types and the canonical relationship registry with allowed endpoint types and aliases.
- Normalization handles case, accents, punctuation, separators, and a leading 'the'. Matching may stem words or accept long near-identical words using Jaro-Winkler ≥0.93; shorter words and numbers have stricter rules.
- Merged entities preserve aliases and contributing source IDs. Every edge keeps its own provenance and remains independently checked.

**Repository evidence:**
- [platform_core/ontology.py:183](C:/Workspace/digitalbrian/platform_core/ontology.py:183) — `typed`
- [platform_core/ontology.py:225](C:/Workspace/digitalbrian/platform_core/ontology.py:225) — `normalise`
- [platform_core/ontology.py:267](C:/Workspace/digitalbrian/platform_core/ontology.py:267) — `merge_entities`

## 13. Reuse unchanged work; save a new draft
Digests detect changed content. Fingerprints describe the graph-generation input. A saved revision is a stable snapshot.

**Say this:** Explain the difference between a digest and a fingerprint. A source digest describes its content; the generation fingerprint includes the source set and generation settings. Rebuilding a ready unchanged graph is a no-op. AI reuse requires matching source ID and digest under the same ontology. The transaction checks whether the input changed while the worker was running, so it does not commit a snapshot based on outdated input.

**Technical detail:**
- KnowledgeGraph holds the current build state. GraphRevision saves numbered data/quality, provider/model, and fingerprint for that application.
- reusable reads the latest AI revision with matching ontology, retains completely processed unchanged sources, and carries forward their entity nodes and edges.
- The current reuse check is based on source ID/digest and ontology; it is not a guarantee that every model or prompt change automatically re-extracts all unchanged sources.
- The standard after-save benchmark reads stored information and calls no model. Benchmark failure cannot roll back a successfully saved revision.

**Repository evidence:**
- [platform_core/graphs.py:41](C:/Workspace/digitalbrian/platform_core/graphs.py:41) — `fingerprint`
- [platform_core/graphs.py:222](C:/Workspace/digitalbrian/platform_core/graphs.py:222) — `rebuild`
- [platform_core/graph_ai.py:194](C:/Workspace/digitalbrian/platform_core/graph_ai.py:194) — `reusable`
- [platform_core/benchmark.py:67](C:/Workspace/digitalbrian/platform_core/benchmark.py:67) — `score`

## 14. Embeddings choose where to look
Word matching and optional meaning matching rank candidate evidence. A vector itself is never a citation.

**Say this:** Use two differently worded questions to explain meaning search, without promising that it will always find the answer. This implementation uses optional OpenAI embeddings, 512-dimensional unit vectors, cosine similarity, and reciprocal rank fusion. AI generation can index relationships and passages if embeddings are configured. Search can still use words when no new embeddings are available.

**Technical detail:**
- Embeddings are opt-in and content-addressed, so stored unchanged texts are reused. Question embeddings can incur a provider request when a question is asked.
- Retrieval uses BM25 covering ranks, optional nearest-vector candidates, and bounded typed multi-hop/one-hop expansion. Direct candidates are considered ahead of multi-hop finds.
- The in-process NumPy search is exact cosine similarity over stored vectors; this is not a claim that a managed vector database is deployed.
- If embedding indexing fails, graph relationships can still be saved with a warning; word ranking and available existing vectors remain usable.

**Repository evidence:**
- [platform_core/embeddings.py:61](C:/Workspace/digitalbrian/platform_core/embeddings.py:61) — `configuration`
- [platform_core/embeddings.py:89](C:/Workspace/digitalbrian/platform_core/embeddings.py:89) — `request`
- [platform_core/graph_ai.py:693](C:/Workspace/digitalbrian/platform_core/graph_ai.py:693) — `citations_from`

## 15. Measure coverage, evidence, and retrieval separately
A connected graph can still retrieve the wrong source. A quoted relationship can still need semantic review.

**Say this:** Open the redesigned Quality tab during the demo. Read the highest-priority issue first, then explain the four indicators. A benchmark question has an expected source, and the system checks whether that source appears in the retrieved citations. It measures retrieval, not correctness of a model-written final answer. No test questions means unknown retrieval quality, not zero accuracy and not perfect accuracy.

**Technical detail:**
- health_report computes components, orphans, evidence bands, and structural issues. at_a_glance and quality_overview turn those signals into readable priorities.
- Benchmark outcomes include expected-source retrieval, top-three rank, and ranking quality. The ordinary automatic benchmark uses a corpus-wide measurement viewer, not an end-user permission probe.
- Human review remains necessary for misleading source statements, extraction nuance, merging errors, and omitted content.

**Repository evidence:**
- [platform_core/graph_quality.py:203](C:/Workspace/digitalbrian/platform_core/graph_quality.py:203) — `health_report`
- [platform_core/graph_quality.py:344](C:/Workspace/digitalbrian/platform_core/graph_quality.py:344) — `quality_overview`
- [platform_core/benchmark.py:67](C:/Workspace/digitalbrian/platform_core/benchmark.py:67) — `score`

## 16. Old evidence is checked against today's source
A published version is stable. Its citations must still pass current source and access checks.

**Say this:** Click Source edited. Even if a familiar sentence still exists, a changed digest prevents this old edge being used as fresh evidence. Click Restricted: the source is removed from answerable querysets immediately, including citations from earlier published versions. Click Viewer denied: an enforced source permission list filters the result. This is an educational simulation; do not change real records to demonstrate these states.

**Technical detail:**
- graph_snapshot chooses the newest published revision or a requested published number. It refuses unpublished graph versions for graph answering.
- citations_from builds a live active-source map, applies permitted(viewer), then checks source digest and quote presence. It skips candidates that fail.
- A stale published graph is not entirely discarded solely because another source changed; still-valid relationships may answer. New source content is not magically added to the old snapshot.

**Repository evidence:**
- [platform_core/graph_ai.py:429](C:/Workspace/digitalbrian/platform_core/graph_ai.py:429) — `snapshot_revision`
- [platform_core/graph_ai.py:693](C:/Workspace/digitalbrian/platform_core/graph_ai.py:693) — `citations_from`
- [platform_core/permissions.py:66](C:/Workspace/digitalbrian/platform_core/permissions.py:66) — `permitted`

## 17. Identity, application scope, and evidence checks
Security is enforced in several places along the data path, not in the graph picture alone.

**Say this:** A platform administrator does not automatically gain application knowledge access. The application grant and organization membership are explicit. A reader may view allowed features; owners and contributors may write within the feature rules. Calls are bound to an application and user, and expensive AI calls recheck access after the provider returns. Avoid saying 'the model handles access control': the server does.

**Technical detail:**
- applications_for requires active user, active hierarchy, organization membership, and an application grant. application_for returns 404 for an inaccessible application; an owner-only action rejects a non-owner.
- workbench.access checks feature enablement and write roles. Graph generation invokes the purpose-specific permission checks in invoke_ai.
- Source-system permission lists are distinct from application grants. A missing viewer cannot cite an enforced record; failed Jira identity/access checks leave the record unallowed.

**Repository evidence:**
- [platform_core/policy.py:25](C:/Workspace/digitalbrian/platform_core/policy.py:25) — `applications_for`
- [platform_core/policy.py:41](C:/Workspace/digitalbrian/platform_core/policy.py:41) — `application_for`
- [platform_core/workbench.py:42](C:/Workspace/digitalbrian/platform_core/workbench.py:42) — `access`
- [platform_core/ai.py:278](C:/Workspace/digitalbrian/platform_core/ai.py:278) — `invoke_ai`

## 18. Know what leaves the application
Structural generation and conversion are local. Optional model and embedding calls send selected text to their configured external providers.

**Say this:** Point to the trust boundary. Enabling AI is a data-flow decision: the provider receives selected source text and relevant requests. Credentials are read server-side from files; they are not stored as plaintext database fields. The database still contains knowledge content, graph evidence, and vectors. Do not promise zero data retention or no training by the provider without checking the actual provider agreement and configuration.

**Technical detail:**
- Application-scoped provider credentials are resolved server-side from mounted or managed secret files. Managed credential metadata stores a salted digest, not the redisplayed secret value.
- Operator secrets take precedence over managed secrets. Optional shared connector credentials are a documented development/demo convenience; they are not shared model-provider credentials.
- Provider, model, request ID, token counts, and estimated cost are recorded as AIUsage. Budgets are enforced only when an application budget is configured.
- At-rest encryption, key management, backup access, retention policy, and external-provider contractual guarantees require deployment evidence; this walkthrough does not claim them.

**Repository evidence:**
- [platform_core/ai.py:162](C:/Workspace/digitalbrian/platform_core/ai.py:162) — `provider_credential`
- [platform_core/ai.py:278](C:/Workspace/digitalbrian/platform_core/ai.py:278) — `invoke_ai`
- [platform_core/secrets.py:205](C:/Workspace/digitalbrian/platform_core/secrets.py:205) — `application_secret`
- [platform_core/budget.py:72](C:/Workspace/digitalbrian/platform_core/budget.py:72) — `check`

## 19. Treat files, links, and model output as untrusted
Bounded processing and server-enforced checks reduce exposure; they do not make arbitrary content harmless.

**Say this:** Explain SSRF as a server being tricked into fetching a private address. The fetcher checks all resolved addresses and pins the connection to the checked address, while preserving hostname certificate verification for HTTPS. Operators may explicitly allow internal hosts. Prompt injection is handled as untrusted source text plus validation, but a prompt instruction is not a complete defense by itself. Show security settings as implemented code, not as a certification claim.

**Technical detail:**
- Link fetches are bounded to 8 MiB and 20 seconds, with a 5-second per-address connection timeout. Private/reserved destinations are blocked unless the operator allowlists that hostname.
- Production enables HTTPS redirect, secure session/CSRF cookies, HSTS, and PostgreSQL verify-full TLS. These require a correctly configured deployment; the local HTTP demo is not evidence of production TLS.
- Conversion is not a full OS sandbox. Malware scanning depends on installed scanner/signatures. Automated safeguard tests are useful probes, not proof against every attack.

**Repository evidence:**
- [platform_core/fetching.py:110](C:/Workspace/digitalbrian/platform_core/fetching.py:110) — `resolve`
- [platform_core/processing.py:59](C:/Workspace/digitalbrian/platform_core/processing.py:59) — `process_document`
- [digitalbrain/settings.py:1](C:/Workspace/digitalbrian/digitalbrain/settings.py:1)
- [platform_core/middleware.py:59](C:/Workspace/digitalbrian/platform_core/middleware.py:59) — `ResponsePolicyMiddleware`

## 20. Explain the limits as clearly as the features
Students should understand what the product verifies, what remains heuristic, and what depends on deployment.

**Say this:** This is the most important honesty slide. An answer permission boundary is narrower than an explorer confidentiality boundary. An application member may still see imported records in the Sources and graph screens. Restricted means excluded from AI use and citations, not deleted from storage or hidden from application members. A published version is not a secure erase mechanism. If your audience asks about regulated deployment, explain that legal, contractual, infrastructure, and operational checks are separate work.

**Technical detail:**
- permissions.py documents the UI/explorer boundary and supports only Jira for optional source permission enforcement. The normal recheck window is 24 hours, performed during sync; this is not an immediate live-per-question Jira ACL query.
- Generation selects application-level active, non-restricted sources; it does not build a separate permission-filtered graph for each viewer. Answer-time permission checks must not be described as full source ACL isolation of generation/enrichment or the explorer.
- sensitivity.scan checks a bounded set of patterns over up to 200,000 characters and leaves labeling to a human.
- The corpus-wide benchmark's EVERYONE viewer deliberately ignores viewer-specific permission lists to measure retrieval; it does not prove that a particular end user may access every benchmark source.

**Repository evidence:**
- [platform_core/permissions.py:1](C:/Workspace/digitalbrian/platform_core/permissions.py:1)
- [platform_core/sensitivity.py:62](C:/Workspace/digitalbrian/platform_core/sensitivity.py:62) — `scan`
- [platform_core/benchmark.py:67](C:/Workspace/digitalbrian/platform_core/benchmark.py:67) — `score`

## 21. Generation is bounded and can fail visibly
A graph may omit content at a configured limit. Quality warnings and retrieval tests help expose that gap.

**Say this:** Tell the audience that bigger does not mean complete. A source can be read while some of its rows were omitted. That is why both source coverage and content coverage matter. The current limits are implementation choices, not throughput measurements or an SLA. A stalled build is treated as interrupted after its inactivity window; a previous published version is not automatically erased by a failed new build.

**Technical detail:**
- The stall window is derived as extraction timeout / 60 + 5 minutes, currently 15 minutes. Duplicate generation claims are refused while a run is live.
- Embedding failure is nonfatal and warns. A budget interruption may retain partial AI work when there is already usable extraction; initial failure can stop the run.
- The graph explorer uses a browser canvas force simulation. Layout distance and cluster appearance do not prove relationship strength or causality.

**Repository evidence:**
- [platform_core/graphs.py:567](C:/Workspace/digitalbrian/platform_core/graphs.py:567) — `run_in_flight`
- [platform_core/graph_ai.py:251](C:/Workspace/digitalbrian/platform_core/graph_ai.py:251) — `enrich_graph`
- [platform_core/embeddings.py:1](C:/Workspace/digitalbrian/platform_core/embeddings.py:1)
- [static/graph.js:1](C:/Workspace/digitalbrian/static/graph.js:1)

## 22. A five-minute demo that tells the whole story
Use synthetic documents and a prepared published graph so the explanation does not depend on a paid provider responding live.

**Say this:** Prepare the app before presenting: add only synthetic data, generate once, add benchmark questions, and publish the reviewed version. During the demo, explain any pre-generated artifact honestly. If AI is unavailable, show structural generation and use the proposal simulation to teach the enrichment gate. Never display real secrets or sensitive records. Close by asking: what evidence would convince you this answer is correct?

**Technical detail:**
- Suggested synthetic text: 'DEMO-101 violates REQ-7. REQ-7 is implemented in services/export.py.' Add a separate specification sentence explaining what REQ-7 requires.
- Use a benchmark question such as 'Which requirement does DEMO-101 violate?' with the intended source title as the expectation. This teaches retrieval measurement, not guaranteed model answer correctness.
- For questions: a graph organizes knowledge; a model proposes or explains; verification preserves the connection to the source. AI is optional for structure, publication governs graph answers, and deployment determines several security guarantees.

**Repository evidence:**
- [platform_core/graphs.py:706](C:/Workspace/digitalbrian/platform_core/graphs.py:706) — `graph_view`
- [platform_core/benchmark.py:67](C:/Workspace/digitalbrian/platform_core/benchmark.py:67) — `score`

## Questions students may ask

**Why a graph instead of ordinary search?** Word search finds matching passages. A graph also records explicit relationships and can walk from a ticket to a requirement and then to code. This implementation combines ranking with graph expansion; it does not assume the graph is always better. Benchmark questions measure retrieval outcomes.

**Is this retrieval-augmented generation (RAG)?** Graph-based citation retrieval can supply verified evidence to a configured answer model. That is a retrieval-augmented answer flow. Graph construction, retrieval, and answer wording are distinct stages; creating the graph does not train a new model.

**Does a SHA-256 hash protect confidentiality?** No. Here it detects content changes and provides stable identity inputs. Encryption and access controls solve different problems.

**Can an AI relationship still be wrong after validation?** Yes. A quote can exist while the proposed relation misreads it, or the source itself can be wrong. Exact-quote and type checks reduce unsupported output; they do not replace semantic review.

**Will a restricted record disappear everywhere?** No. The restriction keeps it out of AI use and citations. It remains stored and visible through application-scoped source access. Source-system answer permissions are also narrower than an explorer confidentiality boundary.

**Do providers train on these documents?** This repository shows what text is sent and which provider is used. Training, retention, data residency, and contractual assurances must be checked against the actual configured service and agreement. Do not infer them from the code.

**What if the source changes after publication?** The old revision stays a snapshot. An edge whose digest or exact evidence no longer matches is skipped at citation time. A new reviewed and published version brings in new content.

**Does 100% retrieval mean the application is accurate?** No. It means the expected source was retrieved for the configured test questions under that measurement setup. It does not measure every possible question, final answer correctness, or each user's permission outcome.

**How are the moving nodes positioned?** The browser canvas uses a force simulation to make the graph navigable. Screen distance, motion, and cluster appearance do not add facts to the stored graph.
