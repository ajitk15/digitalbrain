"""Code Factory before approval: triage, analysis and design, and the plan they make.

Each agent is one model call through `ask` with its own instructions, phase
limit and receipt; `execute` runs them in order for a claimed run and
re-resolves the published graph before spending anything (`NO_GRAPH`).
Split out of `code_factory`, which keeps the gates, the phases, repository
pinning and `start_run`, and imports nothing from here.
"""

import json
import re
import time
from collections import Counter

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .code_factory import (
    GROUND_RULES,
    MANUAL_TICKET_LIMIT,
    NO_GRAPH,
    digest_of,
    evidence_for,
    finish_phase,
    note,
    output_limit,
    phase_timeout,
    pin_repository,
    prevalidate,
    repository_from,
    start_phase,
)
from .code_factory_evidence import (
    code_citations,
    code_context_for,
    labelled_graph_citations,
    ticket_citation,
)
from .models import RUBRIC, ChangePlan, FactoryRun, PlanItem, RunPhase
from .services import audit

#: Bounds on what an analysis may produce.
#:
#: These are a budget, not a preference. MAX_ITEMS entries of this size have to
#: fit inside the output cap with room to spare, or the model runs past the cap
#: and the JSON arrives truncated - which is exactly what a live run did: 4,773
#: completion tokens against the analysis budget of 4,096, and nothing usable back.
#: Storage caps are not instructions; the prompt has to state the same numbers,
#: and test_code_factory pins the arithmetic.
MAX_ITEMS = 8
MAX_EXPLANATION_CHARACTERS = 600
MAX_EVIDENCE_PER_ITEM = 2
MAX_QUOTE_CHARACTERS = 300
MAX_ITEM_CHARACTERS = 4000

#: The item categories as they read when counted in a sentence.
COUNTED_CATEGORIES = {
    "stated": "asked for in the ticket",
    "functional": "functional",
    "non_functional": "non-functional",
}

TRIAGE_INSTRUCTIONS = GROUND_RULES + (
    "Classify this ticket and restate what it asks for. Return only a JSON object "
    'with: kind (one of "bug", "fix", "enhancement"), summary (one sentence), '
    "requirements (an array of short strings, each one thing the ticket explicitly "
    "asks for), and repository (an owner/name string if the ticket names a source "
    'repository, otherwise ""). Do not infer a repository that is not written down.'
)

ANALYSIS_INSTRUCTIONS = GROUND_RULES + (
    "Compare what the ticket asks for against what the evidence says the system "
    f"actually does, and against these non-functional headings: "
    f"{', '.join(label for _, label in RUBRIC)}. "
    f"Return only a JSON object with an items array of at most {MAX_ITEMS} entries. "
    'Each entry has: category (one of "stated", "functional", "non_functional"), '
    'rubric (one of "security", "availability", "performance", "auditability", or '
    '"" when the category is not non_functional), title, explanation, severity '
    '("low", "medium" or "high"), and evidence (an array of objects with source_id '
    "and quote). source_id is the number shown beside the evidence you were "
    "given - not a name, a path or an identifier from anywhere else. "
    f"Keep each explanation under {MAX_EXPLANATION_CHARACTERS} characters, give at "
    f"most {MAX_EVIDENCE_PER_ITEM} evidence entries per item, and keep each quote "
    f"under {MAX_QUOTE_CHARACTERS} characters - the shortest excerpt that carries "
    "the point. These limits are what let the whole answer arrive; exceeding them "
    "truncates it and nothing can be used. "
    "A stated item is something the ticket asks for directly. A functional gap is "
    "a difference between what is asked and what the evidence shows exists. A "
    "non-functional gap is a concern under one of those headings that the ticket "
    "does not mention but the change would raise. Prefer few well-evidenced items "
    "over many speculative ones."
)

DESIGN_INSTRUCTIONS = GROUND_RULES + (
    "For each supplied item, say exactly what should change. Return only a JSON "
    "object with an items array in the same order, each entry having: id (echo the "
    "id you were given), change_summary (what will change and why, in specific "
    "terms a reviewer can check), and targets (an array of short strings naming "
    "the components, files or settings the change touches, as far as the evidence "
    "supports; an empty array when the evidence does not say). Do not invent file "
    f"paths that no evidence mentions. Keep each change_summary under "
    f"{MAX_EXPLANATION_CHARACTERS} characters."
)


class UnusableAnswer(ValidationError):
    """The model answered, but not in the shape asked for.

    Carries a bounded sample of what came back. "Did not return usable JSON" on
    its own is unactionable - it cannot distinguish a truncated answer from prose,
    a refusal or a fenced block, and those need different fixes. The sample is the
    model's reply to our own prompt, the same class of content a chat answer is,
    and it is kept on the phase record rather than shown as an error message.
    """

    def __init__(self, message, sample=""):
        super().__init__(message)
        self.sample = sample


def parse_json(answer, label):
    """A model's JSON, or a readable failure. Fenced output is tolerated.

    Every piece between fences is tried, the last first, then the outermost
    object, then the whole reply. Nothing is repaired: a candidate is accepted
    only if it parses as it stands, so a half-written answer is never used.

    When none parses, the sample records the parser's own reason and the text
    around where it gave up. "Did not return usable JSON" with only the start
    and end of the reply could not say what was wrong with the middle.
    """
    raw = answer or ""
    text = raw.strip()
    candidates = []
    if "```" in text:
        # Split on every fence rather than pairing them, and try the pieces last
        # first. The Claude runtime joins the text of every model turn in a call,
        # so a model that began its JSON, paused for a tool, and wrote it again
        # arrives as "```json {half```json {whole} ```". Pairing fences took the
        # unfinished half and never saw the whole; the last piece is the model's
        # final answer, which is the one that counts.
        pieces = [piece.strip() for piece in re.split(r"```(?:json)?", text)]
        candidates += [piece for piece in reversed(pieces) if piece]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.append(text)
    payload, failure = None, None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            break
        except ValueError as error:
            failure = failure or error
    else:
        if isinstance(failure, json.JSONDecodeError):
            near = failure.doc[max(0, failure.pos - 80) : failure.pos + 80]
            reason = (
                f"parser stopped at line {failure.lineno}, column {failure.colno}: "
                f"{failure.msg}; near: {near!r}"
            )
        else:
            reason = "no JSON object found"
        raise UnusableAnswer(
            f"{label} did not return usable JSON.",
            sample=(
                f"{len(raw)} characters; {reason}; starts: {raw[:300]!r}; "
                f"ends: {raw[-200:]!r}"
            ),
        )
    if not isinstance(payload, dict):
        raise UnusableAnswer(f"{label} did not return a JSON object.", sample=repr(payload)[:400])
    return payload


def ask(user, app_id, phase_name, instructions, question, citations, receipt):
    """One phase's model call, with its own budget and its own receipt."""
    from .ai import invoke_ai

    return invoke_ai(
        user,
        app_id,
        "plan_drafting",
        question,
        citations,
        receipt=receipt,
        instructions=instructions,
        max_tokens=output_limit(app_id),
        timeout=phase_timeout(phase_name),
    )


def ticket_question(run, extra=""):
    """The ticket, presented as the subject of the work rather than as speech."""
    return (f"Ticket {run.ticket_external_id or '(no id)'}: {run.ticket_title}\n\n{extra}").strip()[
        :8000
    ]


def evidence_question(run, triage, body):
    """What gap analysis asks the knowledge graph: the ticket, triage's reading of
    it, and its text. The scorecard asks this too, with an empty triage, so the
    evidence it measures is the evidence a run is given.

    An imported ticket is asked without its title and connector header: their
    labels and dates outranked what the ticket says, and on the scorecard's
    planted bugs the header cost a fifth of the evidence (76% found with it,
    92% without). Triage still reads the whole record - its status and type
    are what it is asked about. Text typed by hand has no header.
    """
    from .connector_kinds import record_text

    if not run.ticket_body:
        body = record_text(body)
    asked = "\n".join(triage["requirements"])
    return ticket_question(run, f"{triage['summary']}\n\nAsked for:\n{asked}\n\n{body}")


def run_triage(run, body):
    started = time.monotonic()
    phase = start_phase(run, "triage")
    RunPhase.objects.filter(pk=phase.pk).update(input_digest=digest_of(body))
    note(run, "Triage: reading the ticket.", phase="triage")
    note(run, "Triage: asking the model what this ticket is asking for.", phase="triage")
    receipt = {}
    try:
        answer = ask(
            run.requested_by,
            run.application_id,
            "triage",
            TRIAGE_INSTRUCTIONS,
            ticket_question(run, body),
            [],
            receipt,
        )
        payload = parse_json(answer, "Triage")
        output = {
            "kind": payload.get("kind")
            if payload.get("kind") in {"bug", "fix", "enhancement"}
            else "fix",
            "summary": str(payload.get("summary") or "")[:500],
            "requirements": [str(item)[:300] for item in (payload.get("requirements") or [])[:20]],
            "repository": repository_from(payload.get("repository")),
        }
    except ValidationError as failure:
        note(run, f"Triage failed. {' '.join(failure.messages)}", phase="triage", level="problem")
        finish_phase(
            phase,
            "failed",
            started,
            error=" ".join(failure.messages),
            usage=receipt,
            sample=getattr(failure, "sample", ""),
        )
        raise
    finish_phase(phase, "ok", started, output=output, usage=receipt)
    note(
        run,
        f"Triage: read as a {output['kind']} asking for "
        f"{len(output['requirements'])} thing(s). {output['summary']}",
        phase="triage",
        level="result",
    )
    pin_repository(run, output["repository"])
    return output


def collect_items(run, payload, citations):
    """Turn the analysis payload into items, attaching the evidence they cite.

    The model says *which* supplied evidence supports an item; it does not re-quote
    it. What we hand it is already verified - graph_citations checks the source is
    active, the digest matches and the quote appears in the source before returning
    anything - and what we show a reviewer is that verified citation, not the
    model's rendition of it.

    Asking it to re-quote was a mistake worth recording: the excerpt shown to the
    model is composed ("Graph v18: A uses B / Source evidence: ..."), while
    verification compared against raw source text, so every quote failed and a live
    run rejected all nine of its citations. An item then read "no evidence was
    supplied" when the evidence was sitting right there.
    """
    # Keyed by the number the model was shown. Asking a model to carry a UUID
    # accurately through a long answer does not work - a live run cited
    # "source_id": "3" - and a citation that cannot be matched is evidence lost
    # rather than evidence checked.
    supplied = {str(index): citation for index, citation in enumerate(citations, start=1)}
    items, verified_total, rejected_total = [], 0, 0
    for entry in (payload.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(entry, dict) or not str(entry.get("title") or "").strip():
            rejected_total += 1
            continue
        # A citation naming a source we never supplied is rejected: the model may
        # only point at evidence it was actually shown.
        kept, seen = [], set()
        for cited in (entry.get("evidence") or [])[:MAX_EVIDENCE_PER_ITEM]:
            if not isinstance(cited, dict):
                rejected_total += 1
                continue
            source_id = str(cited.get("source_id") or "")
            citation = supplied.get(source_id)
            if citation is None or source_id in seen:
                rejected_total += 1
                continue
            seen.add(source_id)
            kept.append(citation)
        verified_total += len(kept)
        category = entry.get("category")
        items.append(
            {
                "category": category
                if category in {"stated", "functional", "non_functional"}
                else "functional",
                "rubric": entry.get("rubric")
                if entry.get("rubric") in {key for key, _ in RUBRIC}
                else "",
                "title": str(entry["title"])[:300],
                "explanation": str(entry.get("explanation") or "")[:MAX_ITEM_CHARACTERS],
                # Stored generously; the prompt asks for far less so the answer fits.
                "severity": entry.get("severity")
                if entry.get("severity") in {"low", "medium", "high"}
                else "medium",
                "citations": kept,
            }
        )
    return items, verified_total, rejected_total


def run_analysis(run, triage, body):
    started = time.monotonic()
    phase = start_phase(run, "analysis")
    question = evidence_question(run, triage, body)
    if run.code_snapshot_id:
        note(run, "Gap analysis: reading the pinned code snapshot.", phase="analysis")
    code_context = code_context_for(run, question)
    if code_context:
        question = f"{question}\n\nPinned code structure:\n{code_context}"
    note(
        run,
        f"Gap analysis: connecting to the {run.application.name} knowledge graph "
        f"v{run.graph_version}.",
        phase="analysis",
    )
    graph = labelled_graph_citations(
        evidence_for(run.application_id, question, run.graph_version, run.requested_by),
        run.graph_version,
    )
    # Every piece of evidence says where it came from: the ticket itself, the
    # knowledge graph (and the system the passage was imported from), or the
    # pinned code snapshot. The model is shown the label in each title.
    # Graph passages keep the first numbers - they are the primary evidence -
    # then the ticket, then the code.
    extra = ticket_citation(run) + code_citations(run, question)
    citations = graph + extra
    also = f" Also offered: {len(extra)} from the ticket and the code graph." if extra else ""
    note(
        run,
        f"Connected. {len(graph)} passage(s) came back verified against their sources.{also}"
        if graph
        # A published graph that matches nothing is a real answer, not a missing
        # one: the run continues, and the thin evidence shows on every item.
        else "Connected, but nothing in the graph matched this ticket. The items "
        f"this produces will carry little or no graph evidence.{also}",
        phase="analysis",
        level="result" if graph else "check",
    )
    note(
        run,
        "Gap analysis: asking the model to compare what the ticket asks for "
        "against what the evidence says exists.",
        phase="analysis",
    )
    # Numbered for the prompt; the stored citation is always the original.
    numbered = [
        {
            **citation,
            "id": str(index),
            "title": f"[{citation.get('origin_label', '')}] {citation.get('title', '')}",
        }
        for index, citation in enumerate(citations, start=1)
    ]
    RunPhase.objects.filter(pk=phase.pk).update(input_digest=digest_of([question, citations]))
    receipt = {}
    try:
        answer = ask(
            run.requested_by,
            run.application_id,
            "analysis",
            ANALYSIS_INSTRUCTIONS,
            question,
            numbered,
            receipt,
        )
        payload = parse_json(answer, "Analysis")
        items, verified_total, rejected_total = collect_items(run, payload, citations)
    except ValidationError as failure:
        finish_phase(
            phase,
            "failed",
            started,
            error=" ".join(failure.messages),
            usage=receipt,
            sample=getattr(failure, "sample", ""),
        )
        raise
    except Exception:
        # A phase that stops for any other reason must still say so: leaving the
        # row as "running" while the run is failed makes the record a liar.
        finish_phase(
            phase, "failed", started, error="The analysis stopped unexpectedly.", usage=receipt
        )
        raise
    if not items:
        finish_phase(
            phase,
            "failed",
            started,
            error="The analysis produced no items that could be read.",
            citations=(verified_total, rejected_total),
            usage=receipt,
        )
        raise ValidationError("The analysis produced no items that could be read.")
    finish_phase(
        phase,
        "ok",
        started,
        output={"items": items},
        citations=(verified_total, rejected_total),
        usage=receipt,
    )
    counts = Counter(item["category"] for item in items)
    # The stored labels read as headings ("Non-functional gap"), which does not
    # pluralise in a sentence. Counted, they want the shorter word.
    breakdown = ", ".join(
        f"{counts[key]} {word}" for key, word in COUNTED_CATEGORIES.items() if counts[key]
    )
    note(
        run,
        f"Gap analysis found {len(items)} item(s): {breakdown}. "
        f"{verified_total} quotation(s) verified"
        + (f", {rejected_total} rejected as unverifiable." if rejected_total else "."),
        phase="analysis",
        level="result",
    )
    return items


def run_design(run, triage, items):
    started = time.monotonic()
    phase = start_phase(run, "design")
    numbered = [
        {"id": index, "title": item["title"], "explanation": item["explanation"]}
        for index, item in enumerate(items)
    ]
    question = ticket_question(run, json.dumps({"items": numbered}))
    note(
        run,
        f"Change design: asking the model what should change for each of the "
        f"{len(items)} item(s), and which files it touches.",
        phase="design",
    )
    # Design is the phase that names the files implementation will read, so it
    # is the phase that most needs to know which files exist.
    code_context = code_context_for(run, question)
    if code_context:
        question = f"{question}\n\nPinned code structure:\n{code_context}"
    RunPhase.objects.filter(pk=phase.pk).update(input_digest=digest_of([numbered, code_context]))
    receipt = {}
    try:
        answer = ask(
            run.requested_by,
            run.application_id,
            "design",
            DESIGN_INSTRUCTIONS,
            question,
            [],
            receipt,
        )
        payload = parse_json(answer, "Design")
    except ValidationError as failure:
        finish_phase(
            phase,
            "failed",
            started,
            error=" ".join(failure.messages),
            usage=receipt,
            sample=getattr(failure, "sample", ""),
        )
        raise
    designs = {}
    for entry in (payload.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        designs[index] = {
            "change_summary": str(entry.get("change_summary") or "")[:MAX_ITEM_CHARACTERS],
            "targets": [str(t)[:200] for t in (entry.get("targets") or [])[:12]],
        }
    for index, item in enumerate(items):
        design = designs.get(index, {})
        item["change_summary"] = design.get("change_summary", "")
        item["targets"] = design.get("targets", [])
    finish_phase(phase, "ok", started, output={"items": items}, usage=receipt)
    named = sorted({target for item in items for target in item["targets"]})
    note(
        run,
        f"Change design: {len(items)} item(s) designed, naming {len(named)} target(s)"
        + (f": {', '.join(named[:8])}." if named else ", none of them a file."),
        phase="design",
        level="result",
    )
    return items


def execute(run):
    """Drive one queued run through Build A's phases.

    Runs on the worker. Each phase records itself; a failure stops the run there
    and leaves the record showing exactly which phase failed and why.
    """
    from .graphs import published_revision
    from .models import KnowledgeEntry

    claimed = FactoryRun.objects.filter(pk=run.pk, status="pending").update(status="running")
    if not claimed:
        return False
    # A hand-written ticket carries its own text. Otherwise the body is looked up
    # by the ticket's address - and only when there is one, since an empty
    # source would otherwise match any free-text note.
    entry = (
        KnowledgeEntry.objects.filter(
            application_id=run.application_id, source=run.ticket_url
        ).answerable().first()
        if run.ticket_url and not run.ticket_body
        else None
    )
    body = (run.ticket_body or (entry.content if entry else run.ticket_title))[:MANUAL_TICKET_LIMIT]
    prevalidate(run, entry)
    # Re-resolved rather than trusted from start_run, for the same reason
    # code_context_for re-checks access: a run sits in a queue, and a revision
    # can be withdrawn while it waits. Unlike the code context, this is not
    # something the run can continue without.
    if published_revision(run.application_id) is None:
        note(run, NO_GRAPH, level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(
            status="failed", error=NO_GRAPH, finished_at=timezone.now()
        )
        return True
    try:
        triage = run_triage(run, body)
        items = run_analysis(run, triage, body)
        items = run_design(run, triage, items)
        plan = build_plan(run, triage, items)
    except ValidationError as failure:
        note(run, "The run stopped here. Nothing was changed anywhere.", level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(
            status="failed", error=" ".join(failure.messages)[:2000], finished_at=timezone.now()
        )
        return True
    except Exception:
        note(run, "The run stopped unexpectedly. Nothing was changed anywhere.", level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(
            status="failed",
            error="The run stopped unexpectedly. See the phase record.",
            finished_at=timezone.now(),
        )
        raise
    FactoryRun.objects.filter(pk=run.pk).update(
        status="awaiting_review", plan=plan, finished_at=timezone.now()
    )
    note(
        run,
        f"Waiting for approval. Open the plan to read each of the "
        f"{len(items)} item(s) with its evidence, then approve or reject it. "
        "Nothing is written anywhere until somebody does.",
        level="result",
    )
    return True


def build_plan(run, triage, items):
    """Turn the designed items into a plan awaiting human review."""
    from .models import KnowledgeEntry  # noqa: F401 - documents the provenance chain

    with transaction.atomic():
        proposal = "\n\n".join(
            f"{index}. [{item['category']}] {item['title']}\n{item['explanation']}"
            for index, item in enumerate(items, start=1)
        )[:20000]
        validation = (
            "Each item below states what changes. Verify every citation against its "
            "source before approving, and confirm the repository named in the ticket "
            "is the one you intend to change."
        )
        plan = ChangePlan.objects.create(
            application=run.application,
            author=run.requested_by,
            title=(triage["summary"] or run.ticket_title)[:200],
            proposal=proposal,
            validation=validation,
            # The digest travels with the id because approval re-checks it:
            # a plan may only be approved while the evidence it rests on is
            # unchanged. Written without one, every Code Factory plan reached
            # the review gate and died there on a KeyError.
            sources=[
                {
                    "id": citation["id"],
                    "title": citation.get("title", ""),
                    "digest": citation.get("digest", ""),
                    "knowledge_link": citation.get("knowledge_link"),
                }
                for item in items
                for citation in item["citations"]
            ][:50],
            digest=digest_of([run.ticket_digest, items]),
            graph_version=run.graph_version,
        )
        PlanItem.objects.bulk_create(
            PlanItem(
                plan=plan,
                sequence=index,
                category=item["category"],
                rubric=item["rubric"],
                title=item["title"],
                explanation=item["explanation"],
                change_summary=item["change_summary"],
                targets=item["targets"],
                citations=item["citations"],
                severity=item["severity"],
            )
            for index, item in enumerate(items)
        )
        audit(
            run.requested_by,
            "factory.plan_drafted",
            plan.pk,
            run.application.product.portfolio.organization,
            details={
                "run": str(run.pk),
                "items": len(items),
                "graph_version": run.graph_version,
            },
        )
    note(run, f'Wrote the change plan "{plan.title}" with {len(items)} item(s).', level="result")
    return plan


def process_next_run():
    """One queued run, for the worker's factory lane."""
    run = FactoryRun.objects.filter(status="pending").order_by("created_at").first()
    if run is None:
        return False
    from .tenancy import scoped

    with scoped(run.application_id):
        return execute(run)
