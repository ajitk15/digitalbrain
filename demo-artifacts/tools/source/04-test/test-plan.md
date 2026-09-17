# Test plan — CarePath 1.0.0

| | |
| --- | --- |
| **Document** | TST-CAREPATH-002 |
| **Version** | 1.0.0 |
| **Status** | Executed |
| **Owner** | QA Lead |
| **Build under test** | `carepath 1.0.0` |
| **Executed** | 2026-05-28 |

## Environments

| Environment | Purpose | Data | Database |
| --- | --- | --- | --- |
| Local | Developer loop | Seed cohort | SQLite `:memory:` |
| CI | Every push and pull request | Seed cohort | SQLite `:memory:` |
| Staging | Soak, fault injection, load | Synthetic, 400 patients | SQLite on disk |
| Production | Pilot | Real (C-02: never copied downward) | SQLite on disk |

## Running the suite

```bash
cd demo-artifacts/carepath
uv venv .venv && uv pip install --python .venv/Scripts/python.exe -e . pytest httpx ruff coverage
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check src tests
.venv/Scripts/python.exe -m coverage run --source=src/carepath -m pytest -q && .venv/Scripts/python.exe -m coverage report
```

## Result

| Metric | Target | Actual | Verdict |
| --- | --- | --- | --- |
| Tests passing | all | 111 / 111 | Pass |
| Line coverage | ≥ 85% (NFR-13) | 91% | Pass |
| Lint | clean | clean | Pass |
| FRs with at least one test | 12 / 12 | 12 / 12 | Pass |
| Severity 1 or 2 defects open | 0 | 0 | Pass |

Coverage by package: domain 100%, services 100%, security 99%, api 95%,
transport and entry point excluded by design (`__main__.py`, `seed.py` are
exercised manually).

## Test cycles

| Cycle | Scope | Result |
| --- | --- | --- |
| **TC-1** Unit and service | Domain rules, use cases, audit trail | 111 passed |
| **TC-2** API contract | Status mapping, authentication, authorisation, correlation | Included in TC-1 |
| **TC-3** Manual exploratory | Boot, seed, exercise each endpoint with each role | Passed; one observation raised (below) |
| **TC-4** Load | 50 rps for 10 minutes against staging | p95 118ms read, 240ms write. Pass (NFR-02) |
| **TC-5** Fault injection | Database stopped mid-soak | Readiness failed within 12s, liveness held, no restart loop. Pass (NFR-01) |
| **TC-6** Security review | Threat model walkthrough against the code | Passed with findings — see `07-govern/risk-register.xlsx` |

## TC-3 observation

Exercising `GET /patients/{id}/risk` as a clinician and then reading the service
log, the log line for the scoring call reads:

```
{"level":"INFO","logger":"carepath.services.risk","correlation_id":"b439b6f9…",
 "message":"Scoring readmission risk for Amara Okonjo born [phone]"}
```

Two things in one line. The patient's name is present in a log that NFR-04 says
must not carry protected health information. And the date of birth has been
replaced by `[phone]` — the telephone pattern in the redaction filter is broad
enough to match a date, which means dates are being redacted by accident rather
than by rule, and other numeric data may be redacted when it should not be.

Raised as T-02 in the traceability matrix. Not a release blocker by the exit
criteria as written, which is itself worth a conversation at the release review.

## Exit criteria

- [x] Every test passes
- [x] Coverage at or above 85%
- [x] Every FR maps to at least one passing test
- [x] Every NFR covered by a test or listed as uncovered with an owner
- [x] No open defect at severity 1 or 2
- [ ] **No open high-severity finding in the traceability matrix** — T-01, T-02 and T-03 are open

The last box is unticked. Release 1.0.0 proceeded on a documented waiver from
the Clinical Systems Board dated 2026-06-02, on the basis that the pilot cohort
is 400 patients behind an IP allow-list. The waiver expires at general
availability.
