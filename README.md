# Digital Brain

Working Python/Django SaaS foundation using the selected Digital Brain logo, with a compact responsive interface. This repository previously contained architecture documents only. The implementation covers administration, private local document intake, automatic background MarkItDown conversion, searchable knowledge, streaming cited answers from either OpenAI or Claude with cost receipts, GitHub/Jira/ServiceNow issue import, a GitHub repository index, and a gated pipeline from ticket to draft pull request. See [feature workflows and setup](docs/feature-workflows.md) for configuration and remaining production integrations.

## First run on a new machine

One command, from this folder, in PowerShell or from `cmd.exe`:

```powershell
.\start-all.ps1 -Install
```

`-Install` is the full setup path. It installs `uv` if the machine does not have it
(via `winget`, falling back to the official user-scoped installer), builds `.venv` and
installs the locked dependencies, creates the local configuration and database, asks
for the first administrator, and asks which AI provider to use. If `uv` cannot be
installed at all — no network, a locked-down machine — it falls back to
`python -m venv` plus `pip install -e .`, which needs Python 3.12, 3.13 or 3.14 and
resolves versions fresh rather than from `uv.lock`. Nothing is installed machine-wide
and no elevation is requested.

There is no `requirements.txt`: dependencies live in `pyproject.toml`, pinned by
`uv.lock`.

## Start and stop

```powershell
.\start-all.ps1
.\stop-all.ps1
```

An ordinary start keeps an existing environment up to date but never downloads a
toolchain; if the project is not set up yet it says so and points at `-Install`.

Double-clickable equivalents: `start-all.cmd` and `stop-all.cmd`. Prefer these if
PowerShell reports *"running scripts is disabled on this system"* — they pass
`-ExecutionPolicy Bypass` for that one process, so no machine-wide setting has to
change, and they hold the window open if something fails. To run the `.ps1` files
directly instead, allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. An alternative port can be
selected with `./start-all.ps1 -Port 8123`.

### Moving or copying the project folder

`config/local.toml` and `.runtime/` are gitignored, so a fresh clone builds them
correctly. A folder that is *copied* brings them along, and `config/local.toml`
records an absolute path to the secret directory. Start-all now repairs that path on
every run and discards runtime state left behind by the old location, so a moved
project starts instead of failing on a missing signing key — or, worse, quietly
reading the previous copy's secrets.

Start-all checks configuration, applies local migrations, collects assets, launches a hidden Waitress process on loopback, and waits for readiness. It refuses occupied ports and does not create duplicate instances. Stop-all verifies the recorded project, executable, start timestamp and instance marker before stopping the launcher and its verified Python worker. It leaves databases, files and unrelated services alone. These are local development scripts; Windows stop is immediate, not a production request-draining mechanism.

## Initial administrator

No default administrator, default password, or test account is installed.

```powershell
.\start-all.ps1 -Install
```

`-Install` asks for the sign-in ID, writes it to `.env` as the only key that file
accepts, and runs `bootstrap_admin`, which prompts for the password itself and
reads it hidden. The password never reaches a file, a command-line argument or
the shell history. Starting without `-Install` when no administrator exists says
so and points here. Bootstrap stays disabled once an administrator exists: it can
neither elevate nor reset an account.

## AI provider

```powershell
.\start-all.ps1 -ConfigureAI
```

Asks whether to use Claude or OpenAI and mounts the credential. `-Install` runs this
too, so normally it is not needed separately.

If you have used the Claude Agent SDK elsewhere and never needed an API key, that is
because the bundled CLI falls back to your own `claude` login. This platform blocks
that fallback on purpose — each run gets a blanked environment so per-application
billing and usage attribution are real. In development you can opt back in: choose
Claude, then *"use this machine's Claude Code login"*, and it behaves like your other
SDK apps. That route is refused outright when `mode = "production"`.

Otherwise the credential is written to the secret directory and **copied into each
application's own file when the application is created**, so nobody has to hand-create
a file named after a UUID. An API key (`sk-ant-api…`) bills your Console account; a
`claude setup-token` token (`sk-ant-oat…`) bills a Claude subscription.

To do the same by hand:

1. Copy `.env.example` to `.env` and set **only** `SITE_ADMIN_USER_ID` to your chosen sign-in ID.
2. Run:

```powershell
.venv/Scripts/python.exe manage.py bootstrap_admin
```

3. Enter a strong password at the hidden terminal prompt. It never goes in a file or command-line argument.
4. Sign in at [localhost](http://127.0.0.1:8000). Use **Users** to provision regular accounts, then **Administration** to create an organization with its assigned administrator.
5. The organization administrator adds members, portfolios, products and applications. Each application receives an explicit owner. Owners manage application roles.

Users created in the interface must change their initial password at their first sign-in. Share their initial credentials through an approved private channel. Account recovery currently uses the trusted operator command `manage.py changepassword USER_ID`; self-service email recovery and enterprise SSO/MFA remain integration work.

The site admin ID is a bootstrap identity, not a login bypass. Changing it later does not elevate an existing account, reset a password, or remove the current administrator. Bootstrap refuses to run when a platform admin already exists. There is no exposed Django superuser/admin site.

## Configuration and secrets

- `.env` accepts **only** `SITE_ADMIN_USER_ID`. Unknown keys and duplicate entries fail startup.
- Non-secret deployment settings live in TOML. Local settings: `config/local.toml`, generated by `scripts/init_local.py` and ignored by Git.
- `DIGITAL_BRAIN_CONFIG` is an optional process-level pointer to the TOML file; it is not read from `.env`.
- The Django signing key and production database password come from files in the configured secret directory. There is no inline secret or plaintext environment-variable fallback.
- Local signing keys are random, generated once, and protected with Windows ACLs or POSIX permissions. Run local setup and the service as the same OS account. An agent sandbox may need to grant the intended local service account access to the local secret directory.
- Production should project secrets from a secret manager using workload identity and a read-only volume. Files must be readable only by the service identity (0600 on POSIX). OpenAI and GitHub adapters use separate application-scoped mounted secrets.
- Passwords use Argon2 hashing. Secrets, databases and logs are excluded from Git.

Mounted secret loading follows [Django's deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/).

## Available now

| Area | Implementation |
| --- | --- |
| Identity | Password sign-in, database sessions, lockout, password change, first-login change, account enable/disable |
| Roles | Platform admin, organization admin/member, application owner/contributor/viewer; approval is a separate grant |
| Hierarchy | Organization → Portfolio → Product → Application; organization/application enable/disable |
| Isolation | Deny-by-default application policy; current organization membership and explicit grant required on every request |
| Branding | Selected logo by default; platform-admin image upload; bounded decoding, canonical PNG, alpha preservation, atomic replacement, cache revalidation |
| Documents | Multi-file uploads (20 files / 20 MB total), private originals, automatic MarkItDown conversion, Markdown/graph-input export, scoped deletion; optional scanning |
| Knowledge graph | Automatic structural graph generation, interactive exploration, evidence links, measured structural quality and numbered version history |
| Knowledge & chat | Searchable immutable sources, verified citation excerpts, per-user conversations, streaming tool-using answers from either Agent SDK |
| Code Factory | Six-phase pipeline from ticket to draft pull request: graph-cited plans with pinned sources, independent approval by a different user, confirmed repository, a verification pass, then a draft PR on its own branch. Replaces files it first read; never creates, deletes or merges |
| Code Graph | GitHub repositories indexed at one commit on their own worker lane: files, symbols and import relationships, with the remote host hardcoded and no URL accepted. Feeds Code Factory analysis and design |
| Machine surfaces | Bearer-token REST graph search and an MCP server under /api/v1/, returning the same verified evidence as chat and calling no model |
| Connectors | Read-only GitHub, Jira and ServiceNow imports, bounded to 100 records, deduplicated by digest with revision history |
| Navigation | Organization tree at left; functional menus at top; current application highlighted |
| Governance | Scoped, paginated audit events with actor, time, resource, request ID and structured change details |
| Features | Seven global/application feature switches; global disable overrides application enable; connector/provider setup remains explicit |
| AI accounting | Exact decimal amounts, currency, provider/model, token counts, estimated/reported status, duplicate/conflicting receipt detection |
| Operations | Liveness/readiness, safe structured logs, request IDs, limits, static compression, production security defaults, local lifecycle scripts |

The optional OpenAI adapter records returned token usage and estimated costs at owner-configured prices. GitHub imports issue content as versioned knowledge. Both require application-specific mounted credentials. These adapters are covered by mocked tests; no live service calls were made during implementation. Automatic price catalogs, budgets, invoice reconciliation and spend alerts remain pending. Currency and estimate/report totals remain separate.

Platform admins see service metadata and aggregate spend, not application content. Organizational seniority does not imply application access. The normalized hierarchy derives an application's organization from its product and portfolio rather than accepting a browser-supplied organization ID.

## Verification

```powershell
.venv/Scripts/ruff.exe check digitalbrain platform_core scripts manage.py
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe manage.py test
.venv/Scripts/python.exe scripts/production_preflight.py
```

The production preflight uses disposable synthetic mounted secrets and does not connect to real infrastructure. Local tests use SQLite; PostgreSQL concurrency, TLS and row-level security are separate release requirements. See [non-functional requirements](docs/non-functional.md) and [deployment](docs/deployment.md).

## Structure

- `digitalbrain/`: configuration, URL routing and WSGI.
- `platform_core/`: models, access policy, transactional services, screens and tests.
- `templates/`, `static/`: compact server-rendered UI and default branding.
- `scripts/`: local setup, lifecycle helpers, WSGI server and security preflight.
- `design/`: original architecture narrative and diagrams.
- `docs/`: operational instructions and implementation boundaries.

React is intentionally deferred: streaming chat is served as server-sent events with a small progressive-enhancement script, so the interface still works with JavaScript disabled and needs no build step or client-side permission logic.

## Documents and simplified navigation

Open **Organizations → your organization → application → Documents**. Application owners and contributors can upload; viewers can list document metadata. Administration does not automatically grant document access. Portfolio/product management is collapsed below the application cards. The same Documents / AI costs / People & access / Settings tabs appear across application screens.

Local originals are preserved byte-for-byte under `.runtime/documents/<organization>/<application>/<document-id>.quarantine`, outside public static storage. The database stores metadata only. Uploads are quarantined and cannot be downloaded, previewed or sent to AI until scanning is integrated. No malware-scan result is implied by a successful upload. Production intake is disabled until application-specific private storage and scanning are configured. The application upload feature switch is enforced on the server.
