# Document build

The documents in `../docs/` are **generated**. Their Markdown originals live in
`source/`, and `build_formats.py` renders each one into the format that kind of
document actually takes in a health network.

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe python-docx python-pptx openpyxl reportlab
.venv/Scripts/python.exe build_formats.py      # rebuild ../docs/
.venv/Scripts/python.exe verify_conversion.py  # convert every output via the platform
```

## Why the formats differ

The point is to exercise Digital Brain's ingestion across the range a customer
actually has. An estate is never all Markdown: it is Word documents signed by a
board, spreadsheets that were born as grids, an intranet page, a PDF issued by
governance, a deck someone presented, and a text file opened on a pager at three
in the morning.

| Format | Count | Which documents, and why |
| --- | :-: | --- |
| `.docx` | 8 | Requirements, security design, test strategy and plan, release plan, postmortem, change policy — the ones that get signed |
| `.xlsx` | 5 | NFRs, traceability matrix, SLOs, control matrix, risk register — documents that are **born as grids** |
| `.md` | 7 | ADRs, data model, coding standards, deployment runbook — they live in the repository beside the code |
| `.html` | 3 | Architecture, API specification, release notes — published pages |
| `.pptx` | 2 | The lifecycle overview, and the release readiness review |
| `.pdf` | 1 | Regulatory requirements, issued read-only by governance |
| `.txt` | 1 | Operations runbook |
| `.csv` | 1 | The CARE board export |
| `.json` | 1 | `jira-export.json`, in Jira Cloud's API shape |

**Each document exists in exactly one format.** Two copies would be two sources
in the knowledge graph saying the same thing, and a citation would point at
whichever happened to be indexed first.

The spreadsheets are the interesting case: a traceability matrix or a risk
register converts back to a *grid*, not to prose. The conversion is a genuine
change of shape rather than a change of container.

## Do not load `source/`

It holds the editable originals of documents that already exist in `../docs/`.
Loading both is exactly the duplication described above.

## Verification

`verify_conversion.py` does not use a converter of its own. It runs the
platform's `scripts/extract_text.py` as a subprocess, with the same suffix
argument `platform_core.processing` passes, using the platform's interpreter —
so a file that converts here converts on upload.

It also checks that each output still *says* the right thing afterwards. A file
that converts is not the same as a file that converts usefully: an empty
spreadsheet converts perfectly and carries nothing. The table asserts that
`BR-05` survives the round trip through PowerPoint, `T-03` through Excel,
`REG-03` through PDF, and so on.

Current state: **34 files, 0 conversion failures, 0 content checks missed.**

## Files

| File | Purpose |
| --- | --- |
| `mdparse.py` | A small Markdown reader — the subset these documents use, not a general implementation |
| `renderers.py` | One writer per format: docx, xlsx, html, txt, pdf, csv |
| `decks.py` | PowerPoint, both from a document and from slides written as slides |
| `build_formats.py` | The manifest and the build |
| `verify_conversion.py` | Round-trips every output through the platform's own converter |
| `source/` | The Markdown originals |

## Two things that bit during the build

**`| | |` read as a table divider.** These documents open with a metadata table
whose header row is empty. `set("| | |")` is a subset of the divider character
set, so the parser dropped the row, mistook the first data row for the header,
and stopped recognising the metadata table. The divider check now requires a
dash.

**Blank cells convert to `NaN`.** The platform reads a spreadsheet through a
dataframe, so a sheet with gaps becomes a grid of `NaN` in the Markdown — and a
citation quoting `NaN` is a citation nobody trusts. Every sheet now keeps all its
columns populated, and ragged table rows are padded.
