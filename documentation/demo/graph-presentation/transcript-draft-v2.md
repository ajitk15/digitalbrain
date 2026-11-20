# Graph generation — revised two-minute transcript

Draft for review before video generation. Narration: conversational English with an Indian accent. Timings are provisional until the approved narration is recorded.

## 00:00–00:12 · Start with a simple example

**Narration:** “Let’s take a health insurance example: ‘To process a health insurance claim, the claims portal uses the policy database and claims database, and the claims guide explains the claims portal.’ How does this one sentence become a graph?”

**Visual:** One source-note card flows towards an empty graph. Keep the sentence visible throughout the demonstration. It produces four entity nodes: Claims Portal, Policy Database, Claims Database and Claims Guide; and three relationships.

## 00:12–00:25 · Read the notes

**Narration:** “First, we read the documents. For uploaded files, MarkItDown converts the content into text. Our Python code then identifies documents, headings and tables. This gives us the basic structure, before we add meaning.”

**Visual:** File → readable text → document and heading boxes. Show that this example starts with text already prepared.

**Technology:** MarkItDown · Python · custom graph builder.

## 00:25–00:42 · Find items and connections

**Narration:** “Next, optional OpenAI or Claude enrichment suggests four items: the claims portal, two databases, and the claims guide. It proposes three connections: the portal uses each database, and the guide explains the portal. Items become nodes; connections become edges.”

**Visual:** Highlight all four names in the sentence. Reveal Claims Portal → uses → Policy Database, then Claims Portal → uses → Claims Database, then Claims Guide → explains → Claims Portal. Keep each arrow labelled. Do not turn the generic claim phrase into an additional entity node.

**Technology:** Optional OpenAI / Claude · Python enrichment code.

## 00:42–00:59 · Check the evidence

**Narration:** “We don’t accept a suggestion straightaway. Python checks the exact supporting sentence, the named items, and allowed connections. If AI invents a sentence saying the portal uses a payment database, that quote is absent, so the suggestion is rejected. Human review is still needed.”

**Visual:** Proposed connection → checks → accepted / rejected branches. Accepted: the original quote supports the portal-to-policy-database relationship. Rejected: invented quote ‘The claims portal uses a payment database.’ Reason: quote not in source. Show the payment database only in the rejected proposal, outside the final graph.

**Technology:** Python validation · project ontology, meaning allowed item and connection types.

## 00:59–01:14 · Join matching items and keep proof

**Narration:** “All three connections refer to the same claims portal. We keep one shared portal node. Each connection keeps its source and quote. A SHA-256 fingerprint helps detect source changes. Django stores the graph as a numbered draft.”

**Visual:** Portal references from the three relationship proposals merge into one node. Attach source and quote cards to its edges. Show a numbered draft card.

**Technology:** Python entity merging · SHA-256 · Django JSON storage and graph revisions.

## 01:14–01:29 · Understand where Graphify fits

**Narration:** “So, where does Graphify come in? At the grouping stage. Using NetworkX and Leiden clustering, Graphify groups connected items into themes for review. It groups the graph we already built; it does not create these facts from the notes.”

**Visual:** Existing graph → separate theme-grouping box → coloured group around related nodes. Keep this branch visibly separate from AI extraction.

**Technology:** Graphify, installed as graphifyy · NetworkX · Leiden clustering.

## 01:29–01:45 · Review, publish and protect access

**Narration:** “A reviewer checks the draft and publishes it. Graph answers use published versions and check the user’s access and current evidence. Restricted sources stay outside AI enrichment. This keeps source permissions part of the process.”

**Visual:** Draft → human review → published graph. Add an access gate before graph answers and a restricted-source block before AI.

**Technology:** Django · GraphRevision workflow · source access and evidence checks.

## 01:45–02:00 · Show the result honestly

**Narration:** “Now we can see the claims portal, both databases, and the guide together, with evidence for each connection. The planned demo will run our real builder, validator and Graphify grouping with prepared AI suggestions. Saving and publishing will be illustrated.”

**Visual:** Final four-node graph with three labelled edges and the original source quote beside it. Footer: actual project functions; prepared AI suggestions; saving and publication illustrated. Update the executable example to this health insurance source before recording; the existing library demo is not evidence for the revised example.

## Presentation accuracy notes

- Use source wording on the diagram. Explain in the technical detail panel that the stored ontology normalizes ‘uses’ to ‘depends on’ and ‘explains’ to ‘covers’.
- Quote and type checks filter unsupported proposals; they do not prove semantic truth.
- Knowledge graph generation uses the project's custom pipeline. Graphify is used here for theme grouping; its separate code-graph AST extraction is outside this demonstration.
- MarkItDown conversion, persistence and publication are explained; the repeatable synthetic example starts with normalized text and does not write or publish a production revision.
- Do not label the narration as an Indian accent unless the selected voice actually provides it.
- This is a fictional insurance-system example, not a claim about a deployed insurer. Claims Portal, Policy Database and Claims Database use the Component type; Claims Guide uses Runbook. The diagram describes system dependencies and documentation, not insurance coverage or claim eligibility rules.
