# Digital Brain

An application-isolated knowledge platform for engineering and IT operations teams:
documents, tickets, incidents and code become a versioned knowledge graph that answers
questions, plans code fixes and triages incidents, with every citation re-verified
against its live source. Django 5.2, Python 3.12, server-rendered.

**Documentation: [`docs/index.html`](docs/index.html)** (one file, open it in a browser: product overview, architecture,
components, engineering, infrastructure, marketing). Rules for changing code:
[`CLAUDE.md`](CLAUDE.md).

## First run on a new machine

**Double-click `start-all.cmd`.** That is the whole thing — no arguments, no
prerequisites, no PowerShell settings to change. A folder with no `.venv` is a first
run by definition, so it sets the project up and then starts it.

Use `start-all.cmd`, not `start-all.ps1`. Windows ships with the `LocalMachine`
execution policy set to `Restricted`, which blocks `.ps1` files outright, so
`.\start-all.ps1` fails on a clean laptop before it runs a line. The `.cmd` wrapper
passes `-ExecutionPolicy Bypass` for that one process — nothing machine-wide changes
— and holds the window open if something goes wrong, which is exactly the first-run
case where the message matters.

From a terminal, the same thing:

```
start-all.cmd
```

Setup installs `uv` if the machine does not have it (via `winget`, falling back to
the official user-scoped installer), builds `.venv` and installs the locked
dependencies, creates the local configuration and database, asks for the first
administrator — press Enter to take the default `siteadmin@db.com` — then asks for
that account's password, and finally which AI provider to use. The password is read
hidden and never reaches a file, an argument or the shell history. If `uv` cannot be installed at
all — no network, a locked-down machine — it falls back to `python -m venv` plus a
`pip install` of the dependencies exported from `pyproject.toml`, which needs Python
3.12, 3.13 or 3.14 and resolves versions fresh rather than from `uv.lock`. Nothing is
installed machine-wide and no elevation is requested.

`start-all.cmd -Install` re-runs setup deliberately on a project that already has a
`.venv` — worth knowing, but not needed for a first run.

There is no `requirements.txt`: dependencies live in `pyproject.toml`, pinned by
`uv.lock`.

## Start and stop

```
start-all.cmd
stop-all.cmd
```

An ordinary start keeps an existing environment up to date but never downloads a
toolchain. The one exception is a folder with no `.venv`, which is a first run
rather than an ordinary start, and sets itself up.

The `.ps1` files do the actual work and take the same arguments, but Windows blocks
them by default. To call them directly, allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. An alternative port:
`start-all.cmd -Port 8123`.

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

```
start-all.cmd -Install
```

Setup asks for the sign-in ID, writes it to `.env` as the only key that file
accepts, and runs `bootstrap_admin`, which prompts for the password itself and
reads it hidden. The password never reaches a file, a command-line argument or
the shell history. Starting without `-Install` when no administrator exists says
so and points here. Bootstrap stays disabled once an administrator exists: it can
neither elevate nor reset an account.

## Demo in one step

```powershell
start-all.cmd -Demo
```

Builds the CarePath demonstration before the server starts: ACME, CarePathDev
(Engineering) and CarePathOps (Operations), their document source, repository,
Jira and ServiceNow connectors, and their credentials - asked for on the console,
hidden, and skippable with Enter. It then imports, converts, builds the graph and
publishes it, and prints each application's onboarding checklist. Run it again
after adding a skipped credential; it keeps what exists. On a new machine,
`start-all.cmd -Install -Demo` does setup and the demo together.

## AI provider

```powershell
start-all.cmd -ConfigureAI
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

## Verification

All five must be clean before a change is done (see
[How we build](docs/index.html#engineering--the-five-gates-all-must-be-clean)):

```powershell
.venv/Scripts/ruff.exe check digitalbrain platform_core scripts manage.py
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe manage.py test
.venv/Scripts/python.exe scripts/production_preflight.py
.venv/Scripts/python.exe manage.py scorecard --check
```
