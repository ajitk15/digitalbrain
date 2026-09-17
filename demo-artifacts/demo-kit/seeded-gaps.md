# Answer key — the gaps seeded in CarePath

> **Demo operator only.** Do not open this in front of a customer and do not
> load it as a knowledge source. Loading it would let Chat answer from the
> answer key instead of from the project's own documents, which is precisely
> the thing the demo is trying to show it does not need.

Every gap below is **real**: the code genuinely behaves this way, and the
documents genuinely say what they say. Nothing is staged in the sense of being
faked. What is staged is that they were left in deliberately, so that an
analysis run has true findings to make.

---

## The seven

| # | Gap | Where | Ticket | Documents that betray it |
| --- | --- | --- | --- | --- |
| **1** | `GET /patients` passes `limit` to the database unclamped. The three sibling endpoints clamp | `api/routes_patients.py::search_patients` | CARE-412 | NFR-06; T-01; postmortem INC-2026-0814; R-04 |
| **2** | `referrals.transition` writes no audit event. Every other write path does | `services/referrals.py::transition` | CARE-425 | NFR-03; T-04; control matrix C-03; R-02 |
| **3** | The scoring log line passes the patient's name and birth date | `services/risk.py::score_for` | CARE-418 | NFR-04; T-02; test plan TC-3; R-03 |
| **4** | The FHIR export never consults consent | `api/routes_fhir.py::export_everything` | CARE-401 | BR-05; T-03; W-01; control matrix C-09 (F-01); R-01 |
| **5** | The telephone regex matches an ISO-8601 date, so dates are redacted by accident | `security/phi.py::PATTERNS` | CARE-418 | Visible in any log line containing a birth date |
| **6** | No rate limiting on authentication. The control was never written | absent | CARE-437 | NFR-12; threat model; ADR-0002; T-05; R-05 |
| **7** | `PatientSummary` and `mask_mrn` are written, tested and unreachable. An auditor receives the full record | `schemas.py`, `security/phi.py`, `api/routes_patients.py` | CARE-431 | REG-03; security design; control matrix C-08 (F-03) |

---

## Two levels of finding, and why both are there

**Discovered.** Gaps 1, 2, 4, 5, 6 and 7 are stated as *rules* in the
requirements and design documents, with no corresponding implementation. An
analysis run has to notice that a rule exists and that nothing satisfies it.
This is the impressive half.

**Confirmed.** T-01 to T-05 in the traceability matrix, F-01 to F-04 in the
control matrix, and R-01 to R-12 in the risk register name several of these
outright. An analysis run finds them **and cites the document that said so**.

Both belong in the demo, and the second is not a weakness. A customer's real
estate looks like this: half the problems are already written down somewhere
nobody reads. Showing the platform cite a QA document from four months ago is
often more persuasive than showing it reason from first principles, because it
is the situation the customer is actually in.

If someone objects that the findings were "already in the documents", the answer
is: yes, in four different documents, owned by four different people, and the
release shipped anyway. That is the point.

---

## What is deliberately *not* wrong

Do not let a run talk you into "fixing" these. They are decisions, recorded in
ADRs, and a good analysis should leave them alone or at least name the ADR.

| Looks wrong | Actually deliberate | Record |
| --- | --- | --- |
| SQLite in production | Pilot volumes, moving schema, no procurement budget | ADR-0001 |
| Static tokens in the repository | They protect nothing; a deployment replaces the directory | ADR-0002 |
| Hand-tuned risk weights | Explainability chosen over accuracy; keeps the REG-05 determination intact | ADR-0003 |
| No US Core conformance claim | Conformance needs a validator, and the pipeline has none | ADR-0004 |
| `404` for both "absent" and "not permitted" | Confirming existence is itself a disclosure | REG-03 |
| An audit write failure fails the request | A disclosure we cannot record is one we do not make | Architecture §4 |
| Consent ordered by `recorded_at DESC, rowid DESC` | Timestamps tie at clock resolution; insertion order breaks it | Data model |

**If a run proposes changing one of these, that is a good demo moment, not a bad
one.** Reject it at the approval gate and say why. It shows the gate is real
and that a human judgement still sits in the loop.

---

## Restoring the gaps after a demo

Code Factory delivers a **draft pull request**; it merges nothing. So a run
leaves the working tree untouched and the next demo starts clean.

If you merged a fix to show the full loop:

```bash
cd demo-artifacts/carepath
git checkout -- src/carepath tests
.venv/Scripts/python.exe -m pytest -q     # expect 111 passed
.venv/Scripts/ruff.exe check src tests    # expect clean
```

If the suite does not read **111 passed**, the tree is not in its demo state.

A fix for CARE-401, CARE-425 or CARE-431 will *add* tests, so a higher count
after a merge is expected rather than alarming — but the starting state for a
fresh demo is 111.
