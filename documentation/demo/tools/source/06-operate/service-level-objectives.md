# Service level objectives — CarePath

| | |
| --- | --- |
| **Document** | OPS-CAREPATH-001 |
| **Version** | 1.1 |
| **Status** | Active |
| **Owner** | Platform Lead |
| **Review** | Monthly, with the pilot review |

## Objectives

| SLO | Indicator | Target | Window | Requirement |
| --- | --- | --- | --- | --- |
| **Availability** | Successful readiness probes ÷ total probes | 99.9% | 30 days rolling | NFR-01 |
| **Read latency** | p95 of `GET` requests | < 300ms | 30 days rolling | NFR-02 |
| **Write latency** | p95 of `POST` requests | < 800ms | 30 days rolling | NFR-02 |
| **Audit completeness** | Audit events ÷ patient-data accesses | 100% | 30 days rolling | NFR-03, REG-01 |
| **Export correctness** | Exports with a current positive consent decision ÷ total exports | 100% | 30 days rolling | BR-05 |

## Error budget

99.9% over 30 days permits 43 minutes of unavailability.

| Budget consumed | Action |
| --- | --- |
| Under 50% | Normal delivery |
| 50% | Feature work pauses; reliability work takes priority until the budget recovers |
| 100% | Change freeze except fixes; incident review with the Clinical Systems Board |

**Audit completeness and export correctness have no error budget.** They are
100% or they are an incident, because a partial audit trail is not a partial
control — it is an absent one for the accesses it missed.

## How each is measured

**Availability** — synthetic probe against `/health/ready` every 30 seconds from
two locations. Announced maintenance is excluded; unannounced is not.

**Latency** — ingress access logs, excluding `/health/*`.

**Audit completeness** — quarterly reconciliation. Count accesses to patient
data in the ingress log by path and method, count audit events by action, and
compare. They should agree.

The reconciliation as designed compares counts by *endpoint*. It will not detect
an access path that writes no audit event at all, because such a path
contributes to neither side of a per-endpoint comparison unless somebody
notices the endpoint is missing from the report. The first reconciliation
(2026-09-30) should be read with that in mind.

**Export correctness** — for each export in the audit trail, check the patient's
consent position at that timestamp. Manual until the check is automated.

## Alerting

| Alert | Condition | Severity | Response |
| --- | --- | --- | --- |
| Service down | Readiness failing on all instances for 2 minutes | Sev-1 | Page |
| Elevated errors | 5xx above 1% for 5 minutes | Sev-2 | Page |
| Latency breach | p95 above target for 15 minutes | Sev-3 | Ticket |
| Audit write failure | Any 5xx originating in an audit write | **Sev-1** | Page |
| Budget at 50% | Monthly calculation | Sev-4 | Delivery review |

An audit write failure pages at Sev-1 even though no patient is harmed by it,
because the failure mode it guards against — the service continuing to serve
while silently not recording — is the one that is expensive to discover late.
