# Architecture — CarePath 1.0

| | |
| --- | --- |
| **Document** | DES-CAREPATH-001 |
| **Version** | 1.5 |
| **Status** | Approved |
| **Owner** | Principal Engineer, Integrated Care |
| **Reviewed** | Architecture Review Board, 2026-03-04 |

## 1. Context

```
   Dr Okafor            Joseph Mbeki          Compliance          Northvale
   (clinician)          (coordinator)         (auditor)           (partner system)
        |                     |                    |                    |
        +---------------------+--------------------+--------------------+
                                       |
                                   HTTPS, bearer
                                       |
                             +---------v---------+
                             |     CarePath      |
                             |   FastAPI / ASGI  |
                             +---------+---------+
                                       |
                             +---------v---------+
                             |   SQLite (1.0)    |
                             |  PostgreSQL (2.0) |
                             +-------------------+
```

CarePath is a coordination layer. It is not the record system for prescribing,
results or imaging, and it does not attempt to become one.

## 2. Layers

The dependency direction runs one way, top to bottom. Nothing below a line
imports anything above it, which is what lets a service be called from a batch
job with no web framework in scope.

| Layer | Package | Knows about | Must not know about |
| --- | --- | --- | --- |
| Transport | `carepath.api` | HTTP, status codes, FastAPI | SQL |
| Application | `carepath.services` | Use cases, transactions, audit | HTTP |
| Domain | `carepath.domain` | Rules that hold regardless of storage or transport | Both of the above |
| Persistence | `carepath.db` | SQL, schema, transactions | Use cases |
| Cross-cutting | `carepath.security`, `carepath.logging_setup` | Identity, audit, redaction, correlation | Use cases |

**HTTP status codes appear in exactly one place**: the error map in
`carepath.app`. A service raises `NotFound`; only the edge decides that this is
a 404. This is what makes the services reusable from a scheduled job.

## 3. Request path

```
request
  -> correlation middleware      mint or accept x-correlation-id        NFR-08
  -> authentication dependency   bearer token -> Principal              NFR-05, REG-02
  -> authorisation dependency    role -> permission, before the body    NFR-05, REG-03
  -> unit-of-work dependency     open a transaction
       -> service                use case
            -> audit.record      IN THE SAME TRANSACTION                NFR-03, REG-01
            -> repository        SQL
       <- commit, or roll back both the work and its audit row
  <- response, carrying x-correlation-id
```

The authorisation check is a **dependency declared in the route signature**, not
a call in the handler body. A reviewer can see what a route requires without
reading its implementation, and a handler cannot forget the check, because the
check runs before the handler does.

## 4. The audit invariant

This is the design decision the rest of the service is arranged around.

The audit write shares the request's transaction. Three consequences follow, and
all three are intended:

1. A read that rolls back leaves **no** audit row claiming it happened.
2. A read that commits **cannot** leave the trail behind — there is no ordering in which one lands and the other does not.
3. An audit write that fails fails the request. A disclosure we cannot record is a disclosure we do not make.

The third is the uncomfortable one. It means an audit-table problem is an
outage. That is the correct trade for a system whose second-most-cited business
requirement (BR-04) is the access report.

## 5. Component responsibilities

| Component | Responsibility | Requirements |
| --- | --- | --- |
| `api/deps.py` | Connection, principal, permission. `require()` builds a dependency, so the permission is declarative | NFR-05 |
| `security/auth.py` | Token digest comparison with `hmac.compare_digest`; every candidate compared so timing does not leak position | NFR-05, REG-02 |
| `security/rbac.py` | One grant table. An unlisted role holds **no** permissions, so adding a role without deciding its grants grants nothing | REG-03 |
| `security/audit.py` | The only writer of `audit_event`. No update path, no delete path | NFR-03, NFR-09, REG-01 |
| `security/phi.py` | Redaction of shaped identifiers from anything emitted | NFR-04 |
| `domain/referral_state.py` | The lifecycle map, testable without a web server | FR-08 |
| `domain/risk.py` | Transparent arithmetic returning factors alongside the score | FR-06, BR-03 |
| `services/consent.py` | Append-only decisions; absent consent reads as withheld | FR-09, BR-05 |
| `fhir.py` | Base FHIR R4 resource construction | FR-10, REG-07 |

## 6. Quality attribute scenarios

| Scenario | Stimulus | Response | Measure |
| --- | --- | --- | --- |
| Coordinator opens the morning worklist | 400 risk scores requested in five minutes | All served | p95 under 300ms (NFR-02) |
| Database becomes unreachable | Readiness probe fails | Instance leaves the pool; liveness still passes, so no restart loop | Removed within 30s (NFR-01) |
| Risk engine unavailable | Scoring dependency times out | Record returned with `degraded: true` | No 5xx (NFR-10) |
| Auditor asks who read a record | Access report requested | Complete trail for that resource | Same working day (BR-04) |
| Partner requests a record for a patient who withdrew consent | Export requested | Refused | Zero exports without current consent (BR-05) |

## 7. Deferred, with reasons

| Decision | 1.0 position | Record |
| --- | --- | --- |
| SQLite rather than PostgreSQL | Pilot volumes are small and the schema is still moving | ADR-0001 |
| Static bearer tokens rather than an identity provider | The network's identity programme lands after the pilot | ADR-0002 |
| In-process scoring rather than a model service | Explainability matters more than sophistication at this stage | ADR-0003 |
| Base FHIR rather than US Core | Conformance is a claim that needs a validator to back it | ADR-0004 |
