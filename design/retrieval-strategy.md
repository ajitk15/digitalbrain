# Retrieval design — Traditional RAG, NaviRAG and graph RAG

Decision narrative • 12 September 2026 • Applies separately inside every application

## Recommendation

Use the application's knowledge graph as the traceability layer and retain the original/Markdown evidence behind it. Add lexical and vector retrieval to locate relevant passages and graph entry points. Evaluate NaviRAG as an optional navigation strategy for long, structured specifications. Do not require every query to run every strategy.

These are complementary retrieval techniques, not three mutually exclusive databases. The routing and staged adoption below are proposed design choices; no benchmark has been run on this application's documents.

## What is actually stored?

Original documents preserve the uploaded source. Markdown makes extracted content easier to inspect, chunk and cite. A source map connects normalized text back to its original location. A graph records entities and relationships, such as requirement → implementing function → verifying test. A vector index stores numeric representations of text or entity descriptions so the system can retrieve similar meaning. A hierarchical navigation index organizes topics, sections and detailed evidence for a navigation-based retriever.

The graph and the indexes are derived representations. Neither replaces source evidence. Deleting or restricting a source must also remove or restrict its derived graph assertions, vectors, hierarchy summaries and cached answers.

A vector database is an infrastructure choice. Vector RAG is a retrieval-and-answering method. You can implement vector search using a database extension rather than operating a separate vector database product; pgvector, for example, provides vector similarity search in PostgreSQL. If selected here, use an application-isolated database/table and credentials consistent with the isolation design. [pgvector project](https://github.com/pgvector/pgvector).

## Traditional RAG, explained step by step

1. Convert and split application documents into chunks with source references.
2. Compute embeddings and index chunks; optionally add keyword search.
3. Embed the user's question and retrieve a small set of relevant chunks.
4. Rerank and supply the authorized passages to the language model.
5. Generate an answer with citations, or state that evidence is insufficient.

The original RAG research combines retrieval from an external corpus with generation, including dense vector retrieval. RAG in general is broader than vector retrieval: keyword, relational and graph retrieval can also supply evidence. [Original RAG paper](https://arxiv.org/abs/2005.11401).

For this application, vector retrieval is useful when a user asks “How are refunds authorized?” but the specification uses “approval rules for reimbursements.” Exact keyword search remains valuable for ticket IDs, API paths and code symbols. Vector similarity does not establish factual correctness, requirement coverage or whether code implements a business rule. Retrieved chunks may also miss a related exception several sections away.

Traditional RAG is the proposed Chat baseline because its indexing/query path is comparatively straightforward to operate and measure. That is a design expectation to test, not a guaranteed latency or cost result.

## NaviRAG, explained step by step

This comparison uses NaviRAG: **Towards Active Knowledge Navigation for Retrieval-Augmented Generation**. It is distinct from the embodied-navigation NavRAG paper.

The NaviRAG paper describes organizing knowledge hierarchically and using an agent to navigate from broad topics to detailed evidence while identifying information gaps. It reports improvements on its long-document QA evaluations; those results do not establish performance on our corpus. [NaviRAG paper](https://arxiv.org/abs/2604.12766).

A proposed integration for our application would:

1. Preserve document headings and build an application-private hierarchy of topics, sections and evidence records.
2. Inspect the question and choose a starting topic or section.
3. Read that level, identify missing details, and choose another branch or more specific evidence.
4. Continue within a bounded hop/token/time budget, recording the navigation path.
5. Return the underlying source passages and citations for answering.

This integration is an architectural adaptation to evaluate, not a claim that reading headings alone reproduces the research system. Pin the paper/code version and reproduce the intended indexing and navigation behavior before reporting NaviRAG results.

Example: for “What are all the exceptions to refund approval?”, navigation can examine the policy section, its exceptions, related tables and approval notes. This is promising for long specifications whose context is spread across several levels. Risks include wrong-branch choices, inaccurate intermediate summaries and additional model calls. A hierarchy is not the same as a requirement-to-code graph; it does not by itself establish that a function implements a requirement.

## Graph RAG, explained step by step

1. Extract and verify entities, relationships and source-backed assertions from the application's evidence.
2. Resolve the question to relevant entities using exact IDs, aliases or optional vector search.
3. Traverse allowed typed relationships to collect relevant requirements, functions, tests, decisions or dependencies.
4. Fetch their source evidence; apply freshness, confidence and access checks.
5. Generate an answer or structured analysis with the supporting paths and citations.

“Graph RAG” is a family of approaches. Microsoft's GraphRAG describes local search combining graph and source-document context, and also offers other query modes. Graphify is a separate selected graph-building integration; adopting graph retrieval here does not mean adopting Microsoft's entire GraphRAG pipeline. [GraphRAG local search](https://microsoft.github.io/graphrag/query/local_search/), [query modes](https://microsoft.github.io/graphrag/query/overview/).

For Code Factory, the valuable path is “Requirement R-17 → validation function → API endpoint → test T-4.” It allows the system to explain why a proposed fix affects a module and which tests should verify it. However, a missing edge can mean extraction failure rather than missing code. A valid path can still contain an incorrect semantic assertion. Therefore Code Factory must read the pinned code/specification and run appropriate verification rather than declaring a bug solely from graph shape.

## Comparison for Digital Brain

The tradeoffs below are our engineering assessment, to validate on representative application data.

| Question | Traditional/vector RAG | NaviRAG | Graph RAG |
|---|---|---|---|
| Main retrieval object | Relevant passages | Hierarchical knowledge records and evidence | Entities, typed relationships and supporting evidence |
| Strong fit | Finding explanations despite different wording | Long documents with nested rules and exceptions | Traceability, dependency reasoning and structured impact analysis |
| Typical limitation | Similar passages may miss distributed context | Navigation can take a wrong branch or require many steps | Missing/incorrect extraction can distort conclusions |
| Up-front work | Chunking, embeddings and optional lexical index | Hierarchy construction and navigation setup | Ontology, extraction, entity resolution, provenance and quality checks |
| Ongoing maintenance | Refresh changed chunks/embeddings | Refresh affected hierarchy records/summaries | Revalidate changed facts, dependencies and provenance |
| Query cost drivers | Search/rerank/context size | Navigation steps and model calls | Traversal, evidence volume and any synthesis steps |
| Graph required? | No | Hierarchical records; not necessarily our domain graph | Yes |
| Vector engine required? | For the vector variant; not all RAG | Depends on chosen implementation | Optional for entry-point discovery |
| Proposed role here | Baseline Chat retrieval | Optional long-specification navigation | Required traceability for Code Factory and graph questions |

We do not need a vector database to generate or store the graph. We want vector retrieval to improve semantic discovery, especially when a document fact has not yet become a graph node. Conversely, we want graph retrieval because nearest text similarity is not a substitute for explicit requirement/code/test relationships.

## Proposed query router

All routing happens **after** application authorization. The router is given only tools for the selected application. It cannot choose an organization-wide store.

| User request | Proposed route |
|---|---|
| “Show ticket PAY-42 or function authorizeRefund.” | Exact lookup/lexical search, then permitted evidence |
| “Explain the reimbursement policy.” | Lexical + vector search, rerank, cite source |
| “What exceptions apply across this long specification?” | Evaluate bounded NaviRAG navigation with cited leaves |
| “Which functions and tests implement R-17?” | Graph traversal plus source/code verification |
| “What deviates from the approved refund specification?” | Enumerate scoped approved requirements; graph traceability plus direct code/test analysis |

For a whole-application audit, do not use top-k retrieval as the coverage mechanism. Enumerate the complete in-scope approved requirement set, create a coverage ledger and analyze every item in batches. Use vector/navigation search to find supporting evidence for each item. Mark unassessed items explicitly instead of describing a sampled analysis as a complete audit.

Example combined request: “Does the refund API enforce every exception in the approved policy?” The system identifies relevant policy text, navigates the approved document if needed, follows requirement-to-code/test links, inspects the code snapshot, and reports supported findings/unknowns. Each selected method contributes evidence; none supplies permission to change code.

## Isolation and evaluation gates

All graph nodes, vectors, navigation summaries, leaf passages, caches and tool calls are confined to one application and its source ACLs. A hierarchy summary must not expose restricted child content: generate access-compatible summaries or fetch permitted leaves directly. Cross-application retrieval and cross-application agent memory are prohibited even for a user with access to both applications.

Evaluate the same application benchmark across lexical-only, lexical+vector, graph-assisted and NaviRAG routes. Include paraphrase questions, exact identifiers, nested exceptions, multi-step traceability, unanswerable questions and whole-audit coverage. Measure evidence recall, citation support, answer correctness, abstention, coverage, p50/p95 latency, indexing cost and query cost. Include permission-revocation and cross-application attack cases; leakage tolerance is zero.

Recommended adoption order: establish the isolated evidence/graph foundation; measure traditional Chat retrieval and graph-backed Code Factory; then add NaviRAG only if a pilot improves long-document outcomes enough to justify its cost and maintenance. A retrieval feature is selected by measured usefulness, not its name.

## Model flexibility and developer access

[Model selection policy](model-selection.md) applies to every stage and individual agent: users select compatible profiles within application permissions, with explicit fallbacks and audited execution. Code Factory approval pins the model map; embedding changes rebuild the corresponding index. [Application REST and MCP access](developer-access.md) exposes the same authorized retrieval pipeline to developer code and coding assistants. External calls cannot cross application boundaries or bypass source permissions and Factory approval. The consolidated narrative includes both contracts in full.

