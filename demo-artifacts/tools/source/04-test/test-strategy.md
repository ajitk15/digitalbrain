# Test strategy — CarePath

| | |
| --- | --- |
| **Document** | TST-CAREPATH-001 |
| **Version** | 1.2 |
| **Status** | Approved |
| **Owner** | QA Lead |

## Objective

Demonstrate that each functional requirement behaves as the user stories
describe, and that each verifiable non-functional requirement holds. Where a
requirement is **not** covered by an automated test, say so here rather than
letting the coverage percentage imply it is.

## Shape of the suite

| Level | What it covers | Where | Speed |
| --- | --- | --- | --- |
| **Domain** | Rules with no I/O: MRN format, referral transitions, risk arithmetic, age, redaction | `tests/test_patients.py::TestMedicalRecordNumbers`, `test_referrals.py::TestLifecycleMap`, `test_risk.py`, `test_security.py::TestRedaction` | microseconds |
| **Service** | Use cases against a real in-memory database: transactions, audit rows, conflicts | `Test*` classes taking the `connection` fixture | milliseconds |
| **API** | The whole path including authentication, authorisation, status mapping and correlation | `TestApi` classes taking the `client` fixture | milliseconds |

There is no mocking of the database. The service tests run against SQLite
`:memory:`, which is the same engine production uses (ADR-0001), so a test that
passes has exercised the real SQL and the real transaction semantics.

## Principles

**Test the refusal, not just the success.** For every rule there is a test that
the rule bites: a malformed MRN, a forbidden transition, a role without the
permission, a limit above the maximum. A suite that only walks happy paths
tells you the feature exists, not that the constraint holds.

**One assertion about behaviour, not about implementation.** Tests assert on the
audit trail through `events_for`, not on the SQL that wrote it.

**Named as sentences.** A failing test should read as the statement that is no
longer true.

**Every functional requirement appears in at least one test** (NFR-13), joined
in `04-test/traceability-matrix.xlsx`.

## Entry and exit criteria

**Entry:** feature complete against its user story; lint clean; the developer
has run the suite locally.

**Exit — all must hold:**

- [ ] Every test passes
- [ ] Line coverage at or above 85%
- [ ] Every FR maps to at least one passing test
- [ ] Every NFR is either covered by a test or listed as uncovered below, with an owner
- [ ] No open defect at severity 1 or 2

## What is not covered by automated tests

Stated plainly. This list is the honest part of the strategy.

| Requirement | Why not covered | How it is verified instead | Owner |
| --- | --- | --- | --- |
| **NFR-01** availability | Needs production traffic over a month | Synthetic probe and the monthly SLO report | Platform |
| **NFR-02** latency | Needs a load generator and representative volumes | Load test before each release, run manually | QA Lead |
| **NFR-04** no PHI in logs | The redaction *function* is tested. Whether any given log call passes PHI to it is not, and cannot be by a unit test | Log sampling review each sprint; code review checklist | Principal Engineer |
| **NFR-07** file-mounted secrets | A property of the deployment, not the code | Deployment review at release | Platform |
| **NFR-09** audit immutability | The absence of a code path cannot be tested by exercising it | Code review, plus database grants that exclude UPDATE and DELETE | Security Architect |
| **NFR-10** graceful degradation | Requires fault injection | Pre-release soak with the dependency stopped | QA Lead |
| **NFR-11** transaction ceiling | Needs contention | Slow-query log review in production | Platform |
| **NFR-12** rate limiting | **Nothing to test yet.** The requirement is approved and the control is not implemented | Open — see the risk register | Principal Engineer |

NFR-12 is on this list for a different reason from the others. The rest are
covered by something outside the suite. NFR-12 is covered by nothing, and the
coverage percentage does not show it, because you cannot fail to cover code that
was never written.

## Test data

Synthetic only. The seed cohort in `carepath.seed` is deliberately shaped: one
patient above the outreach threshold, one below, one who has withdrawn consent
to data exchange. Production data is never copied into any lower environment
(C-02).

## Regression policy

Every defect fixed arrives with a test that fails before the fix and passes
after. The test is named for the defect's behaviour, not its ticket number.
