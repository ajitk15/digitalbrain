# ADR-0001 — SQLite for release 1.0

- **Status:** Accepted
- **Date:** 2026-02-26
- **Deciders:** Principal Engineer, Platform Lead, Product Owner
- **Supersedes:** —

## Context

The pilot is 400 patients across two clinics (C-04). The schema is still moving:
three of the six tables changed shape during the design phase. The platform team
charges a standing cost per managed PostgreSQL instance, and C-01 forbids new
infrastructure procurement inside the 1.0 budget.

## Decision

Release 1.0 ships on SQLite, on a single encrypted volume, with a documented
migration path to PostgreSQL in release 2.0.

## Consequences

**Accepted:**

- One writer. Concurrent writes serialise. At pilot volumes this is invisible; at network scale it would not be.
- No managed backup. Backup is a volume snapshot on the platform's schedule, tested quarterly rather than continuously.
- No read replicas, so the availability target (NFR-01) is met by fast restart rather than by failover.

**Gained:**

- A new engineer runs the service with no database to provision, which is what NFR-14 asks for.
- Schema changes during the pilot cost a file, not a change request.
- The test suite runs against `:memory:`, so tests are fast and genuinely isolated.

**Constraining the future:** no SQLite-specific feature is used except `rowid`
as a tiebreak in the consent ordering, which becomes an explicit `sequence`
column on migration. Types are chosen so the move is mechanical — see the
migration note in `02-design/data-model.md`.

## Alternatives considered

| Option | Rejected because |
| --- | --- |
| Managed PostgreSQL now | Violates C-01. The standing cost is real and the pilot may not proceed |
| PostgreSQL in a container the team runs | Moves the operational burden onto a team of three and gives up the managed backup that was the point |
| Document store | The data is relational, the queries are relational, and the audit report is a join |

## Revisit when

Any of: the pilot exceeds 5,000 patients; a second writer instance is needed;
the availability target moves above 99.9%.
