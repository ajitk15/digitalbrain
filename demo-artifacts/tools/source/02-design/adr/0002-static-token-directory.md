# ADR-0002 — A static token directory, not an identity provider

- **Status:** Accepted
- **Date:** 2026-02-26
- **Deciders:** Security Architect, Principal Engineer

## Context

The network's identity programme — single sign-on across clinical systems — is
scheduled to complete after the CarePath pilot. Waiting for it would put the
1.0 delivery date (C-04) behind the audit remediation commitment that motivated
BR-04 in the first place.

REG-02 requires a unique identifier per user. It does **not** require a
particular mechanism for establishing it.

## Decision

Release 1.0 authenticates with bearer tokens against a directory held in the
service. Tokens are stored as SHA-256 digests and compared with
`hmac.compare_digest`. The principal behind a token carries the unique subject
identifier that REG-02 asks for, and that identifier is what the audit trail
records.

The authentication boundary is one function, `principal_for`. Replacing it with
an OIDC token introspection call in release 2.0 changes that function and
nothing else.

## Consequences

**Accepted:**

- No expiry beyond a 90-day rotation. A leaked token is valid until someone removes its digest.
- No per-user provisioning workflow. Adding a user is a deployment.
- The demonstration tokens are published in the repository. They protect nothing: there is no real data behind them, and a deployment replaces the directory rather than adding to it.

**Required by this decision:**

- Rate limiting on authentication (NFR-12). Without expiry, resistance to guessing has to come from somewhere, and this is where.
- Timing-safe comparison across the whole directory, so the time taken does not reveal where a token sits.

## Alternatives considered

| Option | Rejected because |
| --- | --- |
| Wait for the identity programme | Misses C-04 and the audit commitment |
| Build a local user store with passwords | Inherits password reset, lockout and complexity policy — a project in itself, thrown away in release 2.0 |
| mTLS for all callers | Right for the partner boundary, unworkable for clinical staff on shared workstations |

## Revisit when

The identity programme reaches production, or before onboarding any
organisation beyond Northvale.
