# Deploying Digital Brain to Coolify

For `srv1874861.hstgr.cloud` (200.141.11.23), serving `ajitconnect.in`.

`docs/deployment.md` is the general procedure and the authority on why each rule
exists. This is the specific one, and it differs in three ways worth knowing
before you start.

**Coolify's proxy already owns 80 and 443.** So there is no web server in
`docker-compose.yaml` and nothing publishes a port. Coolify terminates TLS,
gets the certificate, and routes the domain to the app container over the
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

`ajitconnect.in` must be an A record to `200.141.11.23` before Coolify can get a
certificate. Let's Encrypt validates over HTTP against whatever the name
currently resolves to.

```
A    @      200.141.11.23
A    www    200.141.11.23
```

Check it took, and that you are seeing the VPS rather than Hostinger's parking
page, before going further:

```bash
dig +short ajitconnect.in
```

## 2. Prepare the host

Over SSH as root:

```bash
git clone https://github.com/ajitk15/digitalbrain.git /opt/digitalbrain/src
sh /opt/digitalbrain/src/deploy/prepare-host.sh
cp /opt/digitalbrain/src/deploy/production.example.toml /opt/digitalbrain/config/production.toml
```

`prepare-host.sh` generates `django_secret_key` and `database_password` into
`/opt/digitalbrain/secrets`, owned by uid 10001 and mode 0600. Both are
required: `read_secret` refuses any file carrying group or other permission
bits, so a wrong mode stops the process rather than being ignored.

Edit `production.toml` only if you are using a different hostname. It contains
no credentials and never should.

## 3. Create the Coolify resource

In Coolify: **New Resource → Docker Compose**, pointing at this repository,
branch `main`, compose file `docker-compose.yaml`.

Then, before the first deploy:

- Set the **domain** on the `app` service to `https://ajitconnect.in`. Let
  Coolify write the proxy labels; hand-written ones drift when Coolify updates.
- Set `SITE_ADMIN_USER_ID` as an environment variable to the username you want
  for the first administrator. Remove it once you have signed in.
- Leave the other environment variables at their defaults.

Deploy. The **first boot takes several minutes** because it downloads ClamAV's
signature database (~1 GB) before it starts listening; the healthcheck's start
period allows for it. Later deploys reuse the volume.

## 4. Check it came up honestly

```bash
docker compose logs app | tail -40
```

You are looking for `applying migrations`, then `checking deployment settings`,
then the listener. `manage.py check --deploy --fail-level WARNING` runs on every
start, so a misconfigured cookie or proxy setting stops the deploy rather than
being discovered by a user.

Then sign in at `https://ajitconnect.in`, and verify:

- the certificate is real and the page is not redirect-looping (if it loops,
  `trust_proxy` or `--trusted-proxy` is wrong — see `scripts/serve.py`)
- an application can be created, and a second account gets `404` on it rather
  than `403`

## 5. Mount the provider credentials

One file per provider per application, named with the application's UUID, in
`/opt/digitalbrain/secrets`:

```bash
install -o 10001 -g 10001 -m 600 /dev/null /opt/digitalbrain/secrets/claude_<APP_UUID>
# then write the key into it with an editor; do not echo it into shell history
```

`claude_<uuid>` takes either an API key or a `claude setup-token` OAuth token —
`agent_runtime/credentials.py` decides which variable the CLI is given and
blanks the other. `github_write_<uuid>` is deliberately a different file from
`github_<uuid>`: importing issues must not imply the ability to push.

The directory is mounted, not individual files, so a credential added for a new
application is picked up without a restart. Nothing caches a secret in memory.

## What is not covered here

- **Backups.** `postgres-data` and `app-runtime` both matter: the second holds
  every uploaded document's original bytes. Neither is backed up by anything in
  this file.
- **The single-process limit.** Scaling out needs a shared cancellation channel
  first. Scale with threads inside the one process until then.
- **ClamAV timing.** `processing.py` gives the scanner 30 seconds, and
  `clamscan` loads the whole signature database on every invocation. If large
  documents start failing with "Document scanning failed", that is the cause,
  and the fix is a `clamd` daemon with `clamdscan` — which needs a code change,
  because `--fail-if-cvd-older-than` is a `clamscan` option.
