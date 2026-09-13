# Application features and setup

Navigation uses an organization tree on the left: organization, portfolio, product, application.
Only explicitly granted active applications are shown. Opening an application expands its
ancestors and highlights it. Functional tools live in the top menus; the tree remains available
across pages. The mobile tree has its own bounded scroll area.

All implemented feature switches default to enabled. Global and per-application switches remain
enforced on server routes. Platform administration does not grant application knowledge access.

## Knowledge and documents

Owners and contributors can add immutable plain-text knowledge sources. Sources can be searched,
read, or archived. Archiving excludes them from new retrieval without deleting approval evidence.

All file types are accepted. Select up to 20 documents per batch, with a combined 20 MiB limit.
Original bytes remain private and unchanged. Upload queues automatic Microsoft MarkItDown
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
the setting off, a missing secret stays an error. And the chat page says plainly when answers are
being billed to the machine's own login rather than to the application.

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

**Search source excerpts** is bounded local lexical search with no LLM. **AI conversation** uses
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
access. The chosen provider/model extracts at most 25 relationships
from at most 10 sources / 12,000 characters each. An exact source quote and both named entities
must be present in the supplied text before a relationship is accepted. Unsupported results are
excluded. Entity identity is source-local; matching labels do not silently merge distinct entities.
Semantic meaning is not independently verified and quality explicitly requests human review.

Accepted AI entities are orange in the graph, with source-backed connections and revision metadata
showing provider/model, accepted/rejected relationships and scope limits. A source or selected graph
configuration change creates a new version. Source deletion immediately withholds stale graphs.
Failed or interrupted calls are not retried every worker tick. Owners/contributors can retry under
Knowledge; interrupted runs must be at least two minutes old. Retrying may incur provider charges.

## Code Factory plans

Contributors and owners submit proposed changes and validation/rollback criteria. Each immutable
plan pins the current source IDs and SHA-256 hashes. A different user with explicit approval
permission must approve or reject it and provide a review note. Approval fails if pinned sources
were archived or changed. Decisions are atomic and cannot be applied twice.

Approved plans can be exported as JSON for implementation handoff. The current scope covers
planning, review and export. Isolated repository execution, generated diffs, automated tests,
PR creation and merge-policy enforcement are not implemented. Approval never executes code.

When **Code Factory drafting** is configured, owners and contributors can describe a requirement
and have a draft written into the proposal form. The draft is exactly that: it pre-fills the form
for a human to edit and submit, and creates nothing by itself. Every quote it cites is verified
against live sources the same way graph relationships are, and unverifiable ones are dropped and
counted. Source pinning, independent approval by a different user, and the rule that approval
never runs code are all unchanged.

## GitHub connector

Application owners configure an owner/repository pair and an enable switch. Mount a read-only
token as `github_APPLICATION_UUID` in the secret directory. **Import latest issues** reads the
100 most recently updated issues, skips PRs, and creates knowledge sources. It does not publish
comments or change the repository. Reimports deduplicate unchanged content; changed issues
archive the old source and create a new immutable revision.

Requests use only api.github.com, reject redirects, have a 15-second timeout and a 4 MiB response
limit. This is a bounded manual import, not a full historical synchronization or deletion mirror.
Jira, ServiceNow, GitHub Enterprise and local Git adapters remain pending.

Reference: [GitHub repository issues API](https://docs.github.com/en/rest/issues/issues).

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

This local deterministic generator does not integrate Graphify, LLM entity extraction, a graph
database, vector retrieval or a labeled semantic evaluation set. Those design integrations remain
separate work; current graphs expose the structural facts and limitations directly.


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
