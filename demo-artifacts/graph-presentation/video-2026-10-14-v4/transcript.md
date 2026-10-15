# Digital Brain graph generation

Indian English narration — Microsoft Heera (en-IN).

## 00:00 — Upload source documents

Let’s build a Digital Brain from three health insurance documents. We upload a Policy System note, a Claims Processing note, and a Claims Guide. The first explains where policy details come from, the second describes claim records, and the third explains the portal. We’ll follow their actual sentences into the graph.

Technology: Django · Python · private file storage

## 00:22 — Convert files into readable text

The application checks access and file integrity. MarkItDown converts the PDF and Word files into readable text. Here, each document becomes an Overview heading followed by its sentence. Django stores this text as a knowledge source. A source identifier tells us where it came from; a SHA-256 fingerprint helps detect whether the content changes.

Technology: MarkItDown · Django KnowledgeEntry · SHA-256

## 00:47 — Build the document structure

Now we request graph generation. First, our Python builder creates a node for each document. A node simply means an item in the graph. It also creates section nodes from headings and ordinary text lines. Watch the Policy System document connect to its Overview heading and its source sentence. These arrows say ‘contains section.’ The other documents follow the same pattern. Tables, when present, produce records and field values. At this stage, we have captured document structure; we have not inferred which business system uses which database.

Technology: Custom Python structural graph builder

## 01:26 — Extract business relationships

With optional AI enrichment enabled, OpenAI or Claude reads the text and proposes business relationships. Let’s examine the first sentence. ‘Claims portal’ is one item, ‘policy database’ is another, and ‘uses’ connects them. We display that as two nodes joined by an arrow. The proposal also identifies their types, the source document and the exact quote. The second document proposes another uses connection, this time to Claims Database. The third proposes Claims Guide explains Claims Portal. These are suggestions first. They become accepted connections only after the application checks them.

Technology: Optional OpenAI / Claude · Python enrichment

## 02:10 — Check the supporting evidence

Each proposal passes through checks. Does the source exist? Is the exact quote present? Are the named items in it? Are their types and relationship allowed? Our valid examples pass. An invented sentence saying the portal uses a Payment Database fails because that quote is absent. These checks filter unsupported proposals; they do not replace human review of meaning.

Technology: Python validator · project ontology

## 02:36 — Join matching business entities

The three proposals mention the same Claims Portal. Matching names and types become one shared node. Now the business view has four nodes and three connections. Select an arrow, and its original document and supporting quote can explain why it exists. The full graph also retains document structure and evidence links.

Technology: Python entity merging · source provenance

## 02:58 — Prepare search embeddings

When search embeddings are enabled for this requested AI run, OpenAI converts relationship text and document passages into numerical vectors. Django stores them for later meaning-based search. The vectors help find information when a question is asked; they do not create or prove the graph connections. Unchanged text can reuse its stored vector.

Technology: Optional OpenAI embeddings · Django storage

## 03:22 — Save and organise the draft

Django saves a numbered draft. For review, Graphify uses NetworkX and Leiden clustering to organise existing connected nodes into themes. It groups the graph we built; it does not invent the underlying facts.

Technology: Django GraphRevision · Graphify · NetworkX · Leiden

## 03:38 — Review and publish

A reviewer checks the draft and publishes it. Restricted sources are excluded from graph generation. Later, graph answers check current evidence and user access again. We have turned three documents into connected knowledge, with proof behind the connections. Next, we’ll see how retrieval uses it.

Technology: Django publication workflow · access and evidence checks