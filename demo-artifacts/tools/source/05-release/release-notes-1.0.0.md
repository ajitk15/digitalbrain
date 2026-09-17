# Release notes — CarePath 1.0.0

**Released** 2026-06-04 · **Audience** pilot clinicians, coordinators, Northvale, Information Governance

## What this release does

CarePath holds one record per patient across the network, tracks referrals from
the moment they are raised until they close, and records who opened which record
and why.

### For clinicians

- Register a patient once, against their medical record number. A number already in use is refused rather than duplicated.
- Record admissions, attendances and observations against a patient.
- Raise a referral to a specialty. It starts as a draft — nothing leaves the organisation until it is submitted.
- See a readmission risk score **with the reasons behind it**. The reasons are written to be said aloud to a patient.

### For care coordinators

- A risk score per patient, so the morning list is ordered by who is most likely to return rather than by who telephoned.
- Move a referral through its lifecycle: submitted, accepted, scheduled, completed. The referring clinician sees where it is without a telephone call.
- Record what a patient has agreed to. Decisions are kept in full — granting and later withdrawing leaves both, because when someone withdrew is the question that gets asked.

### For the compliance team

- Every access to a patient record is recorded with the actor, the action, the purpose and the time, and is written in the same transaction as the access itself.
- An access report for any patient is now a single request.

### For Northvale

- A patient's record as a FHIR R4 Bundle: Patient, Encounters, Observations. Base R4, no profile claimed (ADR-0004).

## What this release does not do

Scheduling, prescribing, results, imaging, clinical decision support and
patient-facing access are all out of scope and are not planned for 1.0. See
section 4 of the business requirements.

**The risk score is not clinical advice.** It ranks a worklist. The weights are
illustrative and carry no clinical validity.

## Known limitations

| Limitation | Effect on you | Planned |
| --- | --- | --- |
| Patient search returns an unbounded number of results | A broad search may be slow | 1.0.1 |
| The scoring log line contains patient names | Internal; raised with Information Governance | 1.0.1 |
| The export does not check consent before releasing a record | **Confirm consent before requesting an export** | 1.0.1 |
| Referral status changes are not recorded in the audit trail | A status change will not appear on an access report | 1.0.1 |
| Erasure requests are handled manually | Allow five working days | 2.0 |

The third row is the one to read twice. Until 1.0.1, a partner export does not
consult the patient's consent decision, so the check has to be made by the
person requesting it.

## Upgrading

No action. The pilot database is created on first start.

## Support

Service desk, referencing the `x-correlation-id` from the response — it joins
your request to the service log and to the audit trail.
