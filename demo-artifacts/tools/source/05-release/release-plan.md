# Release plan — CarePath 1.0.0

| | |
| --- | --- |
| **Document** | REL-CAREPATH-001 |
| **Version** | 1.0 |
| **Status** | Executed |
| **Owner** | Delivery Manager |
| **Release date** | 2026-06-04 |

## Scope

FR-01 to FR-12 as described in `01-requirements/user-stories.docx`. Pilot cohort
of 400 patients across two clinics (C-03), behind an IP allow-list.

## Release criteria

| Criterion | Source | Status |
| --- | --- | --- |
| Test plan executed, exit criteria met | TST-CAREPATH-002 | Met with waiver |
| Coverage at or above 85% | NFR-13 | 91% |
| Security review complete | DES-CAREPATH-003 | Complete, findings in the risk register |
| Information governance sign-off | REQ-CAREPATH-004 | Signed 2026-06-02 |
| Deployment rehearsed in staging | REL-CAREPATH-002 | Rehearsed 2026-05-30 |
| Rollback rehearsed | REL-CAREPATH-002 | Rehearsed 2026-05-30 |
| Runbook reviewed against the deployed shape | OPS-CAREPATH-002 | **Not done** — see below |

## Waivers

| Waiver | Finding | Rationale | Granted by | Expires |
| --- | --- | --- | --- | --- |
| **W-01** | T-01, T-02, T-03 open at high severity | Pilot cohort is 400 patients behind an IP allow-list; the exposure is bounded and the audit remediation date (C-04) is not | Clinical Systems Board, 2026-06-02 | General availability |

W-01 covers three findings with one rationale. Whether that rationale holds
equally for all three was not examined at the board, and the minute does not
record a discussion of T-03 — consent not consulted before disclosure — which is
the only one of the three that touches a business requirement rather than a
non-functional one.

## Schedule

| Date | Activity | Owner |
| --- | --- | --- |
| 2026-05-28 | Test plan executed | QA Lead |
| 2026-05-30 | Deployment and rollback rehearsal in staging | Platform |
| 2026-06-02 | Release review; waiver W-01 granted | Clinical Systems Board |
| 2026-06-04 09:00 | Deploy to standby, smoke, move traffic | Platform |
| 2026-06-04 09:30 | Watch window | Platform, Engineering |
| 2026-06-04 17:00 | Release accepted | Delivery Manager |
| 2026-06-11 | Pilot review, first SLO report | Delivery Manager |

## Communications

| Audience | When | What |
| --- | --- | --- |
| Pilot clinicians and coordinators | Two days before | What changes, what to do if it does not work |
| Northvale | One week before | Export endpoint availability, token issue |
| Information Governance | On acceptance | Confirmation the audit trail is live |
| Service desk | Day before | Runbook link, escalation path |

## Known open items carried into the pilot

| Item | Reference | Owner | Target |
| --- | --- | --- | --- |
| T-01 patient search has no upper bound | Traceability matrix | Engineering | 1.0.1 |
| T-02 PHI in the scoring log line | Traceability matrix | Engineering | 1.0.1 |
| T-03 consent not consulted before export | Traceability matrix | Engineering | 1.0.1 |
| T-04 referral transition not audited | Traceability matrix | Engineering | 1.0.1 |
| T-05 rate limiting not implemented (NFR-12) | Traceability matrix | Engineering | 1.1 |
| G-01 erasure is manual | REQ-CAREPATH-004 | Information Governance | 2.0 |
| Runbook not reviewed against the deployed shape | This document | Platform | **Overdue** |
