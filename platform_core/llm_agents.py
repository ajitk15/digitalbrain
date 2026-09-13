"""Scoped OpenAI Agents SDK execution; Django owns history and accounting."""

import asyncio
import json
import uuid

from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
)
from agents.exceptions import AgentsException
from django.core.exceptions import ValidationError
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, OpenAIError

from .agent_runtime.prompts import INSTRUCTIONS
from .agent_runtime.runtime import evidence_payload, recent_turns
from .agent_runtime.usage import checked_answer, checked_request_id, openai_usage

__all__ = ["INSTRUCTIONS", "completion"]

RESPONSES_MODELS = ("gpt-6", "gpt-5.6")


def _http_client():
    return DefaultAsyncHttpxClient(follow_redirects=False, trust_env=False)


async def _run(
    model, question, citations, token, history, instructions=INSTRUCTIONS, max_tokens=1024
):
    responses_api = model.startswith(RESPONSES_MODELS)
    evidence = evidence_payload(citations)
    inputs = [
        message
        for turn in recent_turns(history)
        for message in [
            {"role": "user", "content": turn.question},
            {"role": "assistant", "content": turn.answer},
        ]
    ]
    inputs.append(
        {"role": "user", "content": json.dumps({"question": question, "evidence": evidence})}
    )
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
        agent = Agent(
            name="Digital Brain knowledge assistant",
            instructions=instructions,
            model=(
                OpenAIResponsesModel
                if responses_api
                else OpenAIChatCompletionsModel
            )(model=model, openai_client=client),
            model_settings=ModelSettings(
                store=False,
                preserve_raw_usage=True,
                max_tokens=max_tokens if responses_api else None,
                extra_body={}
                if responses_api
                else {"max_completion_tokens": max_tokens},
            ),
            tools=[],
            handoffs=[],
        )
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                input=inputs,
                max_turns=1,
                run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
            ),
            timeout=35,
        )
    if len(result.raw_responses) != 1:
        raise ValueError("Unexpected model call count")
    raw = result.raw_responses[0]
    # Inspect original usage: missing counts must never be recorded as zero-cost usage.
    prompt_tokens, completion_tokens = openai_usage(raw.raw_usage, responses_api)
    answer = checked_answer(result.final_output)
    request_id = checked_request_id(
        raw.request_id or raw.response_id or f"agents-{uuid.uuid4()}"
    )
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    return {"id": request_id, "usage": usage}, answer


def completion(config, question, citations, token, history=None, **options):
    try:
        # Each synchronous Django worker owns and closes its event loop and async client.
        return asyncio.run(_run(config.model, question, citations, token, history, **options))
    except (OpenAIError, AgentsException, TimeoutError, OSError, ValueError, TypeError):
        raise ValidationError(
            "AI response unavailable. Check the model, credential and provider limits. "
            "The request may have been billed by the provider; it is not retried automatically."
        ) from None
