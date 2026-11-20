# Data model — CarePath 1.0

| | |
| --- | --- |
| **Document** | DES-CAREPATH-002 |
| **Version** | 1.3 |
| **Status** | Approved |
| **Owner** | Principal Engineer, Integrated Care |

## Entities

```
  patient 1---* encounter 1---* observation
     |
     +---* referral
     |
     +---* consent

  audit_event  (references resources by type and id, with no foreign key)
```

`audit_event` deliberately holds **no** foreign key to the resources it
describes. An audit row must survive the deletion of the thing it is about — the
record of an erasure is the part you most need to keep.

## Tables

### `patient` — FR-01

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | Opaque surrogate key. Carries no patient information, so it is safe in a URL |
| `mrn` | TEXT UNIQUE | The network join key. Validated as `AAA-1234567`, never reformatted |
| `given_name`, `family_name` | TEXT | Special-category data (REG-06) |
| `birth_date` | TEXT ISO-8601 | Special-category data |
| `postal_code` | TEXT | Retained for catchment reporting |
| `phone` | TEXT NULL | Nullable — many pilot patients have no number on file |
| `created_at` | TEXT ISO-8601 | |

The uniqueness of `mrn` is enforced by the constraint, not by the service check.
The check exists to produce a useful error; the constraint is what holds when two
registrations race.

### `encounter` — FR-04

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | |
| `patient_id` | TEXT FK | |
| `kind` | TEXT | `inpatient`, `outpatient`, `emergency` |
| `admitted_at` | TEXT ISO-8601 | |
| `discharged_at` | TEXT NULL | Null means the patient is still in the bed |
| `facility` | TEXT | |

### `observation` — FR-05

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | |
| `encounter_id` | TEXT FK | |
| `code` | TEXT | LOINC |
| `value` | REAL | |
| `unit` | TEXT | UCUM |
| `recorded_at` | TEXT ISO-8601 | |

### `referral` — FR-07, FR-08

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | |
| `patient_id` | TEXT FK | |
| `specialty`, `reason` | TEXT | `reason` is free text and therefore PHI |
| `status` | TEXT | Values from `ReferralStatus` |
| `urgency` | TEXT | `routine`, `urgent`, `two-week-wait` |
| `created_by` | TEXT | Principal subject |
| `created_at`, `updated_at` | TEXT ISO-8601 | |

### `consent` — FR-09, BR-05

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | |
| `patient_id` | TEXT FK | |
| `purpose` | TEXT | `treatment`, `research`, `data-exchange` |
| `granted` | INTEGER | 0 or 1 |
| `recorded_at` | TEXT ISO-8601 | |

**Append-only.** The current position for a purpose is the most recent row.
Rows are never updated, because "when did they withdraw" is the question a
partner asks when an export stops.

Ordering is by `recorded_at DESC, rowid DESC`, not by timestamp alone. A patient
who grants and immediately withdraws — a checkbox ticked and unticked, which
people do — produces two rows whose timestamps can be identical at the clock's
resolution. Which one is current must not depend on what the database returns
first.

### `audit_event` — FR-11, NFR-03, NFR-09, REG-01

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | |
| `actor` | TEXT | Principal subject. **Never the credential** |
| `actor_role` | TEXT | Role at the time of access, not the role now |
| `action` | TEXT | `create`, `read`, `search`, `export` |
| `resource_type`, `resource_id` | TEXT | No foreign key, by design |
| `purpose` | TEXT | Treatment, payment, operations, data-exchange. **Required** |
| `occurred_at` | TEXT ISO-8601 | |
| `correlation_id` | TEXT | Joins the row to the request's log lines |

`purpose` is required rather than optional. An access log that cannot say why a
record was opened does not answer the question an investigator actually asks.

`actor_role` records the role **at the time of access**. Joining to a live role
table at report time would rewrite history every time somebody changed job.

## Indexes

| Index | Serves |
| --- | --- |
| `idx_encounter_patient` | Patient history and risk scoring |
| `idx_observation_encounter` | Risk scoring |
| `idx_referral_patient` | Referral listing |
| `idx_consent_patient` | Consent lookup before disclosure |
| `idx_audit_resource` | The access report (BR-04) |

## Retention

| Data | Retention | Basis |
| --- | --- | --- |
| Patient, encounter, observation, referral | For the life of the record, per the network's clinical retention schedule | Clinical governance |
| Consent | Same as the patient record. Withdrawn consent is retained, not deleted | Evidence of the decision |
| Audit events | Six years minimum, never deleted by application code | NFR-09, REG-01 |

Database grants for the application role exclude `UPDATE` and `DELETE` on
`audit_event`. The absence of a code path is a convention; the absence of a
grant is a control.

## Migration to PostgreSQL

Planned for release 2.0 (ADR-0001). Types are chosen so the move is mechanical:
TEXT timestamps become `timestamptz`, `INTEGER` consent becomes `boolean`,
`REAL` becomes `numeric`. No SQLite-specific feature is used except `rowid` in
the consent ordering, which becomes an explicit `sequence` column on migration.
