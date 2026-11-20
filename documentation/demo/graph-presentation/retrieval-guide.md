# Digital Brain: the actual graph retrieval pipeline

Prepared 13 October 2026 from the repository implementation. Open **retrieval.html** for the presentation visual. It uses grouped process panels, inspired by the supplied reference, with five stages and a separate offline measurement path.

## Read the diagram

| Stage | Actual operation | Underlying technology and important boundary |
|---|---|---|
| 1. Prepared evidence | Store graph revisions and, when configured/indexed, text vectors. The diagram starts after source ingestion and graph construction. | Django JSON storage; optional OpenAI embeddings and stored NumPy-compatible vectors. A graph is not built or republished on each question. Graphify contributes Code Graph extraction and Knowledge graph themes, not this retrieval algorithm. |
| 2. Query and scope | Check the user's application/feature access and selected answer mode. Form the retrieval query, carrying conversation-topic terms for short/pronominal follow-ups. Resolve the newest published graph or the conversation's chosen published version. | Django grants and feature checks; application query rules; published revision selection. This is not an autonomous query planner generating multiple tailored subqueries. No published version means the graph path refuses to answer. |
| 3. Search and expand | Rank relationship text with covering query terms and BM25. If a usable question vector exists, fuse its nearest-relationship ranking with word ranking. Add bounded typed walks and one-hop neighbouring edges. | Custom BM25; NumPy exact cosine similarity; reciprocal rank fusion; application graph traversal. Typed walk: at most 2 hops, 5 seed relationships, 25-edge fanout cap, and at most 6 walked relationships considered. One-hop expansion uses endpoints from the top 3 ranked relationships. |
| 4. Verify and select | Filter against current answerable sources and the viewer's enforced permission list. Reject absent or changed evidence. Deduplicate relationship signatures and spread citations across sources. Optionally rerank a wider verified candidate set. | Source must remain active, non-restricted, same digest, with an exact quote still present. The ordinary limit is 8 citations. Configured reranking considers up to 20 candidates, chooses 8, and falls back to the original ordering on failure. Reranking cannot invent or edit evidence. |
| 5. Generate and deliver | Give verified evidence and conversation context to the selected answer runtime. In the streaming path, scoped tools can add evidence; the citation recorder rechecks live sources and permissions before citations are emitted and stored with the answer. | Configured OpenAI/Claude SDK runtime; user/application-bound ToolScope; CitationRecorder. The synchronous graph path uses graph_retrieval answer generation after graph citation retrieval. The additional final recorder recheck in the visual explicitly describes the streaming path. |
| Separate offline measurement | On a newly saved graph version, run expected-source benchmark questions through retrieval/verification and store results. Maintainers can review the results and choose source/configuration changes and a new reviewed version. | Drafts may be measured without being allowed to answer users. The normal automatic benchmark uses stored vectors and no answer-model generation; optional operator-requested reranked benchmarks can spend. Measures include expected source found, top 3 and reciprocal rank. No automatic model training or automatic corrective publication is implemented. |

## Important details to say aloud

- Graph answering uses **published snapshots**, with evidence verified against **live sources**. A stale edge is skipped; the old snapshot does not silently gain new source content.
- Direct matches are considered before typed multi-hop results. Structural edges are not used by the typed multi-hop walk, because common field values can connect too many unrelated records. Structural edges can still participate in the separate one-hop neighbour expansion.
- Vector search and reranking are **optional**. Word-based retrieval remains the base path. Vectors rank candidates; they are not citations or proof of a fact.
- New question embeddings can incur provider charges when configured. Enabling a reranker adds a paid provider call to answer retrieval when applicable.
- Source permission enforcement here protects answer evidence. It should not be presented as complete per-record confidentiality for all Sources/explorer screens or ingestion.
- The corpus-wide benchmark's measurement viewer is not an individual user's permission test. Retrieval success does not measure every answer's correctness.
- The scoped streaming tools do not constitute a guarantee that the model uses only the initial seed set. Tool evidence is recorded and checked before final citations are returned. A verified citation is not proof that every sentence of generated prose is entailed by that citation.
- This is a custom Graph RAG pipeline, not a Microsoft GraphRAG/Neo4j service. The graphify.net hosted service is not used by the inspected library integrations.

## Presenter script

“We begin with already prepared graph revisions. When someone asks a graph question, the server checks their access and chooses a published version. It ranks evidence using words and, optionally, meaning vectors. It then follows a small number of graph connections, rather than traversing everything. Each candidate has to pass current source, quote, digest, and permission checks. We keep diverse sources and can optionally rerank them. Only then do we give the checked context to the selected answer model. For streaming answers, tools may add evidence and the citation recorder checks it again before delivery. Separately, saved versions are benchmarked against known questions. People review those results and improve the next version; the system does not automatically train itself from every conversation.”

## Repository evidence

- `platform_core/workbench.py`: `retrieval_query`, `answer_question`, `answer_worker`.
- `platform_core/graph_ai.py`: `snapshot_revision`, `graph_citations`, `citations_from`, `walk`.
- `platform_core/embeddings.py`: `question_vector`, `nearest`, `fuse`, `relation_matrix`.
- `platform_core/ranking.py`: query terms, covering BM25 ranks and source spreading.
- `platform_core/rerank.py`: configuration, candidate limit, parsing and fallback.
- `platform_core/ai.py`: `stream_chat_answer`, application credentials and access rechecks.
- `platform_core/agent_runtime/tools.py`: `ToolScope`, `CitationRecorder`, `verify_citations`.
- `platform_core/benchmark.py`: `score`, `run`, `run_after_save`.

The companion generation diagram explains how the prepared graph is built; this diagram focuses on graph-mode answering. The **Sources** and ordinary **AI** chat modes have their own lexical evidence paths.
