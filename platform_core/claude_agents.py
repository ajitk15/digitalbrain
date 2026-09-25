"""Text-only Claude Agent SDK with isolated runtime settings and mounted credentials."""

import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    query,
)
from django.core.exceptions import ValidationError

from .agent_runtime.credentials import (
    HOST_LOGIN_PASSTHROUGH,
    claude_environment,
    host_login_enabled,
)
from .agent_runtime.prompts import INSTRUCTIONS
from .agent_runtime.runtime import evidence_payload, recent_turns
from .agent_runtime.usage import checked_answer, claude_usage

logger = logging.getLogger(__name__)

#: Failures a user can actually act on. The provider's own text is never shown -
#: it may carry request details - but the *category* is safe and is the whole
#: difference between "it failed" and "your login expired, sign in again".
DIAGNOSES = (
    (
        re.compile(r"authenticat|oauth|credential|api[ _-]?key|401|unauthor", re.I),
        "Claude rejected the credentials for this application. If this application "
        "uses the machine's own Claude login, that session has expired - sign in "
        "again with `claude /login`. Otherwise check the mounted API key.",
    ),
    (
        re.compile(r"timeout|timed out", re.I),
        "Claude did not respond in time. Large source sets can exceed the limit; "
        "try again, or reduce the number of sources.",
    ),
    (
        re.compile(r"model|not[ _-]?found|404", re.I),
        "Claude did not accept the configured model. Check the model name in AI settings.",
    ),
)


#: What the CLI's own terminal reason means for the person reading it. Read from
#: the structured field rather than matched in prose: the SDK puts it there
#: precisely so callers can branch on why a run failed without string matching,
#: and prose from a provider is the thing this module refuses to show.
TERMINAL_REASONS = {
    "api_error": (
        "Claude's API reported an error - most often overload or a timeout on a "
        "large request. Nothing was written. Run it again; this is usually "
        "transient. It is not retried automatically because the failed call may "
        "still have been billed."
    ),
    "max_turns": (
        "Claude stopped after using its allowed turns without producing an "
        "answer. Nothing was written."
    ),
    "refusal": "Claude declined to answer this request. Nothing was written.",
}


def diagnosis(failure):
    """A safe, actionable sentence for a provider failure, or the generic one."""
    reason = getattr(failure, "terminal_reason", None)
    if isinstance(reason, str) and reason in TERMINAL_REASONS:
        status = getattr(failure, "api_error_status", None)
        detail = TERMINAL_REASONS[reason]
        # The status is a number, not prose, so it is safe to carry and it is
        # the difference between "try again" and "something is wrong".
        return f"{detail} (HTTP {status}.)" if isinstance(status, int) else detail
    text = f"{type(failure).__name__}: {failure}"
    for pattern, message in DIAGNOSES:
        if pattern.search(text):
            return message
    return (
        "Claude response unavailable. Check the configured model, API key and runtime. "
        "This request may have incurred provider charges; it is not retried by the application."
    )


#: Wall clock for an interactive answer: a person is waiting on it.
TIMEOUT = 45

#: Graph extraction is a background job with nobody holding a request open, and
#: it asks for thousands of output tokens over every source in the application.
#: Sizing it like a chat reply is what made it fail before the model could finish.
BATCH_TIMEOUT = 600


async def _run(
    model,
    question,
    citations,
    token,
    history,
    instructions=INSTRUCTIONS,
    max_tokens=1024,
    timeout=TIMEOUT,
):
    # The CLI gets an empty workspace/settings home; it cannot inherit another app's session.
    with tempfile.TemporaryDirectory(prefix="digital-brain-agent-") as directory:
        safe_names = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "LANG"}
        host_login = host_login_enabled() and not str(token or "").strip()
        keep = safe_names | (HOST_LOGIN_PASSTHROUGH if host_login else set())
        env = {key: "" for key in os.environ if key.upper() not in keep}
        # An API key and an OAuth token are read from different variables; under host
        # login neither is set and the CLI falls through to the stored session.
        env.update(claude_environment(token))
        env.update(
            {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
                "TRACEPARENT": "",
                "TRACESTATE": "",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens),
                # The CLI's own per-request timeout. It has to be at least the
                # outer budget, or the inner call gives up first and the outer
                # one never gets a chance to apply - which is what happened.
                "API_TIMEOUT_MS": str(timeout * 1000),
                "CLAUDE_CODE_MAX_RETRIES": "0",
            }
        )
        if not host_login:
            env.update(
                {"CLAUDE_CONFIG_DIR": directory, "HOME": directory, "USERPROFILE": directory}
            )
        bundled_cli = (
            Path(claude_agent_sdk.__file__).parent
            / "_bundled"
            / ("claude.exe" if os.name == "nt" else "claude")
        )
        options = ClaudeAgentOptions(
            cli_path=bundled_cli,
            model=model,
            system_prompt=instructions,
            tools=[],
            allowed_tools=[],
            permission_mode="dontAsk",
            mcp_servers={},
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            plugins=[],
            cwd=directory,
            env=env,
            max_turns=1,
            max_buffer_size=1024 * 1024,
            extra_args={"no-session-persistence": None},
            stderr=lambda line: None,
        )
        prompt = json.dumps(
            {
                "conversation": [
                    {"question": t.question, "answer": t.answer} for t in recent_turns(history)
                ],
                "question": question,
                "evidence": evidence_payload(citations),
            }
        )
        result = None
        blocks = []
        async with asyncio.timeout(timeout):
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
                elif isinstance(message, AssistantMessage):
                    # Collected because `result.result` cannot be relied on to
                    # carry the whole reply; see the answer selection below.
                    blocks.extend(
                        block.text for block in message.content if isinstance(block, TextBlock)
                    )
        if result is None or result.is_error:
            # The structured fields only - never `result.result` or `errors`,
            # which are provider prose and may carry request details. These are
            # enough to tell an overload from a limit from a stalled stream,
            # which two identical "api_error" failures at ~170s could not.
            if result is not None:
                logger.warning(
                    "claude_result_error",
                    extra={
                        "event": "claude_result_error",
                        "subtype": result.subtype,
                        "terminal_reason": getattr(result, "terminal_reason", None),
                        "api_error_status": getattr(result, "api_error_status", None),
                        "stop_reason": result.stop_reason,
                        "duration_ms": result.duration_ms,
                        "duration_api_ms": result.duration_api_ms,
                        "num_turns": result.num_turns,
                        "error_count": len(getattr(result, "errors", None) or []),
                        "prompt_bytes": len(prompt),
                        "max_tokens": max_tokens,
                    },
                )
            raise ValueError("No successful result")
        inputs, outputs = claude_usage(result.usage)
        # Whichever carries more of the reply.
        #
        # `result.result` is the CLI's account of the final assistant message,
        # and a live run showed it arriving as a 206-character fragment of an
        # answer the model had spent 4,187 completion tokens on: the tail of a
        # JSON document, cut mid-word. Every phase that asks for JSON then
        # failed with "did not return usable JSON" while the reply had in fact
        # arrived intact in the assistant message itself.
        #
        # Longer rather than always preferring the blocks, because the blocks
        # are not always the whole story either - a turn whose text the SDK
        # does not surface leaves them empty and the summary is all there is.
        # Taking whichever carries more is right in both directions.
        summary = result.result if isinstance(result.result, str) else ""
        answer = checked_answer(max("".join(blocks).strip(), summary, key=len))
        # Anthropic exposes no transport request ID here, so receipts carry a local run ID.
        return {
            "id": f"claude-{uuid.uuid4()}",
            "usage": {"prompt_tokens": inputs, "completion_tokens": outputs},
        }, answer


def completion(config, question, citations, token, history=None, **options):
    try:
        return asyncio.run(_run(config.model, question, citations, token, history, **options))
    except (ClaudeSDKError, TimeoutError, OSError, ValueError, TypeError) as failure:
        # The operator needs the real text to debug; the user must never see it.
        logger.warning("claude_completion_failed", exc_info=True)
        log_result_failure(failure, options.get("max_tokens"))
        raise ValidationError(diagnosis(failure)) from None


#: What an "API Error: ..." result was about, as a fixed label. The text itself
#: is provider prose and is never logged; the label is enough to tell an
#: overload from a timeout from a request the API would not take.
API_ERROR_KINDS = (
    ("overloaded", re.compile(r"overload|529|capacity", re.I)),
    ("rate_limited", re.compile(r"rate.?limit|429", re.I)),
    ("timeout", re.compile(r"time(d)?.?out|deadline", re.I)),
    ("stream", re.compile(r"stream|connection|socket|reset|closed|terminated", re.I)),
    ("too_long", re.compile(r"too long|context|prompt is too|exceed", re.I)),
    ("output_limit", re.compile(r"max.?tokens|output token", re.I)),
    ("server_error", re.compile(r"\b5\d\d\b|internal", re.I)),
)


def log_result_failure(failure, max_tokens=None):
    """The structured fields of a failed CLI result, for the operator's log.

    `ResultError` is raised by the SDK while the stream is read, so the result
    never reaches the check after it. Three live reviews failed as a bare
    "api_error" at ~175s with nothing to say why; this is what says why,
    without writing the provider's prose anywhere.
    """
    data = getattr(failure, "data", None)
    if not isinstance(data, dict):
        return
    errors = " ".join(getattr(failure, "errors", None) or [])
    text = f"{getattr(failure, 'result', '') or ''} {errors}"
    kind = next((label for label, pattern in API_ERROR_KINDS if pattern.search(text)), "other")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    logger.warning(
        "claude_result_error",
        extra={
            "event": "claude_result_error",
            "subtype": getattr(failure, "subtype", None),
            "terminal_reason": getattr(failure, "terminal_reason", None),
            "api_error_status": getattr(failure, "api_error_status", None),
            "api_error_kind": kind,
            "stop_reason": data.get("stop_reason"),
            "duration_ms": data.get("duration_ms"),
            "duration_api_ms": data.get("duration_api_ms"),
            "num_turns": data.get("num_turns"),
            "output_tokens": usage.get("output_tokens"),
            "max_tokens": max_tokens,
        },
    )
