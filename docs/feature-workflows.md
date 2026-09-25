# Application features and setup

Navigation uses an organization tree on the left: organization, portfolio, product, application.
Only explicitly granted active applications are shown. Opening an application expands its
ancestors and highlights it. Functional tools live in the top menus; the tree remains available
across pages. The mobile tree has its own bounded scroll area.

## ServiceOps incident triage: first read-only slice

The ServiceOps application tab lists up to 100 recent active incident sources imported from a
ServiceNow incident table or Jira issues typed Incident. Selecting one shows its structured
context, up to five similar resolved incidents, and verified passages from the published graph.
Similarity uses shared CI, service, environment and symptom terms; it is not a root-cause or
confidence score. The page calls no model and makes no changes to a ticket or live service.

ServiceNow incident imports now place state, priority, impact, service, CI, environment, assignment
group, times, close code and close notes inside the immutable source body. Other ServiceNow tables
keep their existing body format. Older imported incidents without these fields remain readable,
but matching has less context. The precedent search currently checks at most 500 recent candidate
records and makes no completeness or recall claim. Historical backfill, change correlation,
calibrated confidence, outcome feedback and automated triage remain in the
[ServiceOps architecture proposal](diagrams/index.html#serviceops).

All implemented feature switches default to enabled. Global and per-application switches remain
enforced on server routes. Platform administration does not grant application knowledge access.

## Knowledge and documents

Owners and contributors can add immutable plain-text knowledge sources. Sources can be searched,
read, or archived. Archiving excludes them from new retrieval without deleting approval evidence.

All file types are accepted. Select up to 20 documents per batch, with a combined 20 MiB limit.
Original bytes remain private and unchanged.

### Adding sources from a link

The Sources rail also accepts a pasted link, and works out what it points at:

| Pasted | Imported |
| --- | --- |
| Any web page or document URL | that one page or file |
| `github.com/owner/repo` | the repository README |
| a `/blob/` or `raw.githubusercontent.com` link | that one file |
| a `/tree/` directory link | the documentation files under it, up to 25 |
| a SharePoint or OneDrive file link | that document |
| a SharePoint document library folder | the documents in it, up to 25 |

A link becomes an ordinary document, so quarantine storage, scanning, offline MarkItDown
conversion, the immutable knowledge source, graph regeneration and deletion all behave exactly as
they do for an upload. **MarkItDown is still never handed a URL** — the bytes are downloaded
first and converted from disk.

Submitting does not download anything during the request: each file is recorded as **Waiting to
download**, and the background worker fetches them. The rail shows every source's state —
waiting, downloading, queued, converting, ready or failed — with where it came from and the
reason for any failure, so a slow or dead host delays only itself.

### SharePoint and OneDrive

Links are resolved through Microsoft Graph's *shares* endpoint rather than by parsing SharePoint
URLs, so whatever a user copies out of the browser works: a document library path, a
`/:w:/r/...Doc.aspx?sourcedoc=` viewer link, a personal OneDrive sharing link, or a folder.

Two things must be in place. The deployment names the app registration in TOML, which is
non-secret:

```toml
sharepoint_tenant = 'contoso.onmicrosoft.com'
sharepoint_client_id = '00000000-0000-0000-0000-000000000000'
```

and each application that may import mounts the client secret as
`sharepoint_APPLICATION_UUID` in the secret directory. Mounting that secret is the
per-application switch: an application without one cannot reach SharePoint even though the
deployment is configured.

**Read what this means for permissions.** Authentication is app-only client credentials, so the
permission granted to the app registration is the boundary — not the permission of the person
pasting the link. Under `Sites.Read.All` that is every site in the tenant, so a user who can
import is able to obtain a document they could not open in SharePoint themselves. The Sources
panel states this where links are pasted rather than only here. `Sites.Selected` narrows the app
to sites an administrator grants individually and requires no change to this platform, because
the restriction is applied by Entra ID; it is the safer choice where the content is sensitive.

Imports are audited with the resolved item, and only owners and contributors can import at all.
Access tokens are held in memory until shortly before they expire and are never written down.
Graph requests go through the same hardened client as any other host, so the address, redirect,
size and timeout rules all still apply. Document downloads read a fresh pre-authenticated
address at download time rather than following Graph's redirect, which also means a queued item
cannot expire before it is fetched.

**Network policy.** Fetching a link means this server makes a request to an address the user
chose, so the retriever resolves the hostname first and refuses loopback, private, link-local
(including the `169.254.169.254` cloud metadata endpoint), multicast and reserved addresses. The
connection is then pinned to the address that was validated, so a second DNS answer cannot
redirect it. Only `http` and `https` are accepted, redirects are never followed, responses are
capped at 8 MB and 20 seconds, and no credential is ever attached except the application's own
mounted GitHub token on GitHub requests.

Internal hosts can be permitted individually in the deployment TOML, which is what makes an
internal wiki importable without opening the private network:

```toml
fetch_allow_hosts = ['wiki.internal', 'docs.corp.example']
```

Wildcards are refused: an allow-list that allows everything is not one. A private repository
imports when the application's `github_APPLICATION_UUID` token is mounted; public repositories
need no token and use the shared unauthenticated rate limit. Upload queues automatic Microsoft MarkItDown
conversion whenever Knowledge is enabled; no antivirus scan is required in the current local setup.

The managed server hosts a single conversion worker backed by document rows in the database.
It claims one queued document at a time and invokes an offline converter subprocess. The page
updates conversion statuses automatically, unless you have started editing a form or selecting files.
Queued work survives restarts; interrupted conversions become eligible for retry after five minutes.
Users can retry failed conversions on the document page. Current access and feature flags are rechecked.

Successfully converted documents display **Ready for graph generation**. Converted Markdown is stored
as an application-scoped Knowledge source with its SHA-256 digest. The document page provides
**Download .md** and **Download graph input**. The JSON graph input contains Markdown, converter
version, organization/application/document IDs and original/Markdown fingerprints. The local worker then builds a structural graph automatically. Open **Knowledge → Graph / Quality** to inspect it.

MarkItDown document extras for PDF, DOCX, XLSX, XLS, PPTX and Outlook are installed. Its built-in
text/HTML/CSV/JSON/XML conversion is also available. Unsupported, encrypted, image-only or malformed
files can fail conversion; the error is shown and the original remains available. No API keys, LLMs,
plugins or network connections are used for conversion. Markdown is displayed as escaped text.

Conversion has a 45-second subprocess timeout and a 1,000,000-character Markdown output limit.
ZIP-based input is bounded to 2,000 entries / 30 MB declared expansion; PDF is bounded to 200 pages.
The worker is a local implementation, not a hardened production parser sandbox. Production intake
and processing remain blocked pending isolated workers and application-specific object storage.

Optional scanning can be reinstated with top-level non-secret TOML settings and a server restart:

```toml
scan_documents = true
scanner = 'C:\Program Files\ClamAV\clamscan.exe'
```

When enabled, ClamAV errors, detections or stale signatures prevent conversion. The default local
setting is false; production cannot disable its scan requirement through this option.

**Delete** opens a confirmation page. Application owners may delete any application document;
contributors may delete their own uploads. Deletion removes the original and its Knowledge source,
hides the document from normal views and excludes it from new graph inputs. Audit metadata and
previous chat excerpts remain. In-flight conversion cannot restore a deleted document. A file locked
by an active converter may require retrying deletion after conversion finishes.

Reference: [Microsoft MarkItDown](https://github.com/microsoft/markitdown).

## AI providers, models and costs

Application owners configure five independent tasks under **AI settings**: **Chat conversation**,
**Graph generation**, **Graph retrieval**, **Conversation titles** and **Code Factory drafting**.
Each has an enable switch, a grouped OpenAI/Claude model selector, custom provider/model fields
and contracted USD input/output rates. Existing configuration is preserved by the migrations, and
new tasks start disabled.

Each task is priced and enabled on its own because each is a separate billable call. In
particular, conversation titles and plan drafting are never a silent surcharge on chat: if their
task is not configured, the feature simply does not run.

**Graph generation deserves a stronger model than the others.** Extraction must find
relationships, quote them exactly and name both entities, and a small fast model tends to return
few relationships or ones that fail quote verification. The settings page says so next to that
task. Chat can stay on a cheaper model.
The model suggestions were checked against provider documentation on 2026-09-12; account access
is not verified by the catalog. Custom IDs support models not listed there.

Mount credentials in the configured secret directory as `openai_APPLICATION_UUID` and
`claude_APPLICATION_UUID`. Both providers use their own Agent SDK. No credentials are stored
in .env, database, form fields or logs. Restrict secret files to the service identity.

### Which Claude credential to mount

The Claude Agent SDK runs the bundled Claude Code CLI, and that CLI resolves credentials in a
fixed order: `ANTHROPIC_API_KEY`, then `CLAUDE_CODE_OAUTH_TOKEN`, then an interactive login stored
in `CLAUDE_CONFIG_DIR`. This platform never uses the third. The sandbox points `CLAUDE_CONFIG_DIR`,
`HOME` and `USERPROFILE` at an empty directory and blanks the environment, so a run cannot inherit
a login belonging to another application or to the person who started the server. That is what
keeps per-application credentials, budgets and cost attribution meaningful.

Two credentials are therefore accepted in the same per-application file, and its contents decide
which variable the CLI is given:

| Credential | Looks like | Bills | Notes |
| --- | --- | --- | --- |
| API key | `sk-ant-api…` | Console account | Straightforward for server deployments. |
| OAuth token | `sk-ant-oat…` | Claude subscription | Generate with `claude setup-token`. Works headless; no interactive sign-in on the server. |

An OAuth token is not a drop-in for an API key at the protocol level — the CLI reads them from
different variables — but it is a drop-in here, because the platform detects the kind and sets the
right one. Isolation, rotation and file permissions are identical for both.

### Development without any credential

For local work only, `config/local.toml` accepts `claude_use_host_login = true`. When an
application has no mounted Claude credential, the runtime then stops redirecting
`CLAUDE_CONFIG_DIR` and lets the CLI use the Claude Code login already on the machine, so chat
works with no secret file at all.

Its limits are deliberate. Configuration **refuses** the setting when `mode = "production"`
rather than ignoring it, so a production deployment cannot believe it is using per-application
credentials while actually sharing one identity. A mounted credential always wins over it. With
the setting off, a missing secret stays an error. **AI settings** says plainly when answers are
being billed to the machine's own login rather than to the application, and shows a
*Development login* badge on the Credentials card.

Chat does not say it. That page is read by everyone with chat access; AI settings is owner-only,
so the one person who sees the notice is the one who configured it. This is the trade the
paragraph below makes explicit.

This is a convenience for one developer on one machine. A deployment serving other people should
mount per-application credentials, which is what the per-application pricing and usage receipts
are built around.

OpenAI uses Responses for the listed GPT-6/GPT-5.6 models and Chat Completions for existing/custom
older models. Each request owns a client and event loop. Provider storage and SDK tracing are
disabled; no handoffs, no application/HTTP retries or redirects, 30-second HTTP timeout. Model
selection does not change the application's history.

Claude uses the SDK's bundled CLI, an ephemeral settings directory and empty workspace,
application-specific environment credentials, and no inherited user/project settings or sessions.
Built-in tools, plugins and skills stay disabled and session persistence and nonessential
telemetry are off. The application does not retry Claude calls; provider and runtime internal
behaviour remains subject to the SDK. The managed stop script recognizes only the bundled CLI
descendants of the verified application worker.

### Knowledge tools and multi-turn answers

Chat answers are agentic. Both SDKs are given two tools over this application's knowledge:
`search_knowledge` (lexical passage search) and `fetch_source` (a bounded 4,000-character window
of one source). OpenAI receives them as Agents SDK function tools; Claude receives them from an
in-process SDK MCP server, which runs inside the application process over an in-memory transport
and adds no filesystem or network reach. Claude's permission mode remains deny-by-default with an
explicit allow-list, so an unrecognized tool name is refused rather than run.

Each tool is bound to one application and one user before the run starts. **No tool reads an
application or user identifier from model-supplied arguments**; model arguments can only narrow a
queryset that is already scoped, so an identifier belonging to another application reads as "not
found". Access and the Knowledge feature switch are re-checked on every tool call, so a grant
revoked mid-answer stops the next one.

Runs are bounded to four model turns and eight tool calls; past that, tools return a budget
message instead of running. Citations come from what the tools actually returned and are
re-verified against live sources — still active, digest unchanged, excerpt still present — before
anything is stored or displayed, then capped at eight. A citation the model invents resolves to
nothing. Graph generation and graph retrieval remain single-turn and tool-free, because their
output contract is strict JSON.

**A multi-turn answer produces one usage receipt per model turn**, each keyed on its own provider
request ID, so the AI costs page now shows several rows for one question rather than one. This
keeps duplicate detection meaningful; collapsing turns into a single row would not.

Chat conversations are private to each user and application. **New conversation** starts an
independent thread; history resumes it. Each exchange is stored as two messages, a question and
an answer, ordered by an explicit sequence (60 messages per page, 20 conversations per history
page). Existing entries remain in **Earlier chat history**. Recent context is capped at eight
exchanges, with prior answers capped at 4,000 characters. Exchanges referencing changed or
removed sources are excluded from new context; the saved history remains.

**Answer mode belongs to the conversation, not the message.** It is chosen when a conversation
is started and changed from the conversation header. Sending a message cannot change it, so a
thread cannot be moved onto a paid provider by accident or by a crafted request. Each stored
answer also records the mode that actually produced it, so the transcript stays truthful after
the conversation's mode is changed. Missing setup or keys produce a visible setup message and
preserve the draft, never a silent source-search fallback.

**Sources** is bounded local lexical search with no LLM. **AI** uses
the chat task model, seeded with up to five matching passages, and may call the knowledge tools
described below. **Graph answer** uses the graph retrieval task model: lexical matching of saved
graph endpoints/relations plus one-hop neighbours, with up to eight verified source citations. It
refuses stale or unavailable graphs. This is bounded graph retrieval, not embedding search. AI
conversation also answers greetings, clarifications and general explanations without document
matches; it must not invent application facts or source citations. Graph answers still avoid paid
calls when graph evidence is absent.

Answers support escaped paragraphs, bold, lists, tables and code blocks; model links are not
made active. Sources are expandable. Enter sends and Shift+Enter adds a line; provider errors
preserve drafts.

### Streaming, and what happens without JavaScript

AI and graph answers stream token by token over server-sent events. The question and an empty
answer are saved before the provider is contacted, so a dropped connection still leaves a record
of what was asked. **Stop** ends the visible answer and stores what had arrived, marked stopped;
the call is still drained far enough to record its usage, because a provider bills for work
whether or not the reader is still watching. **Regenerate** discards an answer and re-asks the
same question; **edit** replaces a question and discards everything after it.

Streaming is progressive enhancement. Every control is an ordinary form that posts and works with
JavaScript disabled, landing on the same synchronous path. The browser never renders model
Markdown itself: streamed text is inserted as plain text, and the finished message is replaced by
a server-rendered fragment produced by the same escaping filter used everywhere else.

The stream registry is process-local, which is correct for the single managed worker process the
lifecycle scripts start. Running several worker processes would need a shared cancellation
channel before **Stop** could be relied on. Each active stream holds one server thread.

Successful SDK replies record provider, configured model, task, input/output tokens and estimated
USD cost. Original usage counts must exist. Claude cached read/write input is included at the
configured ordinary input rate. Estimates can differ from invoices; there is no cache discount
reconciliation. OpenAI transport IDs and generated local Claude run IDs identify receipts.
Known successful calls are recorded before semantic graph output validation, even if invalid
relationships are rejected. Missing/uncertain responses may still be billed externally.
Budget reservation, crash reconciliation and provider invoice reconciliation remain pending.

References: [OpenAI models](https://developers.openai.com/api/docs/models),
[OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/models),
[Claude models](https://platform.claude.com/docs/en/models/overview),
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python).

## AI graph generation

The existing deterministic Markdown structural graph is always the base, and the background worker
keeps it current as sources change. That rebuild is free: **AI enrichment never runs on its own.**

Open **Knowledge → Graph → Generate graph** (or **Regenerate graph**) to run one. The form asks
which model to use, including **Structural only**, which uses no AI and cannot be charged. A model
chosen there applies to that run only and does not change the application's saved graph settings;
the model used is recorded on the snapshot it produces, and shown in the Versions table.

### Draft and publish

Every generated version is saved as a **draft**. **Chat and Code Factory answer from the latest
published version only** — a rebuild, automatic or manual, cannot change answers until someone
publishes it. Publish from the Versions tab; the newest published version wins, and the banner
there always names the version currently answering.

Publishing is refused when a version's source evidence has since changed, because that snapshot
can no longer be verified. If nothing is published, graph answers in Chat say so rather than
quietly falling back to a draft. Upgrading an existing installation publishes whichever version
was already live, so behaviour does not change on upgrade.

A published snapshot is pinned, so archiving a cited source no longer makes graph answers error:
each affected relationship simply fails verification and the answer has nothing to cite.

When a run is requested, enrichment happens under the configuring owner's current application
access. The chosen provider/model extracts at most 60 relationships
from at most 25 sources / 24,000 characters each. An exact source quote and both named entities
must be present in the supplied text before a relationship is accepted. Unsupported results are
excluded. Entity identity is source-local; matching labels do not silently merge distinct entities.
Semantic meaning is not independently verified and quality explicitly requests human review.

Accepted AI entities are orange in the graph, with source-backed connections and revision metadata
showing provider/model, accepted/rejected relationships and scope limits. A source or selected graph
configuration change creates a new version. Source deletion immediately withholds stale graphs.
Failed or interrupted calls are not retried every worker tick. Owners/contributors can retry under
Knowledge; interrupted runs must be at least two minutes old. Retrying may incur provider charges.

## Graph retrieval API and MCP

The published knowledge graph can be queried from outside the interface, at
`/api/v1/applications/<id>/graph/search/` for REST and `/api/v1/applications/<id>/mcp/`
for MCP. Both answer the same question and return the same verified evidence chat uses:
matching relationships, each with its source document and the exact quoted text supporting it,
re-verified against the live source at request time.

**Retrieval only.** Neither surface calls a model, so no caller can spend the application's
provider budget through them. What comes back is evidence, for the caller to use with whatever
model they already run.

**Versions are explicit.** A request may pin `version`; without one the latest **published**
revision answers, never a draft. Every response states the version that produced it, so a caller
can pin it afterwards for reproducible results.

### Authorization

A token is issued under **Settings → API access** and **acts as the person who issued it, inside
one application**. This is deliberate: there is no token role and no second permission table.
Every request re-runs the same checks a browser session runs, so revoking that user's grant,
disabling their account or turning off the Knowledge feature closes the token immediately, and a
token issued for one application returns 401 on any other.

Only a SHA-256 digest of the secret is stored. The value is shown once at creation and cannot be
recovered; the public prefix locates the row and the digest is then compared in constant time, so
an unknown prefix and a wrong secret cost the same. Tokens carry an optional expiry, can be
revoked, and record when they were last used. Each is limited to 120 requests per minute.

Session cookies are never accepted on these endpoints and tokens are never accepted on the
browser ones — mixing the two is how a token endpoint becomes reachable through an authenticated
browser. The source digest is never returned: it is an integrity token used to verify a citation,
not something a caller needs. Every query is audited with the token name, version and result
count.

The MCP server implements `initialize`, `tools/list` and `tools/call` as JSON-RPC over HTTP and
exposes one tool, `search_knowledge_graph`, taking a question and an optional version. A tool
failure comes back as a readable result with `isError` rather than a protocol error, so a model
can react to it.

## Code Factory plans

Contributors and owners submit proposed changes and validation/rollback criteria. Each immutable
plan pins the current source IDs and SHA-256 hashes. A different user with explicit approval
permission must approve or reject it and provide a review note. Approval fails if pinned sources
were archived or changed. Decisions are atomic and cannot be applied twice.

Approved plans can be exported as JSON for implementation handoff. Approving a plan still
executes nothing: writing to a repository is a second decision, gated on `repository_confirmed`
and on a `github_write_APPLICATION_UUID` credential that is a different file from the read-only
connector token.

Once both gates are passed, delivery runs implementation, then a verification pass, then opens a
**draft** pull request on a `digital-brain/` branch. What it will not do: create or delete a
file - only a file it first read at the base branch may be replaced, and a path that did not come
back from a read is refused rather than sanitised (20 files, 400 KB each); run a build or a test
suite, because this platform holds knowledge about the application, not a checkout of it; or
merge anything. Merge-policy enforcement stays where it already is.

When **Code Factory drafting** is configured, owners and contributors can describe a requirement
and have a draft written into the proposal form. The draft is exactly that: it pre-fills the form
for a human to edit and submit, and creates nothing by itself. Every quote it cites is verified
against live sources the same way graph relationships are, and unverifiable ones are dropped and
counted. Source pinning, independent approval by a different user, and the rule that approval
never runs code are all unchanged.

## Chat API

`POST /api/v1/applications/<id>/chat/` answers a question with the application's own model, so a
team can build their own chat client - a plain HTML page, a React app - against the same evidence
the built-in Chat uses. It is the **only** API surface that calls a model.

The address, an example request and the current on/off state are on **Settings > API access**,
beside the REST and MCP endpoints. The code is `platform_core/utility/api_chat.py`: that package
holds surfaces built *on* the platform, and nothing in `platform_core` imports from it.

**It is off until an owner turns it on.** Settings > Features > *Chat API*. Every application
that existed before this shipped has an explicit disabled row, and a new one starts unticked, so
no provider budget starts being spent because a release went out. Authorization is the same
bearer token as the retrieval endpoints, subject to the same 120 requests per minute.

Ask a question:

```json
POST /api/v1/applications/<id>/chat/
Authorization: Bearer <token>

{ "question": "How does Alpha reach Beta?", "mode": "ai" }
```

The reply carries the answer, the conversation to continue, and the citations that survived
verification:

```json
{
  "conversation_id": "…",
  "message_id": "…",
  "answer": "…",
  "mode": "ai",
  "citations": [{ "source_id": "…", "source_title": "…", "evidence": "…" }]
}
```

Send `conversation_id` back to ask a follow-up. History, the eight-turn context window and Chat
history retention all apply exactly as they do in the browser, and the thread keeps the mode it
started in - a later request cannot move it onto a paid mode.

`mode` is `ai` (default), `search` for lexical excerpts with no model call, or `graph` to answer
from the published graph. A conversation belongs to the user who issued the token; another
user's conversation reads as **not found**, never as forbidden.

### Streaming

Streaming applies to `ai` and `graph` only. `search` calls no model and so has nothing to
stream; asking for it there simply returns the answer synchronously, exactly as the browser
does.

Send `"stream": true` and the POST returns `202` with a `stream_url` instead of an answer:

```json
{ "conversation_id": "…", "message_id": "…", "stream_url": "/api/v1/applications/<id>/chat/<message_id>/stream/", "mode": "ai" }
```

`GET` that address with the same bearer token for `text/event-stream`. It uses the browser's own
streaming worker, so the single-worker-process constraint in
[`deployment.md`](deployment.md) applies to it too.

### What it does not do

No model or credential is chosen by the caller - the application's AI settings decide both, and
usage is recorded against the application as `AIUsage` like any other call. There is no token
role and no second permission model: revoke the user's grant and the endpoint closes. The source
digest is never returned, as on the retrieval endpoints.

## Code Graph

Owners and contributors register a GitHub repository as `owner/name`. A background worker lane,
separate from the knowledge-graph lane and sharing nothing with its fingerprint, clones the
repository at the resolved commit, indexes files, symbols, routes, imports, calls and inheritance,
and deletes the checkout. Bounds: 500 files, 400 KB per file, 20 MiB in total, a five-minute clone
timeout. Snapshots are numbered per repository; an unchanged commit produces no new snapshot.

The clone reads content and never runs it. Hooks are redirected to an empty directory,
submodules are not followed, and `HOME` / `USERPROFILE` / git config point at a scratch
directory, so a run cannot reach the operator's own git credentials. A mounted
`github_APPLICATION_UUID` token is written into that scratch config rather than passed in argv,
where the process list would expose it.

**No input chooses the host.** The remote is built against a hardcoded `github.com` from a name
validated as two path-safe segments. This is the one place the platform retrieves a remote
resource outside `fetching.py`, and it is allowed to be because it takes no URL. Repositories on
other hosts cannot be read at all.

Code Graph feeds **Code Factory**: a run pins one snapshot, and the analysis and design phases
are given a bounded neighbourhood of it - matching files with their symbols, plus import
relationships. The snapshot is chosen from a repository the ticket names, or from the
application's registry when it holds exactly one, and is confirmed by the same person at the same
approval gate. Access and the feature switch are re-checked on every use: withdrawing either
removes the code context rather than failing the run. Code Graph never joins the knowledge graph,
and **chat cannot read it**.

## Connectors

Three kinds ship: **GitHub** issues, **Jira** issues and **ServiceNow** table records. All three
are owner-configured, manual, read-only and bounded to the 100 most recently updated records.
Credentials are mounted per application and per kind as `KIND_APPLICATION_UUID`; nothing is
scheduled and nothing is written back to the tracker.

### GitHub

Application owners configure an owner/repository pair and an enable switch. Mount a read-only
token as `github_APPLICATION_UUID` in the secret directory. **Import latest issues** reads the
100 most recently updated issues, skips PRs, and creates knowledge sources. It does not publish
comments or change the repository. Reimports deduplicate unchanged content; changed issues
archive the old source and create a new immutable revision.

Requests use only api.github.com, reject redirects, have a 15-second timeout and a 4 MiB response
limit. This is a bounded manual import, not a full historical synchronization or deletion mirror.

Reference: [GitHub repository issues API](https://docs.github.com/en/rest/issues/issues).

### Jira

An owner configures the site URL and a JQL query. Jira Cloud authenticates with the account email
plus a mounted API token; Data Center takes the token as a bearer. Issue descriptions arrive as
Atlassian Document Format and are flattened to text. The title and body become one immutable
source per issue, deduplicated by digest like any other import.

### ServiceNow

An owner configures the instance, the table to read, and which fields carry the title and the
body - so incidents, problems, changes and knowledge articles all import through one adapter.
OAuth client credentials are used when configured, basic authentication otherwise.

For the `incident` table, the owner can enter one or more assignment group names in the
connector form. The adapter adds those names to the ServiceNow query and checks every returned
record's displayed assignment group before import. Groups are matched without regard to case;
records with a missing or different group are skipped. Leaving the field blank preserves the
existing all-groups import. The filter affects new imports; knowledge already imported from
other groups remains until retired under the connector's existing retention controls.
If ServiceNow returns no records, the connector shows **No records** with checks for the
instance, account read access, table and filters. An empty provider response never retires
previously imported knowledge, even when retirement is enabled.

Incident state, priority, impact, service, CI, assignment group and resolution details are
recorded in a structured header inside the digested source body. They are still not separate
database columns for aggregation. See the gap list in
[`sdlc-scenarios.md`](sdlc-scenarios.md#gaps-and-how-to-fill-them).

GitHub Enterprise and local Git adapters remain pending.

## Verification limits

Automated tests exercise app isolation, roles, safe rendering, feature enforcement, approval
decisions, scanner fail-closed behavior, bounded extraction, idempotent imports and AI receipts.
Provider and scanner responses are mocked. No real customer documents or provider credentials
are used in tests. Live provider calls, real malware scans and production infrastructure require
deployment-specific validation.


## Knowledge graph and quality

The local worker automatically generates one versioned structural graph per application from
active Markdown sources, then rebuilds when source fingerprints change. It extracts documents,
table records, field/value nodes and prose sections. Relations are explicit containment or table
field assertions. Identical field name/value pairs share a value node inside that application;
this does not imply that their parent records represent the same entity.

The **Knowledge → Graph / Quality** menu provides an interactive SVG graph with searchable nodes, neighborhood
navigation, keyboard selection, exact source lines and excerpts, and graph/quality JSON export.
The canvas shows at most 80 nodes at a time, while search covers every generated node.

Quality reports source counts, represented rows/sections, structural coverage, evidence coverage,
missing cells, duplicate rows and isolated nodes. Graph generation is bounded to 100 sources,
2,000 rows/sections per source, and 6,000 nodes; reported warnings expose omitted content.
Structural coverage is relative to rows/sections in processed sources, not semantic recall.
Semantic accuracy and inferred relationships are not implemented or assigned invented scores.

A graph is served only if its fingerprint matches current active sources. Deleted/archived sources
therefore invalidate old graph views and exports immediately while the worker rebuilds. Application
access and the Knowledge feature switch are checked on every view/export. IDs are application-scoped.

This generator uses no graph database, vector retrieval or labeled semantic evaluation set; those
design integrations remain separate work.

### Graphify

[Graphify](https://graphify.net) (the `graphifyy` package) is used in two places, and only its
deterministic parts:

- **Code Graph** reads Java, Kotlin, Scala, Go, Rust, C, C++, C#, Swift, Ruby and PHP through
  Graphify's Tree-sitter pass, alongside the in-house Python and JavaScript/TypeScript analyser,
  and stores calls and inheritance between files as well as imports. Each edge keeps the line it
  was read from and whether it was written in the source (static) or resolved across files
  (inferred). Code Factory follows calls and inheritance, not only imports, when it chooses the
  reference code a change is shown - in a Java package, where files use each other without
  importing, imports alone reached almost nothing.
- **Knowledge → Quality** lists the graph's themes: Leiden community detection over the stored
  relationships, each named after its most-connected node, with a cohesion figure.

Graphify's semantic pass, which sends text to a model under its own API key, is never called:
it would bypass the application's own credential and its usage receipts. Document relationships
are still extracted by the application's configured model and kept only when their quote
verifies. Graphify runs on text already read and bounded, in a scratch directory of its own, and
calls no network. If it fails, the snapshot keeps what the in-house analyser found and is marked
partial with a warning.


### Answering from a chosen graph version

A graph conversation can be pinned to a numbered graph version from the conversation header, or
left on **Latest**. Pinning deliberately skips the live fingerprint check, because reading an
older version is the point. Safety does not depend on that check: every citation is still
verified edge by edge against currently active sources with matching digests and an exact quote,
so a version whose evidence has since changed yields no citations rather than stale claims.

### Exploring the graph

The graph canvas is a force-directed layout you can pan (drag), zoom (scroll or the zoom
controls), and rearrange (drag a node to pin it). Clicking a node inspects it and offers to expand
its neighbourhood; **Expand** grows the whole visible set. A counter always states how many of the
total nodes are shown and when the display limit is reached, so a large graph is navigated rather
than silently truncated. Node kinds can be filtered on and off. Dashed orange relationships are
AI-inferred and source-verified; solid grey relationships are deterministic structure.

### Unified Knowledge area and saved graph versions

The top menu has one Knowledge entry, opening the generated graph. Its local tabs are Graph,
Sources (converted Markdown/manual notes), Quality, and Versions. Documents remains the original
upload and file-management area. The graph renderer and conversion status polling are served as
same-origin external scripts; CSP permits script-src self, without inline script or eval permission.

Each published rebuild saves a numbered GraphRevision snapshot containing its graph, quality
report, fingerprint and generation timestamp. Unchanged rebuilds do not create duplicate versions.
The current graph existing at migration becomes Version 1; older overwritten graphs cannot be
recovered. Subsequent versions are preserved and selectable from the Versions table, including
version-specific graph, quality and JSON exports. Historical snapshots are not rollback controls.

Access to historical content requires current application permission and active matching source
fingerprints. Deleted, archived or changed source evidence makes that snapshot unavailable for
view/export; its metadata remains in version history. There is no UI to edit published revisions.
