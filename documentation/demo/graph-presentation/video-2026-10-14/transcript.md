# Graph generation in two minutes

Synthetic sources; actual builder, validator and Graphify run. AI proposals are controlled.

## 00:00 · How simple words become a graph
Let's see how Digital Brain turns a short library note into a graph, using simple phases and a repeatable run of our project.

## 00:09 · Read a short note
The library app uses a book list. A help guide explains the app. The book list is a small data store. These notes provide evidence.

## 00:21 · Find the document parts
Mark It Down reads uploaded files. Python finds documents and sections. Each item becomes a node; a connection is an edge. Meaning links come later.

## 00:33 · Connect the key items
Optional OpenAI or Claude enrichment finds key items: library app and book list. It proposes their uses connection, item types, source identifier, and exact quote. Similar connection words get one standard label.

## 00:49 · Check the quoted words
The server checks the quote, named items, and allowed types. An invented toy-list quote is rejected because it is absent from the note. These filters reduce errors; they do not prove every interpretation.

## 01:03 · Keep one item; keep its source
Both notes refer to one library app, so matching names are merged. Every connection keeps its source, quote, and content fingerprint to detect later changes.

## 01:15 · See the real builder output
Our actual builder run accepts two meaning links, merges the shared app, and rejects the invented quote. AI proposals are controlled here; no live model call is made.

## 01:28 · Save and review
Django saves a numbered draft. Review quality and evidence. Graphify groups existing connected nodes using Network X and Leiden clustering; it does not invent facts.

## 01:40 · Publish the reviewed version
A person publishes the reviewed graph. Answers check current evidence and viewer access. Restricted sources stay outside AI.

## 01:50 · Read. Find. Connect. Check.
A graph connects useful items and keeps their original evidence. AI suggests links. The application checks them. A person reviews and publishes.
