# Digital Brain

An application-isolated knowledge platform for engineering and IT operations teams.
Documents, tickets, incidents and code become a versioned knowledge graph that answers
questions, plans code fixes and triages incidents - and every citation is re-checked
against its live source before it is shown.

Django 5.2, Python 3.12, server-rendered. SQLite or PostgreSQL.

## How it helps an enterprise

| Problem | How Digital Brain helps |
|---|---|
| Knowledge is scattered across documents, Jira, ServiceNow, SharePoint and code | One searchable, versioned knowledge base per application, kept current by scheduled imports |
| AI answers can't be trusted | Every answer cites its source, and each citation is re-checked against the live text - a changed or retired source is not cited |
| Teams and clients must not see each other's data | Each application is isolated: access is checked on every request, enforced again by database row security, and source-system permissions (Jira) apply to answers |
| AI spend is hard to control and attribute | A key and a monthly budget per application, a receipt for every paid call, and per-task on/off switches |
| Regulated data (HIPAA, personal data) | Personal-data and PHI detection, masking before storage, restricted sources never sent to AI, virus-scanned uploads, six-year audit trail |
| Slow incident response | Triage cites runbooks and similar past incidents; confirmed causes become reusable knowledge |
| Engineering backlog and onboarding | Code Factory turns a ticket into an evidence-backed plan and change; Code Repos documents and reviews existing code |
| Changes to knowledge go live unchecked | Every knowledge version is benchmarked and checked before it publishes; nothing answers from a draft |
| Fits existing identity and governance | Microsoft Entra sign-in and SCIM provisioning, role-based grants, access reviews and expiry, offboarding to a named successor, two-person review of code changes when required |
| Hosting choice | Runs on a laptop, a single server or a scaled Docker deployment, with secrets kept in files or a secret manager |

## Features

| Area | What it does |
|---|---|
| **Knowledge** | Upload documents (PDF, Word, scans with OCR) or import from Jira, GitHub, ServiceNow and SharePoint. Each source is checked for quality, typed (requirements, runbook, ADR...) and scanned for personal data. |
| **Knowledge graph** | Built from your sources by rules, optionally enriched by AI. Every version is a draft, checked and benchmarked before it is published. |
| **Chat** | Answers questions from the published knowledge, streamed, with citations verified against the source text. |
| **Code Repos** | Indexes a GitHub repository or a local folder: structure, change reviews and documentation read from the code. |
| **Code Factory** | Turns a ticket into an evidence-backed plan, and after a person approves it, a pull request or a local change with its tests run. |
| **ServiceOps** | Incident triage with runbook citations, similar-incident matching, and an operations graph of incidents, changes, knowledge articles and automations. |
| **Learning** | What use teaches - fixes, confirmed causes, unanswered questions - comes back as candidates an owner accepts or rejects. |
| **AI control** | Claude or OpenAI, a key per application, a monthly budget, a receipt for every paid call, and every task switchable in AI settings. |
| **Access and security** | Organizations, portfolios, products and applications; owner, contributor and viewer roles; Microsoft sign-in and SCIM; authenticator app; audit trail; row-level security on PostgreSQL. |
| **API** | REST API and MCP server with per-user tokens that follow the same permissions. |

## How to start

On Windows, **double-click `start-all.cmd`**. The first run installs everything it
needs, asks for an administrator sign-in, a password and an AI key, then opens on
**http://127.0.0.1:8000**.

| Command | Use it for |
|---|---|
| `start-all.cmd` | First run and every normal start |
| `start-all.cmd -Install -Demo` | First run with the CarePath demo data |
| `stop-all.cmd` | Stop the server |

Docker Desktop is optional. With it running you get PostgreSQL, Redis and a virus
scanner as in production. Without it the platform runs on SQLite.

**All options, Docker setup and password reset: [`SETUP.md`](SETUP.md).**

## More

| Document | For |
|---|---|
| [`SETUP.md`](SETUP.md) | Installing, options, Docker, password reset |
| [`deploy/README.md`](deploy/README.md) | Deploying to the hosted VPS |
| [`documentation/engineering/index.html`](documentation/engineering/index.html) | Product overview, architecture and engineering (open in a browser) |
| [`CLAUDE.md`](CLAUDE.md) | Rules for changing the code, and the five checks every change must pass |
