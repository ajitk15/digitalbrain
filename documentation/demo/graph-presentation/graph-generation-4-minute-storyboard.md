# Digital Brain graph generation — four-minute narrative and visual storyboard

Review draft. Target: 240 seconds, conversational English with an Indian-accent voice. Approximately 520 spoken words, with time for pointer movement and visual reveals. Timings are a production plan, not measured audio. Retrieval is a subsequent chapter, outside this generation video.

## Visual language

- One persistent progress strip: Upload → Convert → Structure → Extract → Check → Merge → Embed → Save / Group → Publish.
- Each scene presents INPUT → PROCESS → OUTPUT, with a technology strip below it. Highlight only the transformation being explained.
- Source-document badges remain attached to evidence. Use three stable source colours; do not use colour alone to identify a source.
- Structural nodes are neutral grey; business entities are blue; proposed edges are dashed amber; accepted edges are solid green; rejected proposals are red and remain outside the saved graph.
- Show exact quotations beside proposals. Use short labels on the graph and a separate evidence panel for full sentences.
- Introduce a technical term with its plain-English meaning before displaying its technical label. Do not show scripts as substitutes for technology names.
- Keep source wording in the main diagram. The detail panel explains that stored relation labels normalize uses → depends on and explains → covers.
- Stage 3 heading-only companion is a simplified view, not the full structural graph. The filmed reveal must include the sentence sections described below.

## 1 · 00:00–00:24 · Upload three documents

**Narration**

“Let’s build a Digital Brain from three health insurance documents. We upload a Policy System note, a Claims Processing note, and a Claims Guide. The first explains where policy details come from, the second describes claim records, and the third explains the portal. We’ll follow their actual sentences into the graph.”

**Visual sequence**

1. Show an application upload area with the three files entering it.
2. Open each document card and reveal its heading, `Overview`, and one source sentence:
   - Policy System.pdf: “The claims portal uses the policy database to check policy details.”
   - Claims Processing.docx: “The claims portal uses the claims database to store claim records.”
   - Claims Guide.docx: “The claims guide explains the claims portal.”
3. Add a labelled source badge to each card. Show private storage and an access gate.

**Input / output:** three fictional readable files → uploaded source documents belonging to one application.

**Technology:** Django, Python, private file storage. Do not imply a production insurer or real policyholder data.

## 2 · 00:24–00:52 · Convert files into source text

**Narration**

“The application checks access and file integrity. MarkItDown converts the PDF and Word files into readable text. Here, each document becomes an Overview heading followed by its sentence. Django stores this text as a knowledge source. A source identifier tells us where it came from; a SHA-256 fingerprint helps detect whether the content changes.”

**Visual sequence**

1. Left: original document preview. Centre: MarkItDown conversion box. Right: converted text, including `## Overview`.
2. Animate the same sentence from the original preview to the converted card; no words should appear to be invented by conversion.
3. Show source ID `S1`, `S2` or `S3` as a teaching abbreviation, not an actual database ID.
4. Show fingerprint as a compact badge, not a wall of hexadecimal text. Caption: “Detects content changes; not encryption.”
5. Show malware scanning and scanned-page OCR only as conditional branches labelled “when configured.” Use text-based files in this demo.

**Input / output:** file bytes → normalized Markdown/text plus provenance.

**Technology:** MarkItDown, Python, Django KnowledgeEntry, SHA-256. Tesseract is conditional for OCR.

## 3 · 00:52–01:32 · Build the structural graph

**Narration**

“Now we request graph generation. First, our Python builder creates a node for each document. A node simply means an item in the graph. It also creates section nodes from headings and ordinary text lines. Watch the Policy System document connect to its Overview heading and its source sentence. These arrows say ‘contains section.’ The other documents follow the same pattern. Tables, when present, produce records and field values. At this stage, we have captured document structure; we have not inferred which business system uses which database.”

**Visual sequence — essential teaching scene**

1. Split screen: converted Policy System text on the left; initially empty graph on the right.
2. Outline the document title → reveal its document node.
3. Highlight `## Overview` → reveal a separate heading section node. Connect document → heading with `contains section`.
4. Highlight the sentence line → reveal a second section node carrying that sentence. Connect document → sentence with `contains section`.
5. Repeat the same transformation for S2 and S3, speeding up the repeat without skipping the reveal.
6. Display actual synthetic structural counts: **3 document nodes + 6 section nodes = 9 nodes; 6 contains-section edges**.
7. Add a small, explicitly separate illustration: table row → record → field value. Caption: “Supported for tables; these three samples have no tables.” Do not add it to the example counts.
8. Point to a `contains section` edge and explain its meaning. Do not draw heading → sentence hierarchy: this builder attaches both directly to their document.

**Input / output:** normalized source lines → structural nodes and source-backed edges.

**Technology:** custom Python graph builder. AI, Graphify and embeddings do not perform this structural transformation.

## 4 · 01:32–02:20 · Propose business relationships

**Narration**

“With optional AI enrichment enabled, OpenAI or Claude reads the text and proposes business relationships. Let’s examine the first sentence. ‘Claims portal’ is one item, ‘policy database’ is another, and ‘uses’ connects them. We display that as two nodes joined by an arrow. The proposal also identifies their types, the source document and the exact quote. The second document proposes another uses connection, this time to Claims Database. The third proposes Claims Guide explains Claims Portal. These are suggestions first. They become accepted connections only after the application checks them.”

**Visual sequence — essential teaching scene**

1. Keep the grey structural graph faintly visible; open S1's full sentence beside it.
2. Highlight `claims portal` in blue, `uses` in amber, `policy database` in violet. Keep the original sentence readable.
3. Move those words into three labelled slots: **Subject | Relationship | Object**.
4. Below, reveal **Component | uses | Component**, followed by **Source: S1** and **Exact quote**.
5. Build a dashed proposal arrow between the two proposed entities. Badge: “AI proposal — not accepted yet.”
6. Repeat with S2, showing the second proposed Claims Portal reference rather than merging it prematurely.
7. Repeat with S3: **Runbook | explains | Component**. Define Runbook on screen as “instructions or guide.”
8. Show three proposal cards together. A separate detail tab displays stored relation normalization, without changing the source quotation.
9. In the reproducible demo, badge the model-output cards **Prepared AI proposals**. Do not animate a live provider response that did not occur.

**Input / output:** source excerpts → typed relationship proposals with exact supporting quotations.

**Technology:** configured OpenAI or Claude model, custom Python enrichment, project ontology. Graphify does not extract these facts.

## 5 · 02:20–02:48 · Check evidence before accepting links

**Narration**

“Each proposal passes through checks. Does the source exist? Is the exact quote present? Are the named items in it? Are their types and relationship allowed? Our valid examples pass. An invented sentence saying the portal uses a Payment Database fails because that quote is absent. These checks filter unsupported proposals; they do not replace human review of meaning.”

**Visual sequence**

1. Pipeline of four labelled checks: source exists → exact quote → item names → allowed types.
2. Trace the S1 proposal through the checks. Its dashed arrow becomes solid only after passing.
3. Side-by-side comparison: original S1 sentence versus invented “The claims portal uses a payment database.”
4. Stop the invented proposal at the exact-quote gate. Reason: `quote not in source`. Never add its Payment Database node to the accepted graph.

**Input / output:** proposals → accepted relationships or rejection reasons.

**Technology:** Python validator, project ontology. A checkmark means validation passed, not that semantic truth was proved.

## 6 · 02:48–03:10 · Merge shared entities and attach evidence

**Narration**

“The three proposals mention the same Claims Portal. Matching names and types become one shared node. Now the business view has four nodes and three connections. Select an arrow, and its original document and supporting quote can explain why it exists. The full graph also retains document structure and evidence links.”

**Visual sequence**

1. Animate the three matching portal references into one central entity. Caption: “Same name + same type”; do not suggest general fuzzy synonym merging.
2. Reveal the final business view: guide → explains → portal; portal → uses → both databases.
3. Select the policy-database arrow; open S1's exact quote and source fingerprint.
4. Counter: **Business view: 4 nodes, 3 relationships**. Toggle **Full graph: 13 nodes, 15 edges** for these exact synthetic sources and prepared proposals.

**Input / output:** checked relationships → consolidated graph with provenance.

**Technology:** Python entity merging, source identifiers, quotes and SHA-256 fingerprints.

## 7 · 03:10–03:30 · Prepare optional embeddings

**Narration**

“When search embeddings are enabled for this requested AI run, OpenAI converts relationship text and document passages into numerical vectors. Django stores them for later meaning-based search. The vectors help find information when a question is asked; they do not create or prove the graph connections. Unchanged text can reuse its stored vector.”

**Visual sequence**

1. Branch beside the completed graph: relationship text and passage cards → OpenAI embedding model → numeric-vector strips.
2. Illustrate a few numbers, with a clear ellipsis and label **512 numbers per vector**; do not imply a three-number implementation.
3. Add a reusable-vector badge to an unchanged text card.
4. Keep the business graph on screen and unchanged while vectors appear. Badge: **Optional search preparation**.

**Input / output:** text → stored embeddings for later retrieval.

**Technology:** configured OpenAI embedding model, 512 dimensions, Django Embedding. No mandatory dedicated vector database. Embedding failure does not discard graph relationships.

## 8 · 03:30–03:44 · Save and organise the draft

**Narration**

“Django saves a numbered draft. For review, Graphify uses NetworkX and Leiden clustering to organise existing connected nodes into themes. It groups the graph we built; it does not invent the underlying facts.”

**Visual sequence**

1. Graph → numbered draft card.
2. Side branch: existing nodes → Graphify grouping → theme overlay. A label states “Derived review view.”
3. Do not portray theme grouping as mandatory extraction or invent an exact partition for the small sample. Use the actual recorded grouping result in production visuals.

**Input / output:** consolidated graph → saved revision and derived theme view.

**Technology:** Django GraphRevision, Graphify (graphifyy), NetworkX, Leiden.

## 9 · 03:44–04:00 · Review, publish and close

**Narration**

“A reviewer checks the draft and publishes it. Restricted sources are excluded from graph generation. Later, graph answers check current evidence and user access again. We have turned three documents into connected knowledge, with proof behind the connections. Next, we’ll see how retrieval uses it.”

**Visual sequence**

1. Human review → published-version badge; do not show automatic publication.
2. Source-access gate stays visible at the graph boundary.
3. Close on the business graph, source evidence callout and a small optional vector-index branch.
4. Final roadmap: documents → text → structure → checked relationships → reviewed graph. Retrieval appears only as the next chapter.

**Technology:** Django publication workflow, source permissions and evidence verification.

## Validation and production boundary

- Actual synthetic run: 9 structural nodes / 6 structural edges; after enrichment 13 total nodes / 15 total edges, including 4 business entities / 3 business relationships.
- Prepared extraction proposals; actual builder, validator, merging, Graphify and graph traversal executed. Invented quote rejected.
- 73 focused graph/embedding/ranking/reranking tests passed in the previous revalidation.
- No live upload conversion, paid provider call, graph persistence or publication performed for this synthetic example. These stages must be labelled as illustrated unless executed during the later recording.
- Current user approval stage: narrative and visual plan. No new video or narration generated.
- Actual Indian-accent voice selection and final audio timing remain production work after narrative review.

## Visual companion receipts

Both standalone architecture diagrams passed 9/9 showcase checks, zero errors/warnings, and automated desktop checks at 1440×900, 1600×1000, 1920×1080 and 2048×1320. Light/dark screenshots were inspected at the large endpoint. Correction rounds: 0.

| Diagram | Specification SHA-256 | HTML SHA-256 |
|---|---|---|
| structure-teaching.html | 603d56301f886f2fd4f515edd9989b41ce8c7ca4ba288c19b9820a19fa232d14 | bb30e6218db46d6622f1de1f4725fa4748539712acd7e81160ca18dceae8cf4e |
| entities-teaching.html | e9a0dc18a949ba7102305eb6f3eb540101b037314c10c60c3a81289e018c9a06 | 19de34416a294f4e30fa8adb41caf5cea94d1272b64b64b703df0ba5358539ae |

The structure companion isolates document-to-heading links. The storyboard expands it with source-sentence section nodes during the filmed reveal. The business companion shows the accepted consolidated view; dashed proposals are shown earlier in the scene plan.
