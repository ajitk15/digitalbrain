# Digital Brain — working notes

Django 5.2 / Python 3.12, server-rendered, no JavaScript build step. WSGI under
waitress. SQLite locally, PostgreSQL in production.

## Verify before calling anything done

All four must be clean:

```bash
.venv/Scripts/ruff.exe check digitalbrain platform_core scripts manage.py
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe manage.py test
.venv/Scripts/python.exe scripts/production_preflight.py
```

After changing anything under `static/`, run `manage.py collectstatic` **and restart the
server**. Templates resolve hashed filenames through the staticfiles manifest, which the
running process caches at boot — otherwise you will be testing the previous asset and
wondering why your change does nothing.

## Invariants — do not weaken these without a deliberate decision

**Deny by default.** Application access is re-checked on every request and on every tool
call. A user with no grant gets `Http404` from `policy.application_for`, not `403` — the
platform does not confirm that an application exists. Platform administration does not
imply application access.

**Scope is closed over, never model-supplied.** Agent tools in
`platform_core/agent_runtime/tools.py` are bound to one application and one user before a
run starts. Model arguments may only narrow an already-scoped queryset. A foreign id must
read as "not found", never raise and never leak.

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
only when all three hold: `prune_missing` is ticked, the result was not at
`MAX_RECORDS` (a full page is a page, not an answer), and no other enabled connector
shares the same `Kind.namespace` (two connectors onto one Jira with different
filters would otherwise retire each other's imports). Retiring is `active=False`,
the same supersede an updated record performs - nothing is deleted. Leave it
default-off: under `updated >= -30d`, absence means the ticket is old.

`graph/search/` and the MCP server call no model. `utility/api_chat.py` does - it is the
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
address so DNS cannot change the answer underneath it. http/https only, no redirects
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

**Graphify is used for its deterministic parts only.** `code_graph_graphify.py`
calls `graphify.extract.extract` (Tree-sitter AST, cross-file calls and
inheritance) on text the clone already read, in a scratch directory of its own;
`graph_quality.themes` uses its Leiden clustering and hub labels. Its semantic
pass (`graphify.llm`) must never be called: it spends a model under Graphify's
own key, which bypasses per-application credentials and `AIUsage`, and its
edges carry no quote for `graph_ai` to verify. A Graphify failure degrades a
snapshot to partial, never to no snapshot.

**SharePoint reads as the application, not as the user.** `sharepoint.py` uses
app-only client credentials, so the app registration's grant is the boundary. Under
`Sites.Read.All` an importing user can obtain documents they could not open
themselves - a deliberate deployment choice, stated in the Sources panel. Do not
quietly widen it, and prefer `Sites.Selected` when asked; it needs no code change.

**A link is just a document.** `link_sources.py` records a pending `Document` and the
worker downloads it; conversion never learns URLs exist and MarkItDown is never handed
one. Keep it that way - the offline conversion subprocess patches `socket.connect` to
raise, and that guarantee is worth more than the convenience of fetching inside it.

**Usage is never silently zero.** A missing or malformed token count is an error, not a
free request — providers bill for calls whose usage we failed to parse. See
`agent_runtime/usage.py`. Multi-turn answers record one receipt per model turn, each keyed
on its own provider request ID.

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
is off unless written down, which keeps two-person review the default, but
`load_config` now **accepts** it under `mode = "production"` where it used to
refuse it. It is the one flag of the three that does; `claude_use_host_login` and
`allow_demo_reset` are still refused there, and the difference is intentional —
those spend somebody else's money and delete somebody else's work, while this one
only records a weaker fact about a change. It is not silent where it counts:
`ChangePlan` stores the approver, so a self-approved plan says so in the audit
record. The review screens used to carry a warning too; **that was removed,
deliberately**, because on a one-person instance it told the only reviewer the
same thing on every plan. The audit record is the part that must stay. `deploy/entrypoint.sh`
renders it **on** for the container image, because that image is deployed
single-operator; `DIGITAL_BRAIN_SELF_APPROVAL=0` turns it off where two people
really do review every change.

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
free; **AI enrichment runs only for a requested run**, carried on
`KnowledgeGraph.requested_model/_provider/_by` and cleared once consumed, so a rebuild can
never repeat a paid call. Every run saves a `GraphRevision` as a draft.

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

The stream registry in `agent_runtime/streaming.py` is **process-local**. Correct for the
single managed worker process; multiple processes would need a shared cancellation channel
before Stop could be trusted.

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
has, through the mocked transport.

## Naming

`platform_core/agent_runtime/` is deliberately not called `agents` — the OpenAI Agents SDK
owns the top-level `agents` module and is imported from inside this package.
