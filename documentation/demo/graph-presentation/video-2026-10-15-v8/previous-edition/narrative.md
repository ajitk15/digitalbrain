# CarePath video v8: narrative

The full spoken narrative of `carepath-one-rule-five-files.mp4` (5:55, 10 chapters,
61 sentences). Times are where each chapter starts in the film.

This file is a reading copy. The source is `storyboard.json`: edit the words there, then
follow the steps in `README.md` to re-record and re-render. Sentence numbers matter:
`scene.js` pins animations to them (for example `upload.2`), so adding, removing or
reordering sentences in a chapter means renumbering its cues.

---

## 1. The problem (0:00, 21s)

0. Every IT team knows this situation.
1. The rule is written in one document, the coding standard in another, the test finding is sitting in a spreadsheet, and the outage report is in Word.
2. Each one knows a part of the story.
3. Nobody sees the full picture, until something breaks.

---

## 2. One file goes in (0:21, 18s)

*On screen: Django, ClamAV virus scan, SHA-256 fingerprint*

0. So let's start small, with just one file: our non-functional requirements spreadsheet.
1. As soon as it's uploaded, it is scanned for viruses, and the platform keeps a fingerprint of the file.
2. So far, nothing leaves the server.

---

## 3. Into Markdown (0:39, 40s)

*On screen: MarkItDown, Sealed offline conversion, Tesseract OCR · optional*

0. Next, MarkItDown converts the spreadsheet into Markdown.
1. Each sheet becomes a heading, and each row becomes a line in a table.
2. See this row: no unbounded queries.
3. Every list API must cap its page size on the server.
4. That is our rule for today.
5. The conversion runs in a sealed process, with no network at all.
6. And if a page is only a scanned image, Tesseract OCR can read it, and marks that text with how confident it is.
7. The Markdown gets its own fingerprint, and every citation is checked against it later.

> Footnote: ID column hidden for readability. The stored record keeps it.

---

## 4. Is this file good enough? (1:19, 39s)

*On screen: Rule-based source check, No AI · same score every time*

0. Before building anything, the platform checks the document itself.
1. Six simple checks, out of hundred.
2. Is the text readable? Do the tables have proper column names? Are there headings? Does it say its version and owner? Is anything still marked TBD?
3. There is no AI here, so the same file always gets the same score.
4. Our spreadsheet scores hundred. Ready.
5. A raw export with no column names gets sixty, with the reason for every point lost.
6. It only suggests. It never blocks.

> Footnote: Scores are from the real CarePath files.

---

## 5. The structure graph (1:58, 36s)

*On screen: Python graph builder, No AI · no cost*

0. Now the graph.
1. The document becomes one node, and each heading becomes a section.
2. Each table row becomes a record, and each cell becomes a value.
3. So our rule is linked to, verified by code review and automated test, and, on breach, blocks release.
4. Three other rules also block release, so they meet at the same value node. That linking is free.
5. Every node remembers its line, and its source fingerprint.
6. No AI so far, and no cost.

> Footnote: Focused view: a few of the sheet's 31 rows and lines.

---

## 6. Four more files (2:34, 52s)

*On screen: Same pipeline × 4, Source check across files*

0. Same way, let's add four more files: the coding standard, the API specification, the test traceability matrix, and the outage postmortem.
1. Each goes through the same steps: Markdown, quality check, structure.
2. All four score hundred.
3. The source check also notices that three of these files point to the same page-size rule.
4. Now read them together, and the story is honestly quite shocking.
5. In May, the test review found that patient search had no upper limit.
6. In June, it was waived for the release.
7. And in August, someone searched patients with a limit of one lakh records.
8. The service read the whole table, and for forty-five minutes, sixty percent of requests failed.

> Footnote: Dates and figures are from the CarePath postmortem and traceability matrix.

---

## 7. AI adds the meaning (3:27, 50s)

*On screen: OpenAI or Claude, Fixed vocabulary, Exact-quote check, Entity merge*

0. Structure tells us where things are written. To understand what they mean, we bring in AI.
1. OpenAI or Claude reads each file, but it can only use a fixed vocabulary: eight types, like requirement, component and incident, and twelve relationships, like affects, violates and verifies.
2. So it proposes: the outage affects the patient search API, and that API violates the page-size rule.
3. Every proposal must carry an exact quote.
4. If the quote is not found word for word, it is simply rejected.
5. The same API, named the same way in two files, becomes one node.
6. And only new or changed files go to the model, within the monthly budget.

> Footnote: Proposals shown are illustrative. Each must pass the vocabulary and exact-quote checks.

---

## 8. Ready for questions (4:17, 28s)

*On screen: BM25 keyword index, OpenAI embeddings · optional, NumPy, Two-hop graph walk*

0. Next, we prepare for search.
1. A keyword index, BM25, is always built. It is fast, and it is free.
2. If the owner switches on embeddings, each passage also gets a vector, so a question can match by meaning, and not just by words.
3. Both rankings are combined, and the graph can walk two steps along these relationships to collect connected evidence.

> Footnote: Index terms and vector values are illustrative.

---

## 9. Saved, organised, measured (4:45, 20s)

*On screen: Versioned drafts, Graphify, NetworkX, Leiden clustering, Benchmark*

0. Every build is saved as a numbered draft.
1. Graphify groups the nodes into themes using Leiden clustering, so the graph is easier to explore.
2. And the draft is tested on its own: on CarePath's sixteen test questions, the right document comes back for fifteen.

> Footnote: Theme names illustrative. Benchmark figure from the CarePath scorecard baseline.

---

## 10. Publish, then ask (5:05, 50s)

*On screen: Human review, Publish gate, Live citation check, Access rules*

0. Finally, a person reviews the draft and publishes it.
1. Only the published version answers questions.
2. So let's ask: why did the service go down on the fourteenth of August?
3. The answer points to the postmortem, the test-review finding, and the page-size rule.
4. And every citation is checked again, live: the source is still active, the fingerprint has not changed, and the quote is still there.
5. Restricted documents are never used.
6. So, one spreadsheet became Markdown, then a small graph.
7. Four files later, the graph tells the complete story: the rule, the gap, the waiver, and the outage.
8. And every line points back to where it came from.

> Footnote: Answer wording illustrative. Citations are the expected evidence for these five files.
