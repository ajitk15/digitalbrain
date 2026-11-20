# ADR-0004 — Base FHIR R4, not a claimed profile

- **Status:** Accepted
- **Date:** 2026-03-11
- **Deciders:** Integration Lead, Principal Engineer, Information Governance

## Context

BR-06 requires exchange in a standard format so Northvale can ingest records
without a bespoke mapping. A-02 assumes Northvale can consume base FHIR R4, and
that assumption was unconfirmed when the business requirements were approved.

The temptation is to claim US Core conformance, because it reads better in an
integration conversation.

## Decision

Release 1.0 exports base FHIR R4 resources — Patient, Encounter, Observation —
in a searchset Bundle. **No profile conformance is claimed**, in the code, in
the `meta` block, or in conversation with partners.

Conformance is a claim, and a claim needs a validator behind it. Release 1.0
does not run the US Core validator in its pipeline (G-04), so it does not get to
say it conforms.

## Consequences

**Accepted:**

- A partner expecting US Core must map the difference themselves. Northvale confirmed on 2026-03-09 that base R4 is sufficient, which closes A-02.
- Structure is verified by review, not by a validator. Two engineers sign the export at each release.
- Terminology bindings are minimal: LOINC for observation codes, UCUM for units, v3-ActCode for encounter class. Nothing else is bound.

**Gained:**

- No conformance statement to defend under inspection that the software cannot substantiate.
- The export is small enough to read in full during a review, which is how the structure is actually verified today.

## Alternatives considered

| Option | Rejected because |
| --- | --- |
| Claim US Core without validating | An assertion the pipeline cannot support. This is the option this ADR exists to refuse |
| Run the US Core validator in the pipeline for 1.0 | Correct, and not funded for 1.0. Recorded as G-04 against release 1.1 |
| Custom JSON tailored to Northvale | Defeats BR-06 — the next partner needs it built again |

## Revisit when

A second partner is onboarded, or G-04 is funded — whichever is first.
