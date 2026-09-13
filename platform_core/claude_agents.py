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
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, ResultMessage, query
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


def diagnosis(failure):
    """A safe, actionable sentence for a provider failure, or the generic one."""
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
        async with asyncio.timeout(timeout):
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        if result is None or result.is_error:
            raise ValueError("No successful result")
        inputs, outputs = claude_usage(result.usage)
        answer = checked_answer(result.result)
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
        raise ValidationError(diagnosis(failure)) from None
