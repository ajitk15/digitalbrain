# ADR-0003 — Transparent, in-process risk scoring

- **Status:** Accepted
- **Date:** 2026-03-04
- **Deciders:** Clinical Lead, Principal Engineer, Information Governance

## Context

BR-03 asks for a readmission risk score so coordinators can prioritise outreach.
The obvious implementation is a trained model behind a scoring service.

Two things pull against that. First, A-03: the business case assumes
coordinators will *act* on a ranked worklist, and a coordinator who cannot say
why a patient is on their list has, in the clinical lead's words, a list they
will quietly ignore. Second, REG-05's non-applicability determination rests on
CarePath ranking a worklist rather than recommending a clinical action. A model
whose reasoning cannot be stated is much harder to defend as operational rather
than clinical.

## Decision

Release 1.0 scores in-process with a transparent weighted model. Every
assessment returns the **factors** that produced it, each with a description
written to be repeated aloud: "one admission already recorded", "three
observations outside the expected range".

Weights are illustrative and carry no clinical validity. This is stated in the
module docstring, in the API description and to coordinators during training.

## Consequences

**Accepted:**

- The score is less accurate than a trained model would be. For prioritising a list of 400, the ordering matters more than the calibration.
- Weights are tuned by hand. Changing one is a code change under change control, which is slower — and more reviewable — than retraining.
- Scoring shares the request's process and its latency budget (NFR-02).

**Gained:**

- Every score is explainable, which is what A-03 depends on.
- The regulatory determination in REG-05 holds: the service ranks, it does not advise.
- The model is unit-testable with no fixtures and no service to stand up.

**Designed for the successor anyway:** the `degraded` flag on the response and
`RiskEngineUnavailable` in the error set exist for the day scoring moves out of
process (NFR-10). The contract is already the one a remote engine needs.

## Alternatives considered

| Option | Rejected because |
| --- | --- |
| Trained model behind a service | Explainability gap defeats A-03; puts the REG-05 determination in question; adds an availability dependency |
| Buy a vendor risk product | Procurement exceeds C-01, and the explanation problem is the vendor's, not ours |
| No score; coordinators work alphabetically | This is the status quo BR-03 exists to change |

## Revisit when

The pilot demonstrates that coordinators act on the list, and the ordering — not
the explanation — becomes the limiting factor.
