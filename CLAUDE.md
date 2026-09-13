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

**Secrets are file-mounted only.** `digitalbrain/configuration.py:read_secret`, one file
per provider per application, owner-only permissions on POSIX. Never environment
variables, never the database, never a form field. `.env` accepts only
`SITE_ADMIN_USER_ID`.

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
forces it off there anyway, a mounted credential always takes precedence over it,
and the chat page states plainly when answers are being billed to the machine's own
login. A missing secret with the setting off is still an error — it must never
quietly become "spend the operator's account".

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

**The CSP is pinned by a test.** `platform_core/middleware.py` sets it and
`test_security.py` asserts the exact string. `connect-src 'self'` exists for the chat
EventSource. No `unsafe-inline`, no external origins.

## Graph lifecycle

Generate → draft → publish. The background worker keeps the **structural** graph current for
free; **AI enrichment runs only for a requested run**, carried on
`KnowledgeGraph.requested_model/_provider/_by` and cleared once consumed, so a rebuild can
never repeat a paid call. Every run saves a `GraphRevision` as a draft.

`graph_snapshot(app_id, version=None)` resolves to the **latest published** revision, never
the working graph. Chat and Code Factory both go through it. No published revision is an
error, not a silent fallback to a draft. A conversation may still pin a specific version.

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
