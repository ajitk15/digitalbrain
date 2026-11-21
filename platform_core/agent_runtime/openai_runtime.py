"""Streaming, tool-using OpenAI Agents SDK runtime.

Separate from `llm_agents.completion`, which stays single-turn and tool-free for
the graph tasks whose output contract is strict JSON.

Two details worth knowing before editing:

* Billing reads `ModelResponse.raw_usage` for **every** turn, not the SDK's
  aggregated `context_wrapper.usage`. The aggregate normalizes missing fields to
  zero, which is exactly the silent under-reporting the strict validator exists
  to prevent.
* Cancellation uses `mode="after_turn"`, and the SDK requires the event loop to
  keep draining afterwards. Stopping is about no longer showing tokens to the
  user, not about escaping the bill.
"""

import asyncio
import json

from agents import (
    Agent,
    FunctionTool,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
)
from asgiref.sync import sync_to_async
from django.db import close_old_connections

from .prompts import TOOL_INSTRUCTIONS
from .registry import for_scope
from .runtime import ANSWER_TOKENS, RESPONSES_MODELS, evidence_payload, recent_turns
from .tools import run_tool
from .usage import TurnUsage, checked_answer, checked_request_id, openai_usage

MAX_TURNS = 4
RUN_TIMEOUT = 120
CANCEL_POLL = 0.25


def _guarded(spec, scope, args, recorder):
    """Run one tool on the executor thread, always releasing its connection.

    close_old_connections acts on the calling thread, and this is not the worker
    thread, so the release has to happen here.
    """
    try:
        return run_tool(spec, scope, args, recorder)
    finally:
        close_old_connections()


async def _call(spec, scope, args, recorder):
    # thread_sensitive keeps every tool query on one executor thread with one
    # connection, which is what SQLite wants and Postgres does not mind.
    return await sync_to_async(_guarded, thread_sensitive=True)(spec, scope, args, recorder)


def arguments(spec, raw):
    """The model's arguments as a dict, with the defaults its schema declares.

    `function_tool` used to fill them from the function's signature; strict mode
    sends every key anyway, so this matters only for a model that does not.
    Anything that is not a JSON object is no arguments, which every tool answers.
    """
    try:
        args = json.loads(raw or "{}")
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    _, schema = spec.wire("openai")
    for name, rule in (schema or {}).get("properties", {}).items():
        if name not in args and "default" in rule:
            args[name] = rule["default"]
    return args


def bound(spec, scope, recorder, emit):
    """One registry tool as an Agents SDK tool, bound to one application."""
    description, schema = spec.wire("openai")

    async def invoke(context, raw):
        args = arguments(spec, raw)
        emit("tool", {"name": spec.name, "detail": spec.shown(args)})
        return await _call(spec, scope, args, recorder)

    return FunctionTool(
        name=spec.name,
        description=description,
        params_json_schema=schema,
        on_invoke_tool=invoke,
        strict_json_schema=True,
    )


def build_tools(scope, recorder, emit, tools=None):
    """Tools bound to one application and one user.

    `scope` is closed over. Nothing the model sends can widen it.
    """
    tools = for_scope(scope) if tools is None else tools
    return [bound(spec, scope, recorder, emit) for spec in tools]


def build_agent(model, client, instructions, max_tokens, tools):
    responses_api = model.startswith(RESPONSES_MODELS)
    return Agent(
        name="Digital Brain knowledge assistant",
        instructions=instructions,
        model=(OpenAIResponsesModel if responses_api else OpenAIChatCompletionsModel)(
            model=model, openai_client=client
        ),
        model_settings=ModelSettings(
            store=False,
            preserve_raw_usage=True,
            include_usage=True,
            max_tokens=max_tokens if responses_api else None,
            extra_body={} if responses_api else {"max_completion_tokens": max_tokens},
        ),
        tools=tools,
        handoffs=[],
    )


def build_input(question, citations, history):
    inputs = [
        message
        for turn in recent_turns(history)
        for message in [
            {"role": "user", "content": turn.question},
            {"role": "assistant", "content": turn.answer},
        ]
    ]
    inputs.append(
        {
            "role": "user",
            "content": json.dumps(
                {"question": question, "evidence": evidence_payload(citations)}
            ),
        }
    )
    return inputs


async def _watch_cancel(result, cancel):
    """Ask the run to stop even while no events are arriving."""
    while True:
        await asyncio.sleep(CANCEL_POLL)
        if cancel is not None and cancel.is_set():
            result.cancel(mode="after_turn")
            return


def collect_usage(result, model):
    """One billable TurnUsage per model turn."""
    responses_api = model.startswith(RESPONSES_MODELS)
    turns = []
    for index, raw in enumerate(result.raw_responses):
        prompt_tokens, completion_tokens = openai_usage(raw.raw_usage, responses_api)
        request_id = checked_request_id(
            raw.request_id or raw.response_id or f"agents-turn-{index}"
        )
        turns.append(TurnUsage(request_id, prompt_tokens, completion_tokens))
    if not turns:
        raise ValueError("Missing usage")
    return turns


async def run(
    model,
    question,
    citations,
    token,
    history,
    scope,
    recorder,
    emit,
    cancel=None,
    max_tokens=ANSWER_TOKENS,
    tools=None,
    max_turns=MAX_TURNS,
    instructions=None,
    timeout=None,
):
    """Stream one answer, returning (turn usages, answer text).

    `tools` are registry specs (the scope's own when not given); with none, the
    model answers from the evidence it was handed, in one turn. `instructions`
    replace chat's own, and `timeout` its two minutes, for an agent that writes
    more than a chat answer (Code Factory); each request may then take as long.
    """
    from openai import AsyncOpenAI

    from ..llm_agents import _http_client

    pieces = []
    # Never mutate SDK globals: concurrent applications have independent credentials.
    async with AsyncOpenAI(
        api_key=token,
        base_url="https://api.openai.com/v1",
        organization="",
        project="",
        max_retries=0,
        timeout=timeout or 30,
        http_client=_http_client(),
    ) as client:
        given = build_tools(scope, recorder, emit, tools)
        agent = build_agent(model, client, instructions or TOOL_INSTRUCTIONS, max_tokens, given)
        result = Runner.run_streamed(
            agent,
            input=build_input(question, citations, history),
            max_turns=max_turns if given else 1,
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        )
        watcher = asyncio.ensure_future(_watch_cancel(result, cancel))
        try:
            async with asyncio.timeout(timeout or RUN_TIMEOUT):
                async for event in result.stream_events():
                    # Match the literal event type: the Chat Completions handler
                    # synthesizes the same Responses-shaped delta.
                    data = getattr(event, "data", None)
                    if getattr(data, "type", None) == "response.output_text.delta":
                        piece = getattr(data, "delta", "")
                        if isinstance(piece, str) and piece:
                            pieces.append(piece)
                            emit("delta", {"t": piece})
        finally:
            watcher.cancel()
    stopped = bool(cancel is not None and cancel.is_set())
    text = "".join(pieces).strip()
    answer = text if stopped and text else checked_answer(result.final_output)
    return collect_usage(result, model), answer
