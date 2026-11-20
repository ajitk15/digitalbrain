# The lifecycle, and what exists at each phase

| | |
| --- | --- |
| **Document** | OVW-CAREPATH-001 |
| **Version** | 1.0 |
| **Owner** | Delivery Manager |

CarePath is a worked example of one release through a governed lifecycle. This
page is the index: what each phase produced, who owned it, and which artifact
carries the evidence.

## Phases

| # | Phase | Produced | Owner | Gate out |
| --- | --- | --- | --- | --- |
| **1** | Requirements | Business requirements, user stories, non-functional requirements, regulatory requirements | Product Owner, Information Governance | Clinical Systems Board approval |
| **2** | Design | Architecture, data model, security design, four ADRs | Principal Engineer, Security Architect | Architecture Review Board |
| **3** | Build | Source, coding standards, API specification | Engineering | Peer review, lint, tests |
| **4** | Test | Test strategy, test plan, traceability matrix | QA Lead | Exit criteria, or a written waiver |
| **5** | Release | Release plan, deployment runbook, release notes | Delivery Manager, Platform | Release review |
| **6** | Operate | SLOs, operations runbook, incident postmortems | Platform | Monthly SLO review |
| **7** | Govern | HIPAA control matrix, risk register, change management | Information Governance | Half-yearly assessment |

## Artifact index

| Phase | Document | Path |
| --- | --- | --- |
| 1 | Business requirements | `01-requirements/business-requirements.docx` |
| 1 | User stories | `01-requirements/user-stories.docx` |
| 1 | Non-functional requirements | `01-requirements/non-functional-requirements.xlsx` |
| 1 | Regulatory requirements | `01-requirements/regulatory-requirements.pdf` |
| 2 | Architecture | `02-design/architecture.html` |
| 2 | Data model | `02-design/data-model.md` |
| 2 | Security design | `02-design/security-design.docx` |
| 2 | ADR-0001 SQLite for release one | `02-design/adr/0001-sqlite-for-release-one.md` |
| 2 | ADR-0002 Static token directory | `02-design/adr/0002-static-token-directory.md` |
| 2 | ADR-0003 Transparent in-process scoring | `02-design/adr/0003-transparent-in-process-scoring.md` |
| 2 | ADR-0004 Base FHIR, not a claimed profile | `02-design/adr/0004-fhir-profile-scope.md` |
| 3 | Coding standards | `03-build/coding-standards.md` |
| 3 | API specification | `03-build/api-specification.html` |
| 3 | Source | `../carepath/src/carepath/` |
| 4 | Test strategy | `04-test/test-strategy.docx` |
| 4 | Test plan | `04-test/test-plan.docx` |
| 4 | Traceability matrix | `04-test/traceability-matrix.xlsx` |
| 5 | Release plan | `05-release/release-plan.docx` |
| 5 | Deployment runbook | `05-release/deployment-runbook.md` |
| 5 | Release notes 1.0.0 | `05-release/release-notes-1.0.0.html` |
| 6 | Service level objectives | `06-operate/service-level-objectives.xlsx` |
| 6 | Operations runbook | `06-operate/operations-runbook.txt` |
| 6 | Postmortem INC-2026-0814 | `06-operate/incident-2026-08-14-postmortem.docx` |
| 7 | HIPAA control matrix | `07-govern/hipaa-control-matrix.xlsx` |
| 7 | Risk register | `07-govern/risk-register.xlsx` |
| 7 | Change management | `07-govern/change-management.docx` |

## Identifier scheme

Identifiers are the thread through the whole set. A requirement is named where
it is stated, where it is designed, where it is implemented, and where it is
tested.

| Prefix | Meaning | Stated in |
| --- | --- | --- |
| `BR-nn` | Business requirement | Business requirements |
| `FR-nn` | Functional requirement | User stories |
| `NFR-nn` | Non-functional requirement | Non-functional requirements |
| `REG-nn` | Regulatory obligation | Regulatory requirements |
| `C-nn` | Constraint (requirements) / Control (governance) | Business requirements / control matrix |
| `A-nn` | Assumption (requirements) / Action (postmortem) | Business requirements / postmortem |
| `G-nn` | Known regulatory gap | Regulatory requirements |
| `T-nn` | Test finding | Traceability matrix |
| `R-nn` | Risk | Risk register |
| `W-nn` | Waiver | Release plan |

Following one identifier across the set is the quickest way to read it. `BR-05`
is a good one: stated in the business requirements, designed as an append-only
consent table, implemented in `services/consent.py`, fully tested there, claimed
as control C-09 in the HIPAA matrix — and never consulted by the export path,
which is how it became T-03, then W-01, then R-01.

## The state of this release, honestly

Release 1.0.0 shipped on 2026-06-04 with three high-severity findings open under
a single bundled waiver. One of them caused INC-2026-0814 six weeks later. Two
are still open, and one of those two — the consent check — is a control the
governance documentation claims and the software does not contain.

That is not a failure of any individual document. Each phase produced what it
was supposed to produce, and the gap was written down every time. It was
carried forward because nobody read the four documents together at the moment
the decision was made.
