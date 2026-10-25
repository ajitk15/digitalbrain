/* The "Where do I find..." index: one entry per question someone actually asks.
   q = the question, a = a one-line answer, u = where the full answer is (relative
   to the docs root), for = "everyone" | "tech" | "both", k = extra search words. */
window.DOCS_INDEX = [
  // ------------------------------------------------------------------ basics
  { q: "What is Digital Brain?", a: "A platform that turns an organization's documents, tickets and code into answers you can check: every answer cites the exact passage it came from.", u: "guide/what-is.html", for: "everyone", k: "overview introduction product purpose" },
  { q: "What problem does it solve?", a: "Knowledge scattered across documents, boards and repositories, and AI answers nobody can verify.", u: "guide/what-is.html#problem", for: "everyone", k: "why value" },
  { q: "What can it do?", a: "Answer questions with citations, build a knowledge graph, plan and deliver code changes with review, and triage incidents from runbooks.", u: "guide/what-is.html#four-jobs", for: "everyone", k: "features capabilities jobs" },
  { q: "How is it organized: organizations, portfolios, products, applications?", a: "Organization → portfolio → product → application. Access is granted per application.", u: "guide/what-is.html#organized", for: "everyone", k: "hierarchy structure tree tenant" },
  { q: "What does choosing Engineering, Operations or Both do?", a: "It switches features on: Code Graph and Code Factory for Engineering, ServiceOps for Operations. Changing later is ticking features.", u: "guide/what-is.html#purpose", for: "everyone", k: "purpose create application features" },
  { q: "What is the knowledge cycle?", a: "Generate → draft → check → publish → use → learn. Only a published version answers.", u: "guide/what-is.html#cycle", for: "everyone", k: "lifecycle versions draft publish" },
  { q: "What is AI never allowed to do on its own?", a: "Publish a graph, approve a plan, merge a change, cite something it cannot quote exactly, or read a restricted source.", u: "guide/what-is.html#guardrails", for: "everyone", k: "safety guardrails limits human review" },
  { q: "Every feature, explained", a: "Knowledge, graph and versions, Learned, Rules, Chat, Code Graph, Code Factory, ServiceOps, Automations, Connectors, AI settings, API.", u: "guide/features.html", for: "everyone", k: "feature list" },
  { q: "Every screen, explained", a: "A tour of each screen, from sign-in to Administration.", u: "guide/screens.html", for: "everyone", k: "ui pages tour navigation" },

  // ------------------------------------------------------------------ answers
  { q: "Can I trust an answer?", a: "Each citation is checked against the live source before it is shown: still active, unchanged digest, and the quoted words still there.", u: "guide/faq.html#answers", for: "everyone", k: "citations verify evidence hallucination" },
  { q: "What are the chat modes?", a: "Graph answer reads the latest published graph; Source excerpts and AI answer search the current sources.", u: "guide/features.html#chat", for: "everyone", k: "chat modes graph answer ai answer source excerpts" },
  { q: "Why did an answer say it found nothing?", a: "No source matched well enough, the graph is not published, or the sources that match are restricted or not permitted for you.", u: "guide/faq.html#answers", for: "everyone", k: "empty no results" },
  { q: "Why isn't my new document in Graph answers?", a: "Graph answers read the latest published version; a new document is in the next draft until someone publishes it.", u: "guide/faq.html#answers", for: "everyone", k: "upload missing publish" },
  { q: "How is an answer found and ranked?", a: "Candidates by words (full text on PostgreSQL), ranked by query-term coverage, BM25, then one citation per source first; optional embeddings and reranking only reorder.", u: "tech/answering.html#ranking", for: "tech", k: "bm25 ranking retrieval spread" },
  { q: "How are citations verified?", a: "Active source, matching digest, exact quote - at storage and again at render time.", u: "tech/answering.html#verify", for: "tech", k: "verification digest quote" },
  { q: "What is the two-hop walk?", a: "Retrieval follows AI relationships up to two hops; what it finds comes after every direct match.", u: "tech/answering.html#walk", for: "tech", k: "graph traversal hops" },
  { q: "How do embeddings work here?", a: "OpenAI embeddings choose starting points only; every result is verified like a word match. On only with the application's own OpenAI key.", u: "tech/answering.html#vectors", for: "tech", k: "vectors semantic search openai" },
  { q: "Why does the browser never render model Markdown?", a: "Streamed text is inserted as text; the finished message is a server-rendered fragment that escapes first and makes no links.", u: "tech/answering.html#rendering", for: "tech", k: "xss markdown rendering" },

  // ------------------------------------------------------------------ knowledge
  { q: "How do I add documents?", a: "Knowledge → Documents: upload files, a folder or links. Each is scanned, converted to text and checked.", u: "guide/screens.html#upload", for: "everyone", k: "upload import files" },
  { q: "Which file types can it read?", a: "PDF, Word, Excel, PowerPoint, CSV, Markdown, HTML, text, JSON, XML, rich text and EPUB; PNG, JPEG and TIFF images when OCR is on.", u: "guide/features.html#knowledge", for: "everyone", k: "formats pdf docx xlsx pptx" },
  { q: "Does it read scanned PDFs?", a: "Yes, with OCR on: each page read that way is marked with its confidence, and a low-confidence page is flagged.", u: "tech/knowledge-lifecycle.html#intake", for: "both", k: "ocr tesseract scan image" },
  { q: "What happens to an uploaded file?", a: "Virus scan, offline conversion with no network access, text stored as the record; the original kept on disk or in a bucket unless switched off.", u: "tech/knowledge-lifecycle.html#intake", for: "both", k: "intake conversion clamav markitdown" },
  { q: "What is the source check?", a: "A score per document on a fixed rubric, plus a content check for its document type. It only suggests; it never blocks.", u: "tech/knowledge-lifecycle.html#build", for: "both", k: "quality source check document type" },
  { q: "How do I publish a graph version?", a: "Knowledge → Versions: check the draft's quality and benchmark, then publish. An owner may let a version that passes every release check publish itself.", u: "tech/knowledge-lifecycle.html#publish", for: "both", k: "publish version release check" },
  { q: "What do the Quality and Versions tabs show?", a: "Each version measured with its AI and without it, benchmark scores, and figures marked better or worse than the version before.", u: "guide/screens.html#graph-tabs", for: "everyone", k: "quality benchmark versions trend" },
  { q: "What is Learned?", a: "What the platform learned from reviews and corrections, shown for people to accept.", u: "guide/features.html#learned", for: "everyone", k: "learning loop feedback" },
  { q: "How do I connect Jira, GitHub or ServiceNow?", a: "Connectors: add one, set its credential on Settings → Credentials, import now or on a schedule.", u: "guide/features.html#connectors", for: "everyone", k: "jira github servicenow sharepoint import connector" },
  { q: "Does a scheduled import run as someone?", a: "As the person who created the connector; revoking their grant stops the schedule.", u: "tech/workers.html#jobs", for: "tech", k: "schedule connector sync" },
  { q: "Are tickets that leave the filter deleted?", a: "Only when the owner ticks 'prune', the read was complete and no other connector shares the source - and then they are retired, never deleted.", u: "guide/features.html#connectors", for: "both", k: "prune delete missing" },
  { q: "How do I keep a document away from AI?", a: "Restrict it: AI never reads or cites it again, including from published versions. People with access still read it.", u: "guide/roles-and-access.html#source-permissions", for: "everyone", k: "restrict sensitive phi pii" },
  { q: "Can personal data be masked?", a: "Yes, in AI settings → Personal data: chosen kinds are replaced with stable placeholders before the text is stored.", u: "guide/ai-and-cost.html#data", for: "everyone", k: "masking phi pii mrn" },

  // ------------------------------------------------------------------ code
  { q: "What is Code Graph?", a: "An index of a repository - files, symbols, routes, calls - read by cloning or from a server folder, with code review and reverse-engineered documentation.", u: "guide/features.html#code-graph", for: "everyone", k: "repository code index" },
  { q: "What is Code Factory?", a: "From a ticket to a reviewed plan to a pull request (or a change written into a server folder), each step cited and approved by a person.", u: "guide/features.html#code-factory", for: "everyone", k: "code changes pull request agent" },
  { q: "Will Code Factory change our code without asking?", a: "No. A plan must be approved by someone with approval rights, and delivery is a pull request or a folder write an approver presses.", u: "guide/faq.html#engineering-faq", for: "everyone", k: "approval review" },
  { q: "Why was my Code Factory run refused?", a: "Most often there is no published graph: a run without one would produce findings nobody could check.", u: "guide/faq.html#engineering-faq", for: "everyone", k: "no graph refused" },
  { q: "Can I approve my own plan?", a: "Yes unless the deployment sets allow_self_approval = false; the audit record says who approved.", u: "guide/roles-and-access.html#self-approval", for: "everyone", k: "self approval two person review" },
  { q: "Does Digital Brain run the code it writes?", a: "Only one opt-in exception: an approver's button runs a server folder's pytest suite in development or a demonstration.", u: "guide/faq.html#engineering-faq", for: "everyone", k: "tests execute run code" },

  // ------------------------------------------------------------------ operations
  { q: "What is ServiceOps?", a: "Incident triage from imported incidents, changes, runbooks, knowledge articles and automations, every step cited.", u: "guide/features.html#serviceops", for: "everyone", k: "incidents triage operations" },
  { q: "Can an automation run by itself?", a: "No. A run is requested by an owner, against an allow-list, and approved by a different owner where required.", u: "guide/features.html#automations", for: "everyone", k: "automation awx execute" },
  { q: "Why is a ServiceNow article not in the operations graph?", a: "It joins only while ServiceNow says it is Published and in date.", u: "guide/faq.html#operations-faq", for: "everyone", k: "knowledge article kb" },
  { q: "Why is the operations graph not publish-gated?", a: "It is rebuilt by rules from imported records and every edge is re-verified when read; nothing a model says is written to it.", u: "tech/knowledge-lifecycle.html#ops-layer", for: "tech", k: "operations graph exception" },

  // ------------------------------------------------------------------ access
  { q: "What roles are there?", a: "Viewer, contributor and owner per application, plus approval rights; organization and platform administrators manage, but open nothing by that alone.", u: "guide/roles-and-access.html#roles", for: "everyone", k: "permissions viewer contributor owner admin" },
  { q: "Why can't I see an application my colleague uses?", a: "You have no grant for it. Without one the platform answers 'not found', never 'forbidden'.", u: "guide/roles-and-access.html#deny", for: "everyone", k: "404 access denied" },
  { q: "I am an administrator - why can't I open an application?", a: "Administration grants no application access; an owner must grant you a role.", u: "guide/faq.html#access-faq", for: "everyone", k: "admin access" },
  { q: "How do people join?", a: "An owner invites; someone outside the organization waits for its administrator to approve, then gets a one-time link to make an account.", u: "guide/roles-and-access.html#joining", for: "everyone", k: "invite invitation onboarding member" },
  { q: "Can access end on a date?", a: "Yes for viewers and contributors; owners review their access list every review period.", u: "guide/roles-and-access.html#expiry", for: "everyone", k: "expiry review" },
  { q: "How do I sign in with Microsoft?", a: "If the deployment names an Entra tenant, a 'Sign in with Microsoft' button appears. It authenticates; it grants nothing.", u: "guide/roles-and-access.html#sign-in", for: "everyone", k: "sso entra azure ad" },
  { q: "How do I add an authenticator app?", a: "Account → Authenticator app. Optional; ten recovery codes are shown once.", u: "guide/screens.html#my-access", for: "everyone", k: "totp 2fa mfa" },
  { q: "I lost my phone with the authenticator app", a: "Use a recovery code, ask an administrator to remove it on your person page, or an operator runs manage.py remove_authenticator.", u: "guide/faq.html#access-faq", for: "everyone", k: "lost phone recovery" },
  { q: "Do answers respect Jira's own permissions?", a: "With 'Enforce source permissions' on a Jira connector, each answer is filtered to what Jira lets the asker browse - and fails closed.", u: "guide/roles-and-access.html#source-permissions", for: "everyone", k: "jira permissions" },
  { q: "What is recorded in the audit log?", a: "Every control-plane change, who made it and when - never a secret, never content.", u: "guide/roles-and-access.html#audit", for: "everyone", k: "audit log trail" },
  { q: "Where is administration?", a: "Administration: Overview, People, Organizations, Sign-in and security, Operations, Settings and Audit.", u: "guide/screens.html#administration", for: "everyone", k: "console platform admin" },
  { q: "How do I offboard someone?", a: "Their person page: name a successor who takes over what they owned, then their access and tokens end.", u: "guide/screens.html#administration", for: "everyone", k: "offboarding leaver successor" },

  // ------------------------------------------------------------------ AI & cost
  { q: "Who pays for the AI?", a: "Each application's own provider credential; every call writes a receipt naming who asked.", u: "guide/ai-and-cost.html#which-ai", for: "everyone", k: "billing cost account" },
  { q: "When is money spent?", a: "On a paid task you or an automatic switch asked for: answers, graph AI runs, typing, reviews, triage. Rebuilds reuse cached answers for free.", u: "guide/ai-and-cost.html#when", for: "everyone", k: "spend cost" },
  { q: "How do I set a monthly budget?", a: "AI settings: an owner sets a cap; it is checked before every paid call.", u: "guide/ai-and-cost.html#budget", for: "everyone", k: "cap limit budget" },
  { q: "Where do I see what AI cost?", a: "AI settings and cost reports: spend per task this month, from receipts.", u: "guide/ai-and-cost.html#receipts", for: "everyone", k: "receipts report usage" },
  { q: "Can I use it without AI?", a: "Yes: Source excerpts, the structural graph, search and the API's graph search call no model.", u: "guide/faq.html#ai-faq", for: "everyone", k: "no ai offline" },
  { q: "Where does my data go?", a: "To the AI provider an application is configured for, and only text a task needs; restricted sources never.", u: "guide/ai-and-cost.html#data", for: "everyone", k: "privacy data provider" },
  { q: "What if the AI provider is busy?", a: "A call waits for a slot and is then told nothing was sent - never retried for you.", u: "guide/faq.html#ai-faq", for: "everyone", k: "busy rate limit outage" },
  { q: "What is the AI gateway?", a: "The one path every paid call takes: purpose rule, admission slot and reservation, credential file, adapter, receipt.", u: "tech/ai-gateway.html", for: "tech", k: "gateway admission purposes" },
  { q: "Which AI tasks (purposes) exist?", a: "A closed registry of twelve purposes, each naming the switches and roles it needs.", u: "tech/ai-gateway.html#purposes", for: "tech", k: "purposes registry tasks" },
  { q: "How does admission limit calls?", a: "Per-application and per-provider slots, plus a reservation of the call's most expensive outcome against the budget.", u: "tech/ai-gateway.html#admission", for: "tech", k: "slots reservation concurrency" },
  { q: "How do I measure a model or prompt change?", a: "manage.py llm_drift and typing_pack - paid, receipted runs against frozen packs.", u: "tech/ai-gateway.html#measuring", for: "tech", k: "drift evaluation" },

  // ------------------------------------------------------------------ architecture
  { q: "Architecture diagram", a: "System context, containers, a page request, a streamed answer and background work, each drawn.", u: "tech/architecture.html", for: "tech", k: "diagram c4 system design" },
  { q: "What containers make up a deployment?", a: "Web, worker, sandbox and a migrate job, with PostgreSQL, Redis, clamd and an object store.", u: "tech/architecture.html#containers", for: "tech", k: "containers services" },
  { q: "How does a streamed answer work?", a: "POST creates the question and a placeholder; a worker thread owns the provider call and every write; readers follow a Redis stream and can resume.", u: "tech/architecture.html#answer", for: "tech", k: "sse streaming redis uvicorn" },
  { q: "Why Django, PostgreSQL, Redis...?", a: "Every technology with what it is used for and why, including what was considered and rejected.", u: "tech/technology.html", for: "both", k: "stack technology choices why" },
  { q: "Why build an AI gateway instead of using one?", a: "A bought gateway would hold the provider keys and its own prices and retries, trading four invariants at once.", u: "tech/technology.html#ai-tech", for: "tech", k: "litellm gateway build vs buy" },
  { q: "Why no vector database?", a: "In-process numpy search over unit vectors is enough until a library outgrows memory, so the production database did not change.", u: "tech/technology.html#data", for: "tech", k: "pgvector vector database" },
  { q: "Which modules do what?", a: "The platform's modules by area: access, knowledge, products, runtime - and the management commands.", u: "tech/components.html", for: "tech", k: "modules code layout" },
  { q: "What is in the data model?", a: "Every model by module, the principles the schema follows, and how migrations are written.", u: "tech/data-model.html", for: "tech", k: "models schema database tables" },
  { q: "Where does background work run?", a: "Nine lanes in the worker and sandbox, driven by jobs written in the same transaction as the change.", u: "tech/workers.html#lanes", for: "tech", k: "lanes worker queue jobs" },
  { q: "What is the jobs outbox?", a: "A change writes a Job in its own transaction, so a rolled-back change takes its job with it.", u: "tech/workers.html#jobs", for: "tech", k: "outbox queue" },

  // ------------------------------------------------------------------ security
  { q: "Is it HIPAA compliant?", a: "It implements the §164.312 technical safeguards and probes them in the scorecard; compliance also depends on how you deploy and operate it.", u: "tech/security.html#hipaa", for: "both", k: "hipaa compliance healthcare phi" },
  { q: "How are tenants kept apart?", a: "Three layers: the code's own scoping, PostgreSQL row security on every table, and a database role that can only touch rows.", u: "tech/tenancy.html", for: "tech", k: "multi-tenant isolation rls" },
  { q: "What is row-level security here?", a: "A FORCEd policy on every table with an application, reading the session's bound tenant.", u: "tech/tenancy.html#rls", for: "tech", k: "rls postgres policy" },
  { q: "Where are secrets kept?", a: "Files, one per secret, owner-only - never environment variables, database rows or audit details.", u: "tech/security.html#secrets", for: "both", k: "secrets credentials api keys" },
  { q: "What is the Content Security Policy?", a: "self only, no inline script or style, no external origins - pinned by a test.", u: "tech/security.html#web-edge", for: "tech", k: "csp headers" },
  { q: "How are outbound URLs fetched safely?", a: "One fetcher: resolve, refuse private addresses, pin the validated address, no redirects, size and time caps.", u: "tech/security.html#outbound", for: "tech", k: "ssrf fetch url" },
  { q: "What does production refuse?", a: "Host Claude login, demo reset and local Code Factory outside a demonstration, Redis without TLS, short retention, and more.", u: "tech/security.html#refusals", for: "both", k: "production refused settings" },

  // ------------------------------------------------------------------ APIs
  { q: "Is there an API?", a: "Bearer-token API under /api/v1/: graph search, chat, triage and automations, with the same access checks as the browser.", u: "tech/apis.html", for: "both", k: "rest api token" },
  { q: "How do I get an API token?", a: "Settings → API access; any member can issue their own, shown once.", u: "tech/apis.html#tokens", for: "both", k: "token bearer" },
  { q: "Can I connect an AI assistant over MCP?", a: "Yes: /api/v1/applications/<id>/mcp/ with a bearer token; tools search the graph and read triage briefs, calling no model.", u: "tech/apis.html#mcp", for: "both", k: "mcp model context protocol assistant" },
  { q: "Does it support SCIM provisioning?", a: "Yes, from Entra: creates and deactivates accounts, never grants access.", u: "tech/apis.html#scim", for: "tech", k: "scim provisioning entra" },

  // ------------------------------------------------------------------ quality
  { q: "What are the five gates?", a: "Lint, migrations check, the test suite (with browser tests), production preflight, and the scorecard.", u: "tech/testing.html", for: "tech", k: "ci gates checks" },
  { q: "What is the scorecard?", a: "A ~90 s run that measures retrieval, citation integrity, cost control, safeguards and speed against a baseline, with no model call.", u: "tech/testing.html#scorecard", for: "tech", k: "benchmark baseline metrics" },
  { q: "How is retrieval quality measured?", a: "Benchmark questions per application, scored on every saved version - free, no model.", u: "tech/knowledge-lifecycle.html#check", for: "both", k: "benchmark mrr retrieval" },
  { q: "What is monitored?", a: "Traces and metrics through OpenTelemetry, service levels from records and from Prometheus rules.", u: "tech/observability.html", for: "tech", k: "monitoring metrics prometheus otel" },
  { q: "What are the service levels?", a: "Eight objectives: answers complete and on time, jobs start on time and finish, no server errors, fast pages, calls admitted, fresh queues.", u: "tech/observability.html#slos", for: "tech", k: "slo sla objectives" },
  { q: "Every configuration setting", a: "Each key with its default and what it does, plus secret files and container variables.", u: "tech/configuration.html", for: "tech", k: "config toml settings" },

  // ------------------------------------------------------------------ deployment
  { q: "Which deployment should I choose?", a: "Local without Docker, local with services, a single server, the scaled split, or a managed cloud.", u: "deploy/index.html#choose", for: "both", k: "deployment options strategy" },
  { q: "How do I run it on my laptop?", a: "Docker Desktop and start-all.cmd: PostgreSQL, Redis and ClamAV in Docker, the server on 127.0.0.1:8000.", u: "deploy/local.html#with-docker", for: "tech", k: "local development install windows" },
  { q: "Can I run it without Docker?", a: "Yes: a configuration with no [database] or [redis] runs on SQLite under waitress, in one process.", u: "deploy/local.html#without-docker", for: "tech", k: "sqlite no docker" },
  { q: "Why are there two local configuration files?", a: "local.toml connects as the owner for migrations; local.runtime.toml runs the server as the runtime role.", u: "deploy/local.html#two-files", for: "tech", k: "local.toml runtime role" },
  { q: "How do I deploy to a single server?", a: "docker-compose.yaml: secrets and certificates generated on first boot, PostgreSQL and Redis over TLS, one app container behind your proxy.", u: "deploy/compose.html#single", for: "tech", k: "vps docker compose coolify" },
  { q: "How do I scale it out?", a: "Add docker-compose.scale.yaml: a migrate job, web replicas, a worker, a sandbox and clamd.", u: "deploy/compose.html#scaled", for: "tech", k: "replicas scale worker sandbox" },
  { q: "What is the sandbox container?", a: "The lanes that hand outside content to other programs, in a read-only container with no capabilities and process and memory limits.", u: "deploy/compose.html#scaled", for: "tech", k: "sandbox isolation security" },
  { q: "How do I add Prometheus and a collector?", a: "docker-compose.observability.yaml (and its scale variant): a collector over https and Prometheus with the SLO rules on loopback.", u: "deploy/compose.html#observability", for: "tech", k: "observability prometheus collector" },
  { q: "Can it run on AWS, Azure or Google Cloud?", a: "Yes: the four roles on containers with managed PostgreSQL 16, TLS Redis, an object store and secrets as files - with the requirements of each listed.", u: "deploy/cloud.html", for: "both", k: "aws azure gcp cloud kubernetes ecs" },
  { q: "Can I use a managed PostgreSQL?", a: "Yes, with verify-full TLS, an owner that can create roles, and direct or session-mode pooling for advisory locks.", u: "deploy/cloud.html#cloud-postgres", for: "tech", k: "rds cloud sql flexible server pgbouncer" },
  { q: "How do I deliver secrets on a cloud?", a: "An init step copies them from the secret manager into a memory-backed volume at mode 0600 for uid 10001.", u: "deploy/cloud.html#cloud-secrets", for: "tech", k: "secrets manager key vault kubernetes" },
  { q: "How do I rotate the signing key?", a: "Keep the old one as django_secret_key_previous while the new one takes over.", u: "deploy/operations.html#rotate", for: "tech", k: "rotate key django secret" },
  { q: "How do upgrades work without downtime?", a: "Migrations expand first and contract a release later, so the old release keeps running while the migrate job applies.", u: "deploy/operations.html#upgrades", for: "tech", k: "upgrade migration rollback zero downtime" },
  { q: "What do I back up?", a: "The database, the originals (bucket or volume), the secrets volume and the managed credentials.", u: "deploy/operations.html#backups", for: "tech", k: "backup restore disaster recovery" },
  { q: "How long are audit events kept?", a: "Until a [retention] table says otherwise, and never less than six years.", u: "deploy/operations.html#retention", for: "both", k: "retention audit logs" },
  { q: "Can I undo a delete?", a: "Disabling is reversible; deleting is not, and leaves a tombstone so the audit trail and receipts survive.", u: "guide/faq.html#general-faq", for: "everyone", k: "delete disable restore" },
  { q: "Does it work without JavaScript?", a: "Yes: every control is a real form; scripts only enhance.", u: "guide/faq.html#general-faq", for: "everyone", k: "progressive enhancement accessibility" },
  { q: "What does a word mean?", a: "The glossary defines every term these pages use.", u: "glossary.html", for: "both", k: "glossary terms definitions" }
];
