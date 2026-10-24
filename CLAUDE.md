# Digital Brain — working notes

Django 5.2 / Python 3.12, server-rendered, no JavaScript build step. WSGI under
waitress, or under uvicorn with the answer streams on its event loop wherever Redis is
configured. SQLite locally, PostgreSQL in production.

## Verify before calling anything done

All five must be clean:

```bash
.venv/Scripts/ruff.exe check digitalbrain platform_core scripts manage.py
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe manage.py test
.venv/Scripts/python.exe scripts/production_preflight.py
.venv/Scripts/python.exe manage.py scorecard --check
```

**The scorecard is the fifth gate** (`platform_core/scorecard.py`, about 90 seconds).
It builds its own workspace from files in this repository - the CarePath documents
through `scripts/extract_text.py`, the Jira export through the real Jira connector
code, `benchmarks/*.json` - in a throwaway database with stable ids, calls no model,
and measures retrieval, citation integrity, Code Factory's evidence against the
planted-bug answer key, the ontology, personal data and leaks, HIPAA §164.312 probes,
AI cost control and speed. `--check` fails on any score worse than
`benchmarks/baseline.json` past its tolerance; leak counts and safeguard probes must
match exactly. Raise the baseline (`--set-baseline`) only in the commit that earned
it, and `--record` each release into `benchmarks/history/`. `--replay <commit>`
re-runs the ranking of an older commit on today's corpus, for the day-one comparison.

**LLM drift is measured, not assumed** (`platform_core/drift.py`,
`manage.py llm_drift <app> --as <user> --record`). It sends the frozen
`benchmarks/drift-pack.json` to the application's configured models - about ten paid
calls, receipted and budgeted as usual - scores extraction with `graph_ai.judge` and
answers by their citation markers, and compares with that model's first and latest
recorded runs. Run it after changing a model, a prompt or a provider, and on a
schedule. Never rebuild the pack casually: a new pack starts a new reference.

`manage.py test` includes **browser tests** (`platform_core/tests/test_browser.py`):
Playwright (dev dependency) drives the installed Chrome against a live test server, with
the model patched out, so the JavaScript runs for real. They exist because a script error
made every in-place progress update throw for a day while every HTML test passed. They skip
and say why where Playwright or Chrome is missing, or with `DIGITAL_BRAIN_BROWSER_TESTS=0`.
Changing `static/*.js`, a CSP-affecting template or a `data-live`/`data-modal` flow means
adding or updating one there. Wait on selectors, never `wait_for_function`: that evaluates a
string, and the CSP refuses it - as it should.

**CI runs the suite and the scorecard twice: on SQLite and on PostgreSQL 16**, the
second because the advisory lane locks, `SKIP LOCKED` claims and full-text indexes
exist only there. A `[database]` table (`name`, `user`, `host`, `port`, optional
`sslrootcert`) in a development config points the process at PostgreSQL, with the
password in the secret directory's `database_password` file like everything else. To
run it locally, start a throwaway `postgres:16-alpine`, add that table to a copy of the
config and set `DIGITAL_BRAIN_CONFIG` to it. Two things it has caught that SQLite
cannot: `select_for_update` beside a `select_related` on a nullable key must say
`of=("self",)`, because PostgreSQL refuses to lock the nullable side of an outer join;
and a test that lets `close_old_connections` or `request_finished` run inside a
`TestCase` loses its connection there, because the test's transaction makes autocommit
differ from settings - SQLite's in-memory database ignores the close, PostgreSQL does not.

After changing anything under `static/`, run `manage.py collectstatic` **and restart the
server**. Templates resolve hashed filenames through the staticfiles manifest, which the
running process caches at boot — otherwise you will be testing the previous asset and
wondering why your change does nothing.

## Development runs production's services

The Hostinger VPS is now the hosted demonstration, replacing the local demo.
`docker-compose.yaml` and the scale overlay build the `demo` Docker target and
set `DIGITAL_BRAIN_DEMONSTRATION=1`. This renders production mode with
`demonstration = true`, allowing reset and Code Factory local write/tests and
enabling the ServiceOps test workbench. All transport, cookie, scanner and
database security remains production-grade; host Claude login is still refused.
`scripts/init_vps_demo.py` seeds the persistent CarePath workspace once, keeping
approved edits over redeploys. Its `.venv` points to the image's locked Linux
test environment. `scripts/rebuild_demo.py` accepts a declared hosted demo,
validates rather than rewrites its config, and prompts for new-account passwords.
See `deploy/README.md` for setup and credentials. Statements below restricting
reset/local delivery to development have this explicit demonstration exception.

`start-all.cmd` brings up `docker-compose.dev.yaml` (project `digitalbrain-dev`) before
anything else: PostgreSQL 16 on 127.0.0.1:5442, Redis 7.4 over TLS on 6390 and a ClamAV
daemon on 3310 - production's images, TLS from a CA made in a volume (only `ca.crt` leaves,
into `.runtime/dev-ca`), passwords as files in `.runtime/secrets`. So Redis means uvicorn,
uploads are scanned, row security and the runtime role hold, exactly as deployed.
`scripts/init_local.py --services` points `config/local.toml` at them once and writes
`config/local.runtime.toml` on every start: **`local.toml` is the owner's** - migrations,
`manage.py`, the gates - and the server is started with the runtime file, which connects
as `digital_brain_runtime`, the way the entrypoint renders two configurations. Migrations
run through `apply_migrations`, which makes and corrects that role. Addresses, never
`localhost`: Windows tries ::1 first, which Docker does not publish, and every connection
waited ten seconds for it. `scripts/move_to_postgres.py` copies `.runtime/local.sqlite3`
in once (never into a database that already has a user; the file stays as the backup;
`.runtime/moved-to-postgres` records it). A `DIGITAL_BRAIN_CONFIG` set beforehand skips all
of it. What still differs is what production refuses by design - `claude_use_host_login`,
`allow_demo_reset`, Code Factory's local write and tests, the shared connector folder -
and the bucket's name. `manage.py test` follows `local.toml`, so the suite runs on that
PostgreSQL too; `digitalbrain/test_runner.py` blanks Redis, the scanner, OTel, the bucket
and the web server for tests, as it blanks the shared connector folder.

On a first start the ClamAV image can keep serving its bundled signatures after freshclam
replaced them; the platform then refuses every scan as stale, correctly. A restart of the
`clamd` service loads the new ones.

## Invariants — do not weaken these without a deliberate decision

**Deny by default.** Application access is re-checked on every request and on every tool
call. A user with no grant gets `Http404` from `policy.application_for`, not `403` — the
platform does not confirm that an application exists. Platform administration does not
imply application access.

**Scope is closed over, never model-supplied.** Agent tools in
`platform_core/agent_runtime/tools.py` are bound to one application and one user before a
run starts. Model arguments may only narrow an already-scoped queryset. A foreign id must
read as "not found", never raise and never leak.

**The platform connects as a role that can only touch rows** (`database_roles.py`). The
role Postgres's image creates is a superuser - it can read the server's files, `COPY ...
TO PROGRAM`, drop anything, and is never held to row security - and the platform used to
run as it. Now the migrate step connects as that owner (`[database] user`, plus
`runtime_user` naming the role to make) and `apply_migrations` creates or corrects the
runtime role under its lock: LOGIN and nothing else - no superuser, CREATEDB, CREATEROLE,
REPLICATION, BYPASSRLS or membership of the owner - with SELECT, INSERT, UPDATE and
DELETE on every table and default privileges for tables a later migration adds. Every run
states it all again, so a privilege added by hand is undone. Everything else - web,
worker, `manage.py` through `docker exec` - reads the configuration that connects as the
runtime role (`user = "digital_brain_runtime"`, `password_secret =
"database_runtime_password"`; `password_secret` is one of two names, never a path). The
password is a mounted file like every secret, sent to the server as a SCRAM verifier
computed here, so a server logging DDL logs no password; replacing the file and deploying
rotates it. The entrypoint renders both configurations; `DIGITAL_BRAIN_DATABASE_RUNTIME_ROLE=0`
connects as the owner again, and an operator's mounted file is used for both. Runtime
code must never need DDL - there is none today, and the role cannot run it.

**Row security is the second layer, under the code's own scoping** (`tenancy.py`,
migration 0091). Every table with a required application has a PostgreSQL policy,
FORCEd, reading the session setting `digitalbrain.tenant`: bound, a connection reads and
writes only that application's rows; unbound, it sees everything. `TenantMiddleware`
binds a request under `applications/<uuid:pk>/` to that `pk` before its view runs and
clears it whatever the view did; `api_auth.authenticate` binds once a token names its
application; the chat answer thread runs inside `tenancy.scoped`. So a query that forgot
its `application=` filter reads another application's row as not found and a write for
another application is refused - it does not replace the code's checks, which still
decide access, and it defends against a missing filter, never against SQL injection.
Background work is bound too: a job to its `application_id` (`jobs.run_next`), a lane
step to the row it claimed - a document, a run, a triage, a review, an index - for the
length of its work. Choosing the next row reads every application's queue, so that and
management commands, migrations and platform administration run unbound. `tenancy.UNSCOPED` names the four tables a request reads across applications
(grants, features, logos, gateway admissions) and why; `test_tenancy` fails when a new
model with a required application is in neither the policy list nor that set, so a new
table chooses in its own migration. A superuser bypasses row security entirely, which is
one more reason the platform no longer connects as one. SQLite has none: there the code
is the only layer.

**Citations are verified against live sources.** Before anything is stored or rendered,
each citation must still be active, have an unchanged digest, and its excerpt must still
appear in the source. What the model claims is not evidence. Same discipline as the quote
checking in `graph_ai.py`.

**Secrets live in files, never in the database.** `digitalbrain/configuration.py:read_secret`,
one file per provider per application, owner-only permissions on POSIX. Never environment
variables, never a database row, never an audit detail, never sent back to the browser.
`.env` accepts only `SITE_ADMIN_USER_ID`.

This rule used to end "never a form field" as well. **That half was traded, deliberately,
for self-service**: an application owner can set their own credentials on the Credentials
screen, because needing an operator for every Jira board made the platform unusable
without one. `platform_core/secrets.py` is the only thing that writes them, and what it
writes is a file with the same name, in the same shape, at mode 0600 — so every property
the rule protected except "a secret never travels in a request body" is intact, and
`test_managed_credentials.py` pins each one. The screen is owner-only, the value is never
redisplayed, and `ManagedCredential` holds who set it, when, and a digest salted with the
file name — never the secret.

**Two directories, and the operator's wins.** `secret_directory` is what a deployment
mounts, read-only, and `application_secret` reads it first. `managed_secret_directory` is
where the browser writes, must be a different directory, and is **unset by default** — a
deployment that does not configure it behaves exactly as it did before, credentials
operator-only. A credential projected from a secret manager is never overridden by one
someone typed into a form. `platform_core/secrets.py:MANAGEABLE` is the closed registry of
what can be set this way: application-scoped credentials only. `django_secret_key` and
`database_password` are deliberately absent — the process cannot serve the screen without
them, so offering to set them there would be offering to lock the platform out of itself.
A name never comes from a request: the form posts a registry key and the file name is
built from that key and the application id.

**A shared connector folder, for demos.** `connector_secret_directory` (unset by
default) holds one file per connector named only `jira`, `git_read`, `git_write`
and `servicenow` (`secrets.SHARED_NAMES`), read by **every** application that has
no credential of its own. That deliberately gives up "one application cannot read
another's credential" for connectors, and only for connectors: model providers
are never read from it, because `AIUsage` billing depends on per-application
keys. It is consulted **last** - operator mount, then browser-managed, then
shared - and the browser never writes or clears it. Tests run with it blanked
(`digitalbrain/test_runner.py`) so a file on the developer's machine cannot
change what "not set" means.

`scripts/ai_setup.py` (run by `start-all.cmd` on a first run, or `-ConfigureAI`) asks for the
credential once on a console and writes `<provider>_default`. That file is a **template,
not a fallback**: `ai.seed_application_ai` *copies* it to `<provider>_<application id>`
when an application is created, and `provider_credential` still resolves only the
per-application name. Nothing reads `_default` at request time, so a deleted
per-application file stays an error and one application still cannot read another's
credential. Do not make it a read-time fallback — that is the whole distinction.

**The Claude CLI never uses a host login in production.** The SDK drives the
bundled Claude Code CLI, which would otherwise fall back to an interactive session
in `CLAUDE_CONFIG_DIR`. The sandbox points that (plus `HOME`/`USERPROFILE`) at an
empty temp directory and blanks the environment, so no run can inherit another
application's credential or the operator's own. This is deliberate: per-application
billing and `AIUsage` attribution depend on it. The mounted file may hold either an
API key or a `claude setup-token` OAuth token; `agent_runtime/credentials.py`
decides which variable the CLI is given, and blanks the other.

The one exception is `claude_use_host_login = true` in `config/local.toml`, a
development convenience that lets the CLI see the real `~/.claude` when an
application has no mounted Claude credential. `load_config` **refuses** it when
`mode = "production"` rather than ignoring it, `settings.CLAUDE_USE_HOST_LOGIN`
forces it off there anyway, and a mounted credential always takes precedence over
it. A missing secret with the setting off is still an error — it must never
quietly become "spend the operator's account".

**AI settings is the only place that says so**, and it is owner-only. Chat carried
the same notice and it was removed as redundant for owners, which is a deliberate
trade: on a shared development instance a viewer asking questions is not told whose
account is paying. The AI settings notice says that too, so the page cannot imply a
warning nobody sees. Mount `claude_<application id>` before other people use that
instance, rather than putting the banner back.

**Single sign-on authenticates and grants nothing** (`sso.py`,
`docs/single-sign-on.md`). Microsoft Entra ID OpenID Connect, code flow with
PKCE, pinned to one tenant: `load_config` refuses `common`/`organizations`, and
the ID token must be RS256 by a key the tenant publishes (fetched through
`fetching`, never taken from the token), with exact `iss`, `aud`, `tid` and
`nonce`. The client secret is the mounted file `entra_client_secret`. Accounts
are keyed on tenant + object id (`SignInIdentity`, deliberately not
`ExternalIdentity`, whose rows are written by email lookups); a first sign-in
links by sign-in name only when exactly one user matches, that user is not
linked elsewhere and **is not a platform administrator** - an operator links
those with `manage.py link_sso`. `entra_create_users` (off) makes an account
with no password, no membership and no grant. Entra groups and roles are never
read: mapping them to grants would let the directory widen access without an
audited grant here.

**Provisioning creates and deactivates accounts, and grants nothing** (`scim.py`,
SCIM 2.0 at `/api/scim/v2/Users`, `scim_provisioning`, refused without the Entra
ids; bearer token the mounted `scim_token`, at least 32 characters). Entra's
provisioning service creates an account - no password, no membership, no grant -
updates its name and email, and deactivates it when the person is unassigned or
deleted, so access ends with the directory. It may touch only `ProvisionedUser`
rows: accounts it created, and one existing account matched by sign-in name
(audited `account.directory_linked`). **Never a platform administrator.** Groups
are refused, so the directory still cannot widen access; a user is never deleted,
because the audit trail and receipts name them. Each change is audited by the
non-login `entra-provisioning` account, field names only. Off by default, and a
404 while off.

**API tokens are users, not a second permission model.** `api_auth.authenticate`
returns (user, application); `api.authorize` then runs the same `access()` checks a
session request runs. Never add a token role or a token-specific permission table -
the point is that revoking a grant closes the token with no extra code. Bearer only
on `/api/v1/`, cookies only on the browser routes; accepting both would make the API
CSRF-able. Secrets are stored as a SHA-256 digest and compared with
`hmac.compare_digest`.

**A connector may import on its own, as the person who created it.** A ticket
board is stale knowledge the moment somebody moves a card, so `Connector.sync_interval_minutes`
lets an owner schedule an unattended import, run by the `connectors` worker lane.
There is no background identity: `connectors.process_next_connector` calls the
ordinary `sync` as `created_by`, which re-checks their owner grant inside the same
transaction a button press does. **Revoking the grant stops the schedule, with no
code that knows about schedules** - the same property API tokens have. A connector
whose creator is gone or demoted records why on the row and stops. The schedule
counts from `last_attempt_at`, written on success *and* failure, so an unreachable
instance waits its interval instead of being retried every tick.

**Status is part of the record, not metadata beside it.** `connector_kinds.header`
puts type, status, priority and labels into the imported body, because the digest
is computed over the stored content: with status outside it, a ticket moving to
Done read as an identical record and imported nothing. A board could be synced all
day and never show that anything had been fixed.

**Absence is not deletion unless the owner says the filter is complete.**
`connectors.prune` retires knowledge for records a connector no longer returns, and
only when all three hold: `prune_missing` is ticked, the result is complete
(`connector_kinds.complete`: a plain list at `MAX_RECORDS` is a page, not an answer;
every kind follows its source's paging - Jira's is `nextPageToken` on Cloud, `startAt`
on Data Center - for up to `MAX_PAGES` and returns a `Page` saying whether it reached
the last one; a read since a high-water mark never prunes), and no other enabled connector
shares the same `Kind.namespace` (two connectors onto one Jira with different
filters would otherwise retire each other's imports). Retiring is `active=False`,
the same supersede an updated record performs - nothing is deleted. Leave it
default-off: under `updated >= -30d`, absence means the ticket is old.

**Every kind pages, and says whether it read to the end.** GitHub reads up to
`MAX_PAGES` of issues (a short page is the last, counted before pull requests are
dropped); ServiceNow reads any table but `incident` to its end the same way, so a
knowledge base past one page imports; AWX is asked for page numbers and never
fetched at the `next` address it writes; Rundeck, Azure Automation and an
automation list are handed their whole list and keep up to `MAX_LISTED`, complete
only when nothing was cut (and, on Azure, no `nextLink`). Each returns a `Page`, so
`prune` holds every kind to the same "complete" rule.

**A scheduled import reads what changed since its high-water mark**
(`Connector.high_water`, `connectors.changes_since`, kinds with `incremental`:
GitHub and Jira). The mark is the newest `Record.updated` a read saw, never moved
back. A read since it starts `OVERLAP` (five minutes) earlier and goes oldest first
- GitHub `since=`, Jira a *relative* `updated >= "-Nm"` clause, because JQL reads an
absolute date in the account's own time zone - so a read stopped at `MAX_PAGES`
still read everything older than its newest. A button press always reads the whole
filter, and so does a schedule whose last full read (`last_full_read_at`) is
`FULL_READ_EVERY` (a day) old; **only a full read prunes**, and nothing changed since
the mark is an ordinary answer, not an empty filter. Saving a different filter
clears both, since the mark said nothing about the new set.

`graph/search/` and the MCP server call no model: they pass the token's user as
the viewer and `spend=False` to `graph_citations`, so source permissions apply and
no question embedding or rerank is paid for, whatever the application has switched
on (`test_api` pins it). `utility/api_chat.py` does - it is the
one API surface that spends the application's provider budget - so it is gated on
the `chat_api` feature switch. It used to be **opt-in**, unticked on the create
form through `services.OPT_IN_FEATURES`. **That was traded, deliberately, for
convenience**: a new application now starts with it ticked like every other
feature, and `OPT_IN_FEATURES` is empty but kept as the mechanism. What still
holds: migration 0036's explicit disabled row for every application that existed
before it is untouched, so no release turned it on anywhere; the person creating
an application sees the box and can untick it; and the endpoint is unreachable
without an API token an owner issues. It answers through `workbench.answer_question` and the
browser's own streaming worker - no second answering path, and no way for a
caller to choose a model or a credential.

`platform_core/utility/` holds surfaces built **on** the platform rather than
part of it. The dependency runs one way: nothing in `platform_core` imports from
it, so anything in there can be deleted without the platform noticing. Not named
`tools` because `agent_runtime/tools.py` already owns that word.

**Outbound fetches are address-checked and pinned.** `platform_core/fetching.py` is
the only place this server retrieves a user-supplied URL. It resolves first, refuses
private, loopback, link-local, multicast and reserved addresses unless an operator
named the host in `fetch_allow_hosts`, then pins the connection to the validated
address so DNS cannot change the answer underneath it. Every resolved address is validated,
and they are dialled in turn until one connects - the request itself is sent once. Pinning
to only the first used to fail every GitHub download whenever the resolver listed an
unreachable CDN address first. http/https only, no redirects
followed, 8 MB and 20 s caps, no credentials attached. Do not add a second code path
that fetches URLs.

`code_graph_clone.py` is the one exception, and it is one because it takes no
URL. Code Graph reads a repository by cloning it: the REST path spent one
request per file against an anonymous budget of sixty an hour, so a repository
of any size exhausted the quota before it finished. The caller supplies
`owner/name`, validated by `code_graph_ingest.valid_name`, and the remote is
built against a hardcoded `github.com` - no input chooses the host, which is
what the rule above exists to prevent. The clone reads content and never runs
it: hooks are redirected to an empty directory, submodules are not followed,
and `HOME`/`USERPROFILE`/git config are pointed at a scratch directory so a run
cannot reach the operator's own git credentials. The token is written to that
scratch config rather than passed in argv, where the process list would expose
it. Do not widen this to accept a URL, and do not add a third fetcher.

**A server folder is read only inside roots an operator lists.** Code Graph's
"From a folder on this server" (`code_graph_local.py`) lets an owner or
contributor type a path, which is a browser user choosing what part of the
server's disk to read - so it exists only when `code_graph_local_roots` names
folders in the config, and is unset by default. `load_config` refuses a drive
root the way `fetch_allow_hosts` refuses a wildcard. The path is resolved before
it is judged, so a symlink is held to where it lands; a folder inside any secret
directory is refused and none is walked into; symlinks, junctions, `SKIP_DIRS`
and hidden folders are not entered. Its Browse button lists server folders (`repository_browse`) under the same rules, because a browser's own folder picker uploads files and never reveals a path. All of it is checked again when the index
runs, because the operator can shorten the list. **git is never executed in the
folder** - a working tree's own config can name programs git would start - so
the commit is read from `.git/HEAD` and the ref files as text, and a folder that
is not a checkout is pinned by its manifest digest. It fetches nothing and is
not a third fetcher. Local repositories are `provider="local"`.

**Code Factory can deliver into that folder instead of a pull request**, beside
the GitHub path rather than replacing it - the run's pinned snapshot decides
which. A local run is never handed the GitHub write token (`code_factory_build.gate`),
so every agent reads the snapshot and nothing contacts GitHub; a test pins that
with GitHub refused. `apply_local` is the local `publish`: off unless
`code_factory_local_write = true`, which `load_config` refuses under production;
approval rights required; every file checked against the snapshot before any is
written (changed or removed since means nothing is written), paths held inside
the folder and out of hidden and skipped folders, each file replaced atomically
with its line endings kept. Nothing is committed - the folder's own git decides.
`FactoryRun.delivered_to` is the folder, the counterpart of `pull_request_url`,
and Refresh re-indexes that folder.

**"This platform never runs the code it writes" has one exception, and it is
opt-in.** A folder has no CI, so `run_local_tests` runs its pytest suite after a
change was written into it: off unless `code_factory_local_tests = true`
(refused under production), only on an approver's button press, with the
folder's own `.venv` interpreter, an environment stripped to PATH and system
variables, and a five-minute limit. The verdict is stored in `FactoryRun.checks`,
where a pull request's CI verdict goes, so the Tests stage reads the same. Do not
run anything automatically, and do not use this server's interpreter.

**Graphify is used for its deterministic parts only.** `code_graph_graphify.py`
calls `graphify.extract.extract` (Tree-sitter AST, cross-file calls and
inheritance) on text the clone already read, in a scratch directory of its own;
`graph_quality.themes` uses its Leiden clustering and hub labels. Its semantic
pass (`graphify.llm`) must never be called: it spends a model under Graphify's
own key, which bypasses per-application credentials and `AIUsage`, and its
edges carry no quote for `graph_ai` to verify. A Graphify failure degrades a
snapshot to partial, never to no snapshot.

**Code review compares a change with its snapshot, and reads it like indexing
does** (`code_review.py`, feature `code_review`, the Reviews tab on Code Graph).
The base is always the repository's latest `CodeSnapshot`. Working changes in a
folder are re-read through `local_sources` inside today's roots - git is still
never run, so "changed" means "different on disk from what was indexed", and the
page says so. A branch or pull request is read with the same clone indexing uses,
and one GitHub compare call (`github_write.call`, so through `fetching`) names the
files it touches against its merge base with the snapshot's commit; only those are
compared, so a branch behind main does not read as undoing main, and a merge base
other than the snapshot is said. A missing file counts as removed only when the
head was read completely. The changed files' head text is kept
(`CodeReviewFile`) so a quote from the change stays checkable after the branch
moves. The checks by rule (reach through the import graph, routes and symbols
added or removed, source with no test, requirement passages from
`code_knowledge`) call no model. The AI review (purpose `code_review`) runs only
on a button, on the code_graph worker lane as whoever pressed it; every finding
quotes the diff, a dependent file or a requirement, a `requirement` finding must
quote both a requirement and code, any quote not found exactly drops it, and each
quote is checked again against the live text when the page is drawn. Results are
cached in `DocumentAIResult` (task `change`) by the digest of what the model read.
Nothing is posted to GitHub.

**Reverse-engineered documentation is never requirement evidence for its own
code** (`code_docs.py`, feature `code_docs`, the Documentation tab). It is written
by rules from one snapshot - Python models by `ast`, routes as indexed, components
as Graphify's Leiden clusters, traceability through `code_knowledge.links` - and
every section is labelled read from code, inferred, verified reference or
AI-written (purpose `code_docs`, on a button, every paragraph quoted and checked
against the file). It becomes a knowledge source only when someone adds it, under
`code-graph://<repository id>`, retiring the one before it (`active=False`).
`code_knowledge.references` excludes that prefix (`DERIVED_PREFIX`), and with it
every code-to-requirement link Code Factory and Code review use: a description
derived from the code cannot show the code meets a requirement. Chat may cite it.
A demonstration reset removes these entries with the code graph they came from.

**SharePoint reads as the application, not as the user.** `sharepoint.py` uses
app-only client credentials, so the app registration's grant is the boundary. Under
`Sites.Read.All` an importing user can obtain documents they could not open
themselves - a deliberate deployment choice, stated in the Sources panel. Do not
quietly widen it, and prefer `Sites.Selected` when asked; it needs no code change.

**A link is just a document.** `link_sources.py` records a pending `Document` and the
worker downloads it; conversion never learns URLs exist and MarkItDown is never handed
one. Keep it that way - the offline conversion subprocess patches `socket.connect` to
raise, and that guarantee is worth more than the convenience of fetching inside it.

**The converted text is the record; the original file is optional.** Citations verify
against `KnowledgeEntry.content`, so that is never dropped. `keep_original_documents`
(on unless written down as false, accepted in production) decides the uploaded file:
when off, `processing.remove_original` deletes it **on commit** of a successful
conversion and sets `Document.original_removed_at`; name, size, SHA-256 and
`conversion_report` (a PDF's pages and the ones with no text, read in the extractor
subprocess before the file can go) remain. A failed conversion always keeps its file.
"Convert again" is owner-only, needs the original, and goes through
`processing.supersede`: unchanged text keeps its entry, changed text retires the old one
(`active=False`, document released - the one-to-one allows one) and carries a
restriction over. A failed re-conversion leaves the document ready on its old text.

**Originals may live in a bucket** (`originals.py`, `[originals] store = "s3"`; `"disk"`
is the default and exactly the old file). Same key as the disk layout
(`<prefix>/<organization>/<application>/<id>.quarantine`), every read, write and delete
through `originals.store()` - nothing else touches the path. The scanner, the extractor
subprocess and the integrity check are handed a **local file**: for a bucket a private
copy (mode 0700 directory) removed when the block ends, so the extractor still makes no
network call. The keys are the mounted files `originals_access_key_id` and
`originals_secret_access_key`; botocore would otherwise take credentials, a profile, an
endpoint override (`AWS_ENDPOINT_URL_S3`), proxies and a CA bundle from the environment,
so `originals.connect` states every one (an isolated session, an explicit endpoint -
AWS's for the region when none is named - `proxies={}`, `verify`), and `test_originals`
plants each and checks the request botocore would send. The endpoint is the operator's,
like Redis's host, not a user-supplied URL, so it is not a `fetching` path; production
refuses one that is not https. Bucket failures are `StoreUnavailable(OSError)`: a
missing object is a missing file, and a bucket that does not answer is **never** read as
"upload it again" (`documents.original_stored` returns None, and conversion fails with
"not answering" so Retry works). `manage.py move_originals [--check]` moves disk
originals in after their recorded SHA-256 matches, removing each from disk only once the
bucket returns the same bytes. The entrypoint renders the table when
`DIGITAL_BRAIN_ORIGINALS_BUCKET` is set (the compose files name `digital-brain-originals-prod`,
region `DIGITAL_BRAIN_ORIGINALS_REGION`), and warns at start when the two key files are not
mounted; development uses `digital-brain-originals-dev`, so dev and production differ in
the bucket and nothing else.

**OCR is local and says which text it read.** Off unless written down; the entrypoint
writes it on (`DIGITAL_BRAIN_OCR=0` leaves it off) and so does the development config, so
both read scans the same way. `ocr = true` (with
`tesseract_path` if Tesseract is not on PATH; the Dockerfile installs it) makes the
extractor subprocess render each PDF page that has no text layer with pypdfium2, read it,
and read `.png`/`.jpg`/`.tif` uploads. Images are refused without it. Tesseract is handed
a file and makes no network call, so the subprocess's socket patch still holds. Each
page read that way is headed `## Page N (read by OCR, C% confidence)` in the text itself,
so a citation from it says so too. At most `MAX_OCR_PAGES` a document, which is
equal in `processing` and the extractor and tested; over it the document fails rather
than half-reads. A page under `LOW_CONFIDENCE` makes the Source check's "Readable text"
missing: text read badly is the one case where a quote can verify exactly and still be
wrong. `demo-artifacts/docs/08-quality-samples/` holds scans and broken documents to
try it on (`demo-artifacts/tools/quality-samples.md` says what each shows). The
scorecard reads them with the rest of `docs/` and skips only those the converter
refuses without OCR.

**Usage is never silently zero.** A missing or malformed token count is an error, not a
free request — providers bill for calls whose usage we failed to parse. See
`agent_runtime/usage.py`. Multi-turn answers record one receipt per model turn, each keyed
on its own provider request ID.

**Traces carry ids and timings only, and are off by default** (`telemetry.py`, an `[otel]`
table naming an OTLP/HTTP collector; https in production, its credential the mounted
`otel_authorization`). Three things are spans: a request (`TelemetryMiddleware`, first in
`MIDDLEWARE`: method, the route's *pattern*, status, request id, application id), a lane
step that did work (`run_lane`; an idle step is dropped by `drop_idle`) with the job kind,
and a paid call (`gateway.admitted`, from the first try for a slot to its release:
purpose, provider, model, wait, outcome, and an `ai.receipt` event per receipt - provider
request id and token counts). **What a span may say is the closed registry
`telemetry.ATTRIBUTES`**; anything else is dropped, so a prompt, answer, source text, name,
URL or query cannot reach a collector by accident. Exceptions are their class name, never
their message. No context is read from incoming headers or sent with outgoing calls, and
the global tracer provider is never set (the MCP SDK traces through the API, and records
nothing). The exporter is written here over the SDK's encoder because the OTLP package
merges headers, certificates and a CA from `OTEL_EXPORTER_OTLP_*` whatever it is given.
One attempt per batch and one warning per five minutes when the collector is down; the
batch queue drops rather than grows. A log line inside a span carries its `trace_id`. With
no table the SDK is never imported. Do not add an attribute to the registry that a person
or a model wrote.

**Metrics ride the same table and the same exporter** (`metrics.py`; `metrics = false`
turns them off alone, `metric_interval_seconds` sets the push, 60 by default). Rates,
errors and durations of requests (until the view returns, the route *pattern*, a stream
marked `streamed`), lane steps that did work, admission waits, admitted calls, answers
(question to end, by mode and outcome), receipt tokens and spend; gauges for each lane's
waiting work, oldest age, dead jobs and heartbeat age, calls in flight, answers streaming.
**What an instrument may carry is the closed registry `metrics.INSTRUMENTS`**, a subset of
`telemetry.ATTRIBUTES` with two more rules: nothing new on every call is a label (request
id, response id, token counts - those are trace attributes), and only spend carries an
application id. Gauges that read the database are read by the maintenance lane
(`observe`) and held for the reader, so one process reports them. Histograms are bucketed
in seconds at `metrics.SECONDS`; exemplars are off, temporality cumulative, and every
SDK default an `OTEL_*` variable could change is stated. **Each process reports as its
own `service.instance.id`** (`telemetry.INSTANCE`, a random id, so it names no host):
without it a web and a worker exported identical series and the collector kept the
last - two processes recording 100 tokens each read as 100 in Prometheus. The dashboard's
route pattern is "", reported as `/`; `unmatched` means nothing resolved.

**This has run against a real collector and Prometheus**
(`deploy/observability/`: collector 0.120, Prometheus 3.2). `compose.local.yaml` runs both
on a development machine; `docker-compose.observability.yaml` adds them to a deployment,
the collector serving https with a certificate from the Postgres CA (`otel-certs` copies
only its own key) and the entrypoint rendering `[otel]` from `DIGITAL_BRAIN_OTEL`
(host:port); `docker-compose.observability.scale.yaml` points the worker and sandbox at
it. Prometheus names an OTLP metric with dots as underscores, seconds as `_seconds`, a
counter's `_total` - and an **attribute by its dotted name**: `lane` arrives as
`digitalbrain_lane`. The rules once selected `lane=` and `objective=`, which no series
carries, so the queue and records alerts could never fire; `slo.label` now writes every
label from the registry, `test_slo` checks every selector's metric and labels against
`INSTRUMENTS`, and CI's `slo-rules` job runs `promtool check rules` and `promtool test
rules deploy/observability/slo-rules.test.yml`, which makes each alert fire from series
named as they really arrive (and fails on the old rules).

**Service levels are a registry, checked against records or ruled on metrics**
(`slo.py`, `manage.py slo [--check] [--json] [--rules]`). *Records* objectives - answers
end complete (a Stop is neither), a complete answer ends within `ANSWER_SECONDS`, a job
starts within `START_SECONDS` of falling due (one still waiting past it counts now), a
job ends done rather than dead - are counted from rows the platform keeps, over
`WINDOW_DAYS`, so they need no collector; `--check` fails a judged one below target, and
fewer than `MIN_EVENTS` is reported, never failed. The maintenance lane counts them every
`READ_SECONDS` for the `digitalbrain.slo.events` gauge and a `slo_status` log line.
*Metrics* objectives - no server error on a non-streamed request, a page under
`PAGE_SECONDS`, a paid call admitted rather than busy, intake, graph and import work
fresher than `QUEUE_SECONDS` - are Prometheus rules with multi-window burn-rate alerts,
generated from the registry and committed as `deploy/slo-rules.yml`; `test_slo` fails when
the two differ, so change the registry and regenerate, never edit the file. The synchronous
chat path writes its answer when it ends, so it reads as instant in `answers_in_time`.

**Audit events and receipts are kept until a deployment says otherwise**
(`retention.py`, `[retention]`). Nothing removes either by default - a trail and a bill
are records, not caches. `audit_days` and `usage_days` set windows, and `load_config`
refuses less than six years of audit trail (2,192 days, for HIPAA's documentation rule,
§164.316(b)(2)) or 400 days of receipts (the budget's month, a year's comparison, a late
invoice); 0 keeps everything. The maintenance lane deletes oldest first, `BATCH` rows a
statement and at most once an `INTERVAL`, so a first run against years of rows is many
short deletes rather than one lock on a table every request writes to, and logs counts,
never rows. Do not lower a floor to make a test or a disk happy.

**On PostgreSQL the audit table is partitioned by month** (`partitions.py`, migrations
0092-0093). The swap copied no row: 0092 built `(id, created_at)` concurrently and
validated a check that every row is older than a cut-over; 0093 renamed the table
`_history`, put a partitioned table with the same columns and Django's names under the
old name - so the release before kept writing to it - and attached the history for
everything before the cut-over, in milliseconds. A `_default` partition takes a row no
month was made for, so a write never fails. Making or dropping a partition is DDL, which
the runtime role cannot run, so `partitions.ensure` runs as the owner: in
`apply_migrations` on every deploy, and `manage.py partitions` for a schedule. It makes
`MONTHS_AHEAD` months, moves rows out of the default, and drops a partition only when
`audit_days` is set and every row in it is past the window - what the purge would delete
anyway. The maintenance lane warns (`partitions_short`) and `partitions --check` fails
when fewer than `MIN_AHEAD` months are made or the default holds rows. **`AIUsage` is
deliberately not partitioned**: PostgreSQL enforces a unique key across partitions only
if it includes the partition key, and the receipt key is what records a provider request
once - partitioned, the same request could be recorded again in a new month.

**The browser never renders model Markdown.** Streamed text is inserted with
`textContent`; finished messages are replaced by a server-rendered fragment from the
`chat_text` filter. That filter escapes first and deliberately produces no links. Keeping
one implementation is the point — do not reimplement it in JavaScript.

**Progressive enhancement is a contract, not a nicety.** Every chat control is a real
`<form method="post">` that works with JavaScript disabled and lands on the synchronous
path in `workbench.answer_question`. `static/chat.js` only intercepts. The synchronous
path stays tested and stays in the codebase.

Popups obey the same rule. `static/modal.js` turns any `<a data-modal href="…">` into a
dialog by fetching that href and lifting `<main>` out of the response — so the target
must always be a page that renders and submits on its own, and no view has a
"fragment mode". With JavaScript off the link simply navigates. A redirect in the POST
response means the view accepted it; a response at the same URL is a re-rendered form
with errors and replaces the dialog's contents. Native `<dialog>` is not decoration:
`style-src 'self'` forbids writing `style.top` from script, so its own centring and
focus handling are what make a popup possible at all.

**The CSP is pinned by a test.** `platform_core/middleware.py` sets it and
`test_security.py` asserts the exact string. `connect-src 'self'` exists for the chat
EventSource. No `unsafe-inline`, no external origins.

**Features are a registry, chosen at creation.** `services.FEATURES` is the single list;
`available_features()` filters it to what can actually be switched on. Adding one line
there puts a checkbox on the create form, a row on the Features screen and an entry in
the nav, with nothing else to change. A missing `ApplicationFeature` row means enabled,
so only unticked features are written.

**What an application is for is the switches, not a field.** The create form asks
"What is this application for?" - Engineering (`code_graph`, `code_factory`), Operations
(`service_ops`) or Both - and `services.PURPOSES` turns the answer into those rows; only
the shared features are checkboxes. `services.purposes(app)` reads it back from the switches,
so changing sides is ticking features on the Features screen, which groups them by
`FEATURE_AREAS`. A caller that posts no purpose, or an unknown one, gets Both: a bad value
must never switch everything off. Onboarding (`readiness.all_steps`) shows the shared
connector question, then each purpose's gates on one page; `readiness.steps` stays exactly
the Code Factory list because `prevalidate` narrates it.

Knowledge stays a shared feature - triage cites runbook passages from the published
knowledge graph - but an **Operations-only application calls it "Runbooks"**
(`services.knowledge_label`), and the operations graph is ServiceOps' own **Graph** tab
(`serviceops-graph`), not a tab inside Knowledge. It used to be both, which made an
Operations application read as two products. `graph?tab=operations` redirects there.

**Connector kinds are features too** (`connector_github`, `connector_jira`,
`connector_servicenow`). Onboarding's connector question writes a row for every kind, so
"answered" differs from "not asked" (where every kind is still allowed). A kind switched
off is not offered, 404s on add and edit, and `sync` refuses it - which stops its schedule
with no code that knows about schedules. The Credentials screen lists only what the
application uses, and always lists a credential that is set so it can be cleared.

**Every level of the tree can be disabled and deleted by a platform administrator**
(`hierarchy.py`, the console's Structure page per organization). Deleting an
application used to have no screen - `manage.py delete_application` was the only way.
**That was traded, deliberately, for an operator who runs the platform from the browser**,
and three things hold it in place of "no screen". First, only a platform administrator,
and everyone else gets a 404 rather than a 403. Second, deleting is the second of two
steps: it is offered only for a level that is already disabled, and the name must be
typed exactly. Third, it is refused while anything under it is in flight - runs, indexes,
reviews, conversions, a streaming answer, an admitted paid call, a running job. That
check is made under a lock on the application rows, the lock `gateway.try_admit` takes.
It is allowed in production too.

**Disabling is reversible.** Organization, portfolio, product and application each have
their own `active` flag. An application is live only while all four are on
(`policy.LIVE`, read by `applications_for`, the gateway, connector scheduling and the
graph lane), so turning a parent back on restores exactly what was on. A disable fails
the work still *queued* under it, which has cost nothing, with `hierarchy.STOPPED`. A
step already running finishes, and its next paid call is refused. A schedule just waits.

**Deleting is not reversible, and it leaves a tombstone.** What the applications hold is
removed deepest first. Every level's row stays, with `deleted_at` set and hidden by
`LiveManager` (`objects`; `everything` sees it), because two kinds of record outlive it:
audit events name the organization (a real foreign key, kept six years) and `AIUsage`
receipts name the application. **Neither is ever deleted with what it describes** - the
command used to delete receipts, and no longer does. The slug is freed. Files go after
the commit: originals (a bucket that does not answer is counted in a `.files_left` audit
event) and credential files in the managed directory only. Each act is audited as
`<level>.disabled`, `.enabled` or `.deleted`.

Administering the tree still grants nothing: the Structure page shows names, states and
counts, and links no application. `manage.py delete_application <id> --confirm-name
<name> --as <admin>` remains the host's tool. It runs `hierarchy.delete` without the
disable step and also accepts an organization administrator.

Both the create form and the Features screen post a hidden `features_declared` marker.
An unticked checkbox is simply absent from a POST, so without the marker "every box off"
and "this caller never mentioned features" are the same bytes — and the second must not
silently disable everything. With the marker the checkboxes are taken literally.

**Two people review a change — unless a deployment says there is only one.**
`review_plan` refuses a plan approved by its own author, so whoever starts a run
cannot wave it through. **That was absolute and is now a setting**, traded
deliberately for the same reason the secrets rule was: an instance with one
operator deadlocked at the review gate, and there is no way out of it through the
UI — `change_grant` will not hand out `can_approve` to someone who lacks it, so a
single owner without the flag cannot even create a reviewer. `allow_self_approval`
is now **on unless written down as false** (`configuration.self_approval_allowed`) -
a second deliberate trade, because most instances are run by one person and the
off default made every new one deadlock first. Two-person review is
`allow_self_approval = false`. `load_config` **accepts** it under
`mode = "production"` where it used to refuse it. It is the one flag of the three that does; `claude_use_host_login` and
`allow_demo_reset` are still refused there, and the difference is intentional —
those spend somebody else's money and delete somebody else's work, while this one
only records a weaker fact about a change. It is not silent where it counts:
`ChangePlan` stores the approver, so a self-approved plan says so in the audit
record. The review screens used to carry a warning too; **that was removed,
deliberately**, because on a one-person instance it told the only reviewer the
same thing on every plan. The audit record is the part that must stay. `deploy/entrypoint.sh`
renders it explicitly, **on** by default; `DIGITAL_BRAIN_SELF_APPROVAL=0` writes
`false` where two people really do review every change.

For the same reason the create form's "Grant this owner Code Factory approval
rights" box starts **ticked**. An owner born without `can_approve` could never hand
it to anyone, so every reviewer an application would ever have had to exist before
it did. Holding the flag is not self-approval - `review_plan` still refuses an
author's own plan unless `allow_self_approval` is set.

Onboarding follows the same setting. With it on, `readiness.steps` does not ask for
"A second approver" - a step a one-person instance could never tick - and reports
only the case where nobody holds approval at all, as "An approver".

**Creating an application never widens access.** `views.create_application` writes the
portfolio, product, application, owner grant and feature rows in one transaction, but
the org admin who creates it still gets no access to it: `policy.applications_for`
requires an explicit grant, and `test_security` pins the 404. The create form's
"Also grant me owner access" checkbox records a real grant rather than implying one.

## Graph lifecycle

Generate → draft → publish. The background worker keeps the **structural** graph current for
free; **AI enrichment calls a model only for a requested run**, carried on
`KnowledgeGraph.requested_model/_provider/_by` and cleared once consumed, so a rebuild can
never repeat a paid call. A rebuild does reuse cached answers, which costs nothing. Every run saves a `GraphRevision` as a draft.

`graph_snapshot(app_id, version=None)` resolves to the **latest published** revision, never
the working graph. Chat and Code Factory both go through it. No published revision is an
error, not a silent fallback to a draft. A conversation may still pin a specific version.

**A Code Factory run requires a published graph.** `evidence_for` used to swallow that
error and return no evidence, so a run without a graph quietly produced a plan whose items
cited nothing — a list of confident findings a reviewer cannot check, which is the one
output this pipeline must not make. It is refused in three places because they are three
different moments: `start_run` refuses at the button, `execute` re-resolves it before
spending anything because a revision can be withdrawn while a run waits in the queue, and
the plans screen replaces the form with the reason rather than offering a button that can
only fail. All three say it in the same words, `NO_GRAPH`.

A published graph that matches *nothing* is a different thing and still runs:
`graph_citations` returns an empty list for it and raises only when there is no graph, so
letting it raise fails exactly the case that should fail. The run says so, and the thin
evidence shows on every item it produces.

Neither path checks the live fingerprint — answering from a published snapshot is the point.
Safety comes from per-edge verification instead: active source, matching digest, exact quote.

**Sources are checked before a graph is built, and the check only suggests.**
`source_quality.py` scores each *document* (not typed records, not imported tickets - a
Jira ticket keeps `ticket_quality`) on a fixed rubric with no model, stored on
`KnowledgeEntry.quality` with `QUALITY_VERSION`; a stale version is re-run on read and
kept, and `manage.py check_sources` fills existing rows. Ready needs 80 **and** no check
missing outright. `source_map.py` reads the set `graphs.sources_for` would build from -
references defined and mentioned, table values keyed exactly as `build_graph` keys value
nodes - cached per `graphs.fingerprint`, shown as the Check sources popup, one line on
Sources and a warning on Generate. Nothing here ever disables Generate or leaves a
source out. Both read tables through `graphs.walk`, the walk `build_graph` itself makes:
**do not give either its own table parser**, or a finding will disagree with the graph.

**What a document is decides what it is judged against** (`document_types.py`). A closed
registry like `ontology.TYPES` - business requirements, ADR, runbook, postmortem and
sixteen more, plus Other - each naming the title words, document-number prefixes,
sections and table columns that identify it, and the checks its content must pass
(required sections, numbered requirements, a rationale and measure column, vague words
quoted by line, a timed timeline, owned actions...). It types a document from **its own
content** - title, headings, tables - and only weakly from the file name, because people
name files any way they like; no model, so the same text always gets the same type and
says which lines chose it. Only a document's **own** number counts - the first in its
opening - never one it cites (a release plan listing TST-CAREPATH-002 is not a test plan);
a `Document: … Version: … Owner:` control line is not a title, and nor is the first of
several top-level headings; one heading answers for one section. Nothing clearly ahead (`MIN_SCORE`, `MIN_MARGIN`) is **Other**,
judged by the source check alone: a confident wrong type would judge a document against
the wrong list. It is stored inside `KnowledgeEntry.quality` beside the source check as a
**separate** "Content check" score, so the source check's six checks and verdict are
unchanged. An owner or contributor can set the type (`KnowledgeEntry.document_type`, a
registry key or blank, audited); a person's choice always wins, survives "Convert again",
and the page still says what the rules would have chosen. `REGISTRY_VERSION` re-runs
stored results like `QUALITY_VERSION` does. The scorecard types every CarePath document
against `benchmarks/document-types.json`, as uploaded and with its title and file name
removed - the second is the honest measure of reading content rather than names.

**AI may help with the type, never above the rules** (`document_ai.py`). Two purposes,
both through `ai.invoke_ai` (grants, `budget.check`, an `AIUsage` receipt), both cached in
`DocumentAIResult` by text digest and rubric so the same text is never paid for twice -
a refused verdict included - and neither reads a source `answerable()` excludes.
`document_typing` (seeded on, like every task - see "Every AI task starts on") is asked only where the rules
say Other with low confidence and no person set a type: after conversion in the worker,
charged to whoever converted it, or by an owner's or contributor's "Ask AI what it is".
The model picks one registry key and quotes the lines that show it; a key outside the
registry, Other, no quote or **any** quote not in `KnowledgeEntry.content` exactly leaves
it Other. An accepted type is `quality["type"]` with `source="ai"` and its quotes, which
`source_quality.ai_named` checks against the text again every time the check is worked
out; content checks then judge it as that type. A person's type still wins.
`document_review` (seeded on, like graph generation: it spends only on an owner's button)
judges clarity, testability, contradictions and gaps for the current type; a finding whose
quote is not in the text is dropped, and the card sits beside the Content check as advice
and never changes its score. Both are plain POST forms. Measure a prompt or model change
with `manage.py typing_pack <app> --as <user> --record` (paid, one call per item of
`benchmarks/typing-pack.json`: documents the rules leave as Other, labelled by a person).
Prompt 1 named a type for two of seven Other documents - an on-call handover that
mentioned "the deployment runbook", an overview from its index tables; prompt 2
(`TYPING_VERSION`, part of the cache key) took precision 77.8% -> 100% at recall 87.5% on
claude-sonnet-5. It was tuned on those same fifteen items, so treat the pack as a floor,
not proof, and add to it before trusting a new model. Those figures are the registry-1
pack: registry 2 types four of its CarePath documents by rule, so the pack was rebuilt
(twelve items, four of them a type and eight Other) and has not been re-measured. Its
recall now rests on the four written items alone - more are the first thing to add.

`walk` promotes a wholly bold first row under a blank header to the header - how
MarkItDown writes a Word table whose header row is not marked as one. Stored text is
untouched, so digests and quotes are too; the generator is `structural-v2` so every
structural graph rebuilds once, as a draft. On CarePath it named 400 values that were
"column N", added 8 cross-source joins, and took graph MRR 0.709 -> 0.716 with two
questions ranked higher and none lower. A `**Label** | value` front-matter table is not
promoted and not flagged.

**Retrieval changes are measured, not asserted.** `benchmark.py` scores every saved graph
version against an owner's questions (a version's score in Knowledge → Versions or Quality opens it, `manage.py benchmark`): is the
expected source among the citations, in the top three, and at what rank. It calls
`graph_ai.citations_from` - the ranking and verification an answer uses - and no model, so it
is free and runs automatically after each version is saved. It may score a **draft**: measuring
is not answering, and which graph answers is still `graph_snapshot`'s decision, which refuses
drafts. Change ranking only with a before/after benchmark run; CarePath's questions are in
`demo-artifacts/demo-kit/benchmark-questions.json`.

**Every version is measured with its AI and without it** (`graph_trend.py`, Knowledge →
Quality → "Quality by version"). An AI-built version is two graphs - what the rules read
and what a model proposed and a quote verified - so `without_ai` removes its entity nodes
and every relationship that is inferred or touches one, and the same questions are scored
against that too (`BenchmarkRun.structure`, by `benchmark.score` itself; a version with no
AI is its own structure, and a reranked run is never scored twice, since that would be a
second paid rerank). The shape figures - cross-document links, largest connected piece,
orphans, quoted relationships - are taken both ways as well (`GraphRevision.measures`,
`MEASURES_VERSION`), beside the AI run's own counts (kept, verified share, refused,
outside the ontology). Both are reports *about* a version, filled by `run_after_save` and,
for an older version, the first time the Quality tab draws it; `data` is never written.
The chart is server-drawn SVG (points and classes, no script, no style attribute), and each
figure is marked better or worse than the version before, a change smaller than rounding
reading as none. No model is called.

Ranking is `ranking.py`: texts holding more of the question's terms first, BM25 second, then
`spread` - each source's best citation before any source's second. Both the graph path and
the keyword path (`workbench.retrieve`) use it. On CarePath v4 it took the benchmark from
11/16 found (MRR 0.53) to 15/16 (MRR 0.61). BM25 alone did not help; the spread did most of
it. Graphify's own scorer was not reused: it is private API tuned for code-symbol nodes.
`serviceops.stem` is `ranking.stem`, moved not changed - incident fingerprints depend on it.
Three later fixes removed every question that ranked worse than the old word count: an
identifier is its words (`max_page_size` → page, size), `ranking.term` folds a long
"-ment" ending (requirement → require) **outside `stem`**, so fingerprints are untouched,
and "against" is filler. Measured against HEAD 18dee62 on CarePath v4: graph 11/16 → 15/16
found, MRR 0.53 → 0.69; keyword 8/16 → 14/16, MRR 0.26 → 0.67; no question worse on
either path. Keyword search keeps each source's passage terms (`workbench.passage_counts`,
keyed by digest, title and length) and `term`/`stem` are memoised: 10 ms per question at
HEAD, 23 ms with BM25 alone, 4.4 ms now.

**AI extraction is typed by `ontology.py`**, a closed registry like `services.FEATURES`:
eight types and twelve relations, each relation with the types allowed at each end and the
phrasings it absorbs ("uses", "relies on" → `depends on`). The prompt is written from the
registry and the validator enforces it, so they cannot drift; a relationship outside it is
rejected and counted (`quality["outside_ontology"]`) like an unverified quote, and the
quote check still runs first - a type is a filter, never evidence. `merge_entities` then
joins typed entities across sources only on the same type and the same words (stem- or
long-typo-equal, word by word, numbers exact): whole-string similarity merged
`carepath-api` with `carepath-api-green`, which is CARE-444's mistake. Each edge keeps its
own source, digest and quote. Untyped entities in older versions are left alone.

**A source may be the subject of what it says** (`graph_ai.own_names`, `SELF_TYPES`). A
ticket's body never repeats its own key, so under the exact-quote rule "CARE-401 violates
BR-05" could never verify: the drift check found 11 of 16 proposals were exactly that,
refused, while the prompt asked for "what a ticket affects". The subject may now be the
source itself - named by its title, the key its title starts with, or its imported `Key:`
line, all read from the record, never from the model - when typed as something a source
can be (Ticket, Runbook, Incident, Change). The object must still be in the quote and the
quote must still be exact; every other subject still has to be quoted. On the frozen pack
this took verified proposals from 25% (0% on a rerun) to 91%, and the two still refused
were correctly refused. Re-score a `judge` change against a record's `proposals` with
`drift.rescore` before paying for a run.

**Retrieval walks two hops, typed edges only.** `graph_ai.walk` goes breadth-first from the
best-ranked AI relationships along other AI relationships (`MAX_HOPS`), stops at hubs
(`MAX_FANOUT`), and its finds (at most `MAX_WALKED`) come **after every direct match**,
each saying "Reached by:". They used to take places ahead of weaker direct matches; on an
AI-enriched CarePath version that cost a benchmark question (14/16, MRR 0.678), and after
them it is 15/16, 0.691 - the walk only fills a place a direct match left empty.
`test_the_walk_never_displaces_a_direct_match` pins it. Structural edges are never walked
- a value node such as "Status: Done" joins hundreds of records. Every walked citation is
verified like any other.

**A reference is one term** (`ranking.identifiers`). `words` drops anything under three
characters, so "C-09", "BR-05" and "R-04" were no term at all and "NFR-06" was "nfr", which
every requirement shares; a question's twenty-word cap also cut off references late in a
long ticket. References are now indexed whole beside the words and kept beyond the cap.
That alone took the graph benchmark to 16/16, MRR 0.709. Ranking references *ahead* of
words was tried and measured worse (evidence recall 76% -> 60%): planted evidence such as
a risk register row is found by the ticket's words, not its references.

**Code Factory follows the references its ticket names** (`graph_ai.referenced`,
`MAX_REFERENCED`, asked for by `code_factory.evidence_for` only). After the eight direct
matches - exactly what chat cites, unmoved - up to four passages that name the most of the
question's references, read from the whole passage rather than its label (a table row's
label stops at its first cell), a source not yet cited breaking ties. A source naming its
own key is not a reference to it. Each is verified like any other citation and says
"Reached by: names C-09". A question naming no reference gets nothing extra, so chat and
the benchmark are untouched. Planted-bug evidence recall went 76% -> 96%; the one miss,
F-03, is named only by evidence (C-08), not by the ticket - following references from
the evidence is the next step, and would need measuring for noise first.

**Published graphs are prepared once per process** (`graph_ai.prepared`, LRU of
`CACHE_SIZE`): parsed data, relationship texts and the BM25 index. Safe because a
revision's data never changes after it is saved; a `post_save` drops it anyway. Callers of
`graph_snapshot` must not mutate what it returns. ServiceOps runbook linking matches a
component name however it is punctuated (`ops_graph.name_pattern`) and also under names
the knowledge graph merged (`ontology.aliases_for`); `RULES_VERSION` rebuilds every
operations graph when a rule changes. The plan for the rest is
`docs/retrieval-roadmap-plan.md`.

**Embeddings choose starting points, never citations.** `embeddings.py` (purpose
`embeddings`, OpenAI only - Anthropic has no endpoint - and **on only where the
application has an OpenAI credential of its own**, `ai.has_embeddings_credential`). A vector's nearest relationships or passages are fused with the BM25 order
(`fuse`); every one is then verified exactly as a word match is, and a passage found by
meaning is re-read and re-hashed against the text that was embedded. With no vector the
order is exactly the word order. Spend follows the extraction rule: relationships,
passages and benchmark questions are embedded only in a **requested** AI graph run or by
`manage.py embed --as`, never by a background rebuild; a question is embedded when asked
and charged to whoever asked (`user=` on `graph_citations`/`retrieve`), and a caller with
no user gets stored vectors only. Vectors are content-addressed per application
(`models.Embedding`), so nothing is paid for twice and one application never uses
another's. A failed question embedding answers by words and logs `embedding_unavailable`;
a failed indexing step warns on the version and keeps the relationships. Search is
in-process numpy over unit float32 - pgvector was deliberately left out until a library
outgrows memory, so the production database did not change.

**A monthly AI budget is checked before every paid call** (`budget.py`, `AIBudget`, owner
sets it in AI settings; no row means no cap). The three choke points are `ai.invoke_ai`,
`ai.chat_configuration` and `embeddings.embed` - a new paid path must call `budget.check`
too, and go through `gateway.admitted`. It caps *starting* spend from recorded receipts at the entered rates: a call in flight
is never cut off, and graph extraction that hits the cap keeps what it already paid for.

**Every paid model call passes the gateway** (`gateway.py`, built here rather than
bought). LiteLLM, Portkey or a cloud AI gateway would hold the provider keys and keep
their own prices and retry rules, which is four invariants above traded at once, so the
gateway is a pipeline in front of the SDKs already used: the purpose's rule, the
configuration, admission, the credential file, the adapter, the receipt, release, the
rule again. **Purposes are a closed registry** (`gateway.PURPOSES`), the switches and
roles each needs, read before the call and again after it - it used to be an if/elif
chain written twice, slightly differently. **Admission** (`AIAdmission`, one row per
call in flight) is a slot and a reservation: at most `max_calls_per_application` calls in
flight for one application and `max_calls_per_provider` across the deployment
(`[ai_gateway]` in the config, `load_config` checks it), so one batch cannot take every
connection a provider allows; and it reserves the call's most expensive outcome - every
prompt character a third of a token, the whole reply cap - so recorded spend plus calls in
flight must be under the cap, and two calls at the cap no longer both start. Admission
locks the application row (and a PostgreSQL advisory lock per provider) in a short
transaction of its own: no lock is held across a provider call. A caller waits
`slot_wait_seconds` and is then told nothing was sent - **never retried for it**. A
streamed answer holds its slot for the whole run, tool turns included. A row a dead
process left expires after `admission_minutes`; the maintenance lane sweeps it, and
`queue_status` counts calls in flight by provider. **A receipt is written for the
application authorized before the call** (`record_ai_usage(authorized=...)`): it looked
the grant up again, so a grant revoked while the model answered lost the receipt of a
call the provider had billed. The answer is still lost. The scorecard's "past a
reservation or a full gateway" must stay 0.

**A one-shot Claude call with an API key is one Messages request** (`claude_messages.py`,
the `anthropic` SDK), not a CLI subprocess. An OAuth token from `claude setup-token` is
accepted only by the CLI, and no credential is the development host login, so both still
go to `claude_agents`; `ai.invoke_ai` chooses by `uses_messages_api`, and
`claude_messages_api = false` sends every call to the CLI. Same prompt, instructions,
reply cap, timeout and checks; the receipt is keyed on the provider's message id. The
SDK reads credentials, a base URL, a profile and extra headers from the environment, so
the client is given the key and base URL explicitly, takes no proxy from the environment
and drops `ANTHROPIC_CUSTOM_HEADERS`; `test_claude_messages` plants all four and checks
the request. No retries. Tests run with it off (`digitalbrain/test_runner.py`) because
they mount made-up keys and patch the CLI adapter; a test of this path turns it on.
Agentic runs - chat on Claude, Code Factory's agents - still need the CLI's tools.

**Every AI task starts on** (`ai.SEEDED_DISABLED` is empty, kept as the mechanism). It
used to list document typing, embeddings and reranking, so a new application had AI
features nobody knew to switch on. **That was traded, deliberately, for an application
that uses its model from the first day**: typing runs after conversion and reranking on
every answer, each charged and budgeted like any other call. The one condition left is
a credential: embeddings start on only where the application has its own OpenAI key
(`ai.has_embeddings_credential`), because Anthropic has no embeddings endpoint and a task
that could only fail is not "on". `seed_application_ai` copies `openai_default` like
`<provider>_default` - a template, never read at request time - and `rebuild_demo.py`
switches on whatever an older demo left off. The budget and the per-task switches in AI
settings are how an owner says otherwise.

**A price belongs to a model; a reply cap to only four tasks** (AI settings, `ai.py`). The
page is one list - a row per task with its model, this month's spend and a switch that is a
real POST - and a task is edited on its own page (`ai_task`), opened as a popup. Rates still
live on each `AIConfiguration` row, but are set once per model in the Prices table, which
writes every row using it: six tasks on one model used to be priced six times. A model new
to an application takes the price it already has there, else its list price, else zero,
which the table flags as Not priced because spend - and so the budget - then reads $0.
Owner-set reply caps are `ai.output_limits` only: chat and graph answers (default
`ANSWER_TOKENS`, 1,024, once hard-coded in four places), Code Factory and ServiceOps
triage, read by `ai.reply_cap`. The rest are bounded by what they ask for and stay fixed
in code. A task whose feature is off is not listed.

**A restricted source is never read or cited by AI** (`sensitivity.py`). Every path that
hands a source to a model or verifies a citation goes through
`KnowledgeEntry.objects.answerable()` (active and not restricted) - graph builds via
`graphs.sources_for`, keyword and vector search, the chat tools, ServiceOps' record layers,
Code Factory intake and history resend. Because citations verify against `sources_for`,
restricting takes a source out of already-published versions at once. People with access
still read it. Personal data (`sensitivity.scan`) is only a suggestion on the source page;
a person decides. It is tagged **PHI** when a labelled health identifier is among what was
found (MRN, health plan member number, NHS number, `Patient name:`; `PHI_KINDS`) and
**PII** otherwise - each needs its label beside the value, so text *about* MRNs is not
flagged, and `benchmarks/phi-samples.json` holds both kinds of case. Anyone who may change knowledge can restrict; only an owner lifts it; a
re-import of the same record stays restricted.

**Masking happens once, before a source is stored** (`masking.py`, `PersonalDataPolicy`,
the Personal data card in AI settings, owner-only, off by default). `workbench.add_knowledge`
replaces the ticked kinds before the digest is computed, so the stored text *is* the masked
text and every reader - digest, quote checks, graphs, extraction, embeddings, chat, Code
Factory, API, MCP - needs no masking of its own. **Do not move it to the model boundary**:
that is one masking call per path, and the structural graph would still carry raw values
into chat. Health identifiers are ticked by default, the rest opt-in. A value becomes
`[MRN-QZKTWBAF]`: label kept, letters an HMAC of the normalised value under
`masking_key_<application id>` - a secret file like any other (operator mount first, else
written to the managed directory when switched on, never the shared connector folder), so
the same MRN joins across documents and cannot be guessed back. Letters only, so the scan
never reads a placeholder as a value and masking is idempotent. A missing key with masking
on refuses the record; it never stores raw text. Anything that compares incoming text with a
stored digest compares the *stored* form (`masking.stored_content`): connector sync,
"Convert again", code docs. "Mask existing sources" rewrites every entry **in place,
retired ones included** - a deliberate exception to "prior revisions stay immutable",
because superseding would keep the values in the retired row; the changed digest takes old
citations out of published versions. It is minimization, not de-identification: only what
the scan's rules find, and graph revisions, chat history and cached AI results already
written keep what they hold. The page says both.

**AI graph runs are incremental, and every answer is cached as it arrives**
(`graph_ai.enrich_graph`). Each call's answer is written to `DocumentAIResult` (task
`extract`) keyed by the digest of exactly what the model read - title and window - under
the ontology version, before the next call starts. So nothing paid for is lost when the
version is not saved (a source changed mid-run, a later call failed, the budget ran out),
a superseded record with unchanged text is answered for free, and only windows with no
cached answer go to the model, up to `MAX_EXTRACTION_SOURCES` calls a run. A source
longer than `MAX_SOURCE_CHARACTERS` is read in `windows`, cut at line ends and before a
heading, at most `MAX_WINDOWS`; text past them is counted on the version, never silently
dropped. An answer is kept whole even past `MAX_RELATIONSHIPS` (it stops new calls, not
answers). Proposals are judged on every build against the source the model was reading,
so an answer naming another source's id is refused. **Every rebuild carries cached answers
forward with no model call** (`enrich_graph(app_id, None, ...)`): a background rebuild
after a source change keeps the AI relationships of everything unchanged and names their
model on the version, instead of dropping to structure. `graph_ai.reusable` is only the
fallback for one-window sources extracted before the cache existed. A demonstration reset
deletes the cached answers (typing and review answers stay with the sources it keeps), so
the first rebuild after it is structure alone.
`data["ai_sources"]` records which sources have a complete extraction. **Automatic refresh is the one standing request**
(`graphs.arm_auto_refresh`): owner-only and off by default, it turns a source change into
an incremental AI run charged to whoever switched it on, re-checking their grant first -
losing it stops the refresh and says why - like a connector schedule. It produces drafts;
the publish gate is untouched.

**Answer reranking can only reorder** (`rerank.py`, purpose `reranking`, seeded on with
every other task since `SEEDED_DISABLED` was emptied). It sees verified citations and returns their numbers; anything else is
dropped and any failure keeps the original order. Measure it first with
`manage.py benchmark --rerank --as <user>` - the one benchmark run that spends.

**Keyword candidates come from full text on PostgreSQL** (`workbench.candidate_sources`);
SQLite keeps the substring scan. Its test skips outside PostgreSQL and says so. The
tsvector is a **stored generated column** (`search_vector`, migration 0088) with the GIN
index on it, for sources and passages both: the expression indexes of 0073 and 0087 let
`ts_rank` tokenize every matching row again on every question, which the first
PostgreSQL scorecard measured at 51 ms a keyword answer against SQLite's 5 (10 ms after).
Django does not know the column; PostgreSQL fills it on every write, in-place rewrites
included. `FULL_TEXT` and `chunks.CHUNK_TEXT` name it, table-qualified, because the
passage query joins both tables.

**Shared streams** (`shared_streams = true`, `StreamLease`) let Stop and `is_live` reach
every process when there is more than one and no Redis. Only those two cross processes
there - a stream and its worker share one - so a polled lease row, written by a keeper
thread and never by the SSE generator, is enough. `load_config` refuses it beside `[redis]`.

**With Redis, an answer outlives its connection** (`broker.py`, the Redis mode in
`agent_runtime/streaming.py`, a `[redis]` table; the password is the mounted
`redis_password`, and production refuses it without verified TLS). Each answer's events
are a Redis stream, and every reader - the request that started it, a browser reconnecting
with `Last-Event-ID`, a second tab, on any web process - replays it from the start or from
that id and follows it. So a dropped connection resumes (`chat.js` lets the EventSource
reconnect only when the start payload says `resumes`), and a reader leaving no longer stops
the answer: Stop does, pushed onto a list the writing process is blocked on, and
`MAX_STREAM_SECONDS` does. **Nothing is paid for twice**: the writing process holds an
owner key, renewed every `LEASE_POLL_SECONDS`; a reader that finds one follows, one that
finds events and no owner has found an answer whose process died and closes it out once as
interrupted (with the text it got to), and only an answer with no events is started. A
finished answer is replayed while its events last (`SESSION_TTL_SECONDS`) and never
started again - the views start or close out only a row still marked streaming. The
worker still owns every database write; readers touch Redis only, and hold a reader slot
each (`MAX_ACTIVE_STREAMS`, a waitress thread apiece). The API rate limit is one Redis
counter per token and window, refusing (503) when Redis cannot count. Redis holds copies
in transit, never a record, so the compose service has no volume: TLS from the Postgres
CA, a generated password in its own config file, `noeviction`. Without `[redis]` none of
it is used. `test_redis_streams` runs against fakeredis and again against a real Redis
when `DIGITAL_BRAIN_TEST_REDIS` names one, which CI's PostgreSQL job does and requires.
With it, and the migration job below, the web container can be more than one replica.

**Source-system permissions on answers** (`permissions.py`, connector "Enforce source
permissions", Jira only). Each record from an enforcing connector carries the platform
users Jira lets browse it (`access_principals`); every answer path filters by the asker
with `permissions.permitted`, and **fails closed**: unchecked records, members with no
matched account, and callers with no user see nothing restricted. Members are matched by
email (`ExternalIdentity`) or set by `manage.py link_identity`. The benchmark measures as
`EVERYONE` because it answers nobody. The Sources list and graph explorer still show
titles to members - this governs answers. ServiceNow and SharePoint are not supported:
both need delegated per-user access.

**The operations layer is the one exception, and it is deliberate.** The operations layer
(incidents, changes and what connects them) is live, not publish-gated: it is rebuilt by rules
from imported records whenever they change, and every edge is re-verified against its records
when read. Documents stay publish-gated. Nothing a model says is written to either.
`platform_core/ops_graph.py` builds it (`OperationsGraph`/`OpsNode`/`OpsEdge`); `ensure_current`
rebuilds whenever the fingerprint over incident and change ids and digests, plus the published
knowledge-graph revision, has moved. Runbook passages join it only from the **latest published**
revision, by exact whole-word component or service name, and a passage from an older version
is skipped at read. **Knowledge articles and automations are operations nodes too** (`ops_graph.link_articles_and_automations`).
A ServiceNow `kb_knowledge` import joins the graph when **ServiceNow** says Published
and in date - that review is the gate, deliberately not this platform's publish
step, because the article was already published where it is written. Every
workflow state is imported, so an article retired there leaves at the next sync,
and the day is part of the fingerprint so an expiry takes effect without a record
changing. Automations come from AWX, Rundeck, Azure Automation, ServiceNow
`sys_hub_flow` or a team's CSV list, all reduced to one `Type: Automation` header
(`connector_kinds.automation_record`). Rules join them - covers, cites (a KB number
in an incident), targets, remediates, references, resolved by (a resolved
incident's notes name it) - and nothing a model says is written. **An automation
is never executed.** "Simulate run" (`serviceops.simulate_automation`) records an
`AutomationRun` with exactly what a launcher would send and sends nothing; a real
launcher would replace that function's body and keep its checks and audit row.

`neighbourhood()` is the one reader: the incident page and the triage
evidence pack both use it, so they cannot disagree, and `score_pair` / `same_event` are the one
definition of "similar" and "same event" for the page, triage, replay and the graph. The only
edge a person makes is `confirmed` (a verdict's actual cause, chosen from the idea's own
citations); a rebuild never touches it, and withdrawing the verdict deletes it.

## Chat shape

`ChatConversation` → many `ChatMessage`, one row per message with `role`, `status` and an
explicit `sequence`. Both halves of an exchange are written in one transaction, so
timestamps cannot order them — `sequence` does.

Answer mode lives on the **conversation**, not the message. Sending a message cannot
change it. `ChatMessage.mode` records what actually produced that answer.

Streaming: POST creates the question and a `status="streaming"` placeholder, then the
browser opens `chat-stream`. A worker thread owns the provider call **and every database
write** for that message; the SSE generator does no ORM work at all. That single-writer
rule is what makes a racing stop safe — completion is a guarded
`filter(status="streaming").update(...)`.

The stream registry in `agent_runtime/streaming.py` is **process-local** unless a deployment
shares it: through a database lease (`shared_streams`) or Redis, which also lets an answer
resume after a dropped connection - see "With Redis" above.

**Under uvicorn a reader holds no thread** (`digitalbrain/asgi.py`). **Uvicorn is the
default wherever `[redis]` is set**, waitress otherwise, and `web_server` names either
(`configuration.web_server`): without Redis a process follows `MAX_ACTIVE_STREAMS`
answers under either server, so there is nothing to gain. Under waitress each open
stream held a request thread for its whole life, so a process followed
`MAX_ACTIVE_STREAMS` readers at most. `asgi.Router` sends only `chat-stream` and
`api-chat-stream` to Django's ASGI handler and every other request to the same WSGI
application in a bounded `a2wsgi` pool, so views, middleware and the CSP are unchanged.
What the view returns is `streaming.attach(..., asynchronous=True)`
(`served_by_event_loop`), an `AsyncReader` that polls a local session's queue every
`POLL_SECONDS` or awaits Redis through `broker.async_client()` (one per event loop,
its pool sized `ASYNC_CONNECTIONS` - redis-py's default of 100 failed every reader past
it), with its own slots (`MAX_ASYNC_READERS`). The frames are the blocking readers' own
(`decoded`), and still no ORM work. The answers - a worker thread and a paid call each -
are bounded as before. uvicorn does none of waitress's edge work, so the router does it:
X-Forwarded-* is believed only from `--trusted-proxy` (its last hop) and removed from
anyone else, and a body over `MAX_BODY` (413) or of no stated length (411) is refused
before Django reads it. `digitalbrain/servers.py` holds both servers' options, for
`serve.py` and the load test alike.

**Stream views share `STREAM_THREADS` lanes** (`asgi.Streams`). Django's ASGI handler
gives each request a thread for as long as its response lasts (asgiref's
`ThreadSensitiveContext`), so 400 open streams held 400 idle threads; one shared
thread instead admitted twenty readers a second. Each request now gets one of a fixed
set of thread-sensitive contexts, each with its executor made once, by setting
asgiref's `thread_sensitive_context` and filling `context_to_thread_executor` - what
`ThreadSensitiveContext` itself does - and `test_asgi` fails if asgiref renames them.
**A reader gathers words** (`streaming.GATHER_SECONDS`, both followers): after each read
it waits a tenth of a second, so the words written meanwhile come back in one Redis
read and leave in one write. A read and a write per word per reader was the measured
limit.

**Stream capacity is measured, not asserted** (`manage.py load_streams`,
`load_streams.py`). It runs the real application under each server with deployment's
options, in a throwaway database, against a throwaway Redis (`--redis host:port`, never
a deployment's; "fake" for fakeredis), with only the model replaced by a worker writing
twenty words a second. Readers arrive spread over each stage, the first of each answer
starting it, in a client process of their own (`load_clients`, which imports nothing
from Django) so their parsing is not counted against the server. On the development
machine: waitress accepted 12 readers and refused the rest; uvicorn held 50 with pages
at 547 ms p95 (waitress's 12: 594 ms) and 100 with every reader receiving and pages at
1.3 s p95; at 200 admission took ten seconds, so `MAX_ASYNC_READERS` is 100 and a
reader past it is refused (503) for another replica to serve. Change it only with a run.

## Web and worker can run apart

`scripts/serve.py` runs every worker lane inside the web process unless it is started with
`--no-worker`; `manage.py run_worker [--lanes a,b]` runs them in a process of their own
(`DIGITAL_BRAIN_ROLE=all|web|worker|migrate` in `deploy/entrypoint.sh`, and
`docker-compose.scale.yaml` for the split). Chat answers are not a lane - each runs on a
thread its stream request starts - so they stay in the web process either way.

**The lanes that hand outside content to another program run in a sandbox**
(`sandbox` in `docker-compose.scale.yaml`): intake (converters, OCR, the virus scan on
uploads), code-graph (git on a repository) and factory (the Claude CLI over a
repository's code). Same image, `DIGITAL_BRAIN_ROLE=worker` with those lanes, in a
container that is read-only but for `/tmp` and the runtime volume, drops every Linux
capability, cannot gain one (`no-new-privileges`), reads secrets and credentials
read-only and stops at a process and a memory limit - so a parser bug, a runaway
converter or a fork bomb is held to that container. The worker runs the other six lanes,
and a test holds the two lists to `LANES` with no lane in both. Each service's health
check names only its own lanes (`queue_status --check --lanes`). A container that waits
for migrations only reads `/run/secrets`; only the one that applies them must write it.
Still not in a sandbox: chat on Claude starts the CLI on the web container's stream
thread, and every container still mounts the whole secrets volume, owner's database
password included.

**Migrations are applied once, under a lock** (`manage.py apply_migrations`). `migrate`
takes no lock of its own, so two containers starting together both applied the pending
list - which is what held the web container to one replica. The command holds a session
advisory lock on a connection of its own for the whole run; a second caller says it is
waiting, then finds nothing to do. It lifts the 10 s `statement_timeout` for its own
session (an index on a large table outlasts a request's limit) and keeps `lock_timeout`,
trying a migration again only on `lock_not_available` - each is one transaction on
PostgreSQL, so that undoes it whole. The entrypoint never calls `migrate` directly. The
scale overlay runs it as a one-shot `migrate` service (`restart: "no"`) that applies,
writes the signing key if there is none, runs `check --deploy` and exits; `app` and
`worker` start only after it exits cleanly, and the web container waits rather than
applies (`DIGITAL_BRAIN_MIGRATIONS=wait`), so `DIGITAL_BRAIN_WEB_REPLICAS` can be more
than one. The signing key is linked into place, so two writers still end up with one key.
**A migration must be one the previous release can run against**: the new schema is in
place while the old web containers still serve. Add a column nullable or with a
`db_default` - a Python `default` is dropped from the column once it is filled, so the old
release's inserts fail - and remove or rename only a release after the code stopped using
it. `test_migration_job.ExpandContractTests` refuses anything else after 0090 unless it is
named in `CONTRACTS`, the second half of such a pair.

**Every lane but maintenance runs in every process** (`document_worker.SHARED_LANES`).
Each step claims its own row before acting: a document by a conditional update, a job by
a row lock, a Code Factory run, triage, review or index by a guarded status change on
its job token, a poll (CI checks, a branch head) by moving its turn on. Work found by its
age - an index "indexing" ten minutes, a review "reading" fifteen - is touched while it
runs (`keepalive.KeepAlive`), so another process reads a stopped worker, never a slow
one, and a paid review is never paid for twice. Code Factory's lane claims only
`prepare_queued` (`prepare(queued=True)`): a run prepared or failed a moment ago is
finished, and a worker that loses a claim writes nothing to the run it lost.
`test_shared_lanes` stages each race. `run_worker --concurrency factory=2` gives a lane
more threads. **Maintenance is still held by one process** (`LaneKeeper`): a PostgreSQL
session advisory lock on a connection of its own, so `close_old_connections` between
steps never drops it, with a second holder standing by. Its sweeps are cheap and one is
enough. Adding a step to a shared lane means it claims first. **SQLite has no advisory
locks**: there every keeper holds, so never run `run_worker` beside a `serve.py` that runs
its own lanes on the same local database. A session lock does not survive a
transaction-mode pooler, so a worker reaches PostgreSQL directly.

A stop (`SIGTERM`) is honoured **between steps, never inside one**: a conversion or a paid
call that started is finished and recorded. Each holder writes `LaneHeartbeat`;
`manage.py queue_status [--json] [--check]` reports each lane's waiting work, oldest age and
heartbeat - counts and ages only, never a record - and the maintenance lane logs the same
figures every five minutes as `queue_status` events.

`clamd = "host:port"` (or an absolute socket path) scans through a running daemon instead
of a `clamscan` per file, which reloaded the whole signature database for every document.
Fail-closed rules are the same: anything the daemon does not answer `stream: OK` or
`… FOUND` to is unscanned, and signatures more than a week old (from the `VERSION` date)
are refused, as `--fail-if-cvd-older-than=7` refuses them for clamscan.

## Background work is asked for, not scanned for

`jobs.py` / `models.Job`. The graph lane used to compute every application's fingerprint
every two seconds and the connector lane read every connector every ten - a cost that grew
with applications, not with work. **A change now writes a `Job` in its own transaction**:
that is the outbox, so a rolled-back change takes its job with it and no event table is
drained. `triggers.py` asks for `graph_refresh` on anything in `graphs.fingerprint` - a
source saved or deleted, `KnowledgeEntryQuerySet.update`/`delete` on a `GRAPH_FIELDS` field
(querysets send no signal, so the queryset reports itself), graph generation settings, the
Knowledge switch - and `claim_generation` and Retry ask explicitly. `Job.application_id` is
deliberately not a foreign key: deleting an application deletes its sources, each asks for
a refresh, and a job referencing the row being deleted would fail the delete at commit.

**One waiting row per key** (a partial unique index): a burst folds into one job, earliest
due time wins, and `graph_refresh` waits five seconds so it can. `enqueue` folds with an
UPDATE, never a read, so a request racing a claim gets its own row rather than folding into
work already reading. A running job does not count, and `claim` never starts a key that is
running, so a change made mid-rebuild is rebuilt after it. Claims are `SELECT ... FOR
UPDATE SKIP LOCKED` on PostgreSQL and a conditional update on SQLite; an application holds
at most `TENANT_CAP` running jobs per lane. A lease (`LEASE`, renewed by a thread that
first waits a whole interval) is how a dead worker's job comes back: `jobs.maintain` in the
maintenance lane puts it back, or marks it dead past `max_attempts`. **Both kinds are one
attempt**: a failed rebuild was a failed paid call and the scan never repeated those, and
a half-finished import is not repeated by the queue - the schedule asks again. `Later` is
"not yet" (documents still converting) and uses no attempt.

Handlers decide who the work runs as from the thing itself - `run_scheduled` as the
connector's creator, `arm_auto_refresh` as whoever switched it on - re-checked as before.
The scan functions stay as one-shot catch-ups (`process_next_graph`,
`process_next_connector`); the lanes run `jobs.step_for(lane)`. **The reconcile pass**
(`graphs.reconcile_if_due`, every `RECONCILE_INTERVAL`, first as the lane starts) enqueues
every candidate application, for what changed without a trigger: a bulk feature `update`, an
organization reactivated, a restore. Connector scheduling (`connectors.schedule_due`) still
reads due connectors, but only to enqueue; the job re-checks due-ness, since a button press
may have run it meanwhile. `queue_status` counts graph work from jobs, connectors from
due-ness (jobs are written by the lane itself, so a stopped lane would read idle), and each
lane's `dead` jobs.

Intake takes turns (`document_worker.fairly`): the oldest document from an application with
nothing converting, else the oldest, so one large import cannot hold everyone's single upload.

## Passages are rows

`KnowledgeChunk` (`chunks.py`) holds each **active** source's passages - the same
`PASSAGE_LENGTH`/`PASSAGE_STEP` windows `retrieve` cuts - written in the transaction that
writes the source: `triggers.entry_saved` and `KnowledgeEntryQuerySet.update` on a
`CHUNK_FIELDS` field call `chunks.sync`, a retirement clears them, a new title or text cuts
them again. They are a copy, never evidence: citations still verify against
`KnowledgeEntry.content`, and a passage counts only while its source's `chunked_digest`
equals its `digest`. Restricted sources keep theirs (lifting is common) and are excluded at
query time like every other path. `retrieve(candidates="chunks")` takes candidates from them
- PostgreSQL's GIN index over the stored `CHUNK_TEXT` column (migration 0088), a substring scan on SQLite -
with the source text deferred, then ranks exactly as the source path does.
`retrieval_candidates` (config) picks the path. The default is **"auto"**, since the
scorecard's Passage answers matched its Keyword answers exactly (14/16, MRR 0.674): an
application answers from passages only while every active source has current ones
(`chunks.complete`), and from sources otherwise - a source with none current is not among
the passages, so answering from them mid-backfill would miss it rather than rank it lower.
"sources" and "chunks" force one path; the maintenance lane backfills a batch at a
time and `manage.py chunk_sources --check` says when it is done. Counting deletions: a source's delete cascades to its passages, so count by model
(`QuerySet.delete()[1]`), never the total.

## Graph versions are rows too

`GraphElement` (`graph_store.py`) holds each source, node and edge of a version **once
for every version it is in**: `first_number`, and `last_number` empty while it is still
current. Version N is the rows with `first_number <= N` and `last_number` empty or
`>= N`, **in `key` order, and reads back exactly as its JSON** - same lists, same order.
Order is kept because ranking breaks ties by position and the one-hop expansion walks
edges in list order. A key is a sparse integer; a row stays open only while it keeps its
place among the rows that stay (`increasing`), new rows take keys in the gaps
(`placed`), and a list with no gap left is written afresh - one full write, never a wrong
order. `graphs.rebuild` calls `record` in the transaction that saves the version, so a
rebuild after one source changes closes that source's rows and adds its new ones - 1.4%
of a CarePath version, where the JSON was written whole again.

Edges also carry what ranking reads, derived exactly as `graph_ai.prepare` derives it:
their text, `ranking.terms` of it, the references they name, and whether `walk` may follow
them - all part of the digest, so a node relabelled under an unchanged edge makes a new
row rather than stale terms. PostgreSQL holds the terms and references as generated
arrays with GIN indexes (migration 0089); SQLite matches words in the padded text.

**`graph_candidates = "rows"` (the default) answers graph questions from them**
(`RowsGraph`), and `"memory"` from the prepared JSON; a version not yet recorded as rows
answers from memory either way. `citations_from` asks one interface of
either (`graph_ai.InMemory`): the edges holding a question's terms, ranked by the same
`rank_covering` with the whole version's size and average length (`BM25.population`,
`graph_store.statistics`) - every edge holding a query term is a candidate, so each term's
frequency is already right; the walk and the expansion read the rows touching the nodes
they reach; cross-references read the rows naming the question's references. Verification
is the one path both share. **The scorecard holds them to identical citations** on every
benchmark question and seeded ticket (Graph storage), and measures the cost: 21 ms
against 10 ms on CarePath, where memory is a warm cache - rows do not grow with the
graph, memory does, which is why rows became the default. A question with an embedding
still ranks in memory, where vectors are. Keep that metric at 100%.

The JSON is still written; rows are also read for which sources a version read
(`recorded_sources`, for drift on every chat page and the publish check), which is why
`published_revision` defers `data`. Versions are recorded in number order under a lock
on the application row; one older than the newest recorded is left to its JSON. The
maintenance lane records older versions a few at a time (`record_step`), and
`manage.py graph_store [--check]` does it all or fails on anything unrecorded or not
reading back exactly. **Old versions keep their lists only as rows**
(`graph_store.slim`, the maintenance lane's `slim_step`): once a recorded version reads
back exactly, its sources, nodes and edges leave its JSON and `data["slimmed"]` says so -
never the newest `KEEP_WHOLE` or the latest published. `GraphRevision.from_db` puts them
back on load, so every reader of `data` sees the whole version; a read that skips the
model (`values_list("data")`, `data__sources`) must inflate it too, as `graph_ai.prepared`
and `source_library.graph_usage` do.

**A graph is built a source at a time and takes every source** (`graphs.fragment`). It
used to take the oldest `MAX_SOURCES` (100) and cap the whole graph at 6,000 nodes, so a
large library answered from its oldest hundred documents and whether a row was included
depended on what came before it. Each source's structure is now built from that source
alone - `MAX_NODES_PER_SOURCE` and `MAX_RECORDS_PER_SOURCE` bound one source, never the
graph - and kept per process (`FRAGMENT_CACHE`) by application, entry, title and digest,
so a rebuild after one change reads one source: 1.9 ms for CarePath warm, 45 ms cold.
`build_graph` merges the fragments in source order, nodes in first-seen order with the
first source's copy of a shared value, so its output is **exactly** the single pass it
replaced (held equal on CarePath; `test_graph_per_source`), and it copies what it merges,
so a caller changing the graph never changes the cache. AI entities are capped per source
the same way (`source full`). The fingerprint's limits changed, so every graph rebuilt
once, as a draft. The source check is the one reader still bounded (`source_map.MAX_MAPPED`,
100): it compares every source with every other, and says when it read only the oldest.
What still grows with the library is a build holding every answerable source's text at
once, which AI carry-forward and quote checks read.

## Verified against a live CLI

`agent_runtime/claude_runtime.py:TOOL_PREFIX` — **confirmed**. A live run reported
`mcp_servers: [{'name': 'brain', 'status': 'connected'}]` and a tool list of exactly
`['mcp__brain__fetch_source', 'mcp__brain__search_knowledge']`. The count of 2 also
confirms `tools=[]` disables every built-in tool, MCP tools being additive.

Do not iterate the `query()` stream only until `ResultMessage` and then `break`: that
strands the CLI subprocess, which on Windows keeps a handle on the working directory and
makes `TemporaryDirectory` cleanup fail with WinError 32. Drain to the end.

A live run also confirmed the rest of the Claude path end to end:
`include_partial_messages=True` yields real `StreamEvent` partials (120 events, 33 text
deltas, first delta ~8.7s), the model calls `mcp__brain__search_knowledge` and our handler
runs, and the tool's citations survive verification. Token streaming and the agentic tool
loop both work against the real CLI.

## Known unverified

No test makes a real provider call. OpenAI is exercised through the real Agents SDK over a
mocked HTTP transport; Claude's `query` is replaced outright, so the bundled CLI
subprocess and the environment scrub are never executed in CI. The OpenAI streaming
runtime has not been run against a live account at all — only its non-streaming sibling
has, through the mocked transport. The Claude Messages adapter is the real `anthropic` SDK
over a mocked transport and has not been run against a live API key.

Run live, read-only, on 2026-10-19: a scheduled changes-since read through
`jira_records` against the CarePathDev Jira (7 issues full, 1 inside the overlap, the
relative `updated >= "-Nm"` clause accepted) and `github_records` anonymously against a
public repository (148 issues over several pages, pull requests dropped; a read since a
mark returned only later issues, oldest first, all within the full read). The stream
load test has been run only on the development machine, not production hardware.

## Naming

`platform_core/agent_runtime/` is deliberately not called `agents` — the OpenAI Agents SDK
owns the top-level `agents` module and is imported from inside this package.

`platform_core/models/` is a package by sub-domain - `accounts` (users, the organization
tree, applications, grants, the audit trail), `ai`, `knowledge`, `operations`,
`code_graph`, `chat`, `factory`, `runtime` - imported in that order, each needing only
those before it; a later one refers to an earlier one's model by its class, never the
other way round (use a string reference instead). Import every model from
`platform_core.models`, which re-exports all of them; a new model goes in its area's
module and in `__init__`. Moving a model between them changes no migration.

Code Factory after approval is four modules: `code_factory_build` (the gates, the
preparation queue, `prepare`, `publish`, the stall and checks sweeps, the refresh),
`code_factory_agents` (each agent, and delivery to GitHub - checks no grant, so call it
only behind `gate`), `code_factory_local` (delivery into a folder, its tests) and
`code_factory_picture`. The agents module imports nothing from the build module. A test
that patches an agent the build module calls - `run_work_order`, `run_implementation` -
patches it on `code_factory_build`, where `prepare` looks it up; one that patches what an
agent calls patches it on `code_factory_agents`.

Before approval it is three: `code_factory` (the gates, phases, repository pinning,
`evidence_for`, `start_run`, `retry_analysis`), `code_factory_evidence` (code and ticket
passages, labelled by system; reads only) and `code_factory_analysis` (triage, analysis,
design, `ask`, `execute`, `build_plan`, `process_next_run`), which imports from both and
neither imports it. Patch `ask` and `collect_items` on `code_factory_analysis`.

`workbench` keeps writing knowledge, retrieval, conversation storage and
`answer_question`; `knowledge_views` holds the Knowledge screens and `chat_views` the chat
screens, submissions and the streamed answer (`answer_worker`, `run_answer`). Both import
from `workbench` and it imports neither. A test that posts to the chat view and patches
`answer_question` patches it on `chat_views`, where the view looks it up; one that
patches what `answer_question` calls (`lexical_citations`) patches it on `workbench`.
