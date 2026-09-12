"""Text-only Claude Agent SDK with isolated runtime settings and mounted credentials."""

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, ResultMessage, query
from django.core.exceptions import ValidationError

from .llm_agents import INSTRUCTIONS


async def _run(
    model, question, citations, token, history, instructions=INSTRUCTIONS, max_tokens=1024
):
    # The CLI gets an empty workspace/settings home; it cannot inherit another app's session.
    with tempfile.TemporaryDirectory(prefix="digital-brain-agent-") as directory:
        safe_names = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "LANG"}
        env = {key: "" for key in os.environ if key.upper() not in safe_names}
        env.update(
            {
                "ANTHROPIC_API_KEY": token,
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                "CLAUDE_CONFIG_DIR": directory,
                "HOME": directory,
                "USERPROFILE": directory,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
                "TRACEPARENT": "",
                "TRACESTATE": "",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens),
                "API_TIMEOUT_MS": "30000",
                "CLAUDE_CODE_MAX_RETRIES": "0",
            }
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
                    {"question": t.question, "answer": t.answer[:4000]}
                    for t in (history or [])[-8:]
                ],
                "question": question,
                "evidence": [
                    {"source": i + 1, "id": c.get("id"), "title": c["title"], "text": c["excerpt"]}
                    for i, c in enumerate(citations)
                ],
            }
        )
        result = None
        async with asyncio.timeout(45):
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        if result is None or result.is_error or not isinstance(result.usage, dict):
            raise ValueError("No successful result")
        usage = result.usage
        for key in ("input_tokens", "output_tokens"):
            if type(usage.get(key)) is not int or not 0 <= usage[key] <= 1000000:
                raise ValueError("Missing token counts")
        inputs = usage["input_tokens"]
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(key, 0)
            if type(value) is not int or not 0 <= value <= 1000000:
                raise ValueError("Invalid cache tokens")
            inputs += value
        answer = result.result
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 20000:
            raise ValueError("Invalid answer")
        return {
            "id": f"claude-{uuid.uuid4()}",
            "usage": {"prompt_tokens": inputs, "completion_tokens": usage["output_tokens"]},
        }, answer


def completion(config, question, citations, token, history=None, **options):
    try:
        return asyncio.run(_run(config.model, question, citations, token, history, **options))
    except (ClaudeSDKError, TimeoutError, OSError, ValueError, TypeError):
        raise ValidationError(
            "Claude response unavailable. Check the configured model, API key and runtime. "
            "This request may have incurred provider charges; it is not retried by the application."
        ) from None
