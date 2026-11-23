"""One way to run an agent, whatever it is for.

Phase 0 of `documentation/engineering/agentic-ai-plan.md`. Chat is the only
agent today; Code Factory, triage and code review will run through here too, so
each adds tools to `registry.TOOLS` rather than rebuilding the sandbox, the
reservation and the checks. What `run_agent` holds every run to:

* **Authorized before, and again after.** The purpose's rule is read before
  admission and once the answer is in, as every paid call's is.
* **Tools from the registry, scope closed over.** The run is given the tools its
  `ToolScope` is allowed (`registry.for_scope`), bound to one application and one
  user before it starts. Every one is read-only, and `run_tool` re-checks the
  grant on every call.
* **Every turn reserved before the first is sent.** Admission is asked for the
  worst case of `turns` turns (`gateway.turn_costs`) and pays for as many as the
  budget's headroom allows. A run allowed one turn is given no tools - a tool
  call would need a second turn the budget cannot pay for - and answers from the
  evidence it was handed. Fewer turns also means a smaller tool budget
  (`tool_budget`), so the model is told to answer while it still has a turn to
  answer in.
* **One receipt per turn**, written while the admission is still held, as
  before.
* **A refusal ends the answer.** A grant revoked or a feature switched off while
  the run reads is raised by the tool; a provider SDK turns that into text and
  carries on, so the harness raises it again after the receipts are written. The
  answer is lost, the bill is kept - the gateway's rule.
* **Out of turns is an answer, not a lost bill.** Every runtime ends a run that
  used every turn without answering with `TurnLimit`, carrying the usage of the
  turns it sent, which get their receipts. A run asked for with `wrap_up` keeps
  its last turn back: run out, it is asked once more with no tools, over the
  evidence that still verifies (`WRAP_UP`), so the person gets an answer from
  what was read rather than "unavailable". Without it, or with no turn kept
  back, the run fails as before - its receipts kept.
* **Stop is honoured between turns.** Once the answer's cancel event is set no
  further tool runs (`tools.STOPPED`); the provider call that is running is
  drained and billed, as the runtimes always have.
* **The trajectory is kept, with no text in it** (`AgentStep`, one row per tool
  call): which tool, the shape of its arguments, what came of it, how much came
  back and how long it took. Written after the run on the thread that owns the
  run's writes, and only for a run that names what it belongs to.

* **Measured as it runs** (`measured`): every admitted run records how long it
  took, whether it had tools and how it ended (`OUTCOMES`), the turns it sent and
  each tool call by tool and outcome - registry names and counts, never text -
  to `metrics.INSTRUMENTS`, for the agent objective in `slo.py`.

A provider failure is raised as `AgentFailure`, carrying only the original's
class name for the caller's log: a provider's message can quote the prompt.
"""

import asyncio
import inspect
import logging
import math
import time
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.http import Http404

from .registry import for_scope
from .tools import KNOWLEDGE_PURPOSES, MAX_TOOL_CALLS, RESULT_CHARACTERS, CitationRecorder
from .usage import Billed, TurnLimit, turns_of

logger = logging.getLogger(__name__)

#: Turns a chat answer may take, as `claude_runtime.MAX_TURNS` and
#: `openai_runtime.MAX_TURNS` always allowed.
TURNS = 4

#: The fewest turns a run must be allowed before one is kept back to answer in:
#: with two, the one tool turn and its answer are all there is.
WRAP_UP_FROM = 3

#: What the turn kept back is asked, after the question. The run has nothing
#: left to search with; it answers from what it was handed and what its tools
#: found that still verifies, and says what that does not cover.
WRAP_UP = (
    "You have used every search this answer may make. Answer now from the "
    "evidence above, citing it as usual, and say plainly what it does not cover."
)


#: How an admitted run ended, as its metrics say: an answer; an answer from the
#: turn kept back; out of turns with none kept; access ended; the person's Stop;
#: the provider or runtime failed.
OUTCOMES = ("answered", "wrapped_up", "turn_limit", "refused", "stopped", "failed")


class AgentFailure(Exception):
    """The provider or runtime failed; `kind` is the original's class name only."""

    def __init__(self, failure):
        super().__init__(type(failure).__name__)
        self.kind = type(failure).__name__


@dataclass
class AgentResult:
    answer: str
    citations: list
    usages: list
    turns: int
    steps: list = field(default_factory=list)
    #: What the tools found beyond the seeded evidence, verified (`found_citations`).
    found: list = field(default_factory=list)
    #: Everything the tools returned beyond it, unverified (`read_citations`).
    read: list = field(default_factory=list)
    #: The model that chose the searches when the run was routed, else "".
    routed: str = ""


def runtime_for(provider, token=None):
    """The provider runtime, looked up when called so a test can replace it.

    Claude with an API key runs through the Messages API (`messages_runtime`) when
    `claude_messages_agents` says so; an OAuth token or the development host login
    still needs the CLI (`claude_runtime`).
    """
    from django.conf import settings

    from ..claude_messages import uses_messages_api
    from . import claude_runtime, messages_runtime, openai_runtime

    if provider == "openai":
        return openai_runtime
    if settings.CLAUDE_MESSAGES_AGENTS and uses_messages_api(token):
        return messages_runtime
    return claude_runtime


def joins_turns(runtime):
    """Whether `runtime` joins every turn's text unless told to keep the last turn's
    alone - the Claude runtimes; the OpenAI runtime's answer is its final output."""
    from . import claude_runtime, messages_runtime

    return runtime in (claude_runtime, messages_runtime)


def tool_budget(allowed, requested, calls=MAX_TOOL_CALLS):
    """Tool calls for a run given `allowed` of the `requested` turns.

    None with one turn; all of them with every turn; in between, the same share.
    """
    if allowed <= 1:
        return 0
    if allowed >= requested:
        return calls
    return max(1, math.ceil(calls * (allowed - 1) / (requested - 1)))


def measured(purpose, provider, run, steps):
    """Report one admitted run to `metrics`: how long it took and how it ended, by
    mode; the turns it sent; each tool call by tool and outcome. Registry names
    and counts only. A run never admitted reports nothing here: admission's own
    metric says it was turned away, and nothing was sent."""
    if run["started"] is None:
        return
    from .. import metrics

    try:
        metrics.record(
            "digitalbrain.agent.run.duration",
            time.monotonic() - run["started"],
            purpose=purpose,
            provider=provider,
            mode=run["mode"],
            outcome=run["outcome"],
        )
        if run["turns"]:
            metrics.record("digitalbrain.agent.turns", run["turns"], purpose=purpose,
                           mode=run["mode"])
        for step in steps:
            metrics.record("digitalbrain.agent.tool.calls", 1, purpose=purpose,
                           tool=step["tool"], outcome=step["outcome"])
    except Exception as failure:  # measuring never fails the run it measures
        logger.warning(
            "agent metrics not recorded",
            extra={"event": "agent_metrics_failed", "exception_type": type(failure).__name__},
        )


def keep_steps(
    app,
    purpose,
    steps,
    message_id=None,
    run_id=None,
    phase="",
    triage_id=None,
    review_id=None,
    candidate_id=None,
    docs_id=None,
    entry_id=None,
):
    """Write a run's steps as `AgentStep` rows, for the chat message, the Code
    Factory run (and phase), the triage run, the review, the Learned question, the
    code documentation or the reviewed source they belong to; a failure here never
    fails the run."""
    from ..models import (
        AgentStep,
        ChatMessage,
        CodeDocumentation,
        CodeReview,
        FactoryRun,
        KnowledgeEntry,
        LearningCandidate,
        TriageRun,
    )

    owners = [
        (model, pk, field)
        for model, pk, field in (
            (ChatMessage, message_id, "message_id"),
            (FactoryRun, run_id, "run_id"),
            (TriageRun, triage_id, "triage_id"),
            (CodeReview, review_id, "review_id"),
            (LearningCandidate, candidate_id, "candidate_id"),
            (CodeDocumentation, docs_id, "docs_id"),
            (KnowledgeEntry, entry_id, "entry_id"),
        )
        if pk is not None
    ]
    if not steps or not owners:
        return 0
    owner, pk, field = owners[0]
    try:
        with transaction.atomic():
            # What the steps describe may be gone by now - a message removed by
            # its owner or retention, a run deleted with its application - and
            # its steps go with it; there is nothing left to describe.
            if not owner.objects.filter(pk=pk, application_id=app.pk).exists():
                return 0
            AgentStep.objects.bulk_create(
                AgentStep(
                    application_id=app.pk,
                    **{field: pk},
                    phase=phase,
                    purpose=purpose,
                    sequence=step["sequence"],
                    tool=step["tool"],
                    arguments=step["arguments"],
                    outcome=step["outcome"],
                    result_characters=step["result_characters"],
                    citations=step["citations"],
                    duration_ms=step["duration_ms"],
                )
                for step in steps
            )
    except DatabaseError as failure:
        logger.warning(
            "agent_steps_not_kept",
            extra={"event": "agent_steps_not_kept", "exception_type": type(failure).__name__},
        )
        return 0
    return len(steps)


def run_agent(
    *,
    user,
    app,
    config,
    token,
    purpose,
    scope,
    question,
    citations,
    history,
    emit,
    cancel=None,
    max_tokens,
    prompt_characters,
    turns=TURNS,
    tool_calls=MAX_TOOL_CALLS,
    instructions=None,
    timeout=None,
    message_id=None,
    run_id=None,
    phase="",
    triage_id=None,
    review_id=None,
    candidate_id=None,
    docs_id=None,
    entry_id=None,
    wrap_up=False,
    routing=True,
):
    """Run one agent to its answer. Returns an `AgentResult`.

    `routing` lets chat's run be routed when an owner set up Chat searching
    (`ai.routing_config`): its tool turns on that model, the answer on chat's.
    `chat_pack` turns it off to measure the one model against the two.

    `wrap_up` asks for one turn more than `turns` and keeps it back for an
    answer from the evidence should the agent use all of its own; the agent
    itself is given `turns`, and the tool budget they pay for, as without it.

    `instructions` and `timeout` replace chat's for an agent that is not chat
    (Code Factory); `tool_calls` is the most tool calls its whole run may make,
    and so part of what admission reserves. Its steps are kept for the chat
    message (`message_id`) or the run and phase (`run_id`, `phase`) it served.

    Raises what admission raises (busy, over budget, disabled) before anything
    is sent, `AgentFailure` when the provider or runtime fails, and the refusal
    a tool met when access ended during the run.
    """
    from .. import ai, gateway

    # The caller has usually authorized already (`ai.chat_configuration`); asking
    # again here means no agent can be started by a caller that forgot.
    gateway.authorize(user, app.pk, purpose)
    runtime = runtime_for(config.provider, token)
    tools = for_scope(scope)
    recorder = CitationRecorder(scope, cancel=cancel)
    # Evidence computed before the run seeds the answer, so a single-turn reply is
    # never worse than the non-agentic path.
    recorder.seed(citations)
    # A prose answer (chat) cites by number, so it ends numbered as the page shows
    # it (`CitationRecorder.numbered`); every other agent cites by id.
    prose = purpose in KNOWLEDGE_PURPOSES and not instructions
    # Routed: the searching on a cheaper model, then one turn more - kept back
    # like the wrap-up turn - in which chat's own model answers from what verified.
    route = ai.routing_config(app, config) if routing and prose and tools else None
    keeps = wrap_up or route is not None
    asked = (turns + 1 if keeps else turns) if tools else 1
    allowed = 1

    def quiet(event, data):
        # The searching model's text is never shown: the answer is the other model's.
        if event != "delta":
            emit(event, data)

    def attempt(given, max_turns, evidence, asking, model=None, shown=True):
        # The provider runtimes are coroutines, run here on the caller's thread;
        # the scripted model is plain code, so its tools run on this thread too.
        pending = runtime.run(
            model or config.model,
            asking,
            evidence,
            token,
            history,
            scope,
            recorder,
            emit if shown else quiet,
            cancel,
            max_tokens=max_tokens,
            tools=given,
            max_turns=max_turns,
            # Only when set, so chat's runtimes are called as they were.
            **({"instructions": instructions} if instructions else {}),
            **({"timeout": timeout} if timeout else {}),
            # An agent given its own instructions answers in its last turn; the
            # Claude runtime joins every turn's text unless told, the OpenAI
            # runtime's answer is always its final output.
            **({"final_only": True} if instructions and joins_turns(runtime) else {}),
        )
        return asyncio.run(pending) if inspect.isawaitable(pending) else pending

    # What `measured` reports once the run ends; `started` is set only once admitted.
    run = {"started": None, "mode": "one_call", "outcome": "failed", "turns": 0}
    # `final` is the prose list once the turn kept back was asked over it,
    # already numbered.
    final = {"citations": None}
    # Whose model a failed turn's receipts are priced and filed under.
    paying = {"config": config, "purpose": purpose}

    def wrapped_up(limit, kept, attempt, ask=None):
        """What a run that used every turn answers with: the turn kept back, over
        the evidence that still verifies - or nothing, raised again, when no turn
        was kept, access ended, or the person pressed Stop (who keeps the text)."""
        stopped = cancel is not None and cancel.is_set()
        if stopped and limit.text:
            return [], limit.text
        if recorder.revoked is not None:
            raise recorder.revoked
        if not kept or stopped:
            raise limit
        if limit.text:
            emit("delta", {"t": "\n\n"})
        written, evidence = limit.text, recorder.verified_citations()
        if prose:
            # Asked over the list the page will show, numbered as it shows it, with
            # what the turns wrote moved onto those numbers first - so the turns'
            # [n] and this turn's [n] name the same sources.
            written, evidence = recorder.numbered(limit.text)
            final["citations"] = evidence
        more, answer = attempt([], 1, evidence, ask or f"{question}\n\n{WRAP_UP}")
        if written and not (instructions and joins_turns(runtime)):
            # As it was streamed: what the turns wrote, then this.
            answer = f"{written}\n\n{answer}"
        return more, answer

    try:
        try:
            with gateway.admitted(
                user,
                app,
                # Each turn may run on either model: reserved at the dearer rates.
                ai.dearer(config, route) if route else config,
                purpose,
                prompt_characters=prompt_characters,
                max_tokens=max_tokens,
                turns=asked,
                tool_characters=tool_calls * RESULT_CHARACTERS if tools else 0,
            ) as admission:
                run["started"] = time.monotonic()
                allowed = getattr(admission, "turns", None) or asked
                # The turn kept back, when there is room for one beside a tool turn.
                kept = 1 if keeps and tools and allowed >= WRAP_UP_FROM else 0
                agent_turns = allowed - kept
                recorder.budget = tool_budget(
                    agent_turns, asked - (1 if keeps else 0), tool_calls
                )
                given = tools if agent_turns > 1 else []
                run["mode"] = "tools" if given else "one_call"
                # Routed only with a turn kept for the answer and tools to search with;
                # with less, chat's model does both, as it always did.
                routed = route if kept and given else None
                recorded = []

                def searched_then_answered():
                    paying.update(config=routed, purpose="chat_search")
                    try:
                        searches, _ = attempt(given, agent_turns, citations, question,
                                              model=routed.model, shown=False)
                    except TurnLimit as limit:
                        searches = limit.usages
                    # Priced at the searching model's rates, filed under its task.
                    ai.record_turns(user, app, routed, "chat_search", searches)
                    recorded.extend(searches)
                    run["turns"] = turns_of(recorded)
                    paying.update(config=config, purpose=purpose)
                    # Its words are dropped; the answer is asked over what verified.
                    found = wrapped_up(TurnLimit(searches), kept, attempt, ask=question)
                    run["outcome"] = "answered"
                    return found

                try:
                    if routed:
                        usages, answer = searched_then_answered()
                    else:
                        try:
                            usages, answer = attempt(given, agent_turns, citations, question)
                            run["outcome"] = "answered"
                        except TurnLimit as limit:
                            # Each turn it sent was billed: receipts first, whatever follows.
                            ai.record_turns(user, app, config, purpose, limit.usages)
                            recorded.extend(limit.usages)
                            run["turns"] = turns_of(recorded)
                            usages, answer = wrapped_up(limit, kept, attempt)
                            run["outcome"] = "wrapped_up"
                except (PermissionDenied, Http404):
                    run["outcome"] = "refused"
                    raise
                except Billed as billed:
                    # Turns that completed before the failure were billed: receipts
                    # first, then the run fails as any provider failure does.
                    ai.record_turns(user, app, paying["config"], paying["purpose"],
                                    billed.usages)
                    run["turns"] = turns_of(recorded) + turns_of(billed.usages)
                    run["outcome"] = (
                        "stopped" if cancel is not None and cancel.is_set() else "failed"
                    )
                    raise AgentFailure(billed.cause) from None
                except Exception as failure:
                    run["outcome"] = (
                        "stopped" if cancel is not None and cancel.is_set()
                        else "turn_limit" if isinstance(failure, TurnLimit)
                        else "failed"
                    )
                    raise AgentFailure(failure) from None
                ai.record_turns(user, app, config, purpose, usages)
                usages = [*recorded, *usages]
                run["turns"] = turns_of(usages)
                if cancel is not None and cancel.is_set():
                    # What it wrote before the person's Stop, kept; neither good nor bad.
                    run["outcome"] = "stopped"
        finally:
            keep_steps(
                app,
                purpose,
                recorder.steps,
                message_id,
                run_id,
                phase,
                triage_id,
                review_id,
                candidate_id,
                docs_id,
                entry_id,
            )
        if recorder.revoked is not None:
            run["outcome"] = "refused"
            raise recorder.revoked
        try:
            gateway.authorize(user, app.pk, purpose)
        except (PermissionDenied, Http404):
            run["outcome"] = "refused"
            raise
    finally:
        measured(purpose, config.provider, run, recorder.steps)
    if not prose:
        citations = recorder.verified_citations()
    elif final["citations"] is not None:
        citations = final["citations"]
    else:
        answer, citations = recorder.numbered(answer)
    return AgentResult(
        answer=answer,
        citations=citations,
        usages=usages,
        turns=allowed,
        steps=list(recorder.steps),
        found=recorder.found_citations(),
        read=recorder.read_citations(),
        routed=routed.model if routed else "",
    )
