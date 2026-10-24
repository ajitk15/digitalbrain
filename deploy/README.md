# Deploying Digital Brain to Coolify

For `srv1874861.hstgr.cloud` (200.141.11.23), serving `ajitconnect.in`.

## Hosted demo on this VPS

This deployment replaces the local demo. Both Compose configurations select
the Dockerfile's `demo` target and set `DIGITAL_BRAIN_DEMONSTRATION=1` on every
application service. The entrypoint renders `mode = "production"` with
`demonstration = true`: demo reset, local Code Factory delivery/tests and the
ServiceOps test workbench are enabled while TLS, secure cookies, scanning and
the restricted database role remain enabled. A mounted configuration wins;
update an existing mounted file using `production.example.toml` if applicable.

The image contains the demo documents, CarePath source, a locked Linux
CarePath test environment, Playwright and Chromium. The migration/all-in-one
container seeds `demo-workspace` once at `/var/lib/digitalbrain/demo/carepath`.
Redeploys update the image's test environment and preserve edits to that source.
Back up this volume with the database; do not delete it to upgrade the image.
The ServiceOps workbench runs its fixed suites in a disposable SQLite database
with its own configuration and signing key, without live provider credentials.

After deploying, create or refresh the demo from the app container:

```bash
docker compose exec app python scripts/rebuild_demo.py
```

It creates ACME, CarePathDev, CarePathOps, `acmeadmin` and, if absent,
`siteadmin@db.com`. New accounts use a password prompted twice with hidden
input and checked against the platform password rules. Existing passwords and
privileges are preserved. The script never writes production configuration or
`.env`. The reset button is available to a **platform administrator** on the
organization screen and still requires the organization's exact name.

On the first run, missing credentials are reported. Sign in as the application
owner `acmeadmin` and use each application's Credentials screen to set
Claude (API key or setup token), OpenAI for embeddings,
Jira and ServiceNow. Review AI settings, then rerun the command to finish imports
and graph generation; this step makes the configured model calls. Credentials
already entered in the browser survive redeploys in `managed-credentials`.
The optional read-only `connector-credentials` volume accepts shared connector
files (`jira`, `servicenow`, `git_read`, `git_write`), owned by uid 10001 with
mode 0600. Per-application credentials take precedence. No shared file is needed
when the application's Credentials screen has been used. The developer's Claude
host login is never used by the VPS.

S3 originals still require `originals_access_key_id` and
`originals_secret_access_key` files in `platform-secrets`; verify those before
uploading. No local secret is copied into the image. Check the deployed settings:

```bash
docker compose exec app python manage.py check --deploy --fail-level WARNING
docker compose exec app python manage.py queue_status --check
```

Sign in as the platform administrator to verify reset visibility. In CarePathDev,
verify the server folder is indexed and an approved Code Factory change can be
written and tested. In CarePathOps, run the ServiceOps test workbench and require
actual browser results with no skipped coverage. Perform any reset on disposable
demo work only; the script can reload the sources afterward.

`docs/index.html#infrastructure` is the general procedure and the authority on why each rule
exists. This is the specific one, and it differs in ways worth knowing.

**Nothing has to be prepared on the server.** No directories to create, no
disks to mount, no repository to clone, no Persistent Storage entries in
Coolify. The compose file uses named volumes that Docker creates on first
start, and the signing key and database password are generated into one of them
by an init service. An earlier version of this file asked you to hand-create
host directories and bind-mount them; that is a Kubernetes-shaped assumption
and it is gone.

**Coolify's proxy already owns 80 and 443.** So there is no web server in
`docker-compose.yaml` and nothing publishes a port. Coolify terminates TLS,
obtains the certificate, and routes the domain to the app container over the
compose network.

**Exactly one app container. Never scale it.** This is correctness, not
capacity. The stream registry in `platform_core/agent_runtime/streaming.py` is
process-local, so with a second process a user's Stop silently fails to cancel
a provider call the application is still being billed for. The per-token API
rate limit in `api_auth.py` is process-local for the same reason and would
multiply by the replica count.

**The Claude Code CLI is not installed in the image, and must not be.** It
arrives inside the `claude-agent-sdk` wheel and is addressed by that path.
`agent_runtime/credentials.py` points `HOME` and `CLAUDE_CONFIG_DIR` at an empty
temporary directory so no run can inherit an operator's login — which is what
makes per-application billing and `AIUsage` attribution mean anything. A host
install would not be used.

## 1. Point the domain at the VPS

An A record to `200.141.11.23`, for every name you will serve. Let's Encrypt
validates over HTTP against whatever the name resolves to.

## 2. Create the Coolify resource

**New Resource → Docker Compose**, pointing at this repository, branch `main`,
compose file `docker-compose.yaml`.

Before the first deploy:

- Set the **domain** on the `app` service, with the container port **8000** —
  that is the port inside the container, not one opened on the host. Let Coolify
  write the proxy labels; hand-written ones drift when Coolify updates.
- Set `SITE_ADMIN_USER_ID` to the username you want for the first
  administrator. Remove it once you have signed in.
- If you serve names other than `ajitconnect.in` and `www.ajitconnect.in`, set
  `DIGITAL_BRAIN_HOSTS` to a comma-separated list of them. A request whose Host
  header is not listed is refused with `400` — the check working, but it looks
  like a broken deployment.

Deploy. The **first boot takes several minutes** because it downloads ClamAV's
signature database (~1 GB) before it starts listening; the healthcheck's start
period allows for it, and Coolify will not route until it passes. Later deploys
reuse the volume.

## 3. Check it came up honestly

```bash
docker compose logs app | tail -40
```

In order: the rendered configuration, ClamAV signatures on a first boot,
`applying migrations`, `checking deployment settings`, then the listener.
`manage.py check --deploy --fail-level WARNING` runs on every start, so a
misconfigured cookie or proxy setting stops the deploy rather than being
discovered by a user.

Then create the first administrator, which prompts for a password and reads it
hidden — it never reaches a file, an argument, or a shell history:

```bash
docker compose exec app python manage.py bootstrap_admin
```

## 4. Configuration and secrets

The **non-secret** configuration is rendered by the entrypoint from environment
variables. To take it over completely, mount your own file at
`/etc/digitalbrain/production.toml` — if one is there it wins, and nothing is
rendered. `production.example.toml` in this directory is a starting point.

**Secrets are always files, never environment variables.** The signing key and
database password are generated once into the `platform-secrets` volume. The
password is written twice, deliberately: `read_secret` requires mode 0600 owned
by the application (uid 10001), and Postgres runs as uid 70 and will not read a
file it does not own. No single file satisfies both without opening permissions
`read_secret` would then reject.

**Provider credentials go through the Credentials screen**, not through a
shell. Settings → Credentials, as the application's owner: Claude, GitHub
(write), Jira. Each is written to its own file at mode 0600 in the
`managed-credentials` volume; the database keeps only who set it, when, and a
digest — never the value, and it is never shown again.

`claude_<uuid>` takes either an API key or a `claude setup-token` OAuth token —
`agent_runtime/credentials.py` decides which variable the CLI is given and
blanks the other. GitHub (write) is deliberately a separate credential from the
read-only one a connector uses: importing issues must not imply the ability to
push.

An operator can still project credentials into `/run/secrets` from a secret
manager, and one mounted there **always wins** over one typed into the browser
— which is what makes migrating a credential into a secret manager need no
change in the product. That is also why the two directories must differ.

**Originals go to S3.** Both compose files set
`DIGITAL_BRAIN_ORIGINALS_BUCKET=digital-brain-originals-prod` and
`DIGITAL_BRAIN_ORIGINALS_REGION=us-west-2`, which the entrypoint renders as
`[originals] store = "s3"`. The bucket's keys are two more files in the
`platform-secrets` volume - `originals_access_key_id` and
`originals_secret_access_key`, mode 0600, owned by uid 10001 - and the
entrypoint warns at start while either is missing (every upload then fails
until they are there, and nothing falls back to disk). Clear the variable to
keep originals on the `app-runtime` volume. `manage.py move_originals --check`
says whether any disk originals are left to move. OCR is rendered on too
(`DIGITAL_BRAIN_OCR=0` turns it off); the image installs Tesseract.

**Microsoft sign-in and provisioning are off until named.** Set
`DIGITAL_BRAIN_ENTRA_TENANT` and `DIGITAL_BRAIN_ENTRA_CLIENT_ID` (both GUIDs) and
mount `entra_client_secret` for the Sign in with Microsoft button;
`DIGITAL_BRAIN_SCIM=1` and a mounted `scim_token` (32+ random characters) let
Entra create and deactivate accounts at `https://<host>/api/scim/v2`. Both secret
files go in `platform-secrets`, mode 0600, owned by uid 10001, and the entrypoint
warns at start while either is missing. Neither grants access to anything.
Setup, Entra side included: `docs/single-sign-on.md`.

## 5. Watching it

Without any of this, `queue_status` says what every lane has waiting:

```bash
docker compose exec app python manage.py queue_status --check
```

and `slo` measures the four service levels kept in the platform's own records -
answers that end complete, answers on time, background work started on time,
background work that finishes:

```bash
docker compose exec app python manage.py slo --check
```

For traces and metrics, add an `[otel]` table to the mounted configuration
naming an OTLP/HTTP collector (`endpoint = "https://collector:4318"`, https in
production; its credential, if any, is the mounted file `otel_authorization`).
Traces carry ids and timings only; metrics carry rates, errors, durations and
queue figures, and only spend is by application. `metrics = false` keeps the
traces alone. `deploy/slo-rules.yml` is the Prometheus rule file for the other
four service levels (server errors, page speed, paid calls admitted, queue age),
with burn-rate alerts - load it where the collector's metrics land. It is
generated from `platform_core/slo.py`; regenerate it, never edit it.

To run a collector and Prometheus beside the platform:

```bash
docker compose -f docker-compose.yaml -f docker-compose.observability.yaml up -d
```

This issues the collector a certificate from the same CA as Postgres and Redis,
sets `DIGITAL_BRAIN_OTEL=otel-collector:4318` on the app (the entrypoint renders
the `[otel]` table from it, https only), and starts Prometheus with the rules,
on this host's loopback port 9090. With `docker-compose.scale.yaml`, add
`docker-compose.observability.scale.yaml` after it so the worker and sandbox
report too. Traces go to the collector's log until its traces pipeline
(`deploy/observability/collector.yaml`) names a backend, and alerts stay in
Prometheus until an Alertmanager is added. `deploy/observability/compose.local.yaml`
is the same pair for a development machine, over plain http.

## What is not covered here

- **Backups.** `postgres-data` and `app-runtime` both matter: the second holds
  every uploaded document's original bytes unless `[originals] store = "s3"`
  puts them in a bucket, which then needs its own versioning or backup.
  `platform-secrets` holds the signing key - lose it and every session is
  invalidated. Nothing here backs any of them up.
- **More than one web container** is `docker-compose.scale.yaml`, which runs a
  migration job, web replicas (`DIGITAL_BRAIN_WEB_REPLICAS`), a worker, a
  sandbox for the lanes that hand outside content to other programs, Redis and
  clamd. On its own, `docker-compose.yaml` still runs everything in one
  container, and that is a single process.
- **PostgreSQL availability.** One database container, no replica and no
  point-in-time recovery. A managed PostgreSQL with both is the production
  answer; nothing here sets one up or rehearses a restore.
