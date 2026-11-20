# HIPAA control matrix — CarePath 1.0

| | |
| --- | --- |
| **Document** | GOV-CAREPATH-001 |
| **Version** | 1.1 |
| **Status** | Active |
| **Owner** | Information Governance Lead |
| **Assessed** | 2026-06-02 |
| **Next assessment** | 2026-12-02 |

Each control names the safeguard, what implements it, what evidence exists, and
an honest effectiveness rating.

**Effectiveness:** Effective · Partially effective · Not implemented

## Technical safeguards

| Control | §164.312 | Implementation | Evidence | Effectiveness |
| --- | --- | --- | --- | --- |
| **C-01** Unique user identification | (a)(2)(i) | Each principal carries a unique subject; the audit event records it, never the credential | `security/auth.py`; `test_registering_writes_an_audit_event` | **Effective** |
| **C-02** Access control | (a)(1) | One role-to-permission table, closed by default, checked as a declared route dependency before the handler runs | `security/rbac.py`, `api/deps.py`; `TestAuthorisation` (5 tests) | **Effective** |
| **C-03** Audit controls | (b) | Audit event written in the same transaction as every access to patient data | `security/audit.py`; audit assertions across the suite | **Partially effective** — the referral status transition writes no audit event (T-04) |
| **C-04** Integrity of audit records | (c)(1) | No application code path updates or deletes `audit_event`; the database grant excludes both | Code review; grant configuration | **Effective** |
| **C-05** Transmission security | (e)(1) | TLS 1.2+ terminated at the ingress; no plaintext listener | Ingress configuration | **Effective** |
| **C-06** Encryption at rest | (a)(2)(iv) | Volume-level encryption | Platform configuration | **Partially effective** — not column-level; a database administrator with host access can read patient rows (G-02) |
| **C-07** Automatic logoff | (a)(2)(iii) | Not applicable to a machine-to-machine API. Session timeout belongs to the clinical client | — | Not applicable |

## Administrative and organisational

| Control | §164 | Implementation | Evidence | Effectiveness |
| --- | --- | --- | --- | --- |
| **C-08** Minimum necessary | 502(b) | Role separation; a caller entitled to know a patient exists is not entitled to their contact details. Absence and refusal are indistinguishable | `TestAuthorisation`; `test_an_unknown_patient_is_a_404` | **Partially effective** — the export returns the full patient resource including telephone and address regardless of the requesting purpose |
| **C-09** Disclosure limited to the permitted purpose | 502(a) | Consent recorded per purpose and consulted before disclosure | `services/consent.py` | **Not implemented** — the consent record exists and is complete; **the export path does not consult it** (T-03) |
| **C-10** Information system activity review | 308(a)(1)(ii)(D) | Quarterly reconciliation of access counts against audit counts | SLO document; first reconciliation 2026-09-30 | **Partially effective** — the reconciliation compares per-endpoint counts and cannot detect a path that writes no audit event at all |
| **C-11** Workforce access management | 308(a)(3) | Grants reviewed at joiner, mover and leaver | Information Governance process | **Effective** |
| **C-12** Contingency plan | 308(a)(7) | Nightly volume snapshot; quarterly restore rehearsal | Operations runbook | **Effective** |

## Assessment summary

| Rating | Count |
| --- | --- |
| Effective | 6 |
| Partially effective | 4 |
| Not implemented | 1 |
| Not applicable | 1 |

## Findings

**F-01 (High) — C-09 is not implemented.** Consent decisions are recorded
faithfully and are never lost. Nothing reads them before a record leaves the
organisation. The control that BR-05 describes, and that this matrix claims
under §164.502(a), does not exist in the software: it exists in the data model
and in the release notes' instruction that the requester should check manually.

A manual check performed by the party who wants the export is not a control.

**F-02 (Medium) — C-03 is incomplete.** Every write path in the service audits
except the referral status transition. Because C-10's reconciliation compares
counts per endpoint, this omission is invisible to the very review designed to
catch it.

**F-03 (Medium) — C-08 over-discloses on export.** The FHIR Patient resource
carries telephone and address on every export, irrespective of what the
receiving purpose needs.

**F-04 (Low) — C-06 residual.** Accepted for 1.0 as G-02, with administrative
session logging as the compensating control.

## Recommendation

F-01 should not be carried past the pilot. It is the one finding in this matrix
where the document claims a control that the software does not contain, and a
control matrix that overstates is worse than one with a gap recorded honestly —
it is the document an inspector will hold up.
