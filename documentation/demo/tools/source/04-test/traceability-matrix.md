# Requirements traceability matrix — CarePath 1.0

| | |
| --- | --- |
| **Document** | TST-CAREPATH-003 |
| **Version** | 1.0.0 |
| **Status** | Current as of build 1.0.0 |
| **Owner** | QA Lead |
| **Suite result** | 111 passed, 0 failed |
| **Line coverage** | 91% (floor 85%, NFR-13) |

Reviewed at each release. A requirement with no covering test is a release
blocker unless it appears in the *uncovered* table with an owner.

## Business requirements

| BR | Realised by | Verified by |
| --- | --- | --- |
| **BR-01** One record per person | FR-01, FR-02 | `TestMedicalRecordNumbers`, `TestRegistration::test_a_duplicate_number_is_a_conflict` |
| **BR-02** Referrals tracked end to end | FR-07, FR-08 | `TestTransitions::test_the_whole_happy_path_can_be_walked` |
| **BR-03** Prioritised outreach | FR-06 | `TestScoring::test_every_contributing_factor_is_named` |
| **BR-04** Access report on demand | FR-11 | `TestAuditEndpoint::test_an_auditor_reads_the_trail_for_a_patient` |
| **BR-05** Consent honoured before disclosure | FR-09, FR-10 | **Partially covered — see below** |
| **BR-06** Standard exchange format | FR-10 | `TestBundle::test_a_bundle_holds_the_patient_and_everything_recorded` |

## Functional requirements

| FR | Requirement | Code | Tests |
| --- | --- | --- | --- |
| **FR-01** | Register a patient | `services/patients.py::register`, `domain/identifiers.py::valid_mrn` | `TestMedicalRecordNumbers` (8), `TestRegistration` (4), `TestApi::test_a_patient_can_be_registered_and_read_back`, `::test_a_duplicate_registration_is_a_409`, `::test_a_malformed_number_is_a_422` |
| **FR-02** | Read a patient | `services/patients.py::read` | `TestRetrieval::test_an_unknown_patient_is_not_found`, `::test_reading_writes_an_audit_event_carrying_the_purpose`, `TestApi::test_an_unknown_patient_is_a_404` |
| **FR-03** | Search by family name | `services/patients.py::search` | `TestRetrieval::test_a_search_records_what_was_looked_for_not_what_was_found`, `::test_a_search_matches_on_a_prefix_only` |
| **FR-04** | Record an encounter | `services/encounters.py::open_encounter` | `TestOpeningAnEncounter` (5) |
| **FR-05** | Record an observation | `services/encounters.py::add_observation` | `TestObservations` (3) |
| **FR-06** | Readmission risk | `domain/risk.py`, `services/risk.py` | `TestAge` (3), `TestObservationRanges` (5), `TestScoring` (5), `TestStayLength` (2), `TestService` (2), `TestApi` (3) |
| **FR-07** | Create a referral | `services/referrals.py::create` | `TestCreating` (3) |
| **FR-08** | Referral lifecycle | `domain/referral_state.py`, `services/referrals.py::transition` | `TestLifecycleMap` (11), `TestTransitions` (4), `TestApi::test_a_forbidden_move_is_a_409` |
| **FR-09** | Consent | `services/consent.py` | `TestRecording` (2), `TestCurrentPosition` (4), `TestApi` (3) |
| **FR-10** | FHIR export | `fhir.py`, `api/routes_fhir.py` | `TestResources` (4), `TestBundle` (1), `TestApi` (3) |
| **FR-11** | Audit every access | `security/audit.py` | `test_registering_writes_an_audit_event`, `test_reading_writes_an_audit_event_carrying_the_purpose`, `test_opening_an_encounter_writes_an_audit_event`, `TestAuditEndpoint` (3) |
| **FR-12** | Health endpoints | `api/routes_health.py` | `TestHealth` (3) |

## Non-functional requirements

| NFR | Covered | By what |
| --- | :---: | --- |
| **NFR-01** availability | ✗ | Synthetic probe and monthly SLO report — Platform |
| **NFR-02** latency | ✗ | Manual load test before release — QA Lead |
| **NFR-03** audit in the same transaction | ◐ | The audit rows are asserted per endpoint. That they share the transaction is verified by code review, not by a test that rolls one back |
| **NFR-04** no PHI in logs | ◐ | `TestRedaction` (5) covers the redaction function. Whether each log call passes PHI to it is **not** covered — see the finding below |
| **NFR-05** authn and authz | ✓ | `TestAuthentication` (5), `TestAuthorisation` (5), plus a role-refusal test on every router |
| **NFR-06** bounded queries | ◐ | `TestBounds` (2) covers encounters. **Patient search is not covered** — see the finding below |
| **NFR-07** file-mounted secrets | ✗ | Deployment review — Platform |
| **NFR-08** correlation identifier | ✓ | `test_every_response_carries_a_correlation_identifier`, `test_a_supplied_correlation_identifier_is_echoed` |
| **NFR-09** audit immutability | ✗ | Code review and database grants — Security Architect |
| **NFR-10** graceful degradation | ◐ | The `degraded` flag is asserted present. The degraded *path* is verified by fault injection in the soak, not by a test |
| **NFR-11** transaction ceiling | ✗ | Slow-query log — Platform |
| **NFR-12** rate limiting | ✗ | **Not implemented.** See the risk register |
| **NFR-13** coverage and traceability | ✓ | This document; coverage gate in the pipeline |
| **NFR-14** runs with no configuration | ✓ | `conftest.py` starts the service with a database path and nothing else |

Legend: ✓ covered by the suite · ◐ partially covered · ✗ verified outside the suite, or not at all

## Regulatory requirements

| REG | Verified by |
| --- | --- |
| **REG-01** audit controls | FR-11 tests, plus the read-only audit router |
| **REG-02** unique user identification | `test_registering_writes_an_audit_event` asserts the actor is the principal's subject |
| **REG-03** minimum necessary | `TestAuthorisation` (5); `test_an_unknown_patient_is_a_404` pins that absence and refusal are indistinguishable |
| **REG-04** transmission security | Ingress configuration — Platform |
| **REG-05** encryption at rest | Volume configuration — Platform. Gap G-02 |
| **REG-06** consent and lawful basis | FR-09 tests. Gap G-03 |
| **REG-07** FHIR R4 | `TestResources` (4), `TestBundle` (1). Gap G-04: no automated validator |
| **REG-08** erasure and portability | Portability by FR-10. Erasure is manual — gap G-01 |

## Findings raised by this review

Recorded here rather than left implied by a ◐.

| # | Requirement | Finding | Severity | Owner |
| --- | --- | --- | --- | --- |
| **T-01** | NFR-06 | `GET /patients` declares `limit` with no `le` bound, unlike the encounter, referral and consent listings. There is no test asserting an upper bound on patient search | High | Engineering |
| **T-02** | NFR-04 | No test asserts that a given log call is free of PHI. The redaction function only catches shaped identifiers; a patient name passes through it unchanged | High | Engineering |
| **T-03** | BR-05 | No test asserts that the FHIR export consults consent. The consent service is fully tested; nothing joins it to the export path | High | Engineering |
| **T-04** | NFR-03 | The referral status transition has no audit assertion, unlike every other write in the suite | Medium | Engineering |
| **T-05** | NFR-12 | Nothing to test — the control does not exist. Coverage cannot show this, because uncovered code that was never written does not appear in a coverage report | Medium | Engineering |
