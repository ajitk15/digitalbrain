## Overview
| Field | Value |
| --- | --- |
| Title | Requirements traceability matrix — CarePath 1.0 |
| Document | TST-CAREPATH-003 |
| Version | 1.0.0 |
| Status | Current as of build 1.0.0 |
| Owner | QA Lead |
| Suite result | 111 passed, 0 failed |
| Line coverage | 91% (floor 85%, NFR-13) |
| Notice | Demonstration artifact. Synthetic content only - not a clinical record. |

## Narrative
| Section | Text |
| --- | --- |
| Requirements traceability matrix — CarePath 1.0 | Reviewed at each release. A requirement with no covering test is a release blocker unless it appears in the uncovered table with an owner. |
| Non-functional requirements | Legend: ✓ covered by the suite · ◐ partially covered · ✗ verified outside the suite, or not at all |
| Findings raised by this review | Recorded here rather than left implied by a ◐. |

## 1. Business requirements
| BR | Realised by | Verified by |
| --- | --- | --- |
| BR-01 One record per person | FR-01, FR-02 | TestMedicalRecordNumbers, TestRegistration::test\_a\_duplicate\_number\_is\_a\_conflict |
| BR-02 Referrals tracked end to end | FR-07, FR-08 | TestTransitions::test\_the\_whole\_happy\_path\_can\_be\_walked |
| BR-03 Prioritised outreach | FR-06 | TestScoring::test\_every\_contributing\_factor\_is\_named |
| BR-04 Access report on demand | FR-11 | TestAuditEndpoint::test\_an\_auditor\_reads\_the\_trail\_for\_a\_patient |
| BR-05 Consent honoured before disclosure | FR-09, FR-10 | Partially covered — see below |
| BR-06 Standard exchange format | FR-10 | TestBundle::test\_a\_bundle\_holds\_the\_patient\_and\_everything\_recorded |

## 2. Functional requirements
| FR | Requirement | Code | Tests |
| --- | --- | --- | --- |
| FR-01 | Register a patient | services/patients.py::register, domain/identifiers.py::valid\_mrn | TestMedicalRecordNumbers (8), TestRegistration (4), TestApi::test\_a\_patient\_can\_be\_registered\_and\_read\_back, ::test\_a\_duplicate\_registration\_is\_a\_409, ::test\_a\_malformed\_number\_is\_a\_422 |
| FR-02 | Read a patient | services/patients.py::read | TestRetrieval::test\_an\_unknown\_patient\_is\_not\_found, ::test\_reading\_writes\_an\_audit\_event\_carrying\_the\_purpose, TestApi::test\_an\_unknown\_patient\_is\_a\_404 |
| FR-03 | Search by family name | services/patients.py::search | TestRetrieval::test\_a\_search\_records\_what\_was\_looked\_for\_not\_what\_was\_found, ::test\_a\_search\_matches\_on\_a\_prefix\_only |
| FR-04 | Record an encounter | services/encounters.py::open\_encounter | TestOpeningAnEncounter (5) |
| FR-05 | Record an observation | services/encounters.py::add\_observation | TestObservations (3) |
| FR-06 | Readmission risk | domain/risk.py, services/risk.py | TestAge (3), TestObservationRanges (5), TestScoring (5), TestStayLength (2), TestService (2), TestApi (3) |
| FR-07 | Create a referral | services/referrals.py::create | TestCreating (3) |
| FR-08 | Referral lifecycle | domain/referral\_state.py, services/referrals.py::transition | TestLifecycleMap (11), TestTransitions (4), TestApi::test\_a\_forbidden\_move\_is\_a\_409 |
| FR-09 | Consent | services/consent.py | TestRecording (2), TestCurrentPosition (4), TestApi (3) |
| FR-10 | FHIR export | fhir.py, api/routes\_fhir.py | TestResources (4), TestBundle (1), TestApi (3) |
| FR-11 | Audit every access | security/audit.py | test\_registering\_writes\_an\_audit\_event, test\_reading\_writes\_an\_audit\_event\_carrying\_the\_purpose, test\_opening\_an\_encounter\_writes\_an\_audit\_event, TestAuditEndpoint (3) |
| FR-12 | Health endpoints | api/routes\_health.py | TestHealth (3) |

## 3. Non-functional requireme
| NFR | Covered | By what |
| --- | --- | --- |
| NFR-01 availability | ✗ | Synthetic probe and monthly SLO report — Platform |
| NFR-02 latency | ✗ | Manual load test before release — QA Lead |
| NFR-03 audit in the same transaction | ◐ | The audit rows are asserted per endpoint. That they share the transaction is verified by code review, not by a test that rolls one back |
| NFR-04 no PHI in logs | ◐ | TestRedaction (5) covers the redaction function. Whether each log call passes PHI to it is not covered — see the finding below |
| NFR-05 authn and authz | ✓ | TestAuthentication (5), TestAuthorisation (5), plus a role-refusal test on every router |
| NFR-06 bounded queries | ◐ | TestBounds (2) covers encounters. Patient search is not covered — see the finding below |
| NFR-07 file-mounted secrets | ✗ | Deployment review — Platform |
| NFR-08 correlation identifier | ✓ | test\_every\_response\_carries\_a\_correlation\_identifier, test\_a\_supplied\_correlation\_identifier\_is\_echoed |
| NFR-09 audit immutability | ✗ | Code review and database grants — Security Architect |
| NFR-10 graceful degradation | ◐ | The degraded flag is asserted present. The degraded path is verified by fault injection in the soak, not by a test |
| NFR-11 transaction ceiling | ✗ | Slow-query log — Platform |
| NFR-12 rate limiting | ✗ | Not implemented. See the risk register |
| NFR-13 coverage and traceability | ✓ | This document; coverage gate in the pipeline |
| NFR-14 runs with no configuration | ✓ | conftest.py starts the service with a database path and nothing else |

## 4. Regulatory requirements
| REG | Verified by |
| --- | --- |
| REG-01 audit controls | FR-11 tests, plus the read-only audit router |
| REG-02 unique user identification | test\_registering\_writes\_an\_audit\_event asserts the actor is the principal's subject |
| REG-03 minimum necessary | TestAuthorisation (5); test\_an\_unknown\_patient\_is\_a\_404 pins that absence and refusal are indistinguishable |
| REG-04 transmission security | Ingress configuration — Platform |
| REG-05 encryption at rest | Volume configuration — Platform. Gap G-02 |
| REG-06 consent and lawful basis | FR-09 tests. Gap G-03 |
| REG-07 FHIR R4 | TestResources (4), TestBundle (1). Gap G-04: no automated validator |
| REG-08 erasure and portability | Portability by FR-10. Erasure is manual — gap G-01 |

## 5. Findings raised by this
| # | Requirement | Finding | Severity | Owner |
| --- | --- | --- | --- | --- |
| T-01 | NFR-06 | GET /patients declares limit with no le bound, unlike the encounter, referral and consent listings. There is no test asserting an upper bound on patient search | High | Engineering |
| T-02 | NFR-04 | No test asserts that a given log call is free of PHI. The redaction function only catches shaped identifiers; a patient name passes through it unchanged | High | Engineering |
| T-03 | BR-05 | No test asserts that the FHIR export consults consent. The consent service is fully tested; nothing joins it to the export path | High | Engineering |
| T-04 | NFR-03 | The referral status transition has no audit assertion, unlike every other write in the suite | Medium | Engineering |
| T-05 | NFR-12 | Nothing to test — the control does not exist. Coverage cannot show this, because uncovered code that was never written does not appear in a coverage report | Medium | Engineering |