# Non-functional requirements — CarePath 1.0

| | |
| --- | --- |
| **Document** | REQ-CAREPATH-003 |
| **Version** | 1.3 |
| **Status** | Approved |
| **Owner** | Principal Engineer, Integrated Care |

Each requirement states a threshold, how it is verified, and what happens when
it is not met. A non-functional requirement with no verification method is a
preference, and preferences do not belong in an approved document.

## Availability and resilience

| ID | Requirement | Verification | On breach |
| --- | --- | --- | --- |
| **NFR-01** | 99.9% availability measured monthly over the readiness endpoint, excluding announced maintenance | Synthetic probe every 30s | Error budget review; feature work stops at 50% budget burn |
| **NFR-10** | When the risk engine cannot be reached, the service returns the rest of the record with `degraded: true` rather than failing the request | Fault injection in the pre-release soak | Sev-3 incident |
| **NFR-11** | No single request holds a database transaction longer than 2s | Slow-query log | Sev-3 incident |

## Performance

| ID | Requirement | Verification | On breach |
| --- | --- | --- | --- |
| **NFR-02** | p95 latency under 300ms for reads, 800ms for writes, at 50 requests per second | Load test before each release; dashboards in production | Sev-3 incident, capacity review |
| **NFR-06** | **No unbounded queries.** Every endpoint returning a collection takes a limit, and that limit is clamped server-side to a maximum page size | Code review and automated test per collection endpoint | Blocks release |

The clamp is the requirement, not the parameter. An endpoint that accepts a
caller-supplied limit and passes it to the database unchanged satisfies the
letter of "takes a limit" and none of the intent: one request can then read the
whole table, and the first time it happens will be the day the table is large.

## Security and privacy

| ID | Requirement | Verification | On breach |
| --- | --- | --- | --- |
| **NFR-03** | **Every access to patient data writes an audit event in the same transaction as the access.** Not after it, not asynchronously | Test per endpoint; quarterly reconciliation of access counts against audit counts | Blocks release; notifiable under REG-01 |
| **NFR-04** | **No protected health information in application logs**, including names, dates of birth, addresses and free-text clinical detail | Automated redaction at the formatter; log sampling review each sprint | Sev-2 incident |
| **NFR-05** | Every clinical endpoint requires authentication, and authorisation is checked against a role table before the handler body runs | Test per endpoint per role | Blocks release |
| **NFR-07** | Secrets are mounted as files with owner-only permissions. Never environment variables, never the database, never source | Deployment review; image scan | Blocks release |
| **NFR-09** | Audit records are immutable and retained for six years. No application code updates or deletes an audit row | Code review; database grants exclude UPDATE and DELETE on `audit_event` | Notifiable |
| **NFR-12** | Authentication endpoints are rate-limited per source to resist credential stuffing | Load test with repeated invalid credentials | Sev-2 incident |

NFR-03 says *same transaction* deliberately. An audit write that happens after
the read has committed leaves a window in which a record was disclosed and the
trail does not say so, and that window is exactly what an investigation is
trying to rule out.

NFR-04's redaction covers identifiers with a recognisable *shape* — record
numbers, telephone numbers, email addresses. A name has no shape. Keeping names
out of log messages is a rule the calling code has to follow, and it is the rule
most easily broken by a debugging line that nobody removed.

## Observability

| ID | Requirement | Verification | On breach |
| --- | --- | --- | --- |
| **NFR-08** | Structured logs, one JSON object per line, each carrying a correlation identifier that is returned on the response | Log format test; manual trace of one request end to end | Sev-3 incident |

## Maintainability

| ID | Requirement | Verification | On breach |
| --- | --- | --- | --- |
| **NFR-13** | Line coverage at or above 85%, and every functional requirement named in at least one test | Coverage gate in the pipeline; traceability matrix reviewed at release | Blocks release |
| **NFR-14** | The service starts and serves with no configuration beyond a database location | New-joiner runs it on day one | Documentation defect |
