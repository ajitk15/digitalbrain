# CarePath graph generation: plain-language edition

## 00:00 Start with one file

Let us build a Digital Brain using the CarePath documents in our project. Start with just one file: user stories. We will follow one example, tracking a referral. A referral is a request for a patient to see another care team. Watch how this one file becomes connected knowledge.

Django / upload checks

## 00:20 Read its content

After upload checks, MarkItDown converts the Word file into readable text. The heading describes a user story: track a referral. Below it is the referral lifecycle. We use short, readable labels on screen. Django keeps the original text with its source identity and content fingerprint, so each fact can point back to the file.

MarkItDown / source identity

## 00:44 Build the normal graph

The normal graph begins with a document node. A node is simply an item. Our Python builder adds section nodes for the heading and ordinary text lines. Arrows connect the document to those sections and mean contains section. The same approach handles other content, including table rows and values. We are showing a small part of this larger graph. So far, we have captured the structure of the document.

Custom Python graph builder

## 01:12 Add AI meaning

Now enable optional A I enrichment. OpenAI or Claude reads the text and proposes typed relationships. Look at the two icons. One represents the user story, and the other represents its referral tracking requirement. They become two business nodes joined by references. Each proposal keeps the original source and exact quotation, even though we use simpler labels here. The document structure remains underneath this new business layer. Evidence checks decide whether these proposed links can be accepted.

OpenAI or Claude / optional

## 01:46 Connect more files

Now upload the system design document. Its component table connects referral tracking to the code that manages the referral lifecycle. The graph grows a referral code node and an implemented in connection. Notice that it joins the same referral tracking requirement. Next, add the test coverage spreadsheet. Its row connects the referral lifecycle tests to this requirement. A referral tests node grows into the graph, joined by verifies. Three files now explain one connected story: what we need, where it is implemented, and how it is tested.

MarkItDown / AI enrichment

## 02:25 Validate and merge

Before accepting links, the application checks the source, exact quote, named items, and allowed types and relationships. Matching names and types become one shared requirement node. Each accepted connection keeps its supporting document. Referral tracking joins the story, code, and tests. These checks filter unsupported proposals; a person still reviews meaning.

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