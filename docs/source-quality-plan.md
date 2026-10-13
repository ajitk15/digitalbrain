# Source quality - implementation plan

Judge uploaded documents before a graph is built from them: how good each one is
for the graph, and how the set connects. Two storage and conversion changes go with
it: a deployment may stop keeping the original file, and scanned pages can be read
by local OCR.

Grounded in a prototype run over the 27 CarePath documents (scratch code, not in the
repository), converted exactly as an upload is. What it found is quoted per phase.

Estimates are engineering days for one developer who knows the codebase, including
tests and the five verification gates in `CLAUDE.md`.

## Summary

| Phase | What | Effort | New dependency | Model cost | Changes the graph |
|---|---|---|---|---|---|
| 0 | Baseline: scorecard and benchmark recorded | 0.5 d | None | None | No |
| 1 | Source check per document | 3 d | None | None | No |
| 2 | How your sources connect | 3 d | None | None | No |
| 3 | Word tables: bold first row is the header | 1.5 d | None | None | **Yes**, measured |
| 4 | Keep or drop the original file; re-convert | 2 d | None | None | No |
| 5 | Local OCR for scanned pages and images | 4 d | Tesseract, `pypdfium2` | None | New sources only |

Phases 1-3 ship together as one release; 4 and 5 are independent and can follow in
either order. Nothing here calls a model or spends budget.

## Principles

- **Deterministic and free.** Same text, same result, every finding traced to a line.
  The same discipline as `ticket_quality.py`.
- **Suggest, never block.** Like the personal-data scan: the platform says what it
  found; a person decides. Generation is never refused because a source scored low.
- **Measure what the graph will read.** The check uses the graph builder's own
  table parser, so a finding cannot disagree with what `build_graph` does.
- **The invariants are untouched.** Converted text stays in the database because
  citations are verified against it. Conversion stays offline. Restricted sources stay
  out of anything a model reads.

---

## Phase 0 - Baseline (0.5 d)

Branch, then record `manage.py scorecard --record` and a benchmark run on CarePath, so
phase 3, the only phase that changes the graph, has a before.

## Phase 1 - Source check per document (3 d)

**New: `platform_core/source_quality.py`**, shaped like `ticket_quality.py`:
`assess(title, content, report=None)` returns a score out of 100, a verdict (Ready /
Needs work / Poor), and a list of `Check`s, each with what it found, why, and what
would earn the rest. `Check` is reused from `ticket_quality`.

| Check | Looks for | CarePath result |
|---|---|---|
| Readable text | Too short, replacement characters, PDF pages with no text (from the conversion report, phase 4) | All 27 pass |
| Tables the graph can read | Blank header rows, header-only fragments, `Unnamed: 3` / `column 2` headers, empty-cell share, repeated rows | 23 of 92 tables have a blank header; `regulatory-requirements.pdf` has 6 header-only fragments |
| Structure | A prose document with no headings | `operations-runbook.txt` |
| Says what it is | Document ID, version, owner or date in the first lines | Most pass; ADRs carry a date only |
| Finished | TBD, TODO, `??`, "to be confirmed" (`ticket_quality.PLACEHOLDER`) | None found |
| Within limits | Over 2,000 table rows, or near the 1,000,000-character cap | None |

Personal data is shown beside the score, never in it: it is a reason to restrict a
source, not a sign of a bad one.

**Refactor first.** Move the table walk out of `graphs.build_graph` into
`graphs.tables(content)`, used by both. Pin it: `build_graph` output on the CarePath
corpus must be byte-identical before and after, checked by a test that hashes it.

**Stored, not recomputed.** `KnowledgeEntry.quality` (JSON, migration 0077), written
in `workbench.add_knowledge` beside `personal_data`, so uploads, links, SharePoint and
connector imports all get it. It carries `QUALITY_VERSION`; a rule change recomputes
on read, the way `ops_graph.RULES_VERSION` rebuilds. `manage.py check_sources --app
<id>` fills existing rows. Jira records keep their ticket score (`SCORED_KINDS`); the
document check is for everything else.

**Screens.**
- Sources table: a Ready / Needs work / Poor pill per row, beside "Personal data?".
- Source page (`document_detail`, `knowledge_detail`): a "Source check" card listing
  the checks, gaps first, with the line each finding came from.

**Tests.** Each check on a minimal Markdown sample; the CarePath counts above as a
fixture test; the pill and card render; a viewer sees them; a restricted source still
gets a check (it is read by people, not by a model).

## Phase 2 - How your sources connect (3 d)

**New: `platform_core/source_map.py`**, computed over `graphs.sources_for(app)` - the
set the graph would be built from - and cached per `graphs.fingerprint`, like
`graph_ai.prepared`.

- **References.** An ID such as `BR-05`, `C-09` or `NFR-06` is *defined* where it
  starts a table row or a heading, or where three or more lines in one source start
  with the same prefix; it is *mentioned* everywhere else. One regex, uppercase,
  one to six letters, up to four digits per group, with the `ticket_quality`
  stop list (`SHA`, `ISO`, `TLS`, ...) plus `SEV`.
- **Per source:** what it defines, what it cites, how many sources cite it, how many
  table values it shares with another source, and its closest neighbour by vocabulary
  (TF-IDF cosine over `ranking.terms`).
- **Across the set:**

| Finding | Wording | CarePath result |
|---|---|---|
| Not referenced anywhere else | "12 user stories (US-01 to US-12) are not mentioned by any other source." Grouped by prefix | US-01..12, TC-1..6, R-02..12 |
| Mentioned, not found | "Not found in your sources: F-01 to F-04." A hint, not an error: an ID defined mid-cell reads as missing | F-01..04, A-01..03 |
| Isolated | Cites nothing defined elsewhere and is cited by nothing | None |
| Off-topic | Closest neighbour below a threshold | None. CarePath's lowest is 0.15; calibrate the threshold on two unrelated documents before shipping |
| Duplicate | Same digest, or vocabulary cosine at or above 0.9 | None |
| How the graph will join them | "7 of 27 sources share a table value with another; references connect all 27." | Shows why the structural graph has few cross-document links |

**Screens.**
- Sources tab, above the table: one summary line, for example "21 ready, 4 need work,
  2 poor. 27 of 27 connect to another source" (illustrative figures), and a "Check
  sources" link.
- `source-check` page, opened as a popup (`data-modal`) and complete on its own:
  the findings above, then the per-source table, each source linking to its page.
- Generate dialog: "6 sources need attention. Review them" linking to the same page.
  It never disables Generate.

**Tests.** Each finding on a three-source fixture; foreign-application sources never
appear; a restricted source is left out like the graph leaves it out; the popup
renders with and without JavaScript; one Playwright test opens it from Sources
(`CLAUDE.md`: a new `data-modal` flow needs one).

## Phase 3 - Word tables: the bold first row is the header (1.5 d)

**Problem.** MarkItDown converts a Word table whose header is not marked as a header
row into a blank header row, with the real header as the first data row in bold:

```
|  |  |  |
| --- | --- | --- |
| **Criterion** | **Source** | **Status** |
```

`build_graph` then names every relationship in that table `column 1`, `column 2`,
makes a junk record from the header row, and cannot join those values to any other
source. 23 of CarePath's 92 tables, across 12 documents, are like this.

**Fix in the parser, not the converter.** In `graphs.tables`: when every header cell
is blank and every cell of the next row is wrapped in `**`, that row is the header.
Stored content is unchanged, so digests, citations and quotes are untouched, and every
existing source benefits without re-conversion. AI extraction and retrieval read the
text as before.

**Rebuild once.** The generator in `graphs.fingerprint` moves from `structural-v1` to
`structural-v2`, so each application's structural graph rebuilds once, for free, as a
new draft. Nothing is published automatically.

**Measure.** Scorecard and benchmark before and after. Expected: more cross-document
value nodes, fewer orphans, retrieval equal or better. If any benchmark question gets
worse it is investigated before merging, as `CLAUDE.md` requires for ranking changes.
Raise the baseline in the same commit if it earned it.

PDF tables broken across lines are **flagged, not repaired**: no deterministic rule
reassembles them reliably. The check suggests uploading the Word or Excel original.

## Phase 4 - Keep or drop the original file; re-convert (2 d)

**Today** the original sits in `.runtime/documents/<org>/<app>/<id>.quarantine` forever.
After a successful conversion it is never read again.

**Setting:** `keep_original_documents` in the config, **true by default**, accepted in
production.

- When false: after a successful conversion commits (`transaction.on_commit`), the file
  is deleted and `Document.original_removed_at` is set. SHA-256, size, name and
  converter version stay as the record of what was converted.
- A failed conversion keeps its file, so Retry works exactly as now.
- A link-sourced document can still be fetched again from its link.

**Conversion report.** `Document.conversion_report` (JSON): page count, pages with no
text, pages read by OCR (phase 5), converter version. Filled from `pypdf`, which is
already a dependency, before the file can be removed. Phase 1's readable-text check
reads it.

**Re-convert.** An owner action on a ready document whose original is still stored:
converts again and supersedes the entry through `add_knowledge`, the way an updated
record already does. It is how existing documents pick up a converter improvement or
OCR. With the original removed, the page says to upload it again.

**Order matters.** Re-convert any documents that need OCR before turning the
setting off; after that, only a new upload can improve them.

**Tests.** The file is gone after success and kept after failure; Retry still works;
re-convert supersedes and keeps the restriction; `load_config` rejects a non-boolean;
`CLAUDE.md` records the setting under the secrets and storage invariants.

## Phase 5 - Local OCR for scanned pages and images (4 d)

**Today** there is none. A scanned PDF fails with "Image-only PDFs require OCR"; an
image fails as unconvertible; and in a mixed PDF the scanned pages disappear without
a word.

**Engine: Tesseract, local and opt-in.**
- `ocr = true` in the config; off by default. `processing.ocr_ready()` reports a
  missing binary the way `scanning_ready()` reports a missing scanner.
- Runs inside `scripts/extract_text.py`, the same subprocess whose network calls are
  already refused, so "conversion never touches the network" still holds.
- PDF: each page with no text layer is rendered with `pypdfium2` (new pip dependency,
  no system libraries) and read by Tesseract. Pages with text are untouched.
- Images: `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff` are accepted for upload only when
  OCR is on.
- Limits: at most 50 OCR pages per document, Pillow's decompression-bomb guard, and
  a longer conversion timeout only when OCR is on. Over the limit fails with a
  message that says which limit.

**Honest output.** The converted text says which pages were read by OCR, and the
conversion report keeps Tesseract's mean confidence per page. The source check scores
low-confidence OCR as Needs work, so a poor scan is visible before it is cited.

**Install.**
- Container: `tesseract-ocr` and `tesseract-ocr-eng` beside `clamav` in the
  `Dockerfile`. Other languages are a deployment choice.
- Windows development: the UB Mannheim installer; `tesseract_path` in the config if
  not on `PATH`.

**Tests.** A page drawn with Pillow is read back; a mixed PDF keeps its text pages and
reads its image page; the page cap and timeout fail cleanly; every test skips and says
why where Tesseract is missing, like the browser tests.

**Not chosen: cloud OCR** (Azure Document Intelligence, AWS Textract). Better on forms
and handwriting, but it sends documents to a third party and is billed per page. That
breaks offline conversion on purpose, so it would need its own decision, a
per-application credential under the secrets rules, and a budget check.

---

## Decisions needed before starting

1. **Phase 3 in the parser, as proposed**, rather than rewriting the Markdown at
   conversion. The parser fixes existing sources without re-conversion and leaves
   stored text and digests alone.
2. **`keep_original_documents` defaults to true.** Set it false per deployment once
   any needed re-conversions are done.
3. **Tesseract as a system dependency and `pypdfium2` as a Python one**, for phase 5.

## Out of scope, worth measuring later

- **References as graph links.** A line citing `BR-05` linked to the row that defines
  it, with that line as the quote. Phase 2 shows this is where CarePath's documents
  actually connect, but it adds many edges and needs a benchmark for noise first.
- **Contradiction detection.** One ID defined differently in two sources. The
  prototype could not tell a traceability matrix listing an ID from a second
  definition of it.
