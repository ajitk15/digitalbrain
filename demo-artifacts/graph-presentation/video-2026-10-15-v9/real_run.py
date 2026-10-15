"""The real AI build behind the film: five CarePath files, one Claude extraction, one answer.

Runs in a throwaway database, as the scorecard does, so no live application changes.
Converts each file with the production converter, scores it, builds the structural
graph, asks Claude for typed relationships (the paid part, through the platform's own
extraction and quote check), publishes the draft, reads its themes and search terms,
then asks the film's question against the published version.

Writes real-run.json. Run from the repository root with the .venv interpreter:
    .venv/Scripts/python.exe demo-artifacts/graph-presentation/video-2026-10-15-v9/real_run.py
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "digitalbrain.settings")

import django  # noqa: E402

django.setup()

FILES = [
    "01-requirements/non-functional-requirements.xlsx",
    "03-build/coding-standards.md",
    "03-build/api-specification.html",
    "04-test/traceability-matrix.xlsx",
    "06-operate/incident-2026-08-14-postmortem.docx",
]
QUESTION = "Why did the service go down on the fourteenth of August?"


def main():
    from platform_core import graph_ai, graphs, scorecard
    from platform_core.ai import invoke_ai, seed_application_ai
    from platform_core.graph_quality import node_themes
    from platform_core.models import (
        AIUsage,
        Application,
        ApplicationGrant,
        GraphRevision,
        KnowledgeEntry,
        KnowledgeGraph,
        Organization,
        OrganizationMember,
        Portfolio,
        Product,
        User,
    )
    from platform_core.ranking import words
    from platform_core.workbench import add_knowledge

    org = Organization.objects.create(name="Film")
    product = Product.objects.create(name="Care", portfolio=Portfolio.objects.create(name="Care", organization=org))
    app = Application.objects.create(name="CarePath film", product=product)
    owner = User.objects.create_user("film-owner", email="owner@film.example")
    OrganizationMember.objects.create(organization=org, user=owner, is_admin=True)
    ApplicationGrant.objects.create(application=app, user=owner, role="owner")
    provider = seed_application_ai(owner, app, provider="claude")

    sources = []
    for name in FILES:
        text = scorecard.convert(scorecard.DOCS / name)
        entry = add_knowledge(owner, app.pk, name, text)
        entry.refresh_from_db()
        sources.append({"file": name, "id": str(entry.pk), "characters": len(text),
                        "quality": entry.quality})

    raw = []
    real_extract = graph_ai.extract_relationships

    def recording(app_id, config, citation, receipt=None):
        receipt = {} if receipt is None else receipt
        found = real_extract(app_id, config, citation, receipt=receipt)
        raw.append({"source": citation["title"], "receipt": receipt, "items": found})
        return found

    from platform_core.models import AIConfiguration

    config = AIConfiguration.objects.get(application=app, purpose="graph_generation")
    KnowledgeGraph.objects.update_or_create(
        application=app,
        defaults={"status": "queued", "requested_provider": config.provider,
                  "requested_model": config.model, "requested_by": owner},
    )
    with patch.object(graph_ai, "extract_relationships", recording):
        graphs.rebuild(app.pk)
    revision = GraphRevision.objects.filter(application=app).latest("number")
    graphs.publish_revision(owner, app.pk, revision.number)

    titles = {str(e.pk): e.title for e in KnowledgeEntry.objects.filter(application=app)}
    lookup = {s["id"]: {"id": s["id"], "title": s["file"], "excerpt": KnowledgeEntry.objects.get(pk=s["id"]).content,
                        "digest": KnowledgeEntry.objects.get(pk=s["id"]).digest} for s in sources}
    proposals = []
    for call in raw:
        for item in call["items"]:
            _, reason = graph_ai.judge(item, lookup)
            proposals.append({**item, "source_title": titles.get(item.get("source_id"), call["source"]),
                              "verdict": reason or "accepted"})

    data = revision.data
    nodes = {n["id"]: n for n in data["nodes"]}
    typed = [
        {"subject": nodes[e["source"]]["label"], "subject_type": nodes[e["source"]].get("type"),
         "relation": e["relation"], "object": nodes[e["target"]]["label"],
         "object_type": nodes[e["target"]].get("type"), "source": titles.get(e["knowledge_id"]),
         "line": e.get("line"), "quote": e.get("evidence")}
        for e in data["edges"] if e.get("inferred")
    ]
    themes = node_themes(data)["themes"]

    citations = graph_ai.graph_citations(app.pk, QUESTION, user=owner)
    receipt = {}
    answer = invoke_ai(owner, app.pk, "graph_retrieval", QUESTION, citations, receipt=receipt)

    entries = list(KnowledgeEntry.objects.filter(application=app))
    terms = {w: sum(w in set(words(e.content)) for e in entries) for w in dict.fromkeys(words(QUESTION))}

    record = {
        "question": QUESTION,
        "provider": provider,
        "model": config.model,
        "sources": sources,
        "revision": {"number": revision.number, "quality": revision.quality},
        "proposals": proposals,
        "typed_relationships": typed,
        "themes": themes,
        "question_terms_in_sources": terms,
        "citations": [{k: c.get(k) for k in ("title", "label", "excerpt", "line", "relation", "reached_by", "source")}
                      for c in citations],
        "answer": answer,
        "answer_receipt": receipt,
        "extraction_receipts": [c["receipt"] for c in raw],
        "usage": [{"purpose": u.purpose, "input": u.input_tokens, "output": u.output_tokens, "amount": str(u.amount)}
                  for u in AIUsage.objects.filter(application=app)],
    }
    (HERE / "real-run.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("model", "themes", "question_terms_in_sources")}, indent=2))
    print("proposals", len(proposals), "accepted", sum(p["verdict"] == "accepted" for p in proposals))
    print("answer:", answer)


if __name__ == "__main__":
    from platform_core.scorecard import throwaway_database

    with throwaway_database(), patch("platform_core.embeddings.configuration", return_value=None):
        main()
