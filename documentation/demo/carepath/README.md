# CarePath

Care coordination and referral service for a regional health network.

> **Demonstration software.** Not a medical device. Synthetic records only. The
> tokens below are published because they protect nothing. See
> [`../README.md`](../README.md).

## Run it

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -e . pytest httpx ruff coverage
.venv/Scripts/python.exe -m carepath --seed
```

`http://127.0.0.1:8100/docs` for the interactive schema. `--seed` prints the
identifiers of the three patients it created.

## Verify it

```bash
.venv/Scripts/python.exe -m pytest -q                       # 111 passed
.venv/Scripts/ruff.exe check src tests                      # clean
.venv/Scripts/python.exe -m coverage run --source=src/carepath -m pytest -q
.venv/Scripts/python.exe -m coverage report                 # 91%
```

## Try it

```bash
TOKEN=demo-clinician-token
BASE=http://127.0.0.1:8100

curl -s -H "Authorization: Bearer $TOKEN" "$BASE/patients?family_name=Okonjo"
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/patients/<id>/risk"
curl -s -H "Authorization: Bearer demo-auditor-token" "$BASE/audit/patient/<id>"
curl -s -H "Authorization: Bearer demo-partner-token" "$BASE/fhir/Patient/<id>/\$everything"
```

| Token | Role | May |
| --- | --- | --- |
| `demo-clinician-token` | clinician | Read and write the clinical record, read risk |
| `demo-coordinator-token` | coordinator | Read the record, move referrals, record consent |
| `demo-auditor-token` | auditor | Read the audit trail |
| `demo-partner-token` | partner | Export FHIR |

## The seed cohort

| Patient | MRN | Shaped to show |
| --- | --- | --- |
| Amara Okonjo | `RGH-0142857` | Scores 0.72 — above the outreach threshold, four risk factors |
| Tomas Iversen | `RGH-0198221` | Scores 0.0 — one unremarkable outpatient visit |
| Priya Raman | `RGH-0177310` | Has **withdrawn** consent to data exchange |

## Layout

```
src/carepath/
├── api/         HTTP. The only layer that knows what a status code is
├── services/    Use cases, transactions, audit. No web framework
├── domain/      Rules that hold regardless of storage or transport
├── security/    Identity, roles, audit, redaction
├── db.py        Schema and the unit of work
├── schemas.py   The wire contract
├── fhir.py      FHIR R4 resource construction
└── app.py       Routers, error map, correlation middleware
```

The dependency direction runs one way. A service can be called from a batch job
because it raises `NotFound`, and only `app.py` decides that this is a 404.

## Three things worth reading

**The audit write shares the request's transaction** (`security/audit.py`). A
read that rolls back leaves no audit row claiming it happened, and a read that
commits cannot leave the trail behind. An audit write that fails fails the
request — a disclosure we cannot record is a disclosure we do not make.

**Permissions are declared, not called** (`api/deps.py`). `require()` builds a
dependency that appears in the route signature, so the check runs before the
handler does and a reviewer sees what a route needs without reading its body.

**Consent is append-only and ordered by insertion** (`services/consent.py`).
Granting then withdrawing leaves both rows, and the current position is resolved
by `recorded_at DESC, rowid DESC` — two decisions a second apart can share a
timestamp at the clock's resolution, and which one is current must not depend on
what the database happens to return first.

## Documentation

The full lifecycle is in [`../docs/`](../docs/). The API is specified in
[`../docs/03-build/api-specification.html`](../docs/03-build/api-specification.html);
`/openapi.json` is authoritative if the two disagree.
