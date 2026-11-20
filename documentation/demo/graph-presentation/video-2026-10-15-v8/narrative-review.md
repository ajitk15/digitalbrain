# CarePath v8: the story, for review

The whole narrative as it would be spoken, one paragraph per chapter. This is the working
draft; nothing is recorded or rendered from it until we agree it.

**Status:** agreed and built on 2026-10-15 (revision 2). The film follows this text.

**The quality thread:** chapter 4 says why a source must be trusted before it is used and scores
it; chapter 7 holds the LLM to the same standard (every link proves itself with an exact quote);
chapter 9 measures the result (15 of 16 test questions) and checks every citation again, live.

**Technology named:** Python, SHA-256, MarkItDown, LLM (OpenAI or Claude), Tesseract OCR, BM25,
embeddings, Graphify.

---

## 1. One story, many files

Most of an application's documentation is spread across many different files. The requirement
lives in a spreadsheet, the coding standard in a Markdown file, the API in its specification, the
test results in a traceability matrix, and the outage in a Word report. Each file holds one piece
of the story, but none of them can tell it alone. So nobody puts the pieces together, until one
afternoon in August, a single search takes the whole service down.

## 2. One file goes in

Let's see how Digital Brain, a Python platform, brings that story together, starting with just one
of these files: the non-functional requirements spreadsheet. As soon as the file is uploaded, the
platform performs a security check and takes a unique SHA-256 fingerprint of it, so we can always
tell if it changes later.

## 3. Turn it into text

Next, MarkItDown, an open-source converter, turns the spreadsheet into Markdown: plain text that
both people and an LLM can read. Each sheet becomes a heading, and each row becomes a line in a
table. Now look at this highlighted row. It says something simple: when someone asks for a list, the
server must limit how many items it sends back. We'll call it the page-size rule, and the rest of
this story is about what happened when it was broken. The Markdown also gets a fingerprint of its
own, because every answer will later be checked against exactly this text.

## 4. Is this file good enough?

Before building anything, there is one more question to ask: can we trust this file? A graph is
only as good as what goes into it. A table with no column names gives you nodes called "column one"
and "column two", a half-finished document marked TBD gives answers that are not true yet, and a
badly scanned page, read by Tesseract OCR, can match a quote exactly and still be wrong. So the
platform scores every document out of hundred, using six rule-based checks written in Python, with
no LLM involved, so the same file always gets the same score. Our spreadsheet scores a full
hundred. A raw export with no column names scores only sixty, and the platform shows exactly which
points it lost and why. The score is advice, not a gate: the owner decides whether to fix the file
or go ahead.

> **On screen:** three small "what goes wrong" cards appear in turn - a node labelled "column 1",
> a TBD stamp, and a blurred scan with "OCR 41% confidence" - before the six checks and the score.

## 5. Four kinds of node

Now Python code builds the first graph, and it uses just four kinds of node. The whole spreadsheet
becomes a document node. Each heading inside it becomes a section node, like Availability,
Performance and Security. Each table row becomes a record node, and here is our page-size rule. And
each cell in that row becomes a value node: the rule is checked by code review and an automated
test, and if it is broken, the release is blocked. Three other rules also block the release, so they
share this same value node, and that is how related rules get connected. Every node keeps the exact
line it came from, and since no LLM has been used so far, all of this costs nothing.

> **On screen:** a "Four kinds of node" legend - Document, Section, Record, Value - lights up one
> item at a time, and the node being named pulses with its type label beside it, while the others
> dim. The value pills get a "Value" label, which they do not have today.

## 6. Four files, one story

Now let's add the other four files: the coding standard, the API specification, the test
traceability matrix and the outage postmortem. Each one goes through the same steps, and all four
score a full hundred. Looking across the files, the source check notices something interesting:
three of them point to the same page-size rule. And when we read them together, the story they tell
is quite insightful. In May, the test review found that patient search had no upper limit on its
page size. In June, that finding was waived so the release could go ahead, bundled with two others
under a single reason. Then in August, someone searched patients with a limit of one lakh records.
The service tried to read the whole table, and for forty-five minutes, sixty percent of all requests
failed.

## 7. AI connects the files

We could see that story because we read all five files ourselves. The graph cannot, yet: it links
things inside each file, but it cannot tell that the outage in the Word report is about the rule in
the spreadsheet. To connect the files by meaning, we bring in an LLM, OpenAI or Claude. It reads
each file, but it may only use a short, fixed list: eight kinds of thing, like a requirement or an
incident, and twelve kinds of link, like affects or violates. To keep costs low, it reads only files
that are new or have changed, and it stays within the monthly budget. So it suggests links like
these: the August outage affected the patient search API, and that API broke the page-size rule. The
same standard of quality applies here: the LLM must prove every link with an exact quote from the
file, and only links with a matching quote are kept. If the quote is not in the file word for word,
the link is thrown away, like this one. And when two files name the same API in the same way, they
are joined into one node. Now the outage, the API and the rule form one connected chain, so a
question about the outage can be traced all the way back to the rule it broke.

> **On screen:**
> - **Why** (the first two sentences): the outage postmortem and the page-size rule pulse at
>   opposite ends of the graph with a broken dashed line between them and the label "Structure
>   alone: no link between these files".
> - The fixed list, the cost note, the proposed links, a kept quote, a rejected one and the merge,
>   as today; the cost note moves up so the chapter ends on its payoff.
> - **What it gives** (the last sentence): the path outage → patient search API → page-size rule
>   lights up end to end, with the label "One question can now follow the whole chain", and a
>   before/after counter: "Links between files: 0 → 3".

This is the real situation today: CarePathDev's published graph has no AI version, and the only
links between its files are shared boilerplate such as "Owner: Engineering".

## 8. Make it searchable

Before anyone asks a question, the platform gets ready to search in two ways. A BM25 keyword index
finds passages by their exact words, which is fast and free. If the owner turns on embeddings, each
passage also gets a vector, so a question can match by meaning too. Then, from the best matches, the
search follows the graph's links two steps further, which is how a question about the outage can
reach the rule and the waiver.

## 9. Test, publish, ask

Every build is saved as a draft, and Graphify groups its nodes into themes so people can explore
it. Before anyone relies on it, the draft is tested: on CarePath's sixteen test questions, the right
document comes back for fifteen. Once a person reviews it and publishes it, only that published
version answers questions. So let's ask: why did the service go down on the fourteenth of August?
The answer points to the postmortem, the test finding and the page-size rule, and each source is
checked again, live, before it is shown. The pieces were always there. Now, for the first time, they
tell the story together: the rule, the gap, the waiver and the outage, each pointing back to where
it was written.

> **On screen:** the draft chip and Graphify's themes, the 15 of 16 benchmark panel, then Publish,
> the question, the answer with its three checked sources (active, fingerprint and quote ticks),
> and the closing chips: the rule, the gap, the waiver, the outage.
