# Retrieval and transparency review - 2026-10-26

Measured before proposing. Two measurements: the scorecard (the fifth gate, no model,
CarePath corpus in a throwaway database) and CarePathDev's live published version 7
(Healthcare ontology, AI-enriched, 43 sources, 16 benchmark questions), scored four ways
from **stored** vectors only, so nothing was paid for.

## What the numbers say

### Live CarePathDev v7

| Scored with | Found | Top 3 | First | MRR |
|---|---|---|---|---|
| AI relationships + vectors (what answers today) | 16/16 | 13 | 11 | 0.773 |
| AI relationships, words only | 15/16 | 11 | 10 | 0.692 |
| No AI relationships, words only | 15/16 | 11 | 10 | 0.692 |
| No AI relationships + vectors | 16/16 | 13 | 11 | 0.783 |

- **Vectors are the largest single gain**: +0.08 MRR. "Which risks are still open?" moves
  from 6th to 1st, "Why did the service fail on 14 August?" from not found to 3rd, "How are
  API tokens issued and rotated?" from 8th to 5th.
- **AI relationships add nothing on these sixteen questions.** Words only, the score is
  identical with or without them; with vectors, the version without them is slightly
  better (one question 2nd instead of 3rd). Graph generation has cost $3.07 on CarePathDev
  so far. This is not a case for switching it off - Code Factory's evidence walks
  relationships and follows references, and sixteen questions are a small sample - but it
  is a case for questions that need a relationship to answer, and for showing it.

Five questions are still not ranked first. In every case the right source **is** found:

| Question | Expected | Rank | Why |
|---|---|---|---|
| Why can a partner download records of people who said no? | KAN-4 | 4 | paraphrase: "said no" is withdrawn consent |
| Why did the service fail on 14 August? | incident postmortem | 3 | the postmortem writes `2026-08-14`, the question "14 August" |
| How is login protected against credential stuffing? | KAN-9 | 4 | broad security documents outrank the ticket |
| Which deployment names does the operations runbook use? | operations runbook | 3 | CARE-444's title says "Operations runbook names a deployment" - a fair answer |
| How are API tokens issued and rotated? | ADR 0002 | 5 | the ADR says "static token directory", not "issued" or "rotated" |

### Scorecard (today, unchanged from the 15 Oct baseline)

Graph 15/16 found, MRR 0.701; keyword and passage 14/16, MRR 0.674. Planted-bug evidence
92% (misses: R-02 for CARE-425, F-03 for CARE-431). The new code-evidence measure reads
5 of 7 planted-bug files cited and 5 of 7 shown in an excerpt; the ranking fix for it is
in progress in its own session. Timings ran several times slower than the baseline while
another session's full test suite was running on the same machine, so they say nothing.

## Would an OpenAI key for embeddings help?

**CarePathDev already has one, and it is working.** `ai.has_embeddings_credential` is
true, 3,213 vectors are stored (`text-embedding-3-small`: 3,067 relationships, 129
passages, 17 questions), every benchmark question used one, and they are the gain in the
table above. No new key is needed. Three things about it:

1. **The price is not set**, so 17 embedding receipts read $0.00 and the budget cannot see
   them. AI settings already flags the model as Not priced. Set it to the list price
   ($0.02 per million input tokens) - the amounts are tiny, but $0 is not true.
2. **Vectors are refreshed only by a requested AI run or `manage.py embed --as`**, never by
   a background rebuild. Nothing on Generate or Quality says how many passages have one.
3. **A larger model is cheap to try and must be measured, not assumed.** Re-embedding about
   3,200 texts with `text-embedding-3-large` costs a few cents. It is worth it only if it
   moves the three paraphrase misses above. The scorecard calls no model, so it cannot
   measure this; the live benchmark can.

## Proposals

### Retrieval

| | Change | Measured by | Cost to run |
|---|---|---|---|
| R1 | **Dates as one term.** `14 August`, `Aug 14` and `2026-08-14` index as the same month-day term (and year when given), in `ranking.terms`, outside `stem` as `term` already does for "-ment". | Scorecard: graph currently misses the 14 August question; live: rank 3 | none |
| R2 | **Code evidence ranked by the ticket's terms**, excerpts centred on the best line. | Scorecard: planted-bug files and excerpts, 5/7 each today | none - in progress |
| R3 | **Follow references from the evidence**, not only the ticket: C-08 names F-03. CLAUDE.md names this as the next step; measure the noise it adds first. | Scorecard: planted-bug evidence 92% | none |
| R4 | **Measure reranking on CarePathDev.** It is on for every chat answer and has never been measured there; the paraphrase misses are what it is for. `manage.py benchmark --rerank --as acmeadmin`. | Live benchmark | ~16 Claude calls, a few cents |
| R5 | **Measure `text-embedding-3-large`** on a copy of the vectors, keep it only if R4 has not already closed the paraphrase misses. | Live benchmark | a few cents |
| R6 | **Add benchmark questions that need a relationship** (a ticket to the requirement it breaks, a control to its finding), so the AI enrichment's value is measured rather than read as zero. | Live benchmark, release check | none |

### Transparency

| | Change |
|---|---|
| T1 | **Say why each source was chosen.** A citation knows whether words, meaning, a walk or a named reference found it, but only the walk leaks out, as raw text inside the excerpt ("Graph v7: ... Reached by: ... Source evidence: ..."). Record `found_by` and the matched terms on each citation and show one line on its chip - "Matched: consent, export", "Similar in meaning", "Via CARE-401 → BR-05", "Names C-09" - with the source's own quote shown cleanly beneath. |
| T2 | **Quality by version shows meaning as well as AI.** The trend separates "with AI / without AI"; vectors, the larger effect, appear only as a per-question note on the benchmark page. Add "words only" beside it. |
| T3 | **Vector coverage where the AI plan is shown** (upload, Knowledge, Generate): "121 of 129 passages have vectors; the next requested run embeds the rest." |
| T4 | **Code Factory: why this file.** Done today: target tags now say what implementation will do, and the review note says it is required. After R2, each code citation can say which ticket terms chose it. |

### Shorter names: "Knowledge" and "Code"

In templates: "Code Graph" 23 times, "Knowledge graph"/"knowledge graph" 23, "Code
graph"/"code graph" 15. In Python, the labels written into each citation at run time:
`Knowledge graph v7` and `Code graph · ajitk15/carepath v1`. **Those are stored inside
plan items**, so renaming the code alone would leave every existing run reading the old
words. The proposal:

- Origin tags become **Knowledge** and **Code** ("Knowledge v7 · 2", "Code · carepath v1"),
  drawn at render time from each citation's `origin` and version rather than its stored
  label, so old and new runs read alike.
- Prose and headings follow ("the published knowledge", "the code snapshot").
- **Unchanged**: the Graph tab inside Knowledge (it is the graph), the feature key
  `code_graph`, every URL and route name, and the worker lane - the same rule the move to
  Knowledge → Code Repos followed.

Open question: tags and prose only, or the feature's own name too (Features screen,
onboarding, AI settings say "Code Graph")?
