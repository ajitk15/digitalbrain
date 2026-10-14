# CarePath: one file becomes connected knowledge

## 00:00 Start with one file

Let us build a Digital Brain using the CarePath documents in our project. Start with just one file: user stories. We will follow one example, tracking a referral. A referral is a request for a patient to see another care team. Watch how this one file becomes connected knowledge.

Django / upload checks

## 00:20 Read its content

After upload checks, MarkItDown converts the Word file into readable text. Here is the actual heading: U S zero eight, Track a referral, F R zero eight. Below it is the referral lifecycle. Django keeps this text with its source identifier and content fingerprint. Every extracted fact can point back to this file.

MarkItDown / source identity

## 00:44 Build the normal graph

The normal graph begins with a document node. A node is simply an item. Our Python builder adds section nodes for the heading and ordinary text lines. Arrows connect the document to those sections and mean contains section. The same approach handles other content, including table rows and values. We are showing a small part of this larger graph. So far, we have captured the structure of the document.

Custom Python graph builder

## 01:12 Add AI meaning

Now enable optional A I enrichment. OpenAI or Claude reads the text and proposes typed relationships. Look at the highlighted heading. U S zero eight is a user story, and F R zero eight is a requirement. They become two business nodes, joined by references. The proposal carries the original source and exact quotation. The document structure stays in the graph underneath this new business layer. These links are still proposals at this point; evidence checks decide whether they can be accepted.

OpenAI or Claude / optional

## 01:46 Connect more files

Now upload the architecture document. Its component table connects F R zero eight to domain slash referral underscore state dot py. That is the code module for the referral lifecycle. The graph grows a code node and an implemented in connection. Notice that F R zero eight is the same requirement. Next, add the traceability spreadsheet. Its row connects Test Lifecycle Map with F R zero eight. A test node grows into the graph, joined by verifies. Three different files now describe one connected story: what we need, where it is implemented, and how it is tested.

MarkItDown / AI enrichment

## 02:25 Validate and merge

Before accepting links, the application checks the source, exact quote, named items, and allowed types and relationships. Matching names and types become one shared requirement node. Each accepted connection keeps its supporting document. F R zero eight joins the story, code, and tests. These checks filter unsupported proposals; a person still reviews meaning.

Python / ontology / entity merge

## 02:51 Prepare search

Prepare search adds a search structure beside the graph. With embeddings enabled, OpenAI converts text into a numerical vector. Watch the text become numbers and enter the search index. The numbers represent meaning, not new graph facts. Later, a question such as, how do we test referral tracking, gets its own vector. Similarity search can find relevant text. Graph traversal follows links to the requirement and tests. These vector values are illustrative; unchanged text can reuse its stored vector.

OpenAI embeddings / Django / NumPy

## 03:25 Organise the graph

Django saves a numbered draft. Graphify uses NetworkX and Leiden clustering to organise existing graph nodes into themes for review. The graph is now easier to explore across documents. Grouping adds organisation; it does not add new facts or prove a relationship.

Django / Graphify / NetworkX / Leiden

## 03:43 Review and publish

A reviewer checks the evidence and publishes. Restricted sources are excluded, and retrieval checks access and current evidence again. One CarePath file has grown into connected stories, requirements, code, and tests, with supporting sources.

Human review / access checks