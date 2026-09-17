# Demo artifacts — CarePath

A complete, governed software lifecycle for one release of one healthcare
service, built to demonstrate Digital Brain across the whole SDLC.

> ## Demonstration software
>
> **CarePath is not a medical device and must never hold real patient data.**
> Every record in it is synthetic. The demonstration tokens are published in
> this repository because they protect nothing. Do not deploy it, do not point
> it at a clinical system, and do not connect it to anything holding protected
> health information.
>
> The risk score is illustrative arithmetic with no clinical validity. It ranks
> a worklist; it does not advise.

## What is here

```
demo-artifacts/
├── carepath/          the service: FastAPI, Python 3.12, 111 tests, 91% coverage
├── docs/              seven lifecycle phases, 27 documents, 7 file formats
├── demo-kit/          how to run the demonstration
└── tools/             the document build, and its Markdown sources
```

### `carepath/` — the codebase

A care-coordination and referral API for a regional health network. Patients,
encounters, observations, referrals, consent, readmission risk, an audit trail,
and a FHIR R4 export.

```bash
cd demo-artifacts/carepath
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -e . pytest httpx ruff coverage
.venv/Scripts/python.exe -m pytest -q          # 111 passed
.venv/Scripts/ruff.exe check src tests         # clean
.venv/Scripts/python.exe -m carepath --seed    # http://127.0.0.1:8100/docs
```

### `docs/` — the lifecycle, in seven file formats

| Phase | Contents | Formats |
| --- | --- | --- |
| `00-overview` | The lifecycle map and the identifier scheme | `.pptx` |
| `01-requirements` | Business, user stories, non-functional, regulatory | `.docx` `.xlsx` `.pdf` |
| `02-design` | Architecture, data model, security design, four ADRs | `.html` `.docx` `.md` |
| `03-build` | Coding standards, API specification | `.md` `.html` |
| `04-test` | Strategy, plan, traceability matrix | `.docx` `.xlsx` |
| `05-release` | Release plan, readiness deck, deployment runbook, release notes | `.docx` `.pptx` `.md` `.html` |
| `06-operate` | SLOs, operations runbook, an incident postmortem | `.xlsx` `.txt` `.docx` |
| `07-govern` | HIPAA control matrix, risk register, change management | `.xlsx` `.docx` |

The mixture is deliberate. No customer's estate is all Markdown: it is Word
documents signed by a board, spreadsheets that were born as grids, an intranet
page, a PDF issued by governance, a deck someone presented, and a text file
opened on a pager at three in the morning. Each document exists in **one**
format, chosen to match how that kind of document really lives — see
[`tools/README.md`](tools/README.md) for the rationale per file.

Every one of them has been round-tripped through the platform's own
`scripts/extract_text.py`: **34 files, 0 conversion failures**, with content
spot-checks confirming that `BR-05` survives PowerPoint, `T-03` survives Excel
and `REG-03` survives PDF.

Start at [`docs/00-overview/sdlc-map.pptx`](docs/00-overview/sdlc-map.pptx).

Identifiers thread the whole set. `BR-05` is the one to follow: stated in the
business requirements, designed as an append-only consent table, implemented in
`services/consent.py`, fully tested there, claimed as control C-09 in the HIPAA
matrix — and never consulted by the export path, which is how it became T-03,
then waiver W-01, then risk R-01.

### `demo-kit/` — running the demonstration

| File | Purpose |
| --- | --- |
| `ingestion-plan.md` | Set the application up. Do this first |
| `demo-script.md` | Five acts, 35 minutes, or 15 for the short form |
| `jira-tickets.md` | Seven CARE tickets — five bugs, one story, one task |
| `jira-export.json` | The same board in Jira Cloud's API shape, verified against the platform's own adapter |
| `build_jira_export.py` | Regenerates that JSON from plain-text bodies |
| `create_jira_issues.py` | Creates the same seven tickets in a live Jira Cloud project |
| `CARE-board-export.csv` | The board as a tracker CSV export |
| `factory-tickets.md` | Run sheet: which ticket for which audience, expected output per phase |
| `seeded-gaps.md` | **Answer key. Operator only** — do not open in front of a customer, do not load as a source |

### `tools/` — the document build

`docs/` is generated. `tools/source/` holds the Markdown originals and
`tools/build_formats.py` renders them.

```bash
cd demo-artifacts/tools
uv venv .venv
uv pip install --python .venv/Scripts/python.exe python-docx python-pptx openpyxl reportlab
.venv/Scripts/python.exe build_formats.py      # rebuild docs/
.venv/Scripts/python.exe verify_conversion.py  # 34 files, 0 failures
```

Edit a document in `tools/source/` and rebuild. **Do not load `tools/source/`
into Digital Brain** — those are the originals of documents that already exist
in `docs/`, and loading both duplicates every source.

## The story

Release 1.0.0 shipped on 2026-06-04 with three high-severity findings open under
a single bundled waiver. Six weeks later one of them took production down for 45
minutes. Two are still open, and one of those is a control that the governance
documentation claims under HIPAA §164.502(a) and the software does not contain.

Every one of those gaps was written down before release — in the traceability
matrix, in the release plan, in the control matrix, in the risk register. Four
documents, four owners, four different months.

They shipped anyway, because nobody read those four documents together at the
moment the decision was made.

That is what the demonstration is about. Not writing documents — the customer
already has documents. Reading them together, on demand, with citations that are
verified against the live source before anyone sees them.

## Honesty about the seeded gaps

The defects in this codebase are **real**: the code genuinely behaves as the
tickets describe, and the documents genuinely say what they say. They were left
in deliberately so that an analysis run has true findings to make.

Several are also named outright in the project's own QA and governance
documents. That is intentional too, and it is not a weakness in the demo. Real
estates look like this — half the problems are already written down somewhere
nobody reads. Watching the platform cite a four-month-old QA document is often
more persuasive than watching it reason from first principles, because it is the
situation the customer is actually in.

Some things that *look* wrong are deliberate decisions with ADRs behind them —
SQLite in production, static tokens, hand-tuned risk weights, `404` for both
"absent" and "not permitted". If a Code Factory run proposes changing one, reject
it at the approval gate and say why. That is a good demo moment: it shows the
gate is real and a human judgement still sits in the loop.
