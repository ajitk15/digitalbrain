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
* **Stop is honoured between turns.** Once the answer's cancel event is set no
  further tool runs (`tools.STOPPED`); the provider call that is running is
  drained and billed, as the runtimes always have.
* **The trajectory is kept, with no text in it** (`AgentStep`, one row per tool
  call): which tool, the shape of its arguments, what came of it, how much came
  back and how long it took. Written after the run on the thread that owns the
  run's writes, and only for a run that names what it belongs to.

A provider failure is raised as `AgentFailure`, carrying only the original's
class name for the caller's log: a provider's message can quote the prompt.
"""

import asyncio
import inspect
import logging
import math
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.http import Http404

from .registry import for_scope
from .tools import MAX_TOOL_CALLS, RESULT_CHARACTERS, CitationRecorder

logger = logging.getLogger(__name__)

#: Turns a chat answer may take, as `claude_runtime.MAX_TURNS` and
#: `openai_runtime.MAX_TURNS` always allowed.
TURNS = 4


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


def runtime_for(provider):
    """The provider runtime, looked up when called so a test can replace it."""
    from . import claude_runtime, openai_runtime

    return openai_runtime if provider == "openai" else claude_runtime


def tool_budget(allowed, requested, calls=MAX_TOOL_CALLS):
    """Tool calls for a run given `allowed` of the `requested` turns.

    None with one turn; all of them with every turn; in between, the same share.
    """
    if allowed <= 1:
        return 0
    if allowed >= requested:
        return calls
    return max(1, math.ceil(calls * (allowed - 1) / (requested - 1)))


def keep_steps(app, purpose, message_id, steps):
    """Write a run's steps as `AgentStep` rows; a failure here never fails the run."""
    if not steps or message_id is None:
        return 0
    from ..models import AgentStep, ChatMessage

    try:
        with transaction.atomic():
            # A message deleted while it was answered (retention, its owner) takes
            # its steps with it; there is nothing left to describe.
            if not ChatMessage.objects.filter(pk=message_id, application_id=app.pk).exists():
                return 0
            AgentStep.objects.bulk_create(
                AgentStep(
                    application_id=app.pk,
                    message_id=message_id,
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
    message_id=None,
):
    """Run one agent to its answer. Returns an `AgentResult`.

    Raises what admission raises (busy, over budget, disabled) before anything
    is sent, `AgentFailure` when the provider or runtime fails, and the refusal
    a tool met when access ended during the run.
    """
    from .. import ai, gateway

    # The caller has usually authorized already (`ai.chat_configuration`); asking
    # again here means no agent can be started by a caller that forgot.
    gateway.authorize(user, app.pk, purpose)
    runtime = runtime_for(config.provider)
    tools = for_scope(scope)
    recorder = CitationRecorder(scope, cancel=cancel)
    # Evidence computed before the run seeds the answer, so a single-turn reply is
    # never worse than the non-agentic path.
    recorder.seed(citations)
    asked = turns if tools else 1
    allowed = 1
    try:
        with gateway.admitted(
            user,
            app,
            config,
            purpose,
            prompt_characters=prompt_characters,
            max_tokens=max_tokens,
            turns=asked,
            tool_characters=MAX_TOOL_CALLS * RESULT_CHARACTERS if tools else 0,
        ) as admission:
            allowed = getattr(admission, "turns", None) or asked
            recorder.budget = tool_budget(allowed, asked)
            given = tools if allowed > 1 else []
            try:
                # The provider runtimes are coroutines, run here on the caller's
                # thread; the scripted model is plain code, so its tools run on
                # this thread as well.
                pending = runtime.run(
                    config.model,
                    question,
                    citations,
                    token,
                    history,
                    scope,
                    recorder,
                    emit,
                    cancel,
                    max_tokens=max_tokens,
                    tools=given,
                    max_turns=allowed,
                )
                usages, answer = (
                    asyncio.run(pending) if inspect.isawaitable(pending) else pending
                )
            except (PermissionDenied, Http404):
                raise
            except Exception as failure:
                raise AgentFailure(failure) from None
            ai.record_turns(user, app, config, purpose, usages)
    finally:
        keep_steps(app, purpose, message_id, recorder.steps)
    if recorder.revoked is not None:
        raise recorder.revoked
    gateway.authorize(user, app.pk, purpose)
    return AgentResult(
        answer=answer,
        citations=recorder.verified_citations(),
        usages=usages,
        turns=allowed,
        steps=list(recorder.steps),
    )
