"""Streaming, tool-using Claude Agent SDK runtime.

The sandbox from `claude_agents` is preserved exactly: an ephemeral config home,
a scrubbed environment, the bundled CLI, no settings sources, no session
persistence. The only additions are an in-process MCP server and partial
messages. An SDK MCP server speaks over an in-memory transport inside this
process, so it adds no filesystem or network reach.

UNVERIFIED AGAINST A LIVE CLI: `TOOL_PREFIX` below. The `mcp__<server>__<tool>`
form is a Claude Code CLI naming convention and appears nowhere in the Python
package, so it could not be confirmed by reading the SDK. If it is wrong the
failure is safe, not dangerous: `permission_mode="dontAsk"` denies anything not
named in `allowed_tools`, so the model simply cannot search and answers from the
seeded evidence alone. Confirm the real name against a live run and fix here.
"""

import asyncio
import contextlib
import json
import os
import tempfile
import uuid
from pathlib import Path

import claude_agent_sdk
from asgiref.sync import sync_to_async
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from django.db import close_old_connections

from .credentials import HOST_LOGIN_PASSTHROUGH, claude_environment, host_login_enabled
from .prompts import TOOL_INSTRUCTIONS
from .runtime import evidence_payload, recent_turns
from .tools import FETCH_SCHEMA, FETCH_SOURCE, SEARCH_KNOWLEDGE, SEARCH_SCHEMA, run_tool
from .usage import TurnUsage, checked_answer, claude_usage

SERVER_NAME = "brain"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
MAX_TURNS = 4
RUN_TIMEOUT = 120
DRAIN_AFTER_STOP = 10
MAX_BUFFER = 4 * 1024 * 1024
ALLOWED_TOOLS = [f"{TOOL_PREFIX}{name}" for name in ("search_knowledge", "fetch_source")]

SAFE_ENVIRONMENT = {
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "LANG",
}


def _guarded(spec, scope, args, recorder):
    try:
        return run_tool(spec, scope, args, recorder)
    finally:
        close_old_connections()


async def _call(spec, scope, args, recorder):
    return await sync_to_async(_guarded, thread_sensitive=True)(spec, scope, args, recorder)


def _text(body):
    return {"content": [{"type": "text", "text": body}]}


def build_server(scope, recorder, emit):
    """An in-process MCP server whose tools are bound to one application."""

    @tool(SEARCH_KNOWLEDGE.name, SEARCH_KNOWLEDGE.description, SEARCH_SCHEMA)
    async def search_knowledge(args):
        emit("tool", {"name": SEARCH_KNOWLEDGE.name, "detail": str(args.get("query", ""))[:120]})
        return _text(await _call(SEARCH_KNOWLEDGE, scope, args, recorder))

    @tool(FETCH_SOURCE.name, FETCH_SOURCE.description, FETCH_SCHEMA)
    async def fetch_source(args):
        emit("tool", {"name": FETCH_SOURCE.name, "detail": str(args.get("source_id", ""))[:64]})
        return _text(await _call(FETCH_SOURCE, scope, args, recorder))

    return create_sdk_mcp_server(
        name=SERVER_NAME, version="1.0.0", tools=[search_knowledge, fetch_source]
    )


def build_environment(directory, token, max_tokens):
    # The CLI gets an empty workspace/settings home; it cannot inherit another
    # application's session or a host credential.
    host_login = host_login_enabled() and not str(token or "").strip()
    keep = SAFE_ENVIRONMENT | (HOST_LOGIN_PASSTHROUGH if host_login else set())
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
            "API_TIMEOUT_MS": "30000",
            "CLAUDE_CODE_MAX_RETRIES": "0",
        }
    )
    if not host_login:
        # Redirecting these is what makes the stored login unreachable.
        env.update({"CLAUDE_CONFIG_DIR": directory, "HOME": directory, "USERPROFILE": directory})
    return env


def bundled_cli():
    return (
        Path(claude_agent_sdk.__file__).parent
        / "_bundled"
        / ("claude.exe" if os.name == "nt" else "claude")
    )


def build_prompt(question, citations, history):
    return json.dumps(
        {
            "conversation": [
                {"question": turn.question, "answer": turn.answer}
                for turn in recent_turns(history)
            ],
            "question": question,
            "evidence": evidence_payload(citations),
        }
    )


def stream_text(message):
    """Text deltas out of a partial message.

    `StreamEvent.event` is typed `dict[str, Any]` by the SDK and documented only
    as the raw Anthropic stream event, so every access here is defensive.
    """
    event = getattr(message, "event", None)
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    piece = delta.get("text")
    return piece if isinstance(piece, str) else ""


async def run(model, question, citations, token, history, scope, recorder, emit, cancel=None):
    """Stream one answer, returning (turn usages, answer text)."""
    with tempfile.TemporaryDirectory(
        prefix="digital-brain-agent-", ignore_cleanup_errors=True
    ) as directory:
        options = ClaudeAgentOptions(
            cli_path=bundled_cli(),
            model=model,
            system_prompt=TOOL_INSTRUCTIONS,
            tools=[],
            allowed_tools=ALLOWED_TOOLS,
            permission_mode="dontAsk",
            mcp_servers={SERVER_NAME: build_server(scope, recorder, emit)},
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            plugins=[],
            cwd=directory,
            env=build_environment(directory, token, 1024),
            max_turns=MAX_TURNS,
            max_buffer_size=MAX_BUFFER,
            include_partial_messages=True,
            extra_args={"no-session-persistence": None},
            stderr=lambda line: None,
        )
        pieces = []
        fallback = []
        result = None
        stopped_at = None
        prompt = build_prompt(question, citations, history)
        async with asyncio.timeout(RUN_TIMEOUT):
            async with contextlib.aclosing(query(prompt=prompt, options=options)) as stream:
                async for message in stream:
                    if isinstance(message, StreamEvent):
                        piece = stream_text(message)
                        if piece and stopped_at is None:
                            pieces.append(piece)
                            emit("delta", {"t": piece})
                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and not pieces:
                                fallback.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                emit("tool", {"name": block.name, "detail": ""})
                    elif isinstance(message, ResultMessage):
                        # Keep draining: leaving here strands the CLI subprocess,
                        # which on Windows keeps a handle on the working directory.
                        result = message
                    if cancel is not None and cancel.is_set() and stopped_at is None:
                        # query() offers no interrupt, and leaving early forfeits the
                        # ResultMessage and therefore the usage record. Stop showing
                        # tokens, but keep draining briefly so the call is still billed
                        # honestly.
                        stopped_at = asyncio.get_running_loop().time()
                    if (
                        stopped_at is not None
                        and asyncio.get_running_loop().time() - stopped_at > DRAIN_AFTER_STOP
                    ):
                        break
    stopped = stopped_at is not None
    text = ("".join(pieces) or "".join(fallback)).strip()
    if result is None or result.is_error:
        if not (stopped and text):
            raise ValueError("No successful result")
        return [TurnUsage(f"claude-{uuid.uuid4()}", 0, 0)], text
    inputs, outputs = claude_usage(result.usage)
    answer = text if stopped and text else checked_answer(result.result or text)
    # Anthropic exposes no transport request ID here, so receipts carry a local run ID.
    return [TurnUsage(f"claude-{uuid.uuid4()}", inputs, outputs)], answer
