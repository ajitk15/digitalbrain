# Risk register — CarePath

| | |
| --- | --- |
| **Document** | GOV-CAREPATH-002 |
| **Version** | 1.4 |
| **Status** | Active |
| **Owner** | Delivery Manager |
| **Last reviewed** | 2026-08-19, after INC-2026-0814 |

Scoring: likelihood × impact, each 1–5. Score 15 or above is escalated to the
Clinical Systems Board.

| ID | Risk | L | I | Score | Treatment | Owner | Status |
| --- | --- | :-: | :-: | :-: | --- | --- | --- |
| **R-01** | A record is disclosed to a partner for a patient who withdrew consent, because the export path does not consult the consent record | 4 | 5 | **20** | **Mitigate** — implement the check in 1.0.1. Interim: manual confirmation by the requester, which is not a control | Engineering | **Open, escalated** |
| **R-02** | An access to a patient record is not recorded, so an access report is incomplete when produced under audit | 3 | 5 | **15** | **Mitigate** — audit the referral transition (T-04); redesign the reconciliation so an unaudited path is detectable | Engineering | **Open, escalated** |
| **R-03** | Patient names in application logs reach the aggregator, where retention and access differ from the clinical record | 4 | 3 | 12 | Mitigate — remove the identifying fields from the scoring log line (T-02); sprint log sampling | Engineering | Open |
| **R-04** | An unbounded query degrades or halts the service | 2 | 4 | 8 | **Realised** as INC-2026-0814. Actions A-1, A-2 open | Engineering | Realised, actions open |
| **R-05** | Credential stuffing against the token endpoint succeeds, because there is no rate limiting and tokens do not expire | 3 | 5 | **15** | Mitigate — implement NFR-12 in 1.1. Interim: IP allow-list | Engineering | **Open, escalated** |
| **R-06** | A database administrator reads patient rows directly | 2 | 4 | 8 | Accept for 1.0 (G-02); administrative sessions logged and reviewed monthly | Platform | Accepted |
| **R-07** | An erasure request cannot be served within the statutory period because the procedure is manual | 2 | 4 | 8 | Accept for 1.0 (G-01); two named engineers under change control | Information Governance | Accepted |
| **R-08** | Northvale cannot ingest the export | 1 | 3 | 3 | Closed — confirmed 2026-03-09, A-02 discharged | Integration Lead | Closed |
| **R-09** | Coordinators do not act on the risk list, so BR-03 delivers nothing | 3 | 4 | 12 | Monitor — the explainability decision in ADR-0003 is the mitigation; measured at the pilot review | Product Owner | Monitoring |
| **R-10** | A waiver bundles several findings, so the weakest rationale carries the strongest finding | 4 | 4 | **16** | **Realised** — W-01 bundled T-01, T-02 and T-03. Actions A-3, A-4, A-5 open | Delivery Manager | **Realised, escalated** |
| **R-11** | An operational procedure is followed confidently from a document that no longer matches the deployment | 3 | 3 | 9 | Mitigate — runbook review at each release; currently overdue | Platform | Open |
| **R-12** | The risk score is read as clinical advice, voiding the REG-05 non-applicability determination | 2 | 5 | 10 | Mitigate — wording in the API description, release notes and coordinator training; reviewed if scoring changes | Clinical Lead | Monitoring |

## Escalated to the board

R-01, R-02, R-05 and R-10, tabled for 2026-09-02.

R-10 is the one to take first. The other three were each visible before release
— R-01 as T-03, R-02 as T-04, R-05 as NFR-12 — and each was carried past the
gate that existed to catch it. R-10 is the reason why, and fixing the individual
findings without fixing it leaves the mechanism in place for the next release.
