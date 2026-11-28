"""Every agent's measurement from one command: what is ready, then the runs.

    manage.py measure_agents <application id> --as <username>               (free)
    manage.py measure_agents <application id> --as <username> --load-questions  (free)
    manage.py measure_agents <application id> --as <username> --run          (paid)

Each agent is measured by its own pack (`agent_measurements.PACKS`), each needing
something the application may not have yet: a code snapshot holding the planted
files, documentation with components, the CarePath documents, benchmark
questions, incidents whose cause a person confirmed, the AI task configured. Run
one at a time, a pack missing its data used to fail - or worse, compare nothing -
and say so only at the end. This checks each pack the way the pack itself checks,
cheaply and before anything is spent (`readiness`), and says what to set up for
every one that is not ready; with `--run` it runs every ready pack with
`--record`, one after another, then `llm_drift --record`.

Nothing here judges or records anything itself: each pack keeps its own verdict,
and one that compared nothing records nothing (`agent_measurements.nothing_compared`).
`--load-questions` adds the demonstration's benchmark questions
(`documentation/demo/demo-kit/benchmark-questions.json`) to an application that has
none, the questions `chat_pack` measures against - the one setup step that is a
copy of repository files and costs nothing.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from django.core.exceptions import ValidationError

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "documentation" / "demo" / "demo-kit" / "benchmark-questions.json"


@dataclass(frozen=True)
class Check:
    pack: str
    agent: str
    #: The AI settings task its runs spend under.
    purpose: str
    #: What the pack needs beyond its task: raises ValidationError saying what to
    #: set up, or returns what it found.
    ready: callable


def configured(app, purpose):
    from .models import AIConfiguration

    return AIConfiguration.objects.filter(
        application=app, purpose=purpose, enabled=True
    ).exclude(model="").exists()


def task_label(purpose):
    from .models import AI_PURPOSES

    return dict(AI_PURPOSES).get(purpose, purpose)


def _snapshot(module):
    def check(app):
        module.snapshot_for(app, module.load_pack())
        return "the CarePath code snapshot is indexed"
    return check


def _documentation(app):
    from . import docs_pack

    doc = docs_pack.documentation(app)
    return f"documentation of {doc.repository.name} has components"


def _documents(app):
    from . import document_review_pack

    items = document_review_pack.load_pack()["items"]
    here = [
        key for key, item in items.items()
        if (entry := document_review_pack.source_for(app, item["document"])) is not None
        and item["find"] in entry.content
    ]
    if not here:
        names = ", ".join(sorted({item["document"] for item in items.values()}))
        raise ValidationError(
            f"None of the CarePath documents it plants a contradiction in is here ({names}). "
            "Upload documentation/demo/docs on Knowledge → Documents."
        )
    return f"{len(here)} of its {len(items)} planted contradictions have their document here"


def _analysis(app):
    from . import analysis_pack
    from .graphs import published_revision
    from .models import KnowledgeEntry

    if published_revision(app.pk) is None:
        raise ValidationError("Nothing is published: publish a version on Knowledge → Versions.")
    key = analysis_pack.load_key()
    tickets = [care for care in key if analysis_pack.ticket_for(app, care) is not None]
    if not tickets:
        raise ValidationError(
            "None of the CarePath tickets (CARE-401 to CARE-437) is imported here."
        )
    identifiers = {i for found in key.values() for i in found}
    sources = KnowledgeEntry.objects.answerable().filter(application=app)
    named = [i for i in sorted(identifiers) if sources.filter(content__contains=i).exists()]
    if len(named) < len(identifiers) // 2:
        raise ValidationError(
            f"Only {len(named)} of the {len(identifiers)} planted identifiers are named by any "
            "source here, so its plans could not cite them: the CarePath documents are not "
            "loaded. Upload documentation/demo/docs on Knowledge → Documents, then publish."
        )
    return f"{len(tickets)} tickets and {len(named)} of {len(identifiers)} identifiers here"


def _questions(app):
    from . import chat_pack

    found = chat_pack.questions(app)
    if not found:
        raise ValidationError(
            "No benchmark questions: run this with --load-questions, or add them on "
            "Knowledge → Quality."
        )
    return f"{len(found)} benchmark questions"


def _cases(app):
    from . import triage_pack

    found = triage_pack.cases(app)
    if found:
        return f"{len(found)} incidents with a confirmed cause"
    labelled = triage_pack.labelled_cases(app)
    if labelled:
        return f"{len(labelled)} labelled incidents (run with --labelled)"
    raise ValidationError(
        "No incident has a cause a person confirmed: triage incidents and record "
        "verdicts first, or load the labelled CarePath incidents with "
        "`triage_pack <app> --as <user> --load-incidents`."
    )


def triage_labelled(app):
    """Whether triage is measured against the pack's labels here: no cause a person
    confirmed, and the labelled incidents loaded."""
    from . import triage_pack

    return not triage_pack.cases(app) and bool(triage_pack.labelled_cases(app))


def checks():
    from . import factory_pack, review_pack

    return (
        Check("factory_pack", "Code Factory agent", "plan_drafting", _snapshot(factory_pack)),
        Check("review_pack", "Code review agent", "code_review", _snapshot(review_pack)),
        Check("docs_pack", "Code documentation agent", "code_docs", _documentation),
        Check("document_review_pack", "Document review agent", "document_review", _documents),
        Check("analysis_pack", "Gap analysis agent", "plan_drafting", _analysis),
        Check("chat_pack", "Chat agent", "chat", _questions),
        Check("triage_pack", "Incident triage agent", "serviceops_triage", _cases),
    )


def readiness(app):
    """[(check, ready, what it found or what to set up)], spending nothing."""
    found = []
    for check in checks():
        if not configured(app, check.purpose):
            found.append((check, False, f"Set up {task_label(check.purpose)} in AI settings."))
            continue
        try:
            found.append((check, True, check.ready(app)))
        except ValidationError as missing:
            found.append((check, False, " ".join(missing.messages)))
    return found


def drift_ready(app):
    return configured(app, "graph_generation") and configured(app, "graph_retrieval")


def load_questions(app, user):
    """Add the demonstration's benchmark questions to an application with none.
    Returns how many were added."""
    from .models import BenchmarkQuestion

    if BenchmarkQuestion.objects.filter(application=app).exists():
        return 0
    rows = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    BenchmarkQuestion.objects.bulk_create(
        BenchmarkQuestion(application=app, question=row["question"][:500],
                          expected=row["expected"][:200], created_by=user)
        for row in rows
    )
    return len(rows)
