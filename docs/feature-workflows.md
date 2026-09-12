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

Application owners configure three independent tasks under **AI settings**: **Chat conversation**,
**Graph generation**, and **Graph retrieval**. Each has an enable switch, a grouped OpenAI/Claude
model selector, custom provider/model fields and contracted USD input/output rates. Existing
OpenAI chat configuration is preserved by the migration. New graph tasks start disabled.
The model suggestions were checked against provider documentation on 2026-09-12; account access
is not verified by the catalog. Custom IDs support models not listed there.

Mount keys in the configured secret directory as `openai_APPLICATION_UUID` and
`claude_APPLICATION_UUID`. Both providers use their own Agent SDK. No credentials are stored
in .env, database, form fields or logs. Restrict secret files to the service identity.

OpenAI uses Responses for the listed GPT-6/GPT-5.6 models and Chat Completions for existing/custom
older models. Each request owns a client and event loop. Provider storage and SDK tracing are
disabled; no tools or handoffs, one model turn, 30-second HTTP / 35-second overall timeout, no
application/HTTP retries or redirects. Model selection does not change the application's history.

Claude uses the SDK's bundled CLI, an ephemeral settings directory and empty workspace,
application-specific environment credentials, no inherited user/project settings or sessions,
no tools, plugins, skills or MCP servers, one turn and a 45-second overall timeout. Session
persistence and nonessential telemetry are disabled. The application does not retry Claude calls;
provider/runtime internal behavior remains subject to the SDK. The managed stop script recognizes
only the bundled CLI descendants of the verified application worker.

Chat conversations are private to each user and application. **New conversation** starts an
independent thread; history resumes it. Messages are chronological (30 turns per page, 20
conversations per history page). Existing entries remain in **Earlier chat history**. Recent
context is capped at eight turns, with prior answers capped at 4,000 characters. Turns referencing
changed/removed sources are excluded from new context; the owner's saved history remains.

Chat defaults to **AI conversation** on both page load and submission. Missing setup or keys
produce a visible setup message and preserve the draft, never a silent source-search fallback.
**Search source excerpts** explicitly selects bounded local lexical search without an LLM.
**AI conversation** uses the
chat task model and up to five matching source passages. **Graph answer** uses the graph retrieval
task model: lexical matching of saved graph endpoints/relations plus one-hop neighbors, with up
to eight verified source citations. It refuses stale/unavailable graphs. This is bounded graph
retrieval, not embedding search. Both answer modes preserve follow-up context. AI conversation also calls the LLM for greetings,
clarifications and general explanations without document matches; it must not invent application
facts or source citations. Graph answers still avoid paid calls when graph evidence is absent.

Answers support escaped paragraphs, bold, lists, tables and code blocks; model links are not
made active. Sources are expandable. Enter sends and Shift+Enter adds a line; provider errors
preserve drafts. Answers arrive together rather than token streaming.

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

The existing deterministic Markdown structural graph is always the base. When graph generation
AI is enabled, source or graph-model setting changes queue an enrichment run under the configuring
owner's current application access. The chosen provider/model extracts at most 25 relationships
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
