# CarePath video v7: review and v8 script

Reviewed: `video-2026-10-14-v7/carepath-growing-knowledge-graph.mp4` (4:00, nine chapters,
Microsoft Heera en-IN). Every claim below was checked against the platform as it is
today. Quality scores were re-run on the real converted files.

---

## 1. Narrative review of v7

**What works**

- The arc is right: one file, then a growing graph. Keep it.
- The chapter rail on the left gives a clear sense of progress.
- The caveats are honest. Grouping "does not add new facts", and AI links still need evidence.
- An Indian English voice was the right call.

**What does not work**

| Problem | Where | Why it hurts |
|---|---|---|
| The example needs a healthcare lesson first | 00:00, "A referral is a request for a patient to see another care team" | IT viewers must learn the domain before they can follow the tech. The lifecycle (draft → submitted → accepted) means nothing to them. |
| No tension and no payoff | Whole video | It ends with "connected stories, requirements, code, and tests", but no question is ever asked and answered. Viewers never see why the graph matters. |
| Definitions break the flow | "A node is simply an item." "Look at the two icons." | These read like a lesson plan, and they are a big part of why it sounds robotic. |
| Disclaimers are spoken aloud | "even though we use simpler labels here", "We are showing a small part of this larger graph", "These vector values are illustrative" | They belong in a small on-screen footnote. Spoken, they make the narrator sound unsure. |
| Every sentence has the same shape | Throughout | Short subject-verb-object lines with no connecting words ("so", "now", "and then") give the machine rhythm. |
| The audio is sped up | `speech-metrics.json`: tempo 1.12× to 1.26× | Each scene's audio was squeezed by 12–26% to fit a fixed slot. That is the single biggest reason it sounds robotic. |
| Captions break mid-clause | e.g. "…source identity and content fingerprint, so each fact can point" | Hard to read, and it looks automated. |
| Django listed as the tech for upload, save and search | Chips on 01, 07, 08 | Django is the web framework. It is not what makes any of those stages work. |

---

## 2. Reality check: what each stage actually does

| Stage | v7 says | What really happens | Main tech to name on screen |
|---|---|---|---|
| Upload | "Django / upload checks" | The file is virus-scanned and refused if the scan fails. Its fingerprint is recorded. | Django · ClamAV virus scan · SHA-256 fingerprint |
| Convert | "MarkItDown converts the Word file into readable text". The screen shows a styled card, not Markdown. | The output is **Markdown**: headings, tables, lists. Conversion runs in a sealed process with no network. Scanned pages can be read by OCR, and each one is marked with its confidence. A Word table whose header row was never marked as a header still gets proper column names. Once converted, the Markdown is the record. The original file is optional. | MarkItDown → Markdown · offline sandbox · Tesseract OCR (optional) |
| **Source quality** | **Missing entirely** | Six rule-based checks, 100 points, no AI: readable text, tables with named columns, structure, says what it is (number, version, owner, date, status), nothing left as TBD, within limits. "Ready" needs 80+ with no check missing. Then **Check sources** reads all files together: shared references, duplicates, and references nothing defines. It suggests and never blocks. | Rule-based quality score · source map |
| Structure graph | "section nodes for the heading and ordinary text lines" | Document → sections (headings and lines) **and** table rows → records → cell values. Rows that share a field and value meet at one value node, which links some things for free. Every node keeps its line number and fingerprint. No AI, no cost. | Python graph builder (no AI) |
| AI meaning | "OpenAI or Claude proposes typed relationships" | Correct, but the controls are the headline: a **fixed vocabulary of 8 types and 12 relationships**, and an **exact-quote check** that throws out anything it cannot find word for word. Only new or changed files go to the model, and the monthly budget is checked first. | OpenAI or Claude · fixed ontology · exact-quote verification |
| Merge | "Matching names and types become one shared node" | Correct: same type **and** the same words, word by word. Each edge keeps its own source and quote. | Entity resolution |
| Search | Presents OpenAI embeddings as *the* search structure | **BM25 keyword ranking is always on and free.** Embeddings are optional and **off by default**: OpenAI vectors with NumPy cosine similarity, fused with the keyword ranking. The graph walk goes up to two hops, and only along AI relationships. | BM25 · OpenAI embeddings (optional) · NumPy · rank fusion · 2-hop walk |
| Draft and organise | "Graphify uses NetworkX and Leiden clustering" | Correct. **Missing:** each saved version is benchmarked automatically against test questions (CarePath: right document found for 15 of 16). | Versioned drafts · Graphify · NetworkX · Leiden · automatic benchmark |
| Publish | "A reviewer checks the evidence and publishes" | Correct. **Missing:** only the published version answers, and every citation is re-checked live (source still active, fingerprint unchanged, quote still present). Restricted sources are never used. | Human review · publish gate · live citation re-check |
| Answer | **Missing** | The question is asked, evidence is retrieved, verified and cited. This is the payoff the video never shows. | Graph RAG with verified citations |

---

## 3. Improvements

1. **Change the example** to one every IT professional already understands (section 4).
2. **Add the source quality stage**, with one good file and one bad file for contrast.
3. **Show real Markdown on screen**: a split view, original on the left and Markdown on the right, with the key row highlighted.
4. **Bring tables into the structure graph** (record → value). That is what makes spreadsheets useful.
5. **End with a real question and a cited answer.** Without it the graph is only a picture.
6. **No short IDs on screen or in the voice.** Say "the page-size rule", "the test-review finding", "the August outage". Crop ID columns out of on-screen Markdown and add a footnote: "IDs hidden for readability; the record keeps them."
7. **No file or code names.** Name file *types* (spreadsheet, Word document, web page) and technologies only.
8. **Stop time-stretching the voice.** Let each scene last as long as its narration plus about half a second. If the total runs long, cut words, not speed.
9. **Better voice.** Heera is an older Windows voice. Azure Neural `en-IN-NeerjaNeural` or `en-IN-PrabhatNeural` (or a human read) sounds far more natural. Add SSML pauses of about 300 ms at commas that carry meaning.
10. **Captions by clause**, at most two lines, and never split "so that" from what follows.
11. **Make the graph bigger** after scene 3. Shrink the chapter rail to icons, because the nodes are tiny at 1080p.
12. **Move the disclaimers on screen** ("illustrative values", "focused view") as a small footnote.

---

## 4. The new example: "Every list API must cap its page size"

Every developer, tester, SRE and architect knows this one: *a list API must cap how many
rows one request can return*. In CarePath it runs through five ordinary IT documents and
ends in a real-looking outage. That gives the video a story with a twist.

| Order | File (type) | What it adds to the story | Quality score (real) |
|---|---|---|---|
| 1 | Non-functional requirements (Excel) | **The rule:** every list endpoint takes a limit, capped on the server. Breach blocks release. | 100 · Ready |
| 2 | Coding standards (Markdown) | **How we build:** "a limit that is accepted and passed through is not a limit." | 100 · Ready |
| 3 | API specification (web page) | **The contract:** the patient search API, with sibling APIs returning 422 above the maximum page size | 100 · Ready |
| 4 | Traceability matrix (Excel) | **The gap:** the test review on 28 May found patient search has no upper limit and no test. Waived on 2 June. | 100 · Ready |
| 5 | Outage postmortem (Word) | **The impact:** 14 August, a search with a limit of 100,000 read the whole table. 45 minutes, 60% of requests failed. | 100 · Ready |

The twist, which is real and in the files: the postmortem asks "Why was it not caught?"
and answers **"It was."** The rule, the finding and the waiver each sat in a different
document. The graph is what puts them side by side.

**Proposed AI relationships.** All of these fit the ontology. Each quote is in the file.
Labels are as shown on screen.

| From | Relationship | To | Quote comes from |
|---|---|---|---|
| Outage | affects | Patient search API | Postmortem, "What happened" |
| Patient search API | violates | Page-size rule | Traceability matrix, test-review finding row |
| Coding standard: bound every list | references | Page-size rule | Coding standards, section 4 |
| Outage | violates | Two-second transaction rule | Postmortem: "past the 2s ceiling" |

> **Before recording:** run these five files through a real AI build and capture which
> proposals are accepted, as v6 did for the referral example. Then ask the scene 10
> question on the published version and screen-capture the real answer. Do not script
> the answer text in advance.

---

## 5. v8 script

About 740 words, so about **5:00** at a relaxed 150 words a minute with short pauses. Do not
speed it up to fit. Numbers are written as they should be spoken. Tech chips are what
appears under each scene title.

**If 4 minutes is a hard limit**, cut words rather than speed: drop the OCR sentence (3),
the "raw export gets sixty" sentence (4) and the budget sentence (7), and fold scene 8
into one line in scene 9 ("A keyword index is built, and embeddings can be added for
meaning."). That brings it to about 600 words, roughly 4:05.

### 1. The problem (0:00)
*Chip: none · Visual: five file icons scattered, each with one line of text, nothing connected*

> Every IT team knows this situation. The rule is written in one document, the coding standard in another, the test finding is sitting in a spreadsheet, and the outage report is in Word. Each one knows a part of the story. Nobody sees the full picture… until something breaks.

### 2. One file goes in (0:20)
*Chip: Django · ClamAV · SHA-256 fingerprint · Visual: the requirements spreadsheet uploads, then a shield tick and a fingerprint badge*

> So let's start small, with just one file: our non-functional requirements spreadsheet. As soon as it's uploaded, it is scanned for viruses, and the platform records a fingerprint of the file. That fingerprint matters later. You'll see why.

### 3. Into Markdown (0:36)
*Chip: MarkItDown · offline conversion · Tesseract OCR (optional) · Visual: split screen, Excel on the left, Markdown on the right, one row highlighted*

> Next, MarkItDown converts the spreadsheet into Markdown. Each sheet becomes a heading, and each row becomes a line in a table. See this row: no unbounded queries. Every list API must cap its page size on the server. That is our rule for today. The conversion runs in a sealed process, with no network at all. And if a page is only a scanned image, Tesseract OCR can read it, and marks that text with how confident it is.

### 4. Is this file good enough? (1:08)
*Chip: Rule-based quality check · no AI · Visual: six check rows filling in, score dial to 100, then a second card at 60*

> Before building anything, the platform checks the document itself. Six simple checks, out of hundred: is the text readable, do the tables have proper column names, are there headings, does it say its version and owner, is anything still marked TBD? There is no AI here, so the same file always gets the same score. Our spreadsheet scores hundred: Ready. A raw export with no column names gets sixty, with the reason for every point lost. It only suggests. It never blocks.

### 5. The structure graph (1:41)
*Chip: Python graph builder · no AI, no cost · Visual: document node → section nodes → record node → two value nodes*

> Now the graph. The document becomes one node, and each heading becomes a section. Each table row becomes a record, and each cell becomes a value. So our rule is linked to "verified by code review and automated test" and "on breach, blocks release". Rows that share a value meet at the same node, so some linking happens for free. Every node remembers its line and its fingerprint. No AI so far, and no cost.

### 6. Four more files (2:12)
*Chip: Same pipeline × 4 · Source check · Visual: four files drop in, each gets a 100 badge, then dotted lines to the page-size rule, then a timeline: May → June → August*

> Same way, let's add four more files: the coding standard, the API specification, the test traceability matrix, and the outage postmortem. Each goes through the same steps: Markdown, quality check, structure. All four score hundred.
>
> Now read them together, and the story is honestly quite shocking. In May, the test review found that patient search had no upper limit. In June, it was waived for the release. And in August, someone searched patients with a limit of one lakh records. The service read the whole table, and for forty-five minutes, sixty percent of requests failed.

### 7. AI adds the meaning (2:50)
*Chip: OpenAI or Claude · fixed vocabulary · exact-quote check · entity merge · Visual: typed edges appear one by one, a rejected proposal fades out in red*

> Structure tells us where things are written. To understand what they mean, we bring in AI. OpenAI or Claude reads each file, but it can only use a fixed vocabulary: eight types, like requirement, component and incident, and twelve relationships, like affects, violates and verifies. So it proposes: the outage affects the patient search API, and that API violates the page-size rule. Every proposal must carry an exact quote. If the quote is not found word for word, it is simply rejected. The same API, named the same way in two files, becomes one node. And only new or changed files go to the model, within the monthly budget.

### 8. Ready for questions (3:35)
*Chip: BM25 · OpenAI embeddings (optional) · rank fusion · two-hop walk · Visual: keyword index beside the graph, an optional vector strip, a path lighting up two hops*

> Next, we prepare for search. A keyword index, BM25, is always built. It is fast, and it is free. If the owner switches on embeddings, each passage also gets a vector, so a question can match by meaning and not just by words. Both rankings are combined, and the graph can walk two steps along these relationships to collect connected evidence.

### 9. Saved, organised, measured (4:00)
*Chip: Versioned drafts · Graphify · NetworkX · Leiden · benchmark · Visual: "Draft 4" badge, nodes tint into three themes, a scoreboard "15 of 16"*

> Every build is saved as a numbered draft. Graphify groups the nodes into themes using Leiden clustering, so the graph is easier to explore. And the draft is tested on its own: on CarePath's sixteen test questions, the right document comes back for fifteen.

### 10. Publish, then ask (4:18)
*Chip: Human review · live citation check · access rules · Visual: Publish button, question typed, answer with three citation chips, each ticking green*

> Finally, a person reviews the draft and publishes it. Only the published version answers questions. So let's ask: why did the service go down on the fourteenth of August? The answer points to the postmortem, the test-review finding and the page-size rule. And every citation is checked again, live: the source is still active, the fingerprint has not changed, and the quote is still there. Restricted documents are never used.
>
> So, one spreadsheet became Markdown, then a small graph. Four files later, the graph tells the complete story: the rule, the gap, the waiver and the outage. And every line points back to where it came from.

**On-screen footnotes (not spoken):** "Short IDs hidden for readability; the record keeps
them." · "Vector values illustrative." · "Focused view of a larger graph."

---

## 6. Why this sounds natural rather than robotic

- It talks *to* the viewer: "Every IT team knows…", "You'll see why", "See this row".
- It joins sentences with "so", "now", "next" and "same way", the way a person explains at a whiteboard.
- It uses Indian English that feels natural without being overdone: "one lakh records", "out of hundred", "honestly quite shocking", "same way".
- Sentence lengths vary, and a short line comes after a long one ("It never blocks.").
- It has one tension point (May → June → August) and one payoff (the cited answer).
- It sets up a callback: the fingerprint in scene 2 pays off in scene 10.
