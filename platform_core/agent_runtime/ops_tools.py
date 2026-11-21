"""Read-only tools for ServiceOps triage (agentic plan, Phase 3).

Triage is handed one evidence pack, gathered by walking the operations graph out
from the incident (`serviceops_triage.evidence_pack`). An on-call engineer goes
further: opens a similar incident to see what was around *it*, or looks up what
the runbooks say about a component the pack only names. These tools let the
triage model do the same, and nothing more:

* **`incident_context`** walks the graph out from another incident of this
  application - its precedents, the changes before it, related open incidents,
  runbook passages, knowledge articles and automations - through the same
  `neighbourhood` the incident page and the pack use, so the model is shown what a
  person would be. The asked incident's *own* confirmed cause is never in it: a
  cause people confirmed on a similar incident is a lead, and is there already;
  the asked one's own is the answer triage is for.
* **`search_published_knowledge`** searches the latest published knowledge graph,
  verified like every graph citation, charged to nobody (`spend=False`).

With `search_knowledge` and `fetch_source`, which serve triage too. Each is read
through the triage purpose's rule again on every call (`tools._scoped_application`),
and an automation is described, never run: no tool here can start anything.
Everything a tool returns is a citation the harness verifies against its live
source, and joins the run's evidence, so a hypothesis may cite it and the stored
brief checks it again on every read.
"""

import uuid

from .tools import ToolResult, ToolSpec, _scoped_application

TRIAGE_PURPOSES = frozenset({"serviceops_triage"})
#: Items one `incident_context` call returns, and the characters of each.
CONTEXT_ITEMS = 12
CONTEXT_CHARACTERS = 900
NOT_AN_INCIDENT = "No incident with that id is available in this application."


def _viewer(scope):
    from ..models import User

    return User.objects.get(pk=scope.user_id)


def incident_context(scope, args):
    from ..permissions import permitted
    from ..serviceops import incident_queryset, verified
    from ..serviceops_triage import evidence_pack

    app = _scoped_application(scope)
    try:
        incident_id = uuid.UUID(str(args.get("incident_id", "")))
    except (ValueError, AttributeError, TypeError):
        return ToolResult(NOT_AN_INCIDENT, found=False)
    incident = incident_queryset(app).filter(pk=incident_id).first()
    viewer = _viewer(scope)
    if incident is None or not verified(incident) or not permitted(incident, viewer):
        return ToolResult(NOT_AN_INCIDENT, found=False)
    pack, _ = evidence_pack(app, incident, own_confirmed=False)
    shown = [row for row in pack if row["id"] != str(incident.pk)][:CONTEXT_ITEMS]
    if not shown:
        return ToolResult(f"Nothing in the operations graph is connected to {incident.title}.")
    lines = [f"Around {incident.title}: {len(shown)} item(s)."]
    citations = []
    for row in shown:
        excerpt = row["excerpt"][:CONTEXT_CHARACTERS]
        lines.append(
            f"id={row['id']} [{row['kind']}] {row['title']}"
            + (f" (found via: {row['path']})" if row.get("path") else "")
            + f"\n{excerpt}"
        )
        citations.append(
            {"id": row["id"], "title": row["title"], "digest": row["digest"], "excerpt": excerpt}
        )
    return ToolResult("\n\n".join(lines), citations)


def search_published_knowledge(scope, args):
    from django.core.exceptions import ValidationError

    from ..graph_ai import graph_citations

    app = _scoped_application(scope)
    query = str(args.get("query") or "").strip()[:500]
    if not query:
        return ToolResult("Provide words to search for.", found=False)
    try:
        found = graph_citations(app.pk, query, user=_viewer(scope), spend=False)
    except ValidationError:
        return ToolResult("No knowledge is published for this application.", found=False)
    if not found:
        return ToolResult(f"Nothing published matched {query!r}.")
    lines = [f"{len(found)} published passage(s) for {query!r}:"]
    citations = []
    for index, citation in enumerate(found, 1):
        # The quote is the source's own words; the graph's excerpt is not.
        quote = citation.get("quote") or citation["excerpt"]
        lines.append(f"[{index}] id={citation['id']} title={citation['title']}\n{quote}")
        citations.append(
            {
                "id": citation["id"],
                "title": citation["title"],
                "digest": citation["digest"],
                "excerpt": quote,
            }
        )
    return ToolResult("\n\n".join(lines), citations)


def _schema(**properties):
    return {
        "type": "object",
        "properties": {
            name: {"type": kind, "description": description}
            for name, (kind, description) in properties.items()
        },
        "required": list(properties),
        "additionalProperties": False,
    }


INCIDENT_CONTEXT = ToolSpec(
    name="incident_context",
    description=(
        "What the operations graph connects to another incident of this application: "
        "its similar resolved incidents, changes made shortly before it, related open "
        "incidents, runbook passages, knowledge articles and automations. Use an "
        "incident id from the evidence."
    ),
    run=incident_context,
    schema=_schema(incident_id=("string", "An incident id from the evidence.")),
    detail=("incident_id", 64),
    purposes=TRIAGE_PURPOSES,
)

SEARCH_PUBLISHED = ToolSpec(
    name="search_published_knowledge",
    description=(
        "Search this application's published knowledge - runbooks, design documents, "
        "controls - for passages about a component, service or symptom."
    ),
    run=search_published_knowledge,
    schema=_schema(query=("string", "A component, service or symptom to look up.")),
    detail=("query", 120),
    purposes=TRIAGE_PURPOSES,
)

OPS_TOOLS = (INCIDENT_CONTEXT, SEARCH_PUBLISHED)
