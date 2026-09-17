# Security design — CarePath 1.0

| | |
| --- | --- |
| **Document** | DES-CAREPATH-003 |
| **Version** | 1.2 |
| **Status** | Approved |
| **Owner** | Security Architect |
| **Reviewed** | Security Review Board, 2026-03-11 |

## 1. Trust boundaries

| Boundary | Crossing | Control |
| --- | --- | --- |
| Internet → ingress | Partner systems | TLS 1.2+, IP allow-list, bearer token (REG-04) |
| Clinical network → service | Staff clients | TLS, bearer token, role check |
| Service → database | Every query | Parameterised statements, least-privilege grants |
| Service → logs | Every emitted line | Redaction at the formatter (NFR-04) |

Patient data never crosses out of the network boundary except through the FHIR
export, which is the one path deliberately designed for it (C-02).

## 2. Threat model (STRIDE)

| Threat | Scenario | Control | Requirement |
| --- | --- | --- | --- |
| **Spoofing** | A stolen token is replayed | Tokens stored as SHA-256 digests; compared with `hmac.compare_digest`; every candidate compared so timing does not reveal position. Rotation on a 90-day cycle | NFR-05, REG-02 |
| **Spoofing** | Credential stuffing against the token endpoint | **Rate limiting per source.** Stated here and in NFR-12 | NFR-12 |
| **Tampering** | An attacker edits the audit trail to hide an access | No application code path updates or deletes `audit_event`; the database grant excludes both | NFR-09, REG-01 |
| **Tampering** | SQL injection through a search term | Every statement parameterised. No string interpolation into SQL anywhere in the codebase | — |
| **Repudiation** | A clinician denies opening a record | Audit event in the same transaction as the read, naming the unique subject | NFR-03, REG-01, REG-02 |
| **Information disclosure** | A caller probes for the existence of a patient | A record that does not exist and a record the caller may not read are indistinguishable — both "not found" | REG-03 |
| **Information disclosure** | Patient data reaches the log aggregator | Redaction filter on the formatter; log sampling review each sprint | NFR-04 |
| **Information disclosure** | A partner receives more than the purpose requires | Role grants; export limited to the partner role | REG-03 |
| **Information disclosure** | A partner receives a record for a patient who withdrew consent | **Consent consulted before disclosure** | BR-05 |
| **Denial of service** | One request reads the whole patient table | **Server-side clamp on every collection endpoint** | NFR-06 |
| **Denial of service** | A slow query holds a transaction open | 2s transaction ceiling; slow-query log | NFR-11 |
| **Elevation of privilege** | A role gains a permission it was never granted | One grant table; an unlisted role holds nothing. Checked as a declared dependency before the handler runs | NFR-05, REG-03 |

## 3. Authentication

Bearer tokens on the `Authorization` header. The stored form is a SHA-256 digest,
so the directory is not itself a set of usable credentials.

`principal_for` compares **every** stored digest even after a match. An
early return would make the time taken depend on where in the directory a token
sits, which over enough attempts is a distinguishable signal.

Release 1.0 uses a static directory because the network's identity programme
lands after the pilot (ADR-0002). The demonstration tokens are published in this
repository **because they protect nothing** — there is no real data behind them.
A deployment replaces the directory; it does not add to it.

## 4. Authorisation

One table, `GRANTS`, mapping role to permissions. Two properties matter:

- **Closed by default.** `GRANTS.get(role, frozenset())` — an unknown role holds no permissions. Adding a role to the token issuer without deciding its grants therefore grants nothing rather than everything.
- **Declared, not called.** `require(Permission.X)` builds a dependency that appears in the route signature. The check cannot be forgotten in a handler body, and a reviewer sees the requirement without reading the implementation.

Role separation is deliberate and tested:

| Role | May | May not |
| --- | --- | --- |
| clinician | Read and write the clinical record, read risk | Read the audit trail, export, record consent |
| coordinator | Read the record, move referrals, record consent, read risk | Register patients, export |
| auditor | Read the audit trail, confirm a patient exists | Read risk, write anything |
| partner | Export | Everything else |

An auditor deliberately cannot read risk scores. The score is an operational
judgement about a patient, and reviewing access is not a reason to form one.

## 5. Secrets

Mounted as files with owner-only permissions (NFR-07). Never environment
variables — the environment of a process is readable by anything that can read
`/proc`, and it lands in crash dumps and in the output of well-meaning
diagnostic tooling.

## 6. Residual risks accepted for 1.0

| Risk | Why accepted | Compensating control | Review |
| --- | --- | --- | --- |
| A database administrator can read patient rows directly | Column-level encryption is a release 2.0 change (G-02) | Administrative sessions logged and reviewed monthly | Release 2.0 |
| Static tokens have no expiry beyond rotation | Identity programme lands after the pilot | 90-day rotation; revocation by removing the digest | Release 2.0 |
| No automated FHIR validator | Pipeline work not funded for 1.0 (G-04) | Structure reviewed by two engineers at release | Release 1.1 |
