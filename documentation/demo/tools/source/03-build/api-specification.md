# API specification — CarePath 1.0

| | |
| --- | --- |
| **Document** | BLD-CAREPATH-002 |
| **Version** | 1.0.0 |
| **Status** | Active |
| **Base URL** | `https://carepath.riverside.example` |
| **Live schema** | `/docs` (Swagger UI), `/openapi.json` |

This document is the reviewed narrative. The generated schema at
`/openapi.json` is authoritative for shapes; if the two disagree, the generated
one is right and this document has a defect.

## Authentication

All endpoints except `/health/*` require a bearer token.

```
Authorization: Bearer <token>
```

| Failure | Status |
| --- | --- |
| No header, or a scheme other than `Bearer` | `401` with `WWW-Authenticate: Bearer` |
| Token not in the directory | `401` |
| Token valid, role lacks the permission | `403` |

## Correlation

Every response carries `x-correlation-id`. Supply your own on the request and it
is echoed; omit it and one is minted. It is the join key between the response,
the service logs and the audit trail (NFR-08).

## Status codes

| Code | Meaning in this API |
| --- | --- |
| `200` / `201` | Success |
| `401` / `403` | Not authenticated / not permitted |
| `404` | Not found **or** not permitted to know it exists (REG-03) |
| `409` | The request contradicts recorded state — a duplicate number, a forbidden referral transition |
| `422` | Well-formed but unacceptable: a malformed medical record number, a limit above the maximum page size |
| `503` | A dependency is unavailable; readiness is failing |

`404` covering both "absent" and "not yours" is deliberate. Confirming that a
patient exists is itself a disclosure.

## Endpoints

### Health — FR-12

| Method | Path | Role | Notes |
| --- | --- | --- | --- |
| `GET` | `/health/live` | none | Process is up. Consults nothing |
| `GET` | `/health/ready` | none | Consults the database. `503` when it cannot |

### Patients — FR-01, FR-02, FR-03, FR-06

| Method | Path | Permission | Notes |
| --- | --- | --- | --- |
| `POST` | `/patients` | `patient:write` | `201`. `409` on a duplicate MRN, `422` on a malformed one |
| `GET` | `/patients?family_name=&limit=` | `patient:read` | Prefix match. One audit event for the search |
| `GET` | `/patients/{patient_id}` | `patient:read` | Audited with purpose |
| `GET` | `/patients/{patient_id}/risk` | `risk:read` | Score plus the factors behind it |

```bash
curl -H "Authorization: Bearer demo-clinician-token" \
     -H "Content-Type: application/json" \
     -d '{"mrn":"RGH-0142857","given_name":"Amara","family_name":"Okonjo","birth_date":"1943-03-11","postal_code":"RG1 4QT"}' \
     https://carepath.riverside.example/patients
```

Risk response:

```json
{
  "patient_id": "4315c616d0f74449ae5ce9fea4435a92",
  "score": 0.72,
  "needs_outreach": true,
  "factors": [
    {"code": "prior-admissions", "description": "1 admission(s) already recorded", "weight": 0.18},
    {"code": "abnormal-observations", "description": "3 observation(s) outside the expected range", "weight": 0.27},
    {"code": "age", "description": "Older than 75", "weight": 0.15},
    {"code": "length-of-stay", "description": "Longest stay 9 days", "weight": 0.12}
  ],
  "degraded": false
}
```

`degraded` is `true` when the score was produced without a dependency that was
unavailable (NFR-10). The record is still returned.

### Encounters and observations — FR-04, FR-05

| Method | Path | Permission | Notes |
| --- | --- | --- | --- |
| `POST` | `/patients/{patient_id}/encounters` | `encounter:write` | `422` if discharge precedes admission |
| `GET` | `/patients/{patient_id}/encounters?limit=` | `encounter:read` | Newest first. `422` above the maximum page size |
| `POST` | `/encounters/{encounter_id}/observations` | `encounter:write` | LOINC code, value, UCUM unit |

### Referrals — FR-07, FR-08

| Method | Path | Permission | Notes |
| --- | --- | --- | --- |
| `POST` | `/patients/{patient_id}/referrals` | `referral:write` | Created as `draft` |
| `GET` | `/patients/{patient_id}/referrals?limit=` | `referral:read` | Newest first |
| `POST` | `/referrals/{referral_id}/status` | `referral:write` | `409` on a forbidden transition |

```
draft ──> submitted ──> accepted ──> scheduled ──> completed
  │           │                          
  │           └──> declined              
  └───────────┴──────────────┴──> cancelled
```

Completed, declined and cancelled are terminal.

### Consent — FR-09, BR-05

| Method | Path | Permission | Notes |
| --- | --- | --- | --- |
| `POST` | `/patients/{patient_id}/consent` | `consent:write` | Append-only. Purposes: `treatment`, `research`, `data-exchange` |
| `GET` | `/patients/{patient_id}/consent?limit=` | `consent:read` | Full decision history, newest first |

### FHIR export — FR-10, BR-06, REG-07

| Method | Path | Permission | Notes |
| --- | --- | --- | --- |
| `GET` | `/fhir/Patient/{patient_id}/$everything` | `fhir:export` | Base FHIR R4 searchset Bundle |

Returns `Patient`, then `Encounter` resources, then `Observation` resources. No
profile conformance is claimed (ADR-0004).

### Audit — FR-11, REG-01

| Method | Path | Permission | Notes |
| --- | --- | --- | --- |
| `GET` | `/audit/{resource_type}/{resource_id}` | `audit:read` | Oldest first |

Read-only by construction. There is no route that writes, updates or deletes an
audit event.

```json
[
  {
    "id": "0d41…", "actor": "dr.okafor@riverside.example", "actor_role": "clinician",
    "action": "read", "resource_type": "patient", "resource_id": "4315…",
    "purpose": "treatment", "occurred_at": "2026-04-01T09:14:58+00:00",
    "correlation_id": "b439b6f92359434c935f8e63ef63c601"
  }
]
```

## Permission matrix

| | clinician | coordinator | auditor | partner |
| --- | :---: | :---: | :---: | :---: |
| `patient:read` | ● | ● | ● | |
| `patient:write` | ● | | | |
| `encounter:read` / `:write` | ● | read | | |
| `referral:read` / `:write` | ● | ● | | |
| `consent:read` | ● | ● | | |
| `consent:write` | | ● | | |
| `risk:read` | ● | ● | | |
| `fhir:export` | | | | ● |
| `audit:read` | | | ● | |

## Demonstration tokens

`demo-clinician-token`, `demo-coordinator-token`, `demo-auditor-token`,
`demo-partner-token`.

They protect nothing — there is no real data behind them. A deployment replaces
the directory (ADR-0002).
