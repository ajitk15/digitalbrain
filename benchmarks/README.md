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
| `jira-keys.json` | The keys the demo Jira gave the CarePath tickets, frozen so the scorecard imports them under the names the baseline was measured with. |
| `drift-pack.json` | The frozen inputs `manage.py llm_drift` sends to a model. |
| `typing-pack.json` | Documents the rules leave as Other, labelled by a person, that `manage.py typing_pack` sends to the Document typing model. |
| `typing/` | Recorded typing-pack runs, one file per run, named by model. |
| `drift/` | Recorded drift runs, one file per run, named by model. |
| `factory-pack.json` | CarePath tickets with planted code bugs as approved plan items, hand-labelled with what the fix must and must not say, that `manage.py factory_pack` sends to Code Factory's implementation agent one-shot and with tools. |
| `factory/` | Recorded factory-pack runs, one file per run, named by model. |
| `review-pack.json` | CarePath changes that each break something - a caller, every caller, a requirement - labelled with the files a finding must quote, that `manage.py review_pack` hands a code review's AI step one-shot and with tools. |
| `review/` | Recorded review-pack runs, one file per run, named by model. |
| `chat/` | Recorded `manage.py chat_pack` runs: the chat agent with tools and with one call over the same evidence, against the application's benchmark questions. The questions are the application's own, so there is no pack file; their digest is the reference. |
| `triage/` | Recorded `manage.py triage_pack` runs: ServiceOps triage one-shot and with tools, against the causes people confirmed. The cases are the application's own verdicts, so there is no pack file. |

A recorded `factory/`, `review/`, `triage/` or `chat/` run is also kept in the application's database as an agent measurement and drawn on that agent's page (Settings → AI → Agents). `manage.py agent_measurements <application id> --load <file>` keeps one recorded on another machine.

The benchmark questions themselves are `documentation/demo/demo-kit/benchmark-questions.json`.
