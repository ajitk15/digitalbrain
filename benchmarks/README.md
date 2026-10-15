# Benchmarks

What the platform is measured against, and what it measured.

| File | What it is |
|---|---|
| `baseline.json` | The scorecard every change is checked against (`manage.py scorecard --check`). |
| `history/` | Recorded scorecards: one per release, plus `--replay` runs of older commits. |
| `seeded-evidence.json` | For each CarePath ticket with a planted bug, the identifiers that reveal it. |
| `phi-samples.json` | Labelled text for the personal-data scan. Synthetic values only. |
| `ontology-samples.json` | Labelled relationship phrasings and name pairs for the ontology. |
| `document-types.json` | What a person would call each CarePath document, for `document_types`. |
| `drift-pack.json` | The frozen inputs `manage.py llm_drift` sends to a model. |
| `drift/` | Recorded drift runs, one file per run, named by model. |

The benchmark questions themselves are `demo-artifacts/demo-kit/benchmark-questions.json`.
