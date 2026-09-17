# Business requirements — CarePath 1.0

| | |
| --- | --- |
| **Document** | BRD-CAREPATH-001 |
| **Version** | 1.2 |
| **Status** | Approved |
| **Owner** | Director of Integrated Care, Riverside Health Network |
| **Approved** | 2026-02-19 by the Clinical Systems Board |

## 1. Context

Riverside Health Network runs one acute hospital, four community clinics and a
partnership with Northvale Clinic. A patient discharged from Riverside General
and referred to a Northvale specialist is today handed a paper letter. The
referral is re-keyed at the receiving end, the referring clinician learns the
outcome only if someone telephones, and nobody can say how long the average
referral takes because no system holds both ends of it.

Two audits in 2025 raised the same finding: the network cannot produce, on
request, a list of who opened a given patient record and why.

## 2. Business requirements

| ID | Requirement | Rationale | Measure of success |
| --- | --- | --- | --- |
| **BR-01** | One patient record per person across the network, keyed on the medical record number | Re-keying at each hand-off creates duplicates, and duplicates split a clinical history | Duplicate rate below 0.5% of registrations at 90 days |
| **BR-02** | Referrals are raised, tracked and closed in one place, with a status the referring clinician can see | The referring clinician currently has no visibility after the letter leaves | 95% of referrals reach a terminal status without a telephone call |
| **BR-03** | Surface a readmission risk score so care coordinators can prioritise outreach | Coordinator capacity is the constraint; it is currently spent on whoever telephones first | 30-day readmission rate reduced by 8% in the pilot cohort |
| **BR-04** | Produce, on demand, a complete record of who accessed a patient record and for what purpose | Standing audit finding, twice raised | An access report for any patient produced within one working day |
| **BR-05** | Honour the patient's recorded consent decisions before their record leaves the organisation | Consent is captured on paper today and consulted by nobody | Zero exports without a current positive consent decision |
| **BR-06** | Exchange records with partner systems in a standard format | Northvale runs a different vendor; a bespoke feed would need building twice | Northvale ingests the export with no custom mapping |

## 3. In scope for release 1.0

Patient registration, encounters and observations, referral lifecycle, consent
capture, readmission risk, audit trail, FHIR R4 export.

## 4. Explicitly out of scope

| Item | Why | Revisit |
| --- | --- | --- |
| Scheduling and calendars | The existing scheduling system stays; CarePath records that a referral was scheduled, not when | Release 2.0 |
| Clinical decision support | A scoring aid that recommends an action becomes a regulated medical device. CarePath ranks a worklist; it does not advise | Not planned |
| Prescribing, results, imaging | Served by existing systems. CarePath is a coordination layer, not a record system | Not planned |
| Patient-facing access | Needs an identity assurance programme that does not exist yet | Release 3.0 |
| Real-time streaming to partners | Batch export meets BR-06. Streaming adds an availability commitment nobody has funded | Release 2.0 |

## 5. Constraints

- **C-01** Pilot must run on the network's existing container platform. No new infrastructure procurement in the 1.0 budget.
- **C-02** No production data leaves the network boundary, including for support or diagnostics.
- **C-03** The pilot cohort is 400 patients across two clinics.
- **C-04** Delivery by 2026-06-30, set by the audit remediation commitment behind BR-04.

## 6. Assumptions

- **A-01** Medical record numbers are already unique within the network. If they are not, BR-01 becomes a data-cleansing programme rather than a software one.
- **A-02** Northvale can consume FHIR R4 without a profile beyond base resources. Unconfirmed at approval; see ADR-0004.
- **A-03** Coordinators will act on a ranked worklist. This is the assumption BR-03's measure actually tests.

## 7. Related documents

- User stories: `01-requirements/user-stories.docx`
- Non-functional requirements: `01-requirements/non-functional-requirements.xlsx`
- Regulatory requirements: `01-requirements/regulatory-requirements.pdf`
- Architecture: `02-design/architecture.html`
