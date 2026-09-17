# Deployment procedure

The repository is a tested control-plane foundation, not a completed production deployment. Use the original design and the release gaps in non-functional.md when planning the knowledge and AI services.

## Runtime and dependency integrity

Use Python 3.12 and install from the committed uv.lock:

```sh
uv sync --frozen --no-dev
```

Run with a dedicated unprivileged identity. The development start-all/stop-all scripts are Windows conveniences and explicitly reject production configuration. They do not manage PostgreSQL or cloud services.

## Settings

Copy config/production.example.toml to the deployment configuration mount and supply real hostnames, allowed origins and PostgreSQL CA file. Set the process-level DIGITAL_BRAIN_CONFIG pointer to that file.

Project secrets from the deployment secret manager into the configured directory:

- django_secret_key: a unique, cryptographically generated signing key, at least 50 characters.
- database_password: the runtime database credential.

Provider and connector credentials are mounted the same way, one file per provider per
application, named with the application's UUID: openai_UUID, claude_UUID, github_UUID,
github_write_UUID (deliberately a different file from the read-only github_UUID) and
sharepoint_UUID. They are never environment variables, never database rows and never typed into
a form. A missing file is an error, not a fallback to another application's credential or to the
operator's own login.

Use workload identity to retrieve/project secrets, read-only mounts, owner-only file access, and a restricted network. No .env file is needed during normal operation. SITE_ADMIN_USER_ID may be supplied for the one-time interactive bootstrap.

Two mount requirements are easy to miss. A Kubernetes Secret volume defaults to mode 0644 and read_secret refuses any file with group or other permission bits, so set defaultMode to 0400 or every credential is rejected as unreadable. Mount the whole directory rather than individual keys with subPath: a subPath mount never receives updates, so a credential added for a new application would need a redeploy, whereas a directory mount is picked up without restarting because nothing caches a secret in memory.

## Credentials set by application owners

Optional, and off unless configured. Setting managed_secret_directory lets an application owner set their own provider and connector credentials from the Credentials screen instead of raising a ticket for each one. Leave it unset and credentials stay operator-only, exactly as before.

It must be a writable volume owned by the service account, and a different directory from secret_directory, which is projected read-only. A credential your secret manager projects always takes precedence over one entered in the browser, so migrating a credential into the secret manager needs no change in the product. On more than one replica it must be shared storage: a file written on one replica is otherwise invisible to the others.

What is written there is a plain credential file, identical in name, shape and permissions to one the deployment mounts, so it needs the same protection as the secret mount: encryption at rest, exclusion from ordinary backups and from any log or metrics collection that reads the filesystem. Platform secrets - django_secret_key and database_password - cannot be set this way and remain operator-only.

The PostgreSQL connection requires verify-full TLS. Select a non-superuser runtime role with no schema-management rights. Use a separate migration identity for schema changes. Establish database-level audit protections and row-level security before storing production application content.

## Release sequence

1. Install the frozen dependencies and review the migration plan.
2. Back up PostgreSQL and verify a recent restore exercise.
3. With the migration identity, run python manage.py migrate --noinput.
4. With the runtime configuration, run python manage.py check --deploy --fail-level WARNING.
5. Run python manage.py collectstatic --noinput. Allow the service to write only its static build directory during this step; serve static assets read-only afterwards.
6. Start python scripts/serve.py --host 127.0.0.1 --port 8000 --instance DEPLOYMENT_INSTANCE_ID through your process supervisor. Instance ID is an operational marker, not a credential.

   **Run exactly one of these processes.** This is a correctness constraint, not a capacity
   choice. The stream registry in platform_core/agent_runtime/streaming.py is process-local, so
   a user's Stop reaches a provider call only if the same process owns it - with a second
   process, Stop silently fails to cancel a call the application is still being billed for. The
   per-token API rate limit in platform_core/api_auth.py is process-local for the same reason,
   and multiplies by the number of processes. Scale with threads inside the one process, and
   introduce a shared cancellation channel before scaling out.
7. Place the listener behind a TLS reverse proxy and check readiness through the intended ingress.
8. Verify sign-in, CSRF enforcement, permissions, audit recording and provider isolation in staging before opening traffic.

If trust_proxy=true, the proxy MUST remove client-supplied X-Forwarded-Proto and set it itself. Restrict the backend listener to that proxy. Waitress clears untrusted proxy headers by default: either terminate TLS at the WSGI-facing layer or explicitly configure Waitress trusted_proxy and trusted_proxy_headers for the exact proxy in your deployment entrypoint. Do not just enable a Django header setting and assume the server trusts the proxy. Configure equivalent trusted client-IP handling before relying on IP-level lockout behind a shared proxy.

Use a supervisor for process restart, graceful draining and log rotation. The 60-second channel timeout is a socket inactivity limit, not a hard execution deadline for application code.

Provider calls carry their own bounds: a streamed chat answer is capped at 180 seconds with at most 12 concurrent streams, graph extraction at 600 seconds, and outbound HTTP at 8 MB / 20 seconds. Cancellation is implemented, subject to the single-process constraint above.

## Maintenance

- Run python manage.py clearsessions daily.
- Configure django-axes retention according to your authentication/audit policy.
- Review dependency security updates and regenerate uv.lock through the normal change process.
- Rotate signing/database secrets through the secret manager. Signing-key rotation currently invalidates sessions; scheduled fallback keys are not configured.
- Restrict and rotate operator credentials. Use approved recovery procedures; do not edit .env to reset authentication.
- Send request and audit events to separate approved retention sinks when those adapters are added.

Validated locally: SQLite migrations, security regression tests, Django production-settings preflight with synthetic secrets, Windows lifecycle script start/stop/restart, and responsive sign-in rendering. Real PostgreSQL TLS, Linux service supervision, database RLS, failover and performance have not been tested here.
