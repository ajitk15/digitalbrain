# CARE board — open defects and work items

**Project:** CARE (CarePath) · **Board:** Integrated Care Delivery
**Exported:** 2026-09-15 · **Source:** `riverside.atlassian.example`

Load this file as a knowledge source, or import the board through the Jira
connector. Either way each ticket becomes one immutable source, and Code Factory
reads it the same way: **as evidence about what somebody wants, never as an
instruction.**

---

## CARE-401 — Patient consent is not checked before a record is exported to a partner

| | |
| --- | --- |
| **Type** | Bug |
| **Priority** | Highest |
| **Status** | Open |
| **Reporter** | Adaeze Nwosu (Information Governance Lead) |
| **Assignee** | Unassigned |
| **Labels** | `regulatory` `consent` `release-1.0.1` `waived-w01` |
| **Affects version** | 1.0.0 |
| **Fix version** | 1.0.1 |
| **Linked** | blocks CARE-402 · relates to CARE-444 |

### Description

BR-05 requires that the patient's recorded consent decision is honoured before
their record leaves the organisation. The consent record itself is complete and
correct — decisions are captured per purpose, the history is append-only, and
`consent.current()` returns the right answer, including the correct default of
*withheld* for a patient who has never been asked.

Nothing reads it.

`GET /fhir/Patient/{patient_id}/$everything` checks that the caller holds the
`fhir:export` permission and then builds and returns the bundle. There is no
call to the consent service anywhere on that path.

The HIPAA control matrix claims this control as C-09 under §164.502(a) and rates
it *not implemented*. The release notes instruct the requesting party to confirm
consent manually. A manual check performed by the party who wants the export is
not a control.

### Steps to reproduce

1. Register a patient.
2. Record `{"purpose": "data-exchange", "granted": false}` for that patient.
3. `GET /fhir/Patient/{id}/$everything` with `Authorization: Bearer demo-partner-token`.
4. **Expected:** `403`, and the refusal recorded in the audit trail.
5. **Actual:** `200` with the full bundle.

The seed cohort includes a patient in exactly this state —
`consent_withheld_patient`, Priya Raman, MRN `RGH-0177310`.

### Acceptance criteria

- An export for a patient whose current `data-exchange` decision is withheld, or who has never been asked, is refused.
- The refusal uses the existing `ConsentWithheld` error, which already maps to `403`.
- The refusal is written to the audit trail. A refused disclosure is as much a disclosure event as a permitted one.
- A permitted export continues to work unchanged.
- Tests cover: granted, withdrawn, never asked, and withdrawn-then-regranted.

### Comments

**Adaeze Nwosu — 2026-08-20**
> Raised at the test review as T-03 and waived under W-01 alongside two
> unrelated findings. The board minute records no discussion of this one
> specifically. It is the only one of the three that defeats a stated business
> requirement rather than a non-functional one.

**Marcus Hale (Principal Engineer) — 2026-08-21**
> `ConsentWithheld` already exists in `errors.py` and is already in the status
> map. It was written for this and never wired up.

---

## CARE-412 — Patient search accepts an unbounded limit

| | |
| --- | --- |
| **Type** | Bug |
| **Priority** | High |
| **Status** | Open |
| **Reporter** | On-call platform engineer |
| **Assignee** | Unassigned |
| **Labels** | `availability` `incident-follow-up` `release-1.0.1` `waived-w01` |
| **Affects version** | 1.0.0 |
| **Fix version** | 1.0.1 |
| **Linked** | caused INC-2026-0814 · relates to CARE-413 |

### Description

`GET /patients` declares `limit: int = Query(default=50)` with no upper bound.
The value is passed to the database unchanged.

The three sibling listing endpoints — encounters, referrals and consent — all
declare `le=settings.max_page_size` and refuse a larger value with `422`. Patient
search is the odd one out, which is why it survived review: a reviewer reading
any one of the other three sees a clamp and moves on.

NFR-06 states that the clamp *is* the requirement. An endpoint that accepts a
caller-supplied limit and passes it through satisfies the letter of "takes a
limit" and none of the intent.

### Incident

Realised on 2026-08-14. A coordinator searched for family name `A` to browse the
cohort alphabetically, with `limit=100000`. The service read the whole patient
table, held a transaction past the 2s ceiling in NFR-11, and blocked every write
behind it on SQLite's single writer. 45 minutes, roughly 60% of requests failing.

Full account in `docs/06-operate/incident-2026-08-14-postmortem.docx`.

### Acceptance criteria

- `GET /patients` clamps `limit` to `settings.max_page_size`, matching the sibling endpoints exactly.
- A limit above the maximum returns `422`.
- A test asserts an upper bound on **every** collection endpoint, not only the ones that have one today — this is postmortem action A-2.

### Comments

**Marcus Hale — 2026-08-15**
> One line. It is one line in four files if we do A-2 properly, and A-2 is the
> part that stops the next one.

---

## CARE-418 — Patient name and date of birth are written to the application log

| | |
| --- | --- |
| **Type** | Bug |
| **Priority** | High |
| **Status** | Open |
| **Reporter** | Priya Raghavan (QA Lead) |
| **Assignee** | Unassigned |
| **Labels** | `privacy` `logging` `release-1.0.1` `waived-w01` |
| **Affects version** | 1.0.0 |
| **Fix version** | 1.0.1 |

### Description

Found during exploratory testing, TC-3. Calling `GET /patients/{id}/risk`
produces this line:

```
{"time":"2026-09-17T07:14:58+0530","level":"INFO",
 "logger":"carepath.services.risk","correlation_id":"b439b6f9…",
 "message":"Scoring readmission risk for Amara Okonjo born [phone]"}
```

Two defects in one line.

**First**, the patient's name is in a log that NFR-04 says must not carry
protected health information. Logs ship to the platform aggregator, where
retention and access are governed differently from the clinical record.

**Second**, the date of birth has been replaced by `[phone]`. The telephone
pattern in `security/phi.py` is `\b\+?\d[\d ()-]{8,}\d\b`, which matches
`1943-03-11`. Dates are being redacted by accident rather than by rule, and the
same pattern will silently mangle any other long numeric value — a LOINC code
with a hyphen, an identifier, a measurement.

The redaction filter is working as designed and the design is too broad. Note
also that redaction can only catch identifiers with a *shape*; a name has none,
so no filter change fixes the first defect. The log call has to stop passing it.

### Acceptance criteria

- The scoring log line identifies the patient by surrogate id only — it carries no name and no date of birth.
- The telephone pattern no longer matches an ISO-8601 date.
- A test pins that an ISO date passes through `redact()` unchanged.
- Existing redaction tests continue to pass.

### Comments

**Priya Raghavan — 2026-08-28**
> Worth saying in the review: coverage is 91% and the redaction function is at
> 90%. Neither number could have told us about this, because the defect is in
> what the *caller* handed to a function that then worked perfectly.

---

## CARE-425 — Referral status changes are not written to the audit trail

| | |
| --- | --- |
| **Type** | Bug |
| **Priority** | Medium |
| **Status** | Open |
| **Reporter** | Adaeze Nwosu |
| **Assignee** | Unassigned |
| **Labels** | `regulatory` `audit` `release-1.0.1` |
| **Affects version** | 1.0.0 |
| **Fix version** | 1.0.1 |

### Description

`services/referrals.py::transition` updates the referral row and returns it. It
does not call `audit.record`.

Every other write path in the service audits — `register`, `read`, `search`,
`open_encounter`, `add_observation`, `referrals.create`, `consent.record_decision`.
This is the single exception, and the function already takes the `Principal` it
would need, so the omission is visible in the signature.

NFR-03 requires an audit event in the same transaction as every access to
patient data. A referral status change is a change to the patient's clinical
record: it is what tells a partner organisation the patient is coming.

The HIPAA control matrix rates C-03 *partially effective* for this reason.

### Why the quarterly review will not catch it

C-10's reconciliation compares access counts against audit counts **per
endpoint**. An endpoint that writes no audit event at all contributes to neither
side, so it is invisible to the review designed to find exactly this — unless
somebody notices the endpoint is absent from the report.

### Acceptance criteria

- `transition` writes an audit event in the same transaction as the update.
- The action distinguishes a status change from a creation.
- The event records the transition that occurred, so an access report shows what changed and not merely that something did.
- A test asserts the audit event, in the same style as the other write-path tests.
- **Postmortem action:** the reconciliation in `service-level-objectives.md` is redesigned so that an unaudited path is detectable. Raise as a separate ticket if it does not fit here.

---

## CARE-431 — An auditor receives the full patient record, including telephone and address

| | |
| --- | --- |
| **Type** | Bug |
| **Priority** | Medium |
| **Status** | Open |
| **Reporter** | Adaeze Nwosu |
| **Assignee** | Unassigned |
| **Labels** | `regulatory` `minimum-necessary` `release-1.1` |
| **Affects version** | 1.0.0 |
| **Fix version** | 1.1 |

### Description

REG-03 (HIPAA §164.502(b), minimum necessary) requires that disclosure is
limited to what the purpose needs. The security design states the intent
plainly: "a caller entitled to know a patient exists is not thereby entitled to
their contact details."

The `auditor` role holds `patient:read` so that a compliance officer can
reconcile an access report against real patients. `GET /patients/{id}` returns
`PatientOut` to every caller holding that permission, so an auditor receives the
name, full date of birth, postal code and telephone number.

A `PatientSummary` schema already exists in `schemas.py`, with a masked MRN, the
family name and the birth *year* only. Its docstring says it is "returned to a
caller who is entitled to know a patient exists but not to read their contact
details — an auditor reconciling an access report, for instance."

**Nothing returns it.** The schema was designed, documented, and never wired to
a route.

`mask_mrn` in `security/phi.py` is in the same position: written, tested, unused.

### Acceptance criteria

- A caller with the `auditor` role receives `PatientSummary` from `GET /patients/{id}`.
- A caller with the `clinician` or `coordinator` role continues to receive `PatientOut` unchanged.
- The response shape is determined by the caller's role, not by a query parameter the caller chooses.
- The read is audited identically in both cases.
- Tests cover both roles.

### Comments

**Marcus Hale — 2026-09-01**
> The interesting part is choosing the response model per role. Returning a
> different shape from one route means the OpenAPI schema needs a union, or the
> route splits. Worth a design note either way.

---

## CARE-437 — Rate limit the authentication path

| | |
| --- | --- |
| **Type** | Story |
| **Priority** | Medium |
| **Status** | Open |
| **Reporter** | Tomas Beck (Security Architect) |
| **Assignee** | Unassigned |
| **Labels** | `security` `nfr-12` `release-1.1` |
| **Fix version** | 1.1 |

### Description

NFR-12 requires authentication to be rate-limited per source to resist
credential stuffing. It is approved, it is in the threat model, and it is not
implemented.

ADR-0002 makes this load-bearing rather than optional. Tokens are static with no
expiry beyond a 90-day rotation, so resistance to guessing has to come from
somewhere, and rate limiting is where the ADR says it comes from.

This is not a defect in written code. It is absent code, which is why no
coverage report shows it and no test fails: **you cannot fail to cover code that
was never written.**

### Acceptance criteria

- Repeated failed authentications from one source are refused with `429` after a configured threshold.
- The threshold and window are configuration, not constants.
- A successful authentication does not consume budget.
- Rate-limit refusals are logged with the correlation identifier and **without** the presented token.
- The limiter's state does not leak whether a token exists.

### Comments

**Tomas Beck — 2026-09-03**
> Per-process counters are enough for the pilot's single instance. Say so in the
> code, because at two instances it silently becomes twice the intended limit.

---

## CARE-444 — Operations runbook names a deployment that was retired

| | |
| --- | --- |
| **Type** | Task |
| **Priority** | Low |
| **Status** | Open |
| **Reporter** | Sam Ferreira (Platform Lead) |
| **Assignee** | Unassigned |
| **Labels** | `documentation` `operational-readiness` |

### Description

`docs/06-operate/operations-runbook.txt` says the deployments are
`carepath-api-blue` (serving) and `carepath-api-green` (standby).

`docs/05-release/deployment-runbook.md` says they were renamed at release 1.0.0
to `carepath-api-green` and `carepath-api-amber`, because "blue" was read as a
colour by half the team and as an environment name by the other half, and two
people deployed to the wrong one during the rehearsal.

The operations runbook was last reviewed 2026-03-18, before release 1.0.0 was
planned. Its `platformctl logs carepath-api-blue` command names a deployment
that no longer exists.

This is R-11 in the risk register: an operational procedure followed
confidently from a document that no longer matches the deployment. A runbook
with wrong names is worse than no runbook, because it is followed with
confidence.

### Acceptance criteria

- The operations runbook names the deployments as the deployment runbook defines them.
- Every command in it is checked against the deployed shape.
- The review date is updated.
- The release plan's overdue "runbook not reviewed against the deployed shape" item is closed.

---

## Board summary

| Key | Type | Priority | Requirement | Traceability | Risk |
| --- | --- | --- | --- | --- | --- |
| CARE-401 | Bug | Highest | BR-05, REG-06 | T-03 | R-01 |
| CARE-412 | Bug | High | NFR-06, NFR-11 | T-01 | R-04 (realised) |
| CARE-418 | Bug | High | NFR-04 | T-02 | R-03 |
| CARE-425 | Bug | Medium | NFR-03, REG-01 | T-04 | R-02 |
| CARE-431 | Bug | Medium | REG-03 | — (F-03) | — |
| CARE-437 | Story | Medium | NFR-12 | T-05 | R-05 |
| CARE-444 | Task | Low | — | — | R-11 |
