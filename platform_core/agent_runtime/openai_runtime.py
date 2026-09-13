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
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    function_tool,
)
from asgiref.sync import sync_to_async
from django.db import close_old_connections

from .prompts import TOOL_INSTRUCTIONS
from .runtime import evidence_payload, recent_turns
from .tools import FETCH_SOURCE, SEARCH_KNOWLEDGE, run_tool
from .usage import TurnUsage, checked_answer, checked_request_id, openai_usage

RESPONSES_MODELS = ("gpt-6", "gpt-5.6")
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


def build_tools(scope, recorder, emit):
    """Tools bound to one application and one user.

    `scope` is closed over. Nothing the model sends can widen it.
    """

    @function_tool
    async def search_knowledge(query: str, limit: int = 5) -> str:
        """Search this application's knowledge sources for relevant passages.

        Args:
            query: What to look for.
            limit: Maximum passages to return, 1-8.
        """
        emit("tool", {"name": "search_knowledge", "detail": query[:120]})
        return await _call(SEARCH_KNOWLEDGE, scope, {"query": query, "limit": limit}, recorder)

    @function_tool
    async def fetch_source(source_id: str, offset: int = 0) -> str:
        """Read more of one knowledge source returned by search_knowledge.

        Args:
            source_id: The source id to read.
            offset: Character offset to start from.
        """
        emit("tool", {"name": "fetch_source", "detail": source_id[:64]})
        return await _call(
            FETCH_SOURCE, scope, {"source_id": source_id, "offset": offset}, recorder
        )

    return [search_knowledge, fetch_source]


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


async def run(model, question, citations, token, history, scope, recorder, emit, cancel=None):
    """Stream one answer, returning (turn usages, answer text)."""
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
        timeout=30,
        http_client=_http_client(),
    ) as client:
        agent = build_agent(
            model, client, TOOL_INSTRUCTIONS, 1024, build_tools(scope, recorder, emit)
        )
        result = Runner.run_streamed(
            agent,
            input=build_input(question, citations, history),
            max_turns=MAX_TURNS,
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        )
        watcher = asyncio.ensure_future(_watch_cancel(result, cancel))
        try:
            async with asyncio.timeout(RUN_TIMEOUT):
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
