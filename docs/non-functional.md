# Non-functional requirements and implementation status

## Security and privacy

Implemented: explicit per-application access, fresh account checks, membership checks, CSRF protection, secure production cookies, HSTS, TLS redirect, strict CSP, no third-party scripts, HTML escaping, Argon2 passwords, database-backed login lockout, and immediate rejection after access revocation. Disabling an organization blocks its applications. Platform administrators cannot read application pages without a grant. The last active application owner cannot be removed through access management or account disable.

Uploaded branding is public platform identity. It accepts PNG/JPEG/WebP up to 2 MB, 4 megapixels and 4096 pixels per side. The server rejects animated or invalid images and reconstructs a PNG without original metadata or trailing payloads. The approved default logo is copied unchanged. Local document uploads now enter a private application-scoped quarantine; production requires scanning, sandboxed conversion, and application-specific private storage; the branding upload handler is not a document intake pipeline.

No secret value belongs in .env. Missing/empty secret files, unexpected .env keys and wildcard hosts fail closed. Windows service accounts must have appropriate ACLs on mounted secrets; POSIX files must have owner-only permissions.

Still required before an external production release: enterprise SSO/MFA, a separate platform-admin identity audience, account recovery, per-application storage identities, application-specific graph/vector databases, source ACL enforcement, PostgreSQL RLS, restrictive runtime database roles, security review, and restore/isolation tests. Shared metadata currently relies on tested application policy rather than database RLS.

## Availability and operability

- GET /health/live: process liveness.
- GET /health/ready: verifies DB connectivity and the core organization table. It does not certify every migration or external service; startup separately applies/checks migrations.
- JSON request events include a generated request ID, route name, status and duration. They exclude query strings, request bodies, credential values and exception messages.
- Responses include X-Request-ID. Audit events retain that ID for correlation with request logs.
- Local request logs rotate at 5 MB with three backups in .runtime/events.jsonl. Server launcher logs are .runtime/server.log and .runtime/server-error.log.
- Production logs go to stdout/stderr for collection and retention by the supervisor/platform.
- Startup refuses occupied ports and verifies readiness. Shutdown checks process ownership and handles the Windows virtual-environment launcher/worker pair.
- Local shutdown is immediate; configure graceful draining, readiness withdrawal and restart policies through the production supervisor.

Still required: external uptime monitoring, alert destinations, centralized trace/metric export, redundant infrastructure and independently tested disaster recovery. Do not claim an availability SLO until the deployment has measurements.

## Performance and resource limits

- Waitress: eight threads, 100 connections, 60-second channel timeout, 32 KB header limit and 22 MB request-body limit (20 MB documents plus multipart overhead).
- Application middleware rejects oversized Content-Length before body parsing. The server enforces the byte limit even if the header is absent or misleading.
- Production PostgreSQL: five-second connect timeout, ten-second statement timeout, five-second lock timeout, 60-second connection reuse and connection health checks.
- Static assets have content hashes and compression. Branding uses ETags with revalidation.
- User lists, audit history and AI receipts are paginated. Hierarchy views are a first-release management view; very large portfolios will need paginated hierarchy queries.
- No persistent authorization cache; permission and feature revocations are evaluated against current database state.
- AI cost amounts use decimals with eight fractional digits. Currency and estimated/reported charges are never silently merged.

Provider deadlines are bounded (OpenAI 30 seconds, GitHub 15 seconds). Per-tenant quotas, token/cost budgets, worker concurrency limits, queue backpressure and load-test targets remain pending. See feature-workflows.md for parser and scanner bounds. No load benchmark or performance guarantee is claimed by the unit tests.

## Auditability and correctness

Mutations and their audit event share a database transaction. Logo replacement and its new hash become visible together. Application owner changes lock the application row on PostgreSQL. Usage receipts are unique by application/provider/request ID; identical replay is safe and a conflicting replay fails. This prevents duplicate recording for one provider request; it does not by itself prevent duplicate provider calls.

The UI exposes no edit/delete operation for audit events or cost receipts. Database-level immutable storage, retention policies, audit export, and tamper evidence remain release work. The database operator can currently change records.

Estimated and provider-reported costs are explicit categories. Automatic reconciliation, exchange rates, invoice alignment and late-arriving usage after job revocation require a provider/job integration design. Keep the audit ledger separate from document contents and prompts.

## Usability and accessibility

Implemented: persistent compact navigation, context-specific application menus, responsive single-column mobile forms, keyboard-focus styles, semantic form labels, meaningful empty states, server validation, visible action results, and logo replacement without rebuilding the app. The sign-in screen has been visually checked at desktop and 390-pixel mobile width.

Before release, complete keyboard/screen-reader testing for the full administrative workflow and formal WCAG contrast checks. Avoid claiming accessibility conformance based only on this first pass.

## Reliability and recovery

Maintain managed PostgreSQL backups with point-in-time recovery. Back up the branding row, hierarchy, users, grants, features, usage receipts and audit events. Protect backup credentials and encrypt backups. Test restoring into an isolated environment and reapplying revocations before reopening access.

Suggested initial objectives to agree with operations: RPO <= 15 minutes and RTO <= 4 hours. These are planning targets, not capabilities delivered by this repository. SQLite is for local development only; the lifecycle scripts never delete the local database.
