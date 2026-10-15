# CarePath video v8: narrative

The full spoken narrative of `carepath-one-rule-five-files.mp4` (7:14, 9 chapters,
55 sentences). Times are where each chapter starts in the film.

Revision 2, agreed with the reviewer on 2026-10-15 through `narrative-review.md`: presenter fixes,
plain-language titles, the key technology named, and a quality thread through chapters 4, 7 and 9.
The first edition (5:55, ten chapters) is kept in `previous-edition/`.

This file is a reading copy. The source is `storyboard.json`: edit the words there, then
follow the steps in `README.md` to re-record and re-render. Sentence numbers matter:
`scene.js` pins animations to them (for example `upload.1`, or `upload.1@0.55` for a point
partway through a sentence), so adding, removing or reordering sentences in a chapter means
renumbering its cues, and rewording a sentence may mean nudging the fractions.

---

## 1. One story, many files (0:00, 33s)

0. Most of an application's documentation is spread across many different files.
1. The requirement lives in a spreadsheet, the coding standard in a Markdown file, the API in its specification, the test results in a traceability matrix, and the outage in a Word report.
2. Each file holds one piece of the story, but none of them can tell it alone.
3. So nobody puts the pieces together, until one afternoon in August, a single search takes the whole service down.

---

## 2. One file goes in (0:33, 24s)

*On screen: Python · Django, ClamAV security check, SHA-256 fingerprint*

0. Let's see how Digital Brain, a Python platform, brings that story together, starting with just one of these files: the non-functional requirements spreadsheet.
1. As soon as the file is uploaded, the platform performs a security check and takes a unique SHA-256 fingerprint of it, so we can always tell if it changes later.

---

## 3. Turn it into text (0:57, 42s)

*On screen: MarkItDown, Markdown, SHA-256 fingerprint*

0. Next, MarkItDown, an open-source converter, turns the spreadsheet into Markdown: plain text that both people and an LLM can read.
1. Each sheet becomes a heading, and each row becomes a line in a table.
2. Now look at this highlighted row.
3. It says something simple: when someone asks for a list, the server must limit how many items it sends back.
4. We'll call it the page-size rule, and the rest of this story is about what happened when it was broken.
5. The Markdown also gets a fingerprint of its own, because every answer will later be checked against exactly this text.

> Footnote: ID column hidden for readability. The stored record keeps it.

---

## 4. Is this file good enough? (1:38, 59s)

*On screen: Python source check, Tesseract OCR confidence, No LLM · same score every time*

0. Before building anything, there is one more question to ask: can we trust this file?
1. A graph is only as good as what goes into it.
2. A table with no column names gives you nodes called "column one" and "column two", a half-finished document marked TBD gives answers that are not true yet, and a badly scanned page, read by Tesseract OCR, can match a quote exactly and still be wrong.
3. So the platform scores every document out of hundred, using six rule-based checks written in Python, with no LLM involved, so the same file always gets the same score.
4. Our spreadsheet scores a full hundred.
5. A raw export with no column names scores only sixty, and the platform shows exactly which points it lost and why.
6. The score is advice, not a gate: the owner decides whether to fix the file or go ahead.

> Footnote: Scores are from the real CarePath files. The three failure cards are examples.

---

## 5. Four kinds of node (2:37, 50s)

*On screen: Python graph builder, No LLM · no cost*

0. Now Python code builds the first graph, and it uses just four kinds of node.
1. The whole spreadsheet becomes a document node.
2. Each heading inside it becomes a section node, like Availability, Performance and Security.
3. Each table row becomes a record node, and here is our page-size rule.
4. And each cell in that row becomes a value node: the rule is checked by code review and an automated test, and if it is broken, the release is blocked.
5. Three other rules also block the release, so they share this same value node, and that is how related rules get connected.
6. Every node keeps the exact line it came from, and since no LLM has been used so far, all of this costs nothing.

> Footnote: Focused view: a few of the sheet's 31 rows and lines.

---

## 6. Four files, one story (3:27, 57s)

*On screen: Same pipeline · 4, Source check across files*

0. Now let's add the other four files: the coding standard, the API specification, the test traceability matrix and the outage postmortem.
1. Each one goes through the same steps, and all four score a full hundred.
2. Looking across the files, the source check notices something interesting: three of them point to the same page-size rule.
3. And when we read them together, the story they tell is quite insightful.
4. In May, the test review found that patient search had no upper limit on its page size.
5. In June, that finding was waived so the release could go ahead, bundled with two others under a single reason.
6. Then in August, someone searched patients with a limit of one lakh records.
7. The service tried to read the whole table, and for forty-five minutes, sixty percent of all requests failed.

> Footnote: Dates and figures are from the CarePath postmortem and traceability matrix.

---

## 7. AI connects the files (4:24, 85s)

*On screen: LLM: OpenAI or Claude, Fixed vocabulary, Exact-quote check, Entity merge*

0. We could see that story because we read all five files ourselves.
1. The graph cannot, yet: it links things inside each file, but it cannot tell that the outage in the Word report is about the rule in the spreadsheet.
2. To connect the files by meaning, we bring in an LLM, OpenAI or Claude.
3. It reads each file, but it may only use a short, fixed list: eight kinds of thing, like a requirement or an incident, and twelve kinds of link, like affects or violates.
4. To keep costs low, it reads only files that are new or have changed, and it stays within the monthly budget.
5. So it suggests links like these: the August outage affected the patient search API, and that API broke the page-size rule.
6. The same standard of quality applies here: the LLM must prove every link with an exact quote from the file, and only links with a matching quote are kept.
7. If the quote is not in the file word for word, the link is thrown away, like this one.
8. And when two files name the same API in the same way, they are joined into one node.
9. Now the outage, the API and the rule form one connected chain, so a question about the outage can be traced all the way back to the rule it broke.

> Footnote: Proposals shown are illustrative. Each must pass the vocabulary and exact-quote checks.

---

## 8. Make it searchable (5:49, 32s)

*On screen: BM25 keyword index, Embeddings · optional, NumPy, Two-hop graph walk*

0. Before anyone asks a question, the platform gets ready to search in two ways.
1. A BM25 keyword index finds passages by their exact words, which is fast and free.
2. If the owner turns on embeddings, each passage also gets a vector, so a question can match by meaning too.
3. Then, from the best matches, the search follows the graph's links two steps further, which is how a question about the outage can reach the rule and the waiver.

> Footnote: Index terms and vector values are illustrative.

---

## 9. Test, publish, ask (6:22, 53s)

*On screen: Versioned drafts, Graphify, Benchmark, Publish gate, Live citation check*

0. Every build is saved as a draft, and Graphify groups its nodes into themes so people can explore it.
1. Before anyone relies on it, the draft is tested: on CarePath's sixteen test questions, the right document comes back for fifteen.
2. Once a person reviews it and publishes it, only that published version answers questions.
3. So let's ask: why did the service go down on the fourteenth of August?
4. The answer points to the postmortem, the test finding and the page-size rule, and each source is checked again, live, before it is shown.
5. The pieces were always there.
6. Now, for the first time, they tell the story together: the rule, the gap, the waiver and the outage, each pointing back to where it was written.

> Footnote: Theme names and answer wording illustrative. Benchmark from the CarePath scorecard baseline.
