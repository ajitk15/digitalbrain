"""Generate the nine repository-grounded design pages and submit them to draw.io MCP."""

import argparse
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from mcp_client import DrawioMCP

OUT = Path(__file__).parent
PALETTE = {
    "step": ("#dae8fc", "#6c8ebf"),
    "data": ("#d5e8d4", "#82b366"),
    "gate": ("#fff2cc", "#d6b656"),
    "security": ("#e1d5e7", "#9673a6"),
    "external": ("#ffe6cc", "#d79b00"),
    "error": ("#f8cecc", "#b85450"),
    "group": ("#f5f7fa", "#bac5d3"),
}


class Page:
    def __init__(self, slug, title, subtitle, evidence):
        self.slug, self.title, self.subtitle, self.evidence = slug, title, subtitle, evidence
        self.nodes, self.edges = [], []
        self.model = ET.Element("mxGraphModel", adaptiveColors="auto", dx="1120", dy="1040",
                                grid="1", gridSize="10", page="1", pageWidth="1120",
                                pageHeight="1040", background="#ffffff")
        self.root = ET.SubElement(self.model, "root")
        ET.SubElement(self.root, "mxCell", id="0")
        ET.SubElement(self.root, "mxCell", id="1", parent="0")
        self.text("title", title, 35, 22, 1050, 42, 27, True)
        self.text("subtitle", subtitle, 35, 70, 1050, 38, 14)

    def text(self, key, value, x, y, w, h, size=14, bold=False):
        cell = ET.SubElement(self.root, "mxCell", id=key, value=value, vertex="1", parent="1",
                             style=f"text;html=1;whiteSpace=wrap;align=left;verticalAlign=middle;"
                             f"fontSize={size};fontStyle={int(bold)};fontColor=#24344b;")
        ET.SubElement(cell, "mxGeometry", x=str(x), y=str(y), width=str(w), height=str(h),
                      attrib={"as": "geometry"})

    def box(self, key, title, detail, x, y, kind="step", w=220, h=86, parent="1"):
        fill, stroke = PALETTE[kind]
        value = f"<b>{html.escape(title)}</b>"
        if detail:
            value += f"<br><span style='font-size:13px'>{html.escape(detail).replace(chr(10), '<br>')}</span>"
        shape = "rounded=1;arcSize=12;"
        if kind == "data":
            shape = "shape=cylinder3;boundedLbl=1;size=12;"
        if kind == "group":
            shape = "swimlane;startSize=30;horizontal=1;collapsible=0;container=1;pointerEvents=0;"
            value = html.escape(title)
        style = (f"{shape}whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                 "strokeWidth=1.5;fontColor=#24344b;fontSize=16;spacing=8;")
        cell = ET.SubElement(self.root, "mxCell", id=key, value=value, vertex="1", parent=parent,
                             style=style)
        ET.SubElement(cell, "mxGeometry", x=str(x), y=str(y), width=str(w), height=str(h),
                      attrib={"as": "geometry"})
        self.nodes.append(dict(id=key, title=title, detail=detail, x=x, y=y, w=w, h=h,
                               kind=kind, parent=parent))
        return key

    def edge(self, source, target, label="", dashed=False, failure=False):
        by_id = {n["id"]: n for n in self.nodes}
        def ancestors(key):
            result = [key]
            while key != "1":
                key = by_id[key]["parent"]
                result.append(key)
            return result
        a, b = ancestors(source), ancestors(target)
        parent = next(key for key in a[1:] if key in b[1:])
        color = "#b85450" if failure else "#637994"
        style = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;"
                 f"strokeColor={color};strokeWidth=1.5;fontSize=12;fontColor=#3b4e66;"
                 "labelBackgroundColor=#ffffff;defaultLabelBackgroundColor=gap;"
                 f"dashed={int(dashed)};")
        cell = ET.SubElement(self.root, "mxCell", id=f"e{len(self.edges)}", edge="1", parent=parent,
                             source=source, target=target, value=label, style=style)
        ET.SubElement(cell, "mxGeometry", relative="1", attrib={"as": "geometry"})
        self.edges.append(dict(source=source, target=target, label=label, dashed=dashed,
                               failure=failure))

    def finish(self, note):
        self.text("note", note, 35, 896, 1050, 54, 13)
        self.text("legend", "Blue: processing   Green: stored data / result   Yellow: gate   Purple: security   Orange: external   Red: failure\nSolid arrows: flow / dependency   Dashed arrows: asynchronous work", 35, 954, 1050, 40, 12)
        self.text("meta-evidence", "Evidence: " + self.evidence + " | Working tree at 08862f2 | 19 Oct 2026", 35, 1002, 1050, 25, 11)
        return self

    def xml(self):
        return ET.tostring(self.model, encoding="unicode")


def pages():
    result = []
    p = Page("01-high-level-design", "01 / Digital Brain — high level design",
             "Application-isolated knowledge, engineering and operations on one Django platform.",
             "README.md; digitalbrain/settings.py; platform_core/broker.py; docker-compose*.yaml")
    p.box("clients", "People and agents", "Browser • bearer-token API • MCP", 45, 130, "external", 1030, 64)
    p.box("access", "Application boundary", "Authentication → feature and grant checks → tenant binding", 45, 235, "security", 1030, 64)
    for key, title, detail, x in [
        ("knowledge", "Knowledge", "Sources, graph generation\nand published retrieval", 45),
        ("chat", "Chat", "Excerpts, AI answers\nand graph answers", 315),
        ("factory", "Code Factory", "Evidence → plan → approval\n→ implementation → delivery", 585),
        ("ops", "ServiceOps", "Incident evidence, hypotheses\nand gated automation", 855),
    ]:
        p.box(key, title, detail, x, 350)
        p.edge("access", key)
    p.box("lanes", "Background work", "Database-backed work claims • graph / connectors / intake / factory / ServiceOps / maintenance", 45, 485, w=1030, h=72)
    for key, title, detail, x in [
        ("pg", "PostgreSQL", "Records, graph versions,\nchat, jobs and audit", 45),
        ("redis", "Redis over TLS", "Answer events, Stop signals\nand API rate limits", 315),
        ("files", "Private files", "Runtime, app credentials\nand code workspace", 585),
        ("s3", "Originals in S3", "Uploaded source objects\nvia configured adapter", 855),
    ]:
        p.box(key, title, detail, x, 620, "data")
        p.edge("lanes", key)
    p.box("providers", "External integrations", "Claude / OpenAI • GitHub • SharePoint • Jira • ServiceNow • AWX", 45, 780, "external", 1030, 65)
    p.edge("clients", "access")
    for key in ["knowledge", "factory", "ops"]:
        p.edge(key, "lanes", dashed=True)
    p.edge("chat", "redis", "events")
    p.edge("chat", "pg", "transcript")
    p.edge("chat", "providers", "HTTPS")
    p.edge("lanes", "providers", "HTTPS")
    result.append(p.finish("Shared rule: scope every request and tool to an application and user; verify citations against live sources.\nThis is a logical architecture. Redis is transient transport; PostgreSQL stores the durable record."))

    p = Page("02-low-level-design", "02 / Digital Brain — low level design",
             "Concrete Python modules, runtime boundaries and persisted domain records.",
             "digitalbrain/urls.py; platform_core/{chat_views,graph_ai,graph_store,document_worker,tenancy}.py; models/")
    p.box("entry", "HTTP entry", "urls.py → views / api.py / mcp", 45, 130, w=1030, h=60)
    p.box("policy", "Access + tenancy", "policy / permissions / tenancy\nFeature, grant and row scope", 45, 240, "security", 1030, 68)
    for key, title, detail, x in [
        ("ingest", "Source pipeline", "documents / processing\nconnectors / chunks", 45),
        ("graph", "Graph pipeline", "graphs / graph_ai\ngraph_store / embeddings", 315),
        ("answer", "Answer runtime", "chat_views / ai\nagent_runtime / streaming", 585),
        ("work", "Domain workflows", "code_factory_* / serviceops_*\nautomation_execution", 855),
    ]:
        p.box(key, title, detail, x, 360)
        p.edge("policy", key)
    p.box("worker", "Claimed worker lanes", "document_worker.py / jobs.py\nRow claims; scoped execution; maintenance advisory lock", 45, 500, w=490, h=80)
    p.box("runtime", "Provider + tool boundary", "agent_runtime/tools.py binds app + user\ncredentials.py • budget.py • usage receipts", 585, 500, "security", 490, 80)
    p.box("knowledge", "Knowledge records", "KnowledgeSource → Document / KnowledgeEntry\nKnowledgeChunk • GraphRevision / GraphElement", 45, 650, "data", 490, 94)
    p.box("workflow", "Workflow records", "ChatConversation / ChatMessage\nFactoryRun / ChangePlan • TriageRun / AutomationRun", 585, 650, "data", 490, 94)
    p.box("db", "PostgreSQL + files + Redis", "Runtime DB role: row operations; migrations use owner role. Secrets: mode-0600 files. Redis: stream events and cancellation.", 45, 800, "data", 1030, 65)
    p.edge("entry", "policy")
    p.edge("ingest", "worker", dashed=True)
    p.edge("work", "worker", dashed=True)
    p.edge("graph", "knowledge")
    p.edge("answer", "runtime")
    p.edge("runtime", "workflow")
    p.edge("worker", "knowledge")
    p.edge("work", "workflow")
    p.edge("knowledge", "db")
    p.edge("workflow", "db")
    result.append(p.finish("GraphRevision retains a JSON snapshot; GraphElement adds version-range rows for scalable retrieval with JSON fallback.\nScope checks stay in Python; PostgreSQL row security adds a second layer when tenant context is bound."))

    p = Page("03-graph-generation", "03 / Graph generation process",
             "A source becomes a draft graph; an explicit publication makes it available for answers.",
             "docs/graph-generation-end-to-end.md; platform_core/{processing,graphs,graph_ai,graph_store}.py")
    specs = [
        ("source", "1. Bring sources in", "Upload, folder, link, GitHub,\nSharePoint, Jira, ServiceNow", 45, 140, "external"),
        ("store", "2. Private original", "Record size and SHA-256\nS3 or configured file store", 315, 140, "data"),
        ("scan", "3. Security scan", "ClamAV; refuse infected\nor unverifiable input", 585, 140, "security"),
        ("reject", "Rejected / failed", "Record the reason\nNever convert unsafe input", 855, 140, "error"),
        ("convert", "4. Convert + OCR", "Isolated offline conversion\nMarkdown text + digest", 585, 290, "step"),
        ("record", "5. Knowledge record", "Immutable source revision\nchunks + source identity", 315, 290, "data"),
        ("checks", "6. Advisory checks", "PII/PHI, readability, type\nand source relationships", 45, 290, "step"),
        ("structural", "7. Structural graph", "Automatic, no model call\nReuse verified unchanged AI", 45, 450, "step"),
        ("paid", "8. AI requested?", "Explicit run or owner refresh\nPermissions + monthly budget", 315, 450, "gate"),
        ("enrich", "9. AI enrichment", "Extract entities / relations\nVerify exact source quotes", 585, 450, "step"),
        ("embed", "10. Embeddings", "Optional; configured provider\nBudgeted and receipted", 855, 450, "step"),
        ("draft", "11. Draft version", "GraphRevision snapshot\nGraphElement version ranges", 315, 620, "data"),
        ("review", "12. Measure + review", "Benchmark + graph health\nReviewer inspects quality", 585, 620, "step"),
        ("publish", "13. Publish?", "An authorized person\nselects the version", 855, 620, "gate"),
        ("published", "Published graph", "Available to graph answers\nand workflow evidence", 855, 780, "data"),
        ("waiting", "Keep draft", "Review or rebuild later\nNever answers questions", 585, 780, "external"),
    ]
    for key, title, detail, x, y, kind in specs:
        p.box(key, title, detail, x, y, kind)
    for a, b, label in [("source","store",""),("store","scan",""),("scan","reject","unsafe"),("scan","convert","clean"),("convert","record",""),("record","checks",""),("checks","structural",""),("structural","paid",""),("paid","enrich","yes"),("enrich","embed",""),("embed","draft",""),("paid","draft","no"),("draft","review",""),("review","publish",""),("publish","published","yes"),("publish","waiting","not yet")]:
        p.edge(a,b,label,failure=b=="reject")
    result.append(p.finish("Advisory checks suggest; a person decides source restrictions. Restricted sources stay out of AI and retrieval.\nSource changes queue a new structural draft; paid enrichment needs an explicit or owner-authorized request."))

    p = Page("04-graph-retrieval", "04 / Graph retrieval process",
             "Rank candidate relationships, then prove every citation from its live source.",
             "docs/graph-retrieval-end-to-end.md; platform_core/{graph_ai,ranking,rerank,graph_store}.py")
    specs = [
        ("question", "Question / caller", "Chat, API, MCP, Code Factory\nCode review or ServiceOps", 45, 140, "external"),
        ("access", "Application access", "Grant + feature checks\nScope fixed before retrieval", 315, 140, "security"),
        ("version", "Published version", "Latest or conversation-pinned\nDrafts refused", 585, 140, "gate"),
        ("deny", "No evidence / refusal", "No grant, no published graph\nor no valid citations", 855, 140, "error"),
        ("query", "Read the question", "Follow-up context, terms\nKeep BR-05-like IDs whole", 585, 290, "step"),
        ("rank", "Rank by words", "Coverage then BM25\nRows or JSON snapshot", 315, 290, "step"),
        ("semantic", "Fuse by meaning", "Optional query embedding\nOwner-enabled paid help", 45, 290, "step"),
        ("walk", "Walk relationships", "Typed two-hop paths\nPlus one-hop neighbours", 45, 450, "step"),
        ("verify", "Verify live evidence", "Allowed + active + same digest\nExact quote still present", 315, 450, "security"),
        ("spread", "Diverse evidence", "Spread across sources\nKeep at most 8 citations", 585, 450, "step"),
        ("rerank", "Optional reranking", "Model reorders IDs only\nCannot invent evidence", 855, 450, "step"),
        ("caller", "Caller decides output", "Evidence API or answer flow", 585, 620, "gate"),
        ("json", "API / MCP evidence", "JSON citations + graph version\nNo model spend", 315, 620, "data"),
        ("answer", "Compose answer", "Chat model uses verified pack\nScoped tools may find more", 855, 620, "step"),
        ("final", "Verify again + save", "Keep only live valid citations\nPersist and render escaped", 855, 780, "data"),
    ]
    for key,title,detail,x,y,kind in specs:
        p.box(key,title,detail,x,y,kind)
    for a,b,label in [("question","access",""),("access","version","allowed"),("version","deny","none / draft"),("version","query","published"),("query","rank",""),("rank","semantic",""),("semantic","walk",""),("walk","verify",""),("verify","spread","valid"),("spread","rerank",""),("rerank","caller",""),("caller","json","API / MCP"),("caller","answer","chat"),("answer","final","")]:
        p.edge(a,b,label,failure=b=="deny")
    p.edge("access", "deny", "denied", failure=True)
    result.append(p.finish("Graph-search API and MCP disable paid retrieval assistance. Chat may use embeddings / reranking only when configured.\nA changed or inaccessible source yields no citation. Code Factory may add up to four verified cross-references."))

    for aws in [False, True]:
        name = "06-aws-vps-deployment" if aws else "05-hostinger-vps-deployment"
        title = "06 / AWS EC2 VPS — proposed deployment" if aws else "05 / Hostinger VPS — committed deployment"
        subtitle = "Single-host EC2 adaptation of the Compose stack; no AWS infrastructure is provisioned by this diagram." if aws else "Coolify deploys the repository stack; diagram describes configuration, not a live server audit."
        p = Page(name,title,subtitle,"docker-compose.yaml; docker-compose.scale.yaml; deploy/entrypoint.sh; deploy/README.md")
        p.box("browser","Browser / API clients","Public HTTPS",45,135,"external",300,65)
        p.box("git","Git repository","Build pinned release image\nRun release checks",420,135,"external",300,65)
        p.box("cloud","AWS VPC / public subnet" if aws else "Hostinger VPS", "" ,35,225,"group",1050,525)
        hosttitle = "EC2 Linux + EBS volumes" if aws else "Coolify + private Compose network"
        p.box("host",hosttitle,"",20,45,"group",1010,465,parent="cloud")
        p.box("proxy","TLS reverse proxy","Coolify on EC2 (proposed)" if aws else "Coolify owns :80 / :443",25,50,w=280,h=70,parent="host")
        p.box("init","Init + certificates","Secret files + TLS material\nPersist across deploys",365,50,"security",280,70,parent="host")
        p.box("app","Django app :8000","Default: web + migrations\nand background lanes",25,185,w=280,h=90,parent="host")
        p.box("pg","PostgreSQL 16","Verified TLS; runtime role\nDurable DB + graph + jobs",365,185,"data",280,90,parent="host")
        p.box("redis","Redis 7.4","Verified TLS\nEvents / Stop / rate limit",705,185,"data",280,90,parent="host")
        p.box("volumes","Persistent volumes","Runtime / credentials / originals*\ndemo workspace / AV signatures",25,350,"data",280,90,parent="host")
        p.box("scale","Optional scale overlay","Migrate → web / worker / sandbox\nClamd daemon; shared Redis",365,350,"step",280,90,parent="host")
        p.box("backup","Backup + restore plan","DB dump + persistent volumes\nEBS snapshots" if aws else "DB dump + persistent volumes\nOff-host copy + restore drill",705,350,"security",280,90,parent="host")
        p.box("s3","S3 originals + external APIs","Configured S3 bucket; Claude / OpenAI; source connectors",45,780,"external",1030,65)
        for a,b,label,dashed in [("browser","proxy","HTTPS",False),("git","app","deploy",True),("init","app","files",False),("init","pg","files",False),("init","redis","files",False),("proxy","app","private :8000",False),("app","pg","TLS",False),("app","redis","TLS",False),("app","volumes","",False),("app","s3","HTTPS",False),("scale","pg","TLS",False),("scale","redis","TLS",False),("pg","backup","backup",True),("volumes","backup","backup",True)]:
            p.edge(a,b,label,dashed)
        if aws:
            note="PROPOSED: adapt domain, hosts and bucket; provision EC2 / EBS / DNS / firewall separately. Expose 80/443; restrict SSH.\nKeep PostgreSQL / Redis internal. Single host is a failure domain; snapshots supplement database-consistent backups."
        else:
            note="Default Compose: one all-in-one app. Optional scale overlay separates roles; Redis enables cross-process Stop and replay.\n*Originals use S3 when configured. Demo target is currently selected; preserve demo-workspace and managed credentials."
        result.append(p.finish(note))

    p = Page("07-code-factory", "07 / Code Factory process",
             "Evidence-backed planning and controlled implementation with separate delivery authorization.",
             "platform_core/models/factory.py; code_factory.py; code_factory_analysis.py; code_factory_build.py")
    specs = [
        ("ticket","Ticket / manual request","Authorize feature and user\nCopy ticket + digest",45,140,"external"),
        ("pin","Pin evidence + code","Published graph version\nCode snapshot + repository",315,140,"data"),
        ("triage","1. Triage","Classify request\nExtract stated requirements",585,140,"step"),
        ("analysis","2. Analysis","Stated / functional / NFR gaps\nCite verified evidence",855,140,"step"),
        ("design","3. Design","Targets + change summaries\nCreate ChangePlan",855,300,"step"),
        ("approval","Plan approval","Authorized approver reviews\nReject or approve digest",585,300,"gate"),
        ("order","4. Work order","Approved scope\nImplementation instructions",315,300,"step"),
        ("implement","5. Implementation","Prepare candidate contents\nNo delivery credential needed",45,300,"step"),
        ("tests","6. Test author","Produce relevant tests\nAgainst approved changes",45,470,"step"),
        ("review","7. Change review","Review scope, safety\nand evidence",315,470,"step"),
        ("verify","8. Verification","Check prepared change\nRecord phase result",585,470,"security"),
        ("delivergate","Delivery gate","Confirm repo + base branch\nSeparate write credential",855,470,"gate"),
        ("delivery","9. Delivery","GitHub branch + pull request\nOr permitted local demo delivery",855,640,"external"),
        ("ci","Checks + refresh","Read repository CI outcomes\nRefresh code knowledge",585,640,"step"),
        ("receipt","Run history","Phase status, costs, events\nEvidence and output digests",315,640,"data"),
        ("blocked","Rejected / failed / stalled","No next phase on failure\nReview reason before retry",45,640,"error"),
    ]
    for key,title,detail,x,y,kind in specs:
        p.box(key,title,detail,x,y,kind)
    chain=["ticket","pin","triage","analysis","design","approval","order","implement","tests","review","verify","delivergate","delivery","ci","receipt"]
    for a,b in zip(chain,chain[1:]): p.edge(a,b,"approved" if a=="approval" else "")
    p.edge("approval","blocked","rejected",failure=True)
    p.edge("verify","blocked","failed",failure=True)
    p.edge("delivergate","blocked","not ready",failure=True)
    p.box("notephase","Prepare ≠ deliver","Preparation can finish without GitHub write access. Publishing re-checks grants, plan, repository and credentials.",45,800,"security",1030,65)
    result.append(p.finish("Each named agent phase is a bounded model call with a usage receipt. Plan approval precedes implementation.\nRepository CI executes delivered changes; local write/tests require development or the explicit hosted-demonstration exception."))

    p = Page("08-serviceops", "08 / ServiceOps process",
             "Read-only incident triage and separately gated real automation.",
             "platform_core/serviceops*.py; automation_execution.py; automation_adapters.py; docs/serviceops-real-automation.md")
    specs = [
        ("incident","Incident intake","ServiceNow / Jira import\nOr manual incident",45,140,"external"),
        ("profile","1. Profile symptoms","Service, CI, priority, terms\nDigest + fingerprint",315,140,"step"),
        ("evidence","2. Gather evidence","Ops graph + precedents\nChanges + published runbooks",585,140,"step"),
        ("model","3. Optional AI triage","At most three hypotheses\nRead-only checks only",855,140,"step"),
        ("verify","4. Verify citations","Exact quotes; source usable\nDigest and app scope",855,310,"security"),
        ("score","5. Rate evidence","Support + counter-evidence\nReport limits and uncertainty",585,310,"step"),
        ("save","6. Save brief","TriageRun + hypotheses\nEvidence-only fallback",315,310,"data"),
        ("verdict","Responder verdict","Accept / reject / partial\nRecord confirmed cause",45,310,"gate"),
        ("request","Owner requests runbook","Discover allowed catalog\nIdempotent request key",45,500,"external"),
        ("approve","Execution approval","Different owner if required\nRecheck grants + source digest",315,500,"gate"),
        ("launch","Worker launch","One active execution per app\nRegistered local or AWX adapter",585,500,"step"),
        ("poll","Monitor + verify","Local postconditions\nAWX job status + bounded poll",855,500,"step"),
        ("receipt","Execution receipt","Status, timing, effects\nNo secret-bearing stdout",855,690,"data"),
        ("reconcile","Needs reconciliation","Ambiguous external launch\nNever blindly retry POST",585,690,"error"),
    ]
    for key,title,detail,x,y,kind in specs: p.box(key,title,detail,x,y,kind)
    for a,b,label in [("incident","profile",""),("profile","evidence",""),("evidence","model","configured"),("model","verify",""),("verify","score",""),("score","save",""),("save","verdict",""),("verdict","request","separate action"),("request","approve",""),("approve","launch","authorized"),("launch","poll",""),("poll","receipt","finished"),("launch","reconcile","ambiguous")]: p.edge(a,b,label,failure=b=="reconcile")
    p.edge("evidence", "verify", "evidence only")
    result.append(p.finish("AI triage proposes explanations and read-only checks; it never launches remediation. Local runbooks have verified DB effects.\nAWX integration exists but no live controller is provisioned here. A successful external job does not prove service recovery."))

    p = Page("09-chat-application", "09 / Chat application process",
             "Persist first, retrieve scoped evidence, stream an answer and retain a verified transcript.",
             "platform_core/chat_views.py; ai.py; agent_runtime/{runtime,tools,streaming}.py; broker.py; models/chat.py")
    specs = [
        ("browser","Browser conversation","Create / resume own chat\nSelect mode when created",45,140,"external"),
        ("access","Validate submission","Session + CSRF + app grants\nMode available; deduplicate",315,140,"security"),
        ("save","Persist before provider","ChatConversation + user message\nAssistant streaming placeholder",585,140,"data"),
        ("worker","Start answer worker","Own DB connection\nBind tenant + recheck access",855,140,"step"),
        ("mode","Conversation mode","Fixed for conversation\nGraph version may be pinned",855,310,"gate"),
        ("graph","Graph answer","Published graph retrieval\nVerified graph citations",585,310,"step"),
        ("lexical","AI answer / excerpts","Keyword passage retrieval\nVerified source passages",315,310,"step"),
        ("provider","Model + scoped tools","Claude / OpenAI; budgeted\nExcerpts mode needs no model",315,490,"step"),
        ("stream","Emit answer events","Redis stream if configured\nLocal registry otherwise",585,490,"step"),
        ("sse","Browser SSE reader","Replay after disconnect\nLast-event cursor + heartbeat",855,490,"external"),
        ("stop","Stop request","Recheck access; signal producer\nRedis across replicas",855,680,"security"),
        ("finish","Finalize once","complete / stopped / failed\nGuarded idempotent DB update",585,680,"data"),
        ("citations","Verify final citations","Live source + digest + quote\nEscaped display; usage receipt",315,680,"security"),
        ("history","Saved transcript","Owned by app + user\nEdit, regenerate, retention",45,680,"data"),
    ]
    for key,title,detail,x,y,kind in specs: p.box(key,title,detail,x,y,kind)
    for a,b,label,dashed in [("browser","access","POST",False),("access","save","",False),("save","worker","async",True),("worker","mode","",False),("mode","graph","graph",False),("mode","lexical","AI / excerpts",False),("graph","provider","",False),("lexical","provider","",False),("provider","stream","tokens",True),("stream","sse","SSE",True),("sse","stop","user stop",False),("stop","stream","cancel",True),("provider","citations","answer",False),("citations","finish","",False),("finish","history","",False),("finish","sse","done",True)]: p.edge(a,b,label,dashed)
    result.append(p.finish("Provider errors close the placeholder as failed; a stopped run records partial output. Finalization always closes the stream.\nSource-excerpt mode follows the synchronous lexical path; Redis enables reconnect and cancellation across web replicas."))
    return sorted(result,key=lambda page:page.slug)


def validate(page):
    root=ET.fromstring(page.xml())
    cells=root.findall("./root/mxCell")
    ids={c.get("id") for c in cells}
    assert len(ids)==len(cells), page.slug
    for c in cells:
        for attr in ("parent","source","target"):
            if c.get(attr): assert c.get(attr) in ids, (page.slug,attr,c.get(attr))
        if c.get("edge"): assert c.find("mxGeometry").get("relative")=="1"
    by_id={n["id"]:n for n in page.nodes}
    for n in page.nodes:
        if n["parent"]!="1":
            parent=by_id[n["parent"]]
            assert n["x"]>=0 and n["y"]>=30
            assert n["x"]+n["w"]<=parent["w"]
            assert n["y"]+n["h"]<=parent["h"]
    return {"page":page.slug,"cells":len(cells),"nodes":len(page.nodes),"edges":len(page.edges),"xml_valid":True,"references_valid":True,"containment_valid":True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--submit",action="store_true")
    args=parser.parse_args()
    all_pages=pages()
    mxfile=ET.Element("mxfile",host="app.diagrams.net",agent="Codex + draw.io MCP",type="device")
    records=[]
    client=DrawioMCP() if args.submit else None
    for p in all_pages:
        record=validate(p)
        diagram=ET.SubElement(mxfile,"diagram",id=p.slug,name=p.title.split(" / ",1)[1])
        diagram.append(ET.fromstring(p.xml()))
        (OUT/f"{p.slug}.xml").write_text(p.xml(),encoding="utf-8")
        single=ET.Element("mxfile",host="app.diagrams.net",type="device")
        single.append(ET.fromstring(ET.tostring(diagram,encoding="unicode")))
        (OUT/f"{p.slug}.drawio").write_text(ET.tostring(single,encoding="unicode"),encoding="utf-8")
        if client:
            response=client.call("create_diagram",{"xml":p.xml(),"routing":"libavoid"})
            (OUT/f"{p.slug}.mcp.json").write_text(json.dumps(response,indent=2),encoding="utf-8")
            links=[url for block in response.get("content",[]) if block.get("type")=="text"
                   for url in re.findall(r"https://app\.diagrams\.net/[^\s]+",block["text"])]
            if not links: raise RuntimeError(f"MCP returned no editor URL for {p.slug}")
            record.update(mcp_created=True,editor_url=links[-1])
        else:
            prior=OUT/f"{p.slug}.mcp.json"
            if prior.exists():
                response=json.loads(prior.read_text(encoding="utf-8"))
                links=[url for block in response.get("content",[]) if block.get("type")=="text"
                       for url in re.findall(r"https://app\.diagrams\.net/[^\s]+",block["text"])]
                if links: record.update(mcp_created=True,editor_url=links[-1])
        records.append(record)
        print(f"{p.slug}: {record['nodes']} nodes; {record['edges']} edges; MCP={record.get('mcp_created',False)}",flush=True)
    (OUT/"digital-brain-design.drawio").write_text(ET.tostring(mxfile,encoding="unicode"),encoding="utf-8")
    (OUT/"validation.json").write_text(json.dumps(records,indent=2),encoding="utf-8")
    (OUT/"pages.json").write_text(json.dumps([dict(slug=p.slug,title=p.title,subtitle=p.subtitle,evidence=p.evidence,nodes=p.nodes,edges=p.edges) for p in all_pages],indent=2),encoding="utf-8")


if __name__=="__main__":
    main()
