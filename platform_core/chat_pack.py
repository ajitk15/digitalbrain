"""The chat agent with tools and without, against the application's benchmark questions.

    manage.py chat_pack <application id> --as <username> [--record] [--only tools] [--routed]

Step 4 of measuring the agents (`documentation/engineering/agentic-ai-plan.md`):
the one agent no pack measured. `llm_drift` asks its answer questions as single
calls (`graph_retrieval`), and the benchmark calls no model, so whether chat's
tools find and cite the right source more often than one call over the same
evidence had never been counted. **Charges apply**: each question is asked twice
through the application's own Chat conversation configuration, credential and
monthly budget, receipted as usual - one call, then up to `harness.TURNS` turns
with tools and the turn kept back - and its evidence is found once, as chat finds
it (`workbench.lexical_citations`, which reranks when reranking is on: one paid
call a question, shared by both ways). Run deliberately, never by the suite.

The questions are the application's own `BenchmarkQuestion` rows - the ones the
graph benchmark and the release check already score - so a person maintains one
list, and a changed list is a new reference (`pack`, its digest).

Both ways go through `harness.run_agent`, exactly as a streamed chat answer does
(`ai.stream_chat_answer`): one call is a run allowed one turn, which the harness
gives no tools and answers from the evidence alone, so the prompt, the evidence,
the checks and the receipts are the same and only the tools differ.

Scored by rules, from the answer and the citations the run verified - the list a
person sees under the answer, numbered as the page numbers it (`chat_text.source_chips`):
whether a citation marker (`[n]`) the answer wrote points there to the expected
source (`benchmark.matches`), whether its first marker
does, whether that source was in front of the model at all, and markers that
point at nothing. A marker is read as the page reads it, never as the model may
have meant it: search replies number passages by the run's list and the answer ends
numbered as the page shows it (`CitationRecorder.numbered`), which takes a marker to
nothing out - so "citations to nothing" counts what that missed. "Expected source in
front of it" says whether the model could have cited it at all. Cost and turns are
read from the receipts the case wrote, so a run that failed out of turns is still
counted. Nothing is written but receipts:
no conversation, no message, no step.

**Model routing** (`--routed`, `ai.routing_config`). With Chat searching set up and
switched on, a third way asks each question as chat then does: the searches on that
cheaper model, and the answer on Chat conversation's model over what they found. The
tools way is asked with routing off (`run_agent(routing=False)`), so it is the one
model doing both, and `routing_verdict` holds the routed way to it: no fewer expected
sources cited, or cited first, no more citations to nothing or failures, and cheaper.
Its receipts are read under both tasks (`RECEIPT_PURPOSES`), each at its own model's
price. The measurement kept on the agent's page is the first two ways; the routed
totals stay in the record's file.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured, ValidationError

from .agent_measurements import mark, measuring

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "benchmarks" / "chat"
MODES = ("one_shot", "tools")
#: The third way, asked only when told to (`--routed`).
ROUTED = "routed"
#: Chat's receipts, and those of its searches when they were routed.
RECEIPT_PURPOSES = ("chat", "chat_search")
#: Tools are worth their cost only at no more than this many times one call, and
#: no worse on what is scored - the bar every agent's pack holds.
COST_BAR = 2
MAX_QUESTIONS = 50


def questions(app, limit=MAX_QUESTIONS):
    from .models import BenchmarkQuestion

    return list(BenchmarkQuestion.objects.filter(application=app).order_by("question")[:limit])


def pack_digest(found):
    rows = sorted((question.question, question.expected) for question in found)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def score(answer, citations, expected, entries):
    """What the answer cited, read from its markers into the verified citations."""
    from .benchmark import matches
    from .drift import markers

    numbers = markers(answer or "")
    good = [n for n in numbers if 1 <= n <= len(citations)]

    def expected_at(number):
        entry = entries.get(citations[number - 1]["id"])
        return entry is not None and matches(entry, expected)

    return {
        "cited_expected": any(expected_at(n) for n in good),
        "cited_first": bool(good) and expected_at(good[0]),
        "shown_expected": any(
            (entry := entries.get(c["id"])) is not None and matches(entry, expected)
            for c in citations
        ),
        "with_citation": bool(good),
        "invented": len(numbers) - len(good),
    }


def seeded(app, question, user):
    """The evidence a chat answer starts from, found as chat finds it."""
    from .workbench import lexical_citations

    return lexical_citations(app, question, user)


def ask(user, app, config, token, question, evidence, mode):
    """One question one way: (answer, citations, tool calls, failure)."""
    from .agent_runtime.harness import TURNS, AgentFailure, run_agent
    from .agent_runtime.tools import ToolScope
    from .ai import prompt_size, reply_cap

    try:
        result = run_agent(
            user=user,
            app=app,
            config=config,
            token=token,
            purpose=config.purpose,
            scope=ToolScope(user_id=user.pk, app_id=app.pk, purpose="chat"),
            question=question,
            citations=evidence,
            history=[],
            emit=lambda *args: None,
            max_tokens=reply_cap(app.pk, config.purpose, config),
            prompt_characters=prompt_size(question, evidence, []),
            # One turn is how the harness asks with no tools, over the evidence alone.
            turns=1 if mode == "one_shot" else TURNS,
            wrap_up=mode != "one_shot",
            # Only the routed way is routed: the tools way is the one model doing both.
            routing=mode == ROUTED,
        )
    except AgentFailure as failure:
        return "", [], 0, failure.kind
    except (ValidationError, ImproperlyConfigured) as refused:
        return "", [], 0, " ".join(getattr(refused, "messages", [str(refused)]))[:300]
    return result.answer, result.citations, len(result.steps), ""


def receipts(app, user, since, counted=None):
    """(turns, prompt tokens, completion tokens, amount) the case's receipts recorded:
    since it started, never one an earlier case of this run counted
    (`agent_measurements.case_receipts`)."""
    from .agent_measurements import case_receipts
    from .models import AIUsage

    return case_receipts(
        AIUsage.objects.filter(application=app, actor=user, purpose__in=RECEIPT_PURPOSES),
        since,
        set() if counted is None else counted,
    )


@measuring
def run(user, app, modes=MODES, limit=MAX_QUESTIONS):
    """Every question every way. Returns the record `save` writes."""

    from .ai import chat_configuration, routing_config, routing_problem
    from .models import KnowledgeEntry

    found = questions(app, limit)
    if not found:
        raise ValidationError(
            "This application has no benchmark questions. Add them under Knowledge → "
            "Quality → Benchmark first: the pack measures chat against them."
        )
    app, config, token = chat_configuration(user, app.pk, mode="ai")
    route = routing_config(app, config)
    if ROUTED in modes and route is None:
        raise ValidationError(
            routing_problem(app).replace("It routes", "Chat searching routes")
            or "Set up Chat searching in AI settings and switch it on to measure routing."
        )
    details = []
    counted = set()
    for item in found:
        # Found once, as chat finds it, and handed to both ways alike.
        evidence = seeded(app, item.question, user)
        for mode in modes:
            since, started = mark(), time.monotonic()
            answer, citations, tool_calls, failure = ask(
                user, app, config, token, item.question, evidence, mode
            )
            ids = {citation["id"] for citation in [*citations, *evidence]}
            entries = {
                str(entry.pk): entry
                for entry in KnowledgeEntry.objects.filter(application=app, pk__in=ids).only(
                    "id", "title", "source"
                )
            }
            turns, prompt, completion, amount = receipts(app, user, since, counted)
            details.append({
                "question": item.question,
                "expected": item.expected,
                "mode": mode,
                **score(answer, citations, item.expected, entries),
                "seeded_expected": score("", evidence, item.expected, entries)["shown_expected"],
                "failure": failure,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "turns": turns,
                "tool_calls": tool_calls,
                "cost": str(amount),
                "seconds": round(time.monotonic() - started, 1),
            })
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": config.provider,
        "model": config.model,
        "routing_model": route.model if route is not None and ROUTED in modes else "",
        "pack": pack_digest(found),
        "metrics": metrics(details, modes),
        "details": details,
    }


def metrics(details, modes=MODES):
    found = {}
    for mode in modes:
        rows = [row for row in details if row["mode"] == mode]
        found[mode] = {
            "questions": len(rows),
            "cited_expected": sum(row["cited_expected"] for row in rows),
            "cited_first": sum(row["cited_first"] for row in rows),
            "shown_expected": sum(row["shown_expected"] for row in rows),
            "with_citation": sum(row["with_citation"] for row in rows),
            "invented": sum(row["invented"] for row in rows),
            "failed": sum(bool(row["failure"]) for row in rows),
            "cost": str(sum((Decimal(row["cost"]) for row in rows), Decimal("0"))),
            "turns": sum(row["turns"] for row in rows),
            "tool_calls": sum(row["tool_calls"] for row in rows),
            "seconds": round(sum(row["seconds"] for row in rows), 1),
        }
    return found


def verdict(found):
    """Whether chat's tools clear the bar against one call, and why not if they do not."""
    if not {"one_shot", "tools"} <= set(found):
        return "Run both ways to compare."
    from .agent_measurements import nothing_compared, why_nothing_compared

    if nothing_compared(found):
        return why_nothing_compared(found)
    alone, tools = found["one_shot"], found["tools"]
    reasons = []
    if tools["cited_expected"] < alone["cited_expected"]:
        reasons.append(
            f"cited the expected source {tools['cited_expected']} times against "
            f"{alone['cited_expected']}"
        )
    if tools["invented"] > alone["invented"]:
        reasons.append(f"{tools['invented']} citations to nothing against {alone['invented']}")
    if tools["failed"] > alone["failed"]:
        reasons.append(f"{tools['failed']} questions unanswered against {alone['failed']}")
    base = Decimal(alone["cost"])
    if base and Decimal(tools["cost"]) > COST_BAR * base:
        reasons.append(f"cost ${tools['cost']} against ${alone['cost']} (over {COST_BAR}x)")
    if reasons:
        return ("Tools do not clear the bar: " + "; ".join(reasons)
                + ". Chat always has its tools: look at its turns and searches.")
    if (tools["cited_expected"], tools["cited_first"]) == (
        alone["cited_expected"], alone["cited_first"]
    ):
        return "Tools are no better than one call on these questions."
    return "Tools clear the bar: chat's searches find what one call misses."


def routing_verdict(found):
    """Whether routing chat's searches to the cheaper model holds up against one
    model doing both, and why not if it does not; empty when it was not asked."""
    if not {"tools", ROUTED} <= set(found):
        return ""
    alone, routed = found["tools"], found[ROUTED]
    reasons = []
    for key, said in (("cited_expected", "cited the expected source"),
                      ("cited_first", "cited it first")):
        if routed[key] < alone[key]:
            reasons.append(f"{said} {routed[key]} times against {alone[key]}")
    if routed["invented"] > alone["invented"]:
        reasons.append(f"{routed['invented']} citations to nothing against {alone['invented']}")
    if routed["failed"] > alone["failed"]:
        reasons.append(f"{routed['failed']} questions unanswered against {alone['failed']}")
    if Decimal(routed["cost"]) >= Decimal(alone["cost"]):
        reasons.append(f"cost ${routed['cost']} against ${alone['cost']}, no saving")
    if reasons:
        return "Routing does not clear the bar: " + "; ".join(reasons) + "."
    return (f"Routing clears the bar: as good for ${routed['cost']} against "
            f"${alone['cost']}. Chat searching can stay on.")


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["model"] or "model")
    from .agent_measurements import unused

    path = unused(RECORDS / f"{stamp}-{model}.json")
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
