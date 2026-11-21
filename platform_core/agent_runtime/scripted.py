"""A model that follows a script, for tests and the scorecard - never a provider.

`AIConfiguration.provider` is only ever "openai" or "claude", so nothing a person
configures reaches this; a test hands it to the harness in place of
`harness.runtime_for`. It exists so every guardrail can be exercised with no
provider at all: what a run does when a model asks for a foreign id, a tool it
was not given, more turns than it was allowed, or keeps calling tools after Stop.

A script is a list of turns. Each is a list of tool calls - `(name, args)` pairs,
or a callable run at that moment, to revoke a grant or press Stop mid-run - or a
string, which is the answer and ends the run. Tools run through `run_tool`, the
one path the real runtimes take, on the calling thread: `run` is plain code, not a
coroutine, because the real runtimes' hop to a thread-sensitive executor would put
the tools on a connection a test's transaction cannot be seen from. As
the provider SDKs do, a tool's exception becomes text for the model and the run
carries on, a tool the run was not given is refused without running, and a run
that needs more turns than it was allowed fails.
"""

import uuid

from .runtime import ANSWER_TOKENS
from .tools import run_tool
from .usage import TurnUsage

#: What a tool the run was not given answers, as the CLI's `dontAsk` refuses it.
NOT_GIVEN = "That tool is not available."
TOOL_ERROR = "The tool failed."


class TurnsExceeded(RuntimeError):
    """The script needed more turns than the run was allowed."""


class Script:
    def __init__(self, *turns, usage=(100, 20)):
        self.turns = list(turns)
        self.usage = usage
        self.seen = []
        self.given = None
        self.max_turns = None
        self.tool_text = []

    def run(
        self,
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
        max_turns=4,
    ):
        given = {spec.name: spec for spec in (tools or [])}
        self.given = sorted(given)
        self.max_turns = max_turns
        usages = []
        for number, turn in enumerate(self.turns, 1):
            if number > max_turns:
                raise TurnsExceeded(f"Needed turn {number} of {max_turns}.")
            usages.append(
                TurnUsage(f"scripted-{uuid.uuid4()}", self.usage[0], self.usage[1])
            )
            if isinstance(turn, str):
                return usages, turn
            for call in turn:
                if callable(call):
                    call()
                    continue
                name, args = call
                self.seen.append(name)
                spec = given.get(name)
                if spec is None:
                    self.tool_text.append(NOT_GIVEN)
                    continue
                emit("tool", {"name": name, "detail": spec.shown(args)})
                try:
                    self.tool_text.append(run_tool(spec, scope, args, recorder))
                except Exception:
                    self.tool_text.append(TOOL_ERROR)
        raise TurnsExceeded("The script ended without an answer.")
