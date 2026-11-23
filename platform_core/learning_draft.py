"""Draft a fix for an unanswered question (Knowledge → Learned; agentic plan, Phase 6).

A "gap" is a complete answer that cited nothing, and a "wrong" candidate an
answer its asker marked wrong. Deciding either means naming the source the
question should cite, which then joins the benchmark. Finding that source is
reading work, so an owner or contributor can press **Draft a fix**, and an agent
reads for them:

* **Through the harness, read-only.** `run_agent` with the knowledge tools
  (`search_knowledge`, `fetch_source`) under the `knowledge_upkeep` purpose,
  whose rule - knowledge write, owner or contributor - is read before the run,
  on every tool call and after it. It is seeded with what keyword search finds
  for the question (`workbench.retrieve`, no paid step), and keeps one turn back
  to answer in, as chat does.
* **It proposes; a person decides.** The draft is stored on the candidate, never
  as knowledge and never as a benchmark question. A proposed source counts only
  when it is one the run was shown and verified, and its quote is in that
  source's text exactly; anything else is dropped, and what is left is a note of
  what is missing, quoted as what the AI said. Accepting still takes the
  person's own "Should cite", which the draft only pre-fills.
* **Checked again when it is shown** (`shown`): a source restricted, retired or
  changed since, or a quote no longer in it, reads as no longer holding.
* **Nothing reaches anyone new.** The question was already shown to owners and
  contributors here; a note the personal-data scan flags is not kept.

Measured as the plan says, by use rather than a pack: `manage.py
learning_upkeep <app>` counts how often a drafted candidate is accepted, and
with the drafted source, against candidates decided with no draft.
"""

import json
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .services import audit

logger = logging.getLogger(__name__)

PURPOSE = "knowledge_upkeep"
#: Turns the agent itself is given; the harness reserves one more to answer in.
TOOL_TURNS = 4
TOOL_CALLS = 10
REPLY_TOKENS = 800
#: Passages keyword search seeds the run with.
SEEDED = 5
#: Changes when what the agent is asked changes, so a draft says which it was.
DRAFT_VERSION = 1
NOTE_LENGTH = 600

INSTRUCTIONS = """You look after an application's knowledge library. A person asked the \
question below and the application could not answer it from its sources. Find the one \
source that answers it, if the library has one.

Use the tools to search the sources and read them; the evidence you were handed is a \
start, not the limit. Search with other words than the question's when they find nothing.

Reply with one JSON object and nothing else:
{"source_id": "<id of the source that answers it, or null>", \
"quote": "<a sentence copied exactly from that source that answers it>", \
"missing": "<when no source answers it: what the library would need, in one or two sentences>"}

Only name a source you were shown. Copy the quote exactly, character for character. \
When nothing answers the question, say so with source_id null: a confident wrong source \
is worse than none."""


def drafting_problem(user, app):
    """Why "Draft a fix" cannot run here, or "" - said before the button is pressed."""
    from .ai import credential_problem
    from .models import AIConfiguration

    config = AIConfiguration.objects.filter(application=app, purpose=PURPOSE).first()
    if config is None:
        return "An owner must configure Knowledge upkeep in AI settings first."
    if not config.enabled:
        return "Knowledge upkeep is switched off in AI settings."
    return credential_problem(app, config.provider) or ""


def seeded(app, question):
    """What keyword search finds for the question, as chat would cite it: free."""
    from .workbench import retrieve

    return [
        {"id": str(entry.pk), "title": entry.title, "digest": entry.digest, "excerpt": passage}
        for _, entry, passage in retrieve(app, question, None, limit=SEEDED)
    ]


def asked(candidate):
    lines = [f"Question: {candidate.question}"]
    if candidate.kind == "wrong":
        lines.append("Someone marked the answer it was given wrong.")
        if candidate.note:
            lines.append(f"What they said: {candidate.note}")
    return "\n".join(lines)


def judged(answer, shown, viewer):
    """The draft a reply supports, by rules: a shown, live source whose text holds
    the quote exactly; else a note of what is missing; else nothing."""
    from .graph_ai import answer_object
    from .models import KnowledgeEntry
    from .permissions import permitted
    from .sensitivity import scan

    try:
        reply = json.loads(answer_object(answer or ""))
    except (TypeError, ValueError):
        return {"kind": "unreadable"}
    if not isinstance(reply, dict):
        return {"kind": "unreadable"}
    source_id = str(reply.get("source_id") or "").strip()
    quote = str(reply.get("quote") or "").strip()
    by_id = {citation["id"]: citation for citation in shown}
    if source_id in by_id and len(quote) >= 12:
        entry = KnowledgeEntry.objects.filter(pk=source_id).answerable().first()
        if (
            entry is not None
            and entry.digest == by_id[source_id]["digest"]
            and quote in entry.content
            and permitted(entry, viewer)
        ):
            return {
                "kind": "cite",
                "source_id": source_id,
                "title": entry.title,
                "digest": entry.digest,
                "quote": quote[:1000],
                # What the benchmark matches a citation by (`benchmark.matches`).
                "expected": entry.title[:200],
            }
    missing = str(reply.get("missing") or "").strip()[:NOTE_LENGTH]
    if missing and not scan(missing):
        return {"kind": "missing", "note": missing}
    return {"kind": "none"}


def draft(user, app_id, candidate_id):
    """Ask the agent for the source an open question should cite. Returns the
    candidate with its draft; raises what the gateway or the harness raises."""
    from .agent_runtime.harness import AgentFailure, run_agent
    from .agent_runtime.tools import ToolScope
    from .agent_runtime.usage import turns_of
    from .ai import MissingCredential, agent_configuration, failure_message, prompt_size
    from .models import LearningCandidate
    from .workbench import access

    app, grant = access(user, app_id, "knowledge", write=True)
    candidate = LearningCandidate.objects.filter(pk=candidate_id, application=app).first()
    if candidate is None:
        raise LearningCandidate.DoesNotExist
    if candidate.state != "open" or not candidate.is_question:
        raise ValidationError("Only a waiting question can be drafted.")
    if not candidate.question:
        raise ValidationError("The question is no longer here.")
    try:
        app, config, token = agent_configuration(user, app.pk, PURPOSE)
    except MissingCredential as missing:
        # Said on the page in place of the button, but it can go between the two.
        raise ValidationError(str(missing)) from None
    question = asked(candidate)
    evidence = seeded(app, candidate.question)
    try:
        result = run_agent(
            user=user,
            app=app,
            config=config,
            token=token,
            purpose=PURPOSE,
            scope=ToolScope(user_id=user.pk, app_id=app.pk, purpose=PURPOSE),
            question=question,
            citations=evidence,
            history=[],
            emit=lambda *args: None,
            max_tokens=REPLY_TOKENS,
            prompt_characters=prompt_size(
                question, evidence, [], {"instructions": INSTRUCTIONS}
            ),
            turns=TOOL_TURNS,
            tool_calls=TOOL_CALLS,
            instructions=INSTRUCTIONS,
            candidate_id=candidate.pk,
            wrap_up=True,
        )
    except AgentFailure as failure:
        logger.warning(
            "learning_draft_failed",
            extra={"event": "learning_draft_failed", "exception_type": failure.kind},
        )
        raise ValidationError(
            failure_message(failure, "The AI could not draft a fix. Nothing was changed.")
        ) from None
    found = judged(result.answer, result.citations, user)
    found.update(
        version=DRAFT_VERSION,
        model=config.model,
        steps=len(result.steps),
        turns=turns_of(result.usages),
    )
    with transaction.atomic():
        candidate = (
            LearningCandidate.objects.select_for_update()
            .filter(pk=candidate.pk, application=app)
            .first()
        )
        if candidate is None or candidate.state != "open":
            raise ValidationError("This was decided while the draft was being written.")
        candidate.draft = found
        candidate.drafted_by = user
        candidate.drafted_at = timezone.now()
        candidate.save(update_fields=["draft", "drafted_by", "drafted_at"])
        audit(
            user,
            "learning.drafted",
            candidate.pk,
            app.product.portfolio.organization,
            details={"kind": candidate.kind, "draft": found["kind"]},
        )
    return candidate


def shown(candidate, viewer):
    """The draft as the page may show it: a proposed source only while it still
    holds - answerable, unchanged, visible to this viewer, the quote in it."""
    from .models import KnowledgeEntry
    from .permissions import permitted

    found = candidate.draft
    if not isinstance(found, dict):
        return None
    if found.get("kind") != "cite":
        return found
    entry = KnowledgeEntry.objects.filter(
        pk=found.get("source_id"), application_id=candidate.application_id
    ).answerable().first()
    holds = (
        entry is not None
        and entry.digest == found.get("digest")
        and found.get("quote", "") in entry.content
        and permitted(entry, viewer)
    )
    return found if holds else {**found, "kind": "stale"}


def outcome(candidate, expected):
    """Kept on the draft when its candidate is decided: whether the person cited
    what it proposed. Called inside `learning.accept`'s transaction."""
    found = candidate.draft
    if isinstance(found, dict):
        drafted = (found.get("expected") or "").casefold()
        found["accepted_as_drafted"] = bool(drafted) and expected.strip().casefold() == drafted


def report(app, days=30):
    """Decided questions in the window, drafted against not: how many were
    accepted, and how many with the source the draft proposed."""
    from datetime import timedelta

    from .models import LearningCandidate

    since = timezone.now() - timedelta(days=days)
    decided = LearningCandidate.objects.filter(
        application=app,
        kind__in=["gap", "wrong"],
        state__in=["accepted", "rejected"],
        decided_at__gte=since,
    )
    rows = {"drafted": [0, 0, 0], "rules": [0, 0, 0]}
    for state, found in decided.values_list("state", "draft"):
        row = rows["drafted" if isinstance(found, dict) else "rules"]
        row[0] += 1
        if state == "accepted":
            row[1] += 1
            if isinstance(found, dict) and found.get("accepted_as_drafted"):
                row[2] += 1
    return {
        key: {"decided": decided_count, "accepted": accepted, "as_drafted": as_drafted}
        for key, (decided_count, accepted, as_drafted) in rows.items()
    }
