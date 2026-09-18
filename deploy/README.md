# Deploying Digital Brain to Coolify

For `srv1874861.hstgr.cloud` (200.141.11.23), serving `ajitconnect.in`.

`docs/deployment.md` is the general procedure and the authority on why each rule
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

## What is not covered here

- **Backups.** `postgres-data` and `app-runtime` both matter: the second holds
  every uploaded document's original bytes. `platform-secrets` holds the signing
  key — lose it and every session is invalidated. Nothing here backs any of
  them up.
- **The single-process limit.** Scaling out needs a shared cancellation channel
  first. Scale with threads inside the one process until then.
- **ClamAV timing.** `processing.py` gives the scanner 30 seconds, and
  `clamscan` loads the whole signature database on every invocation. If large
  documents start failing with "Document scanning failed", that is the cause,
  and the fix is a `clamd` daemon with `clamdscan` — which needs a code change,
  because `--fail-if-cvd-older-than` is a `clamscan` option.
