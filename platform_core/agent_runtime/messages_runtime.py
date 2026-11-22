"""A tool-using Claude run through the Messages API, when the credential is an API key.

`claude_runtime` drives the bundled Claude Code CLI: a subprocess per run, with an
empty home, a blanked environment and an in-process MCP server for the tools. That
is the only way a `claude setup-token` OAuth token, or the development host login,
can be used. An API key needs no subprocess - the one-shot path already sends it as
one Messages request (`claude_messages`) - and an agent run with one is the same
loop done here: ask, run the tools the model asked for through `tools.run_tool`,
hand their results back, ask again, until it answers or runs out of turns.

What the model is shown is the CLI path's own, so a run reads the same either way:
the same system prompt (`instructions`, else `TOOL_INSTRUCTIONS`), the same user
turn (`claude_messages.prompt_for`), and the tools under the same names the CLI
gives them (`mcp__brain__<tool>`, `claude_runtime.TOOL_PREFIX`) with the same
descriptions and schemas (`ToolSpec.wire("claude")`). It is still a change of
runtime, so it is switched on with `claude_messages_agents` and measured the way
any such change is (`chat_pack`, `llm_drift`) before production relies on it.

What it adds over the CLI:

* **No subprocess.** Nothing starts before the first token.
* **One receipt per turn, keyed on the provider's message id.** The CLI reports a
  run's usage once, summed, under an id made here.
* **A failure keeps the bill.** A turn that completed was billed: when a later turn
  fails, the usage of those before it is raised with the failure (`usage.Billed`),
  and the harness writes their receipts before it fails the run.

The client is `claude_messages.client_for`'s: the key and base URL given explicitly,
no proxy or headers from the environment, no retries. Tools run on the caller's
thread, as the scripted model's do: they are database reads under the run's scope.

**Prompt caching** (`claude_prompt_caching`, `cached`). Every turn of a run sends
again what the turns before it sent - the tools, the instructions, the evidence,
each tool call and its result - and pays the input rate for all of it. With the
switch on, a run with tools marks two cache breakpoints: the system prompt, which
caches the tools with it, and the last block of the newest message, which moves
forward every turn. A later turn then reads everything before its new tool results
from the cache, at a tenth of the input rate or less, and writes only what the last
turn added, at a quarter more (`ai.CACHE_WRITE`); the receipts say which tokens
were which. The marker is not something the model reads - the request is otherwise
byte for byte the one sent without it - but it changes what a run costs, so it is
switched on once `chat_pack` shows the cost fall. A run with no tools has one turn
and nothing to read back, so it marks nothing: a write there would only cost more.
Below the model's minimum (512 tokens on current models) the API caches nothing
and charges nothing extra.
"""

import time

from django.core.exceptions import PermissionDenied
from django.http import Http404

from .claude_runtime import MAX_TURNS, RUN_TIMEOUT, TOOL_PREFIX
from .prompts import TOOL_INSTRUCTIONS
from .registry import for_scope
from .runtime import ANSWER_TOKENS
from .tools import run_tool
from .usage import Billed, TurnLimit, checked_answer, checked_request_id, claude_turn

#: Anthropic's five-minute cache: enough between the turns of one run, and between
#: runs that share their tools and instructions.
EPHEMERAL = {"type": "ephemeral"}

#: What a tool the run was not given answers, as the CLI's `dontAsk` refuses it.
NOT_GIVEN = "That tool is not available."
TOOL_ERROR = "The tool failed."


def definitions(tools):
    """The tools as the Messages API takes them, named as the CLI names them."""
    found = []
    for spec in tools:
        description, schema = spec.wire("claude")
        found.append({"name": f"{TOOL_PREFIX}{spec.name}", "description": description,
                      "input_schema": schema})
    return found


def turn_usage(message):
    """One turn's receipt: the provider's message id and its token counts."""
    usage = message.usage.model_dump() if message.usage is not None else None
    if isinstance(usage, dict):
        # A cache count the API did not report is absent, not zero tokens.
        usage = {key: value for key, value in usage.items() if value is not None}
    return claude_turn(checked_request_id(message.id), usage)


def cached(messages):
    """`messages` with a cache breakpoint on the last block of the newest one -
    a copy, so the marker moves forward with the run and earlier messages are
    sent as they were. Every message's content is a list of blocks by then."""
    last = dict(messages[-1])
    content = last["content"]
    last["content"] = [*content[:-1], {**content[-1], "cache_control": EPHEMERAL}]
    return [*messages[:-1], last]


def blocks(message):
    """The assistant turn as the next request repeats it."""
    found = []
    for block in message.content:
        if block.type == "text":
            found.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            found.append({"type": "tool_use", "id": block.id, "name": block.name,
                          "input": block.input})
    return found


def answered(spec, scope, args, recorder):
    """A tool's text for the model; a refusal stops the run (the harness raises it)."""
    try:
        return run_tool(spec, scope, args, recorder), False
    except (PermissionDenied, Http404):
        return "", True
    except Exception:
        # As a provider SDK does: the tool's failure is text, and the run goes on.
        return TOOL_ERROR, False


def run(
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
    final_only=False,
):
    """Stream one run, returning (turn usages, answer text). Same contract as
    `claude_runtime.run`: with no tools, one turn from the evidence; `final_only`
    keeps the last turn's text alone; out of turns is `TurnLimit` with the usage
    of the turns sent."""
    from django.conf import settings

    from ..claude_messages import client_for, prompt_for

    tools = for_scope(scope) if tools is None else list(tools)
    given = {spec.name: spec for spec in tools}
    wall = timeout or RUN_TIMEOUT
    deadline = time.monotonic() + wall
    messages = [{"role": "user", "content": prompt_for(question, citations, history)}]
    usages, written, last = [], [], ""
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": instructions or TOOL_INSTRUCTIONS,
    }
    if tools:
        request["tools"] = definitions(tools)
    caching = bool(tools) and settings.CLAUDE_PROMPT_CACHING
    if caching:
        # Rendered tools first, then system: a breakpoint here caches both.
        request["system"] = [
            {"type": "text", "text": request["system"], "cache_control": EPHEMERAL}
        ]
        # Sent as a block on every turn, as the first turn sent it, so the
        # prefix the cache keeps is byte for byte the one sent again.
        messages[0]["content"] = [{"type": "text", "text": messages[0]["content"]}]
    try:
        with client_for(token, wall) as client:
            for _ in range(max_turns if tools else 1):
                pieces = []
                sent = cached(messages) if caching else messages
                with client.messages.stream(messages=sent, **request) as stream:
                    for event in stream:
                        if event.type == "text" and not (cancel is not None and cancel.is_set()):
                            pieces.append(event.text)
                            emit("delta", {"t": event.text})
                        if time.monotonic() > deadline:
                            raise TimeoutError("timed out")
                    message = stream.get_final_message()
                usages.append(turn_usage(message))
                last = "".join(pieces) or "".join(
                    block.text for block in message.content if block.type == "text"
                )
                written.append(last)
                asked = [block for block in message.content if block.type == "tool_use"]
                if message.stop_reason != "tool_use" or not asked:
                    return usages, finished(written, last, final_only)
                if cancel is not None and cancel.is_set():
                    # Stopped: no further tool runs, and what was written is kept.
                    return usages, text_of(written, last, final_only)
                results, refused = [], False
                for block in asked:
                    name = block.name.removeprefix(TOOL_PREFIX)
                    spec = given.get(name)
                    args = block.input if isinstance(block.input, dict) else {}
                    emit("tool", {"name": name, "detail": spec.shown(args) if spec else ""})
                    if spec is None:
                        text = NOT_GIVEN
                    else:
                        text, refused = answered(spec, scope, args, recorder)
                        if refused:
                            break
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": text}
                    )
                if refused:
                    # Access ended: the bill so far is returned, and the harness
                    # raises the refusal the tool met once its receipts are written.
                    return usages, text_of(written, last, final_only)
                messages += [
                    {"role": "assistant", "content": blocks(message)},
                    {"role": "user", "content": results},
                ]
    except Exception as failure:
        if usages:
            raise Billed(usages, failure) from None
        raise
    raise TurnLimit(usages, text_of(written, last, final_only))


def text_of(written, last, final_only):
    return (last if final_only else "".join(written)).strip()


def finished(written, last, final_only):
    return checked_answer(text_of(written, last, final_only))
