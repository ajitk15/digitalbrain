# One simple example: a library app

Both visuals are inspired by the grouped boxes and arrows in the supplied reference. They describe Digital Brain's actual code paths rather than imposing a generic four-step model.

## Explain it in one minute

Imagine a library app that checks which books are available. The **Book Database** is simply the stored list of books.

The uploaded **Library setup** document says:

> Library App depends on Book Database.

An optional AI extractor may propose this relationship:

**Library App → depends on → Book Database**

Digital Brain checks that the exact quote and both names occur in the source and that Component → depends on → Component is an allowed relationship. It saves the source ID, line, quote and digest with the edge. A person reviews and publishes the graph version.

Someone then asks:

> What does the Library App depend on?

The graph-answer path checks access, selects a published version, ranks evidence, and checks the live source. A configured answer model can then produce:

> The Library App depends on the Book Database. [Library setup]

The answer shown is the intended teaching answer, not a claimed live provider result. A citation points back to the real supporting document text.

## Visual 1: graph generation

| Group | What happens in this project | Library example |
|---|---|---|
| Ingest and read | Files are stored privately, scanned when required, and converted locally using MarkItDown and PDF checks. | Read the Library setup document's sentence. |
| Build relationships | Custom Python first captures document/section/table structure. An explicitly requested OpenAI/Claude run can propose typed semantic edges. | Structural link: document contains this sentence. Optional semantic link: Library App depends on Book Database. |
| Validate evidence | Exact-quote, source identity, entity-name and closed-ontology checks filter proposals. Conservative same-type name merging follows. | Both endpoints are Components, the relation is allowed, and the supporting sentence exists verbatim. |
| Save and review | Django stores a numbered draft with graph JSON and source evidence. Known questions can be benchmarked for expected-source retrieval. | Save draft v1; the question should retrieve Library setup. A benchmark is a report, not a mandatory pass gate for publication. |
| Publish and explore | A human publishes a source-consistent version. Local Graphify/NetworkX/Leiden grouping can make graph themes easier to explore. | Publish v1; graph answers may use it. Themes group existing links, not new facts. |

**Without AI enrichment**, the structural graph does not invent the semantic “depends on” relationship. It still keeps the document/section evidence. The dashed route in the diagram shows this structural-only path. Embeddings are optional, not an obligatory part of every generation run.

## Visual 2: graph retrieval

| Group | What happens in this project | Library example |
|---|---|---|
| Receive the question | Application access and graph-answer configuration are checked. The chosen published snapshot is resolved; short follow-ups can carry conversation-topic terms. | Ask what Library App depends on and use published v1. |
| Search and expand | Custom covering BM25 ranks relationship text. Optional embeddings use NumPy cosine similarity and rank fusion. Typed walks and one-hop expansion are bounded; direct matches are considered first. | Rank the “depends on Book Database” evidence. No walk is needed to answer this direct question, though the algorithm may consider neighbours. |
| Verify and select | Source must remain active/non-restricted, same digest, have the exact quote, and be permitted for this viewer. Results are deduplicated and spread across sources; optional paid reranking reorders only verified evidence. | Keep Library setup only if the quote and access checks still pass. |
| Answer with a citation | The configured OpenAI/Claude runtime receives checked context. In the streaming path, scoped tools may add evidence and final citations are rechecked before delivery/storage. | Intended answer: Book Database, with the Library setup citation. |
| Separate measurement | Saved versions, including drafts, can be benchmarked against known expected sources. Humans decide what to improve and whether to publish another version. | Did this known question retrieve Library setup? This is not automatic training or a mandatory step after every chat answer. |

## Show a useful security change

If the source becomes restricted, is edited, or the user loses applicable answer permission, the old edge is skipped for citation. An old published snapshot does not bypass these live checks. Restriction excludes AI use and citations; it is not deletion or a guarantee that application members cannot see the record through every source/explorer screen.

## Why Graphify appears in generation, not retrieval

Graphify is imported as the local **graphifyy** package. The Knowledge graph uses its Leiden clustering to present themes. The separate Code Graph uses its Tree-sitter AST extraction. The library's semantic AI extractor and graphify.net hosted service are not invoked by these integrations. The “depends on” proposal comes from Digital Brain's own configured provider path, and Graph RAG uses the application's ranking, vectors, graph traversal and evidence checks.

## What was actually verified

The synthetic example is checked against the project's structural builder, real enrichment/ontology validator and entity merging, invented-quote rejection, and real BM25 source ranking. Extractor proposals are mocked: **no paid model request or production database mutation** is performed. Live-database citation verification and actual model wording are documented from code, not claimed as exercised in this example test. See library-example-check.json.

Prepared 13 October 2026. All sample names and documents are synthetic.
