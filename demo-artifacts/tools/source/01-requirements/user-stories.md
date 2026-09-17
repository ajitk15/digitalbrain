# User stories — CarePath 1.0

| | |
| --- | --- |
| **Document** | REQ-CAREPATH-002 |
| **Version** | 1.4 |
| **Status** | Approved |
| **Owner** | Product Owner, Integrated Care |

Each story carries the functional requirement identifier it realises. The
traceability matrix in `04-test/traceability-matrix.xlsx` joins these to the tests.

## Personas

| Persona | Role in the system | What they are trying to do |
| --- | --- | --- |
| **Dr Okafor** | `clinician` | Sees patients, records what happened, raises referrals |
| **Joseph Mbeki** | `coordinator` | Works a list of at-risk patients, chases referrals, records consent |
| **Compliance team** | `auditor` | Answers "who opened this record, and why" |
| **Northvale exchange** | `partner` | A machine, pulling records the patient agreed to share |

---

## Registration and retrieval

### US-01 — Register a patient (FR-01)
*As* Dr Okafor *I want* to register a patient with their medical record number
*so that* every subsequent note attaches to one record.

**Acceptance criteria**
- A well-formed medical record number (`RGH-0142857`) is accepted.
- A malformed number is **refused, not corrected**. Silently upper-casing would make CarePath the only system in the network that accepts a malformed number.
- A number already registered returns a conflict, not a second record.
- Registration writes an audit event naming the registering clinician.

### US-02 — Read a patient (FR-02)
*As* Dr Okafor *I want* to open a patient record *so that* I can see who I am treating.

**Acceptance criteria**
- A record I am entitled to read is returned in full.
- A record that does not exist and a record I may not read are **indistinguishable** — both are "not found". Confirming existence is itself a disclosure.
- Every read writes an audit event carrying the purpose of access.

### US-03 — Find a patient by name (FR-03)
*As* Dr Okafor *I want* to search by family name *so that* I can find a patient whose number I do not have.

**Acceptance criteria**
- Matches on a prefix of the family name.
- Returns a bounded number of results.
- Writes **one** audit event describing the search, not one per result. The question an investigator asks is what was looked for.

---

## Clinical record

### US-04 — Record an encounter (FR-04)
*As* Dr Okafor *I want* to record an admission or attendance *so that* the patient's history is complete.

**Acceptance criteria**
- Inpatient, outpatient and emergency are all recordable.
- A discharge earlier than its admission is refused.
- An encounter with no discharge is valid — the patient is still in the bed.

### US-05 — Record an observation (FR-05)
*As* Dr Okafor *I want* to record vital signs against an encounter *so that* they inform the risk score.

**Acceptance criteria**
- An observation carries a LOINC code, a value and a unit.
- An observation against an unknown encounter is refused.

---

## Coordination

### US-06 — See who needs contacting (FR-06)
*As* Joseph *I want* a readmission risk score *so that* I spend my day on the patients most likely to come back.

**Acceptance criteria**
- The score is between 0 and 1.
- **Every score is accompanied by the factors that produced it**, in words I can repeat to a patient. A score I cannot explain is a score I will not act on.
- A patient with no history scores zero rather than erroring.
- Scores at or above the outreach threshold are flagged.

### US-07 — Raise a referral (FR-07)
*As* Dr Okafor *I want* to refer a patient to a specialty *so that* they are seen by the right team.

**Acceptance criteria**
- A new referral starts as a draft. Nothing has left the organisation yet.
- The referral records who raised it.

### US-08 — Track a referral (FR-08)
*As* Joseph *I want* to move a referral through its lifecycle *so that* the referring clinician can see where it is.

**Acceptance criteria**
- Permitted moves: draft → submitted → accepted → scheduled → completed.
- A referral may be cancelled from any non-terminal state.
- Completed, declined and cancelled are terminal.
- A move the lifecycle forbids is a **conflict**, not a validation error — the request was well formed and would have been accepted a moment earlier.

### US-09 — Record consent (FR-09)
*As* Joseph *I want* to record what the patient has agreed to *so that* we only share what they permitted.

**Acceptance criteria**
- Decisions are recorded per purpose: treatment, research, data exchange.
- Consent is **append-only**. Granting then withdrawing leaves two rows, because "when did they withdraw" is the question that gets asked.
- A patient who has never been asked has **not** agreed. Silence is not permission.

---

## Assurance and exchange

### US-10 — Export to a partner (FR-10)
*As* the Northvale exchange *I want* a patient's record as FHIR R4 *so that* I can ingest it without a bespoke mapping.

**Acceptance criteria**
- Returns a Bundle holding the Patient, their Encounters and their Observations.
- The medical record number appears as a FHIR identifier.
- Only a caller holding the partner role may export.

### US-11 — Produce an access report (FR-11)
*As* the compliance team *I want* every access to one record *so that* I can answer an audit question in a day rather than a fortnight.

**Acceptance criteria**
- Every access shows actor, role, action, purpose, timestamp and correlation identifier.
- The endpoint is read-only. There is no route that edits or deletes an audit event.
- An auditor may read the trail but may not register patients or read risk scores.

### US-12 — Know the service is healthy (FR-12)
*As* the platform team *I want* liveness and readiness separately *so that* a database blip takes an instance out of the pool instead of restarting it in a loop.

**Acceptance criteria**
- Liveness needs no token and consults no dependency.
- Readiness consults the database and returns 503 when it cannot.
- Every response carries a correlation identifier; a supplied one is echoed.
