# Digital Brain: architecture and process atlas

Open **index.html** to browse nine diagrams, zoom, download previews, or open a page in draw.io. **digital-brain-design.drawio** is the complete editable nine-page document. Individual `.drawio` files and the ZIP are also provided.

| Page | Content |
| --- | --- |
| 01 | High level design: clients, application security, features, processing, data and integrations |
| 02 | Low level design: Python modules, execution boundaries, data records and persistence |
| 03 | Graph generation: intake, scanning, conversion, quality checks, structural graph, AI enrichment, versioning and publication |
| 04 | Graph retrieval: access, published versions, ranking, optional semantic retrieval, graph walk and citation verification |
| 05 | Hostinger VPS: committed Coolify / Docker Compose deployment and optional scale overlay |
| 06 | AWS EC2 VPS: proposed adaptation of the same stack to a single EC2 host |
| 07 | Code Factory: triage, analysis, design, approval, work order, implementation, tests, review, verification and delivery |
| 08 | ServiceOps: incident evidence, read-only hypotheses, responder verdict and separately gated automation |
| 09 | Chat: submission, saved placeholder, retrieval, provider runtime, SSE, Stop, final verification and transcript |

## Scope and interpretation

Prepared on 19 October 2026 from the repository working tree at Git HEAD `08862f2`. These are design artifacts, not a live infrastructure inventory or a deployment. Existing project code and deployment configuration were not changed.

Hostinger follows `docker-compose.yaml`, `docker-compose.scale.yaml`, `deploy/entrypoint.sh` and `deploy/README.md`. The deployment README contains older process-local scaling advice; the current Compose files and `platform_core/broker.py` show shared Redis transport and an optional split-role scale overlay. The diagrams follow those current implementation files. Observability overlays exist but are not implied to be deployed.

AWS is explicitly proposed: provision the EC2 instance, VPC/subnet, persistent EBS volumes, DNS, firewall and Coolify separately, then adapt allowed hosts, domain and S3 settings. The repository does not currently provide an AWS provisioning stack. Keep database and Redis ports internal. Restrict SSH to the administrative network, allow public HTTP/HTTPS at the proxy, and use PostgreSQL-consistent backups plus backups of required persistent volumes. A single host remains one failure domain. AWS documents [EC2 security-group rules](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-rules-reference.html) and [EBS snapshots](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-snapshots.html); snapshots alone are not a tested database recovery plan.

Graph generation and retrieval follow `docs/graph-generation-end-to-end.md`, `docs/graph-retrieval-end-to-end.md`, `graphs.py`, `graph_ai.py` and `graph_store.py`. Drafts cannot answer questions; optional paid processing is configured and budgeted; live source text proves citations. Knowledge, code and operations graphs are separate domains.

Code Factory follows the phase registry in `models/factory.py` and the approval / publication gates in `code_factory_build.py`. ServiceOps follows `serviceops_triage.py`, `automation_execution.py`, `automation_adapters.py` and `docs/serviceops-real-automation.md`. Read-only triage does not authorize remediation. AWX is an implemented adapter, not a provisioned controller.

The chat diagram concentrates on streaming AI / graph mode. Source excerpts and the no-JavaScript fallback use the synchronous path. Redis enables cross-process event replay, Stop signals and rate limiting; it does not store the durable transcript.

## Generation and verification

`mcp_client.py` connects directly to the requested `https://mcp.draw.io/mcp` endpoint using Streamable HTTP JSON-RPC. It initializes a session, reads `get_drawio_reference`, and calls `create_diagram` with XML and obstacle-avoiding routing. Every page has a saved `.mcp.json` response containing the creation result and its editor URL. This is actual MCP usage; the local Python script prepares the repository-grounded XML.

`validation.json` records XML parsing, unique IDs, parent/source/target references and container bounds. `route.py` uses the MCP viewer's own libavoid implementation to bake the resulting connector geometry into the downloadable XML; `routing-validation.json` records this pass. `render-validation.json` records import and SVG export through the actual draw.io editor's [documented embed protocol](https://www.drawio.com/docs/reference/embed-mode/). SVGs contain their editable XML; PNGs are browser renderings of those SVGs. The gallery embeds SVGs and works offline; its **Edit in draw.io** buttons require internet access.

All nine routed page previews were visually inspected. `final-validation.json` records final XML hashes, the nine-page bundle check, and successful gallery navigation, zoom and fit checks with no browser script errors. `overview.png` shows the complete set.

Regenerate from the repository root with:

```powershell
.venv/Scripts/python.exe docs/drawio-design/generate.py --submit
.venv/Scripts/python.exe docs/drawio-design/route.py
.venv/Scripts/python.exe docs/drawio-design/render.py
.venv/Scripts/python.exe docs/drawio-design/build_gallery.py
```

Generation and rendering require network access; routing downloads official libraries on first use and then uses its ignored local cache. No credentials, `.env`, private runtime files or source-document bodies are sent to draw.io. Only the architecture labels and XML in this folder are submitted.
