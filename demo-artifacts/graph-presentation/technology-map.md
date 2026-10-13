# Technology and module map

Repository-based explanation, 12 October 2026. This maps the code paths actually used in Digital Brain. A technology appearing here does not imply every optional feature is configured in a particular application.

## Where Graphify is used

| Use | Application module | Library entry points | Result |
|---|---|---|---|
| Code Graph extraction | `platform_core/code_graph_graphify.py::analyse`, called by `code_graph_ingest.py` | `graphify.extract.extract` | Tree-sitter symbols and cross-file calls/imports/inheritance, mapped into Code Graph records |
| Knowledge graph themes | `platform_core/graph_quality.py::communities` and `themes` | `graphify.cluster.cluster`, `label_communities_by_hub`, `cohesion_score` | Leiden communities, hub-based labels, and cohesion; shared by Quality and explorer colouring |

The distribution name is **graphifyy**; the Python import namespace is **graphify**. The Code Graph adapter uses its AST extraction path, with no Graphify model-based semantic pass. The adapter writes only already-read, bounded source texts into scratch directories, with a separate scratch cache. It does not expose the original checkout or Git metadata to that extractor. Scratch storage and local execution do not constitute a complete OS security sandbox.

The Knowledge graph is **not wholly built by Graphify**. Its structural builder is `graphs.build_graph`; optional semantic relationships are proposed through `graph_ai.enrich_graph` and checked by `graph_ai.judge`. Graphify clusters existing graph structure; it does not generate the Knowledge graph's source facts.

## Three graph implementations

| Graph | Inputs | Main implementation | Storage / update behavior |
|---|---|---|---|
| Knowledge | Active, non-restricted knowledge entries normalized to text/Markdown | `graphs.py`, `graph_ai.py`, `ontology.py`, `graph_quality.py` | Graph JSON and quality in `KnowledgeGraph` / numbered `GraphRevision`; graph answers use published revisions |
| Code | Bounded repository source snapshots | `code_graph_clone.py`, `code_graph_analysis.py`, `code_graph_graphify.py`, `code_graph_ingest.py` | `CodeRepository`, `CodeSnapshot`, `CodeFile`, `CodeRelationship`; native analyzer plus Graphify results |
| Operations | Incident/change and other operational records | `ops_graph.py`, with `serviceops.py` parsing/scoring | `OperationsGraph`, `OpsNode`, `OpsEdge`; rule-derived, input-fingerprint-driven refresh |
| Cross-graph references | Verified knowledge quotes containing code paths or identifiers, plus a code snapshot | `code_knowledge.py` | Read-only projection between independently versioned Knowledge and Code snapshots; no model calls or graph mutation |

## What “Graph RAG” means here

**Graph RAG is a pattern implemented by the application:** question → retrieve graph evidence → verify current sources and viewer access → provide context to the configured answer model → show checked citations.

| Stage | Code module | Technology / responsibility |
|---|---|---|
| Choose mode and graph version | `workbench.py`, `models.CHAT_MODES` | Sources (`search`), AI (`ai`), Graph answer (`graph`) |
| Select published snapshot | `graph_ai.snapshot_revision`, `graph_snapshot` | Latest published graph or requested published version |
| Rank words | `ranking.py`, used by `graph_ai.citations_from` | Application BM25 implementation and covering query terms |
| Rank meaning, if configured | `embeddings.py` | OpenAI embeddings, 512-dimensional unit vectors, NumPy exact cosine similarity |
| Combine rankings | `embeddings.fuse` | Reciprocal rank fusion |
| Expand relationships | `graph_ai.walk`, `citations_from` | Bounded typed multi-hop and one-hop graph traversal |
| Verify candidate evidence | `graph_ai.citations_from`, `permissions.permitted` | Active/answerable source, matching digest, exact quote, viewer permission |
| Optional reranking | `rerank.py` | Paid configured model reorders verified evidence; failure preserves original ranking |
| Generate an answer | `ai.py`, `agent_runtime/openai_runtime.py`, `claude_runtime.py` | Application-scoped provider adapters; streamed runtime can use scoped tools |
| Check final tool citations | `agent_runtime/tools.py::verify_citations` | Scope/access and live-source verification |

The synchronous graph path in `workbench.answer_question` invokes the `graph_retrieval` AI purpose after retrieving graph citations. The streaming worker seeds the selected provider runtime with those citations and uses a `ToolScope` and `CitationRecorder`.

The inspected paths do **not** invoke a Microsoft GraphRAG framework pipeline, Neo4j query engine, or LangChain retrieval chain. The use of the term Graph RAG describes this application's graph-backed retrieval and generation flow. It should not be presented as a claim that a named third-party GraphRAG framework is deployed.

## Technology stack

| Technology | Where used | Why it is used |
|---|---|---|
| Python 3.12+ / standard library | Backend; `ast`, `hashlib`, subprocess, JSON | Native Python parsing, content digests, bounded workers, serialized graph data |
| Django 5.2 | Views, models, policy, sessions, forms | Server-rendered UI, database access, authentication and authorization hooks |
| SQLite / PostgreSQL | `digitalbrain/settings.py` | Local SQLite storage; production PostgreSQL configuration with certificate-verified TLS |
| Waitress / WhiteNoise | Runtime and Django middleware | WSGI serving and packaged static assets |
| MarkItDown / pypdf | `processing.py`, `scripts/extract_text.py` | Local conversion and bounded PDF checks |
| ClamAV (`clamscan`) | `processing.py` | File scanning, required in production; external executable and signatures must be configured |
| Graphify (`graphifyy`) / Tree-sitter | Code Graph adapter | Multi-language static code extraction; Graphify's AST path only |
| NetworkX / Graphify Leiden / graspologic-native | `graph_quality.py` | In-memory graph projection and community detection, not persistent graph-database storage |
| NumPy | `embeddings.py` | Vector normalization and exact cosine similarity |
| RapidFuzz | `ontology.py` | Conservative same-type name matching with strict identifier/numeric handling |
| OpenAI Agents SDK / Claude Agent SDK | Provider/runtime adapters | Optional extraction and answers through application-managed credentials and usage accounting |
| OpenAI embeddings | `embeddings.py` | Optional semantic search vectors; separately configured from relationship extraction |
| Vanilla JavaScript + Canvas | `static/graph.js` | Interactive force layout, search, inspect, pan/zoom; a d3-force-style algorithm implemented in application code |
| django-axes / Argon2 | Authentication settings | Login throttling and password hashing |
| Playwright / Ruff / scorecard | Development checks | Browser regression tests, linting, and retrieval/safeguard/performance measurement |

Direct dependency declarations are in [pyproject.toml](../../pyproject.toml). Graphify brings algorithm/parser dependencies such as NetworkX and Tree-sitter; do not describe these as separate persistent services. No dedicated vector or graph database is required by the inspected search/generation paths.

Observed **local installed versions** on this date: Django 5.2.17; graphifyy 0.9.67; NetworkX 3.7; graspologic-native 1.3.1; NumPy 2.5.3; RapidFuzz 3.14.6; MarkItDown 0.1.7. These observations describe this environment, not a promise about another deployment or future package releases.

## Security responsibility map

| Module | Responsibility | Boundary |
|---|---|---|
| `policy.py`, `workbench.access` | Application membership/grant, feature, and write role | Administrative hierarchy does not imply application knowledge access |
| `sensitivity.py`, `KnowledgeEntry.answerable` | Restricted-source exclusion | AI use and citations are excluded; stored records are not erased or hidden from all application members |
| `permissions.py` | Optional Jira source-system answer permissions | Not a complete per-record confidentiality boundary for Sources/explorer or generation |
| `processing.py`, `fetching.py` | File scan/conversion and outbound URL safeguards | Scanning depends on deployment; operator-allowed internal hosts are exceptions |
| `secrets.py`, `ai.provider_credential` | Server-side credential resolution | Provider credentials are application-scoped; external calls still transmit selected text |
| `budget.py`, `ai.invoke_ai` | Configured budget checks and AIUsage accounting | Optional budgets and estimated cost records are not an absolute provider billing guarantee |
| `graph_ai.py`, `agent_runtime/tools.py` | Quote, digest, source, scope, and citation checks | Evidence presence/type consistency does not prove every semantic claim correct |

Presenter wording: **“Digital Brain builds its own evidence-backed Knowledge graph, uses Graphify for code extraction and graph themes, and implements Graph RAG through published graph retrieval plus verified context for the answer model.”**
