# Postmortem — INC-2026-0814, patient search exhausted the connection

| | |
| --- | --- |
| **Document** | OPS-CAREPATH-003 |
| **Incident** | INC-2026-0814 |
| **Severity** | Sev-2 |
| **Date** | 2026-08-14, 14:02–14:47 UTC (45 minutes) |
| **Author** | On-call platform engineer |
| **Status** | Actions open |
| **Review** | Blameless, held 2026-08-19 |

## Impact

For 45 minutes, CarePath served 5xx to roughly 60% of requests. Two clinics
could not open patient records and reverted to telephoning the ward. No data was
lost and no record was disclosed improperly. The audit trail is complete for the
period — every request that succeeded wrote its audit event, and the requests
that failed wrote nothing, which is the behaviour NFR-03 intends.

Availability for August fell to 99.83%, below the 99.9% objective. The error
budget for the month was fully consumed.

## Timeline (UTC)

| Time | Event |
| --- | --- |
| 14:02 | A coordinator searches for family name `A` to browse the cohort alphabetically |
| 14:02 | `GET /patients?family_name=A&limit=100000` is served. The request reads and serialises the full patient table |
| 14:03 | p95 latency alert fires (Sev-3) |
| 14:06 | The search is repeated twice more after the browser appears to hang |
| 14:09 | Error rate passes 1%; Sev-2 page |
| 14:11 | On-call acknowledges |
| 14:19 | Ingress logs show three long-running `GET /patients` requests. Cause identified |
| 14:23 | The coordinator is asked by telephone to stop retrying |
| 14:31 | `carepath-api-blue` restarted to clear the held transactions |
| 14:39 | Error rate normal |
| 14:47 | Declared resolved after a clean watch window |

## What happened

`GET /patients` accepts a `limit` query parameter and passes it to the database
unchanged. The three sibling listing endpoints — encounters, referrals and
consent — declare `le=settings.max_page_size`, so a limit above the maximum page
size is refused with a 422. Patient search does not.

A single-letter prefix matched every patient. With a limit of 100,000 the
service read the whole table, held a transaction open well past the 2s ceiling
in NFR-11, and — on SQLite's single writer (ADR-0001) — blocked every write
behind it.

## Why it was not caught

**It was.** T-01 in the traceability matrix records exactly this, raised at the
test review on 2026-05-28:

> `GET /patients` declares `limit` with no `le` bound, unlike the encounter,
> referral and consent listings. There is no test asserting an upper bound on
> patient search.

It was waived under W-01 at the release review on 2026-06-02, bundled with two
unrelated findings under a single rationale: the pilot cohort is small and
behind an allow-list.

That rationale addressed *exposure* — who could reach the endpoint. It did not
address *blast radius* — what one authorised, well-meaning user could do with
it. Nobody at the board separated those, because the three findings were
presented together and discussed as one item.

## Contributing factors

- The three sibling endpoints clamp and this one does not. The inconsistency made it invisible on review: a reviewer reading any one file sees a clamp.
- The alert at 14:03 was Sev-3, six minutes before the Sev-2. A latency alert that precedes an error alert is usually the more informative one.
- The operations runbook's "latency has risen" section already named this cause, and it was reached at 14:19 rather than 14:11, because the on-call went to dashboards first.

## What went well

- The correlation identifier made the trace immediate once the logs were opened.
- Liveness and readiness being separate meant the orchestrator did not restart-loop the instances while the database was contended.
- The audit trail's transactional guarantee held under failure. Nothing was disclosed without a record.

## Actions

| # | Action | Owner | Due | Status |
| --- | --- | --- | --- | --- |
| **A-1** | Clamp `limit` on `GET /patients` to the maximum page size, as the sibling endpoints do | Engineering | 2026-08-22 | Open |
| **A-2** | Add a test asserting an upper bound on every collection endpoint, not only on the ones that have one today | Engineering | 2026-08-22 | Open |
| **A-3** | Waivers cover one finding each. A bundled waiver hides the finding whose rationale is weakest | Delivery Manager | 2026-08-26 | Open |
| **A-4** | Release review to record, per waived finding, both the exposure and the blast radius | Delivery Manager | 2026-08-26 | Open |
| **A-5** | Re-examine W-01's remaining findings individually — T-02 and T-03 were waived on the same sentence | Clinical Systems Board | 2026-09-02 | Open |
| **A-6** | On-call guidance: read the runbook section matching the first alert before opening dashboards | Platform | 2026-08-29 | Open |

A-5 is the action this postmortem exists for. One of the two findings still
waived under W-01 is T-03 — the export path does not consult consent before
releasing a record to a partner. That one touches a business requirement, and
the argument that carried it through the board was written about a different
finding entirely.
