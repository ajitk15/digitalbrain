# Digital Brain: uploaded documents to evidence-backed answers

Draft narration for student presentation, in conversational English for an Indian-accent voice. This is the detailed narrative; final recording duration depends on pacing. No video or audio generated.

## Example documents shown on screen

| Document | Source sentence |
|---|---|
| Policy System.pdf | The claims portal uses the policy database to check policy details. |
| Claims Processing.docx | The claims portal uses the claims database to store claim records. |
| Claims Guide.docx | The claims guide explains the claims portal. |

These are fictional system documents, not medical records or insurance coverage rules.

## Narration and technology labels

### 1. Upload sources

“Let’s see how our Digital Brain turns documents into useful knowledge. We start by uploading three health insurance documents: Policy System, Claims Processing and Claims Guide. Each document explains one part of how the claims portal works.”

Visual: three file cards → application upload area.
Technology: Django, Python, private file storage. Show access and integrity checks; malware scanning is conditional on deployment settings.

### 2. Convert the files

“MarkItDown converts the files into readable text. Our application stores the converted content with its source identity and fingerprint. So, even after conversion, we know which document each sentence came from.”

Visual: PDF/Word → Markdown/text, with the three source sentences visible.
Technology: MarkItDown, Django KnowledgeEntry, SHA-256. Scanned documents require configured OCR; these examples use readable text-based files.

### 3. Build document structure

“When we build the graph, Python first identifies documents, sections and table records. These become connected structural nodes. This tells us where the information sits; it does not yet explain the business relationships.”

Visual: document → contains section → section; structural nodes in grey.
Technology: custom Python structural graph builder.

### 4. Propose business relationships

“With AI enrichment enabled, OpenAI or Claude reads the source text and proposes connections. The first document says Claims Portal uses Policy Database. The second adds Claims Database. The third says Claims Guide explains Claims Portal. Each proposal includes the exact source quote.”

Visual: highlight subject, relationship and object in each sentence → three proposed edge cards.
Technology: configured OpenAI or Claude model, custom Python enrichment. Clearly label prepared AI proposals if used in the demonstration.

### 5. Validate and merge

“Python checks the quote, named items and allowed relationship types. An invented Payment Database quote is rejected. Matching names and types are then merged. We now have one Claims Portal shared across all three documents: four business nodes and three connections, each linked to its evidence.”

Visual: validation gate, rejected proposal outside graph, shared portal node joins the three accepted connections.
Technology: Python validator, project ontology, entity merging, source provenance. Stored labels normalize uses to depends on and explains to covers. Quote/type checks do not prove semantic truth.

### 6. Prepare search embeddings

“There is also an optional search-preparation step. OpenAI converts relationship text and document passages into numerical vectors called embeddings. They help us search by meaning later; they do not create these graph connections.”

Visual: relationship text and passages → embedding model → stored vectors, beside the graph branch.
Technology: configured OpenAI embedding model, 512 dimensions, Django Embedding storage. Models include text-embedding-3-small and text-embedding-3-large. Indexing happens in an explicitly requested AI graph run when embeddings are enabled, or through the operator embed command. Unchanged text reuses stored vectors.

### 7. Save, organise and publish

“Django saves a numbered graph draft. Graphify uses NetworkX and Leiden clustering to group existing connected nodes into themes. A reviewer checks the graph and publishes it. Now we have both connected knowledge and, when enabled, a meaning-based search index.”

Visual: numbered draft, separate Graphify themes branch, human review → published revision.
Technology: Django GraphRevision, Graphify (distribution graphifyy), NetworkX, Leiden. The theme grouping is derived from existing nodes; it does not extract the facts. This sentence groups concepts for teaching, not an assertion of exact internal call ordering.

### 8. Ask a question

“Now the user selects Graph answer and asks: ‘Which systems does the claims portal rely on?’ The application selects the published graph version. It searches the relationship text using keywords, and, when embeddings are enabled, compares the question’s meaning with stored vectors.”

Visual: question → published graph; branch into keyword ranking and vector ranking.
Technology: graph-mode chat, custom coverage-aware BM25, OpenAI question embeddings, NumPy cosine similarity. Application-scoped vectors, not a dedicated vector database.

### 9. Combine and follow connections

“Keyword search matches words like ‘claims portal.’ Vector search can recognise that ‘relies on’ is similar to ‘uses.’ Reciprocal Rank Fusion combines their rankings. The graph can then follow related connections to find supporting information.”

Visual: BM25 and cosine rankings → RRF → candidate relationships → connected graph expansion.
Technology: custom Python RRF and bounded graph traversal. Typed traversal is bounded to two hops; direct matches precede walked results. Also includes one-hop expansion around top relationship endpoints. Semantic matches are candidates, not proof or guaranteed hits.

### 10. Verify and answer

“Before using a result, the application checks source access, the current fingerprint and the supporting quote. Optional reranking can reorder the verified evidence. The configured answer model then explains: ‘The claims portal uses the Policy Database to check policy details and the Claims Database to store claim records,’ with source citations.”

Visual: candidates → source access / current digest / exact quote → optional rerank → configured answer model → answer with citations to documents one and two.
Technology: Django source permissions, SHA-256, quote verification, optional OpenAI/Claude reranking and configured graph-retrieval answer model. The answer above is illustrative, not an observed provider response.

### 11. Close with the safety boundary

“That is our Digital Brain: documents provide evidence, the graph connects it, embeddings help find it, and the answer model explains it. Restricted sources are excluded from graph generation, and evidence is checked again when a question is answered.”

Visual: upload → text → graph plus search index → verified retrieval → cited answer.

## Revalidation scope and code evidence

- `platform_core/processing.py`: file integrity, conditional scanning, conversion and converted knowledge source persistence.
- `platform_core/graphs.py`: answerable source selection, structural building, requested AI enrichment, optional embedding indexing, revisions and publication.
- `platform_core/graph_ai.py`: quote/type checks, entity enrichment, published snapshot selection, hybrid relationship ranking, bounded traversal, permission and evidence verification.
- `platform_core/embeddings.py`: OpenAI embedding calls, 512-dimensional normalized vectors, Django storage, NumPy cosine search and RRF.
- `platform_core/graph_quality.py`: Graphify theme grouping.
- `platform_core/rerank.py`: optional reordering of already verified citations with fallback to original ranking.
- `platform_core/workbench.py`: separate Graph answer versus document-passage retrieval modes and answer-model invocation.
- `platform_core/ai.py`: graph_retrieval model configuration, purpose-specific invocation and usage accounting.

The synthetic insurance run executed real structural building, enrichment/merging, quote validation, Graphify themes and graph traversal, with prepared AI proposals. Result: four business entities, three business edges; complete graph has thirteen nodes and fifteen edges. An invented quote was rejected and traversal reached a related database.

It did not execute file upload/conversion, live extraction/embedding/answer-provider requests, database persistence or publication. No application setting was checked to establish that optional features are enabled for a particular deployed application. Those paths were inspected in source; focused regression tests are recorded separately.

Focused revalidation passed: 73 tests across graph extraction, graph versions, embeddings, ranking and reranking. See narrative-revalidation-tests.log. The initial attempt encountered Windows sandbox temporary-directory cleanup errors; rerunning with a workspace temporary directory passed.

The generation and retrieval models can be configured separately. Graph mode retrieves graph relationships; document AI mode retrieves passages. Do not draw both as an unconditional combined path.
