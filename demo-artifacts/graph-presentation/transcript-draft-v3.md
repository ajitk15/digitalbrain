# Documents to a health insurance knowledge graph

Narrative draft for review before generating the video. Spoken English with an Indian accent. Approximately two minutes; exact timing will be checked after recording.

## Three fictional source documents

| Uploaded document | Example content | Business entity type |
|---|---|---|
| Policy System.pdf | The claims portal uses the policy database to check policy details. | Claims Portal and Policy Database: Component |
| Claims Processing.docx | The claims portal uses the claims database to store claim records. | Claims Portal and Claims Database: Component |
| Claims Guide.docx | The claims guide explains the claims portal. | Claims Guide: Runbook; Claims Portal: Component |

Use readable, text-based sample files. Do not use actual customer, medical or policyholder records.

## 1. Upload the documents

**Narration:** “Let’s start where we start in our app: uploading source documents. We upload three health insurance documents: a Policy System note, a Claims Processing note, and a Claims Guide. Together, they describe the systems used to process a claim.”

**Visual:** Three document cards enter the application's upload area. Keep their names and one example sentence visible.

**Technology on screen:** Django · Python · private document storage.

## 2. Convert files into readable text

**Narration:** “The application checks access and file integrity. MarkItDown converts the PDF and Word files into readable text. Headings and table content are preserved where conversion supports them. Each converted source gets an identity and a fingerprint, so we can trace it.”

**Visual:** Each original file → converted Markdown/text card → source ID and fingerprint badge. Show configured scanning as a conditional security gate, not an unconditional claim about this deployment.

**Technology on screen:** MarkItDown · Python · SHA-256 · Django KnowledgeEntry. Tesseract OCR is conditional for scanned documents; it is not needed for these text-based samples.

## 3. Build the document structure

**Narration:** “When we build the graph, our Python code first creates document and section nodes, and handles table rows. These show where information came from. Business connections, like which database the portal uses, come in the next step.”

**Visual:** Document → contains section → section. Show structural nodes in grey, separate from business entities in colour. No vector embeddings are shown as a mandatory generation step.

**Technology on screen:** Custom Python graph builder.

## 4. Extract business items and relationships

**Narration:** “With AI enrichment enabled, OpenAI or Claude reads the source text. From document one, it proposes: Claims Portal uses Policy Database. Document two adds Claims Database. Document three adds: Claims Guide explains Claims Portal. Every proposal includes its source and exact supporting quote.”

**Visual:** Highlight entity names and relation words inside each converted document. Move them into proposed subject → relationship → object cards. Display the corresponding source quote beneath every proposal.

**Technology on screen:** Optional OpenAI / Claude · custom Python enrichment.

## 5. Check proposals against the documents

**Narration:** “Python then checks those proposals against the documents and allowed relationship types. An invented quote about a Payment Database is rejected because it is absent from the source. These checks filter errors; a reviewer still needs to check meaning.”

**Visual:** Proposal → exact quote / entity names / allowed types → accepted or rejected branch. Rejected proposal stays outside the final graph.

**Technology on screen:** Python validator · project ontology.

## 6. Combine the accepted connections

**Narration:** “All three documents mention Claims Portal. Matching names and types become one shared node. Now we have four business nodes and three connections. Each connection retains its document, quote and fingerprint, so we can inspect the evidence.”

**Visual:** Three proposed portal references merge into a central Claims Portal node. Connect it to Policy Database and Claims Database; connect Claims Guide to it. Keep document and section nodes available as a separate provenance layer.

**Technology on screen:** Python entity merging · source provenance · SHA-256.

## 7. Prepare embeddings for later search — optional

**Narration:** “There is also an optional search-preparation step. OpenAI converts relationship text and document passages into lists of numbers called embeddings. These help find similar meanings later. They do not create the graph connections; they help us search the information.”

**Visual:** Accepted relationship text and document passages → OpenAI embedding model → stored numerical vectors. Draw this as a search-preparation branch beside the graph, not as the step that creates nodes and edges.

**Technology on screen:** OpenAI text-embedding-3-small / configured embedding model · 512-dimensional vectors · Django Embedding storage.

**Implementation scope:** Enabled embeddings are indexed during an explicitly requested AI graph-generation run, after enrichment and before saving the revision, or through the operator embed command. Upload alone does not automatically invoke paid embedding requests. The index also includes configured benchmark questions. Unchanged text reuses stored vectors; embedding failure does not discard the graph.

## 8. Save, group and review

**Narration:** “Django saves a numbered graph draft. Graphify uses NetworkX and Leiden clustering to group connected items into themes. Graphify helps organise the existing graph; the document conversion and relationship extraction are handled by other parts of our application.”

**Visual:** Combined graph → numbered draft. A side branch shows existing nodes → Graphify grouping → themes, followed by human review.

**Technology on screen:** Django GraphRevision · Graphify, installed as graphifyy · NetworkX · Leiden.

## 9. Publish with access checks

**Narration:** “After human review, we publish the graph. Graph answers use published versions, check source access, and verify current evidence. Restricted sources stay outside AI enrichment. That is how three uploaded documents become one connected, traceable knowledge graph.”

**Visual:** Review → published graph → access and evidence checks → graph answers. Finish on the four business nodes with clickable evidence-style callouts.

**Technology on screen:** Django publication workflow · source permission and evidence checks.

## Retrieval bridge for the subsequent explanation

Question: “Which systems does the claims portal rely on?”

The retrieval diagram should show keyword ranking (custom BM25) alongside meaning ranking (question embedding plus NumPy cosine similarity), merged through reciprocal rank fusion (RRF). Graph retrieval then follows related edges, checks source access and current evidence, and supplies verified citations. A semantic match is a candidate, not proof. No particular result should be claimed until the health insurance example is executed.

Technology: custom Python BM25 and RRF · OpenAI embeddings when enabled · NumPy · custom graph traversal · Django source checks. Vectors are stored through Django; this implementation does not require a dedicated vector database. The configured model and whether embeddings are enabled must be checked before presenting them as active for a specific application.

## Accuracy and recording notes

- Four business entity nodes are the teaching focus. The complete graph also contains document, section and provenance connections; do not label its total size as four nodes and three edges.
- The ontology stores uses as depends on, and explains as covers. Show source wording in the teaching diagram and stored labels in the technical detail panel.
- The three documents above are proposed synthetic fixtures. The revised example must be run through the project before claiming its results were observed.
- The future recording must label prepared AI proposals if used, and distinguish illustrated persistence/publication from operations actually performed.
- Scan enforcement and OCR depend on deployment configuration. Do not imply either is enabled without checking.
- No video generation or voice recording until transcript review is complete.
- The added embedding step expands the earlier draft. Rebalance the narration before recording to retain the requested two-minute duration; retrieval gets its own detailed explanation rather than being compressed into graph generation.
