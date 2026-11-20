# Deployment runbook — CarePath

| | |
| --- | --- |
| **Document** | REL-CAREPATH-002 |
| **Version** | 2.0 |
| **Status** | Active |
| **Owner** | Platform Lead |
| **Last exercised** | 2026-06-04, release 1.0.0 |

## Deployment model

Two identical deployments behind one ingress. Traffic moves between them by
changing the ingress backend; the previous deployment stays running and warm
until the release is accepted.

**As of release 1.0.0 the deployments are named `carepath-api-green` and
`carepath-api-amber`.** They were previously `carepath-api-blue` and
`carepath-api-green`; "blue" was retired because half the team read
blue/green as a colour pair and the other half read it as an environment name,
and two people deployed to the wrong one during the 1.0 rehearsal.

| Name | Role |
| --- | --- |
| `carepath-api-green` | Currently serving |
| `carepath-api-amber` | Standby, receives the new build |

## Pre-deployment

- [ ] Release approved at the release review, and the record is in `05-release/release-plan.docx`
- [ ] Test plan executed, exit criteria met or waived in writing
- [ ] Secrets mounted on the target namespace as files, owner-only (NFR-07)
- [ ] Database volume snapshot taken and its identifier recorded below
- [ ] Rollback decision-maker identified and available for the next two hours

## Deploy

1. **Snapshot.** `platformctl volume snapshot carepath-data --tag pre-<version>`. Record the identifier in the release record.
2. **Deploy to standby.** `platformctl deploy carepath-api-amber --image carepath:<version>`
3. **Wait for readiness.** `curl -fsS http://carepath-api-amber.internal/health/ready` until `{"status":"ready"}`. Not `/health/live` — liveness answers before the database is reachable, which is the whole reason there are two endpoints.
4. **Smoke the standby directly**, before any traffic moves:
   ```bash
   TOKEN=$(cat /run/secrets/carepath_smoke_token)
   curl -fsS -H "Authorization: Bearer $TOKEN" http://carepath-api-amber.internal/patients?family_name=Test
   curl -fsS http://carepath-api-amber.internal/health/ready
   ```
5. **Move traffic.** `platformctl ingress set carepath --backend carepath-api-amber`
6. **Watch for ten minutes.** Error rate, p95 latency, readiness. Do not start anything else during this window.
7. **Rename roles.** The standby is now serving. Update the table above in this document at the same time — a runbook whose names are wrong is worse than no runbook, because it is followed confidently.

## Rollback

Decision rule: roll back on any of — error rate above 1% for five minutes, p95
above 600ms for five minutes, any 5xx on the audit endpoint, any report of a
missing audit event.

```bash
platformctl ingress set carepath --backend carepath-api-green
```

The previous deployment is still running, so this takes effect in seconds and
needs no rebuild. **Do not restore the volume snapshot** unless the schema
changed in this release — a restore discards audit events written since the
snapshot, and NFR-09 does not have an exception for convenience.

## Post-deployment

- [ ] Readiness green on the serving deployment for 30 minutes
- [ ] p95 within NFR-02 on the dashboard
- [ ] One end-to-end trace confirmed: a request's `x-correlation-id` found in the service log **and** in an audit event
- [ ] Release record updated with the snapshot identifier and the deployment names as they now stand
- [ ] Standby left running for 24 hours before it is reused

## Database changes

Release 1.0 applies schema at start-up via `CREATE TABLE IF NOT EXISTS`. This is
adequate while the service is single-writer on SQLite (ADR-0001) and **stops
being adequate at the PostgreSQL migration**, where two instances may start
simultaneously. A migration tool is a prerequisite for release 2.0, not a
nice-to-have.
