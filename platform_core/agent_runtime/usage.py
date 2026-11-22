"""Strict provider-usage validation shared by every adapter.

The one rule this module exists to enforce: a missing, malformed or out-of-range
token count is an error, never a zero-cost accounting row. Providers bill for
requests whose usage we failed to parse, so silently recording zero would
understate real spend.
"""

from dataclasses import dataclass

TOKEN_LIMIT = 1000000
#: The longest answer accepted, in characters. It bounds a runaway reply; it
#: must never be smaller than what a caller's own output cap lets the model
#: write. It was 20,000 while Code Factory allows up to 64,000 output tokens
#: (roughly 4 characters each): a live implementation phase - the new contents
#: of every changed file - finished, was charged for, and was then thrown away
#: here as "Invalid answer", shown as "Claude response unavailable".
ANSWER_LIMIT = 300000
REQUEST_ID_LIMIT = 160


@dataclass(frozen=True)
class TurnUsage:
    """Billable usage for a single model turn.

    Multi-turn tool use produces one of these per model call, each with its own
    provider request ID, so `record_ai_usage` keeps its replay idempotency.
    """

    request_id: str
    #: Every prompt token, cached or not: what the turn read.
    prompt_tokens: int
    completion_tokens: int
    #: Of `prompt_tokens`, those read from the prompt cache and those written to
    #: it (`cache_write_long` of them for an hour), priced apart (`ai.cost`).
    cache_read: int = 0
    cache_write: int = 0
    cache_write_long: int = 0


class TurnLimit(RuntimeError):
    """A run used every turn it was allowed and never answered.

    Raised by every runtime in place of its SDK's own error, carrying the usage
    of the turns that were sent - each was billed, so each gets a receipt - and
    whatever text the model wrote on the way (`text`). The message is the class
    name alone, as `AgentFailure` reports it: an SDK's message can quote the run.
    """

    def __init__(self, usages, text=""):
        super().__init__("TurnLimit")
        self.usages = list(usages)
        self.text = text


class Billed(RuntimeError):
    """A run failed after turns that were sent and billed.

    Carries their usage, so the harness writes each one's receipt before it fails
    the run, and the failure itself (`cause`), whose class name is all the
    harness reports - a provider's message can quote the run.
    """

    def __init__(self, usages, cause):
        super().__init__(type(cause).__name__)
        self.usages = list(usages)
        self.cause = cause


def checked_count(value, label="Invalid usage"):
    # `type(...) is int` on purpose: bool is an int subclass and True must not pass.
    if type(value) is not int or not 0 <= value <= TOKEN_LIMIT:
        raise ValueError(label)
    return value


def checked_answer(answer):
    if not isinstance(answer, str) or not answer.strip() or len(answer) > ANSWER_LIMIT:
        raise ValueError("Invalid answer")
    return answer


def checked_request_id(value):
    if not isinstance(value, str) or not value or len(value) > REQUEST_ID_LIMIT:
        raise ValueError("Invalid request ID")
    return value


def openai_usage(raw, responses_api):
    """Normalize one OpenAI `ModelResponse.raw_usage` mapping.

    The Responses API reports input/output; Chat Completions reports prompt/completion.
    """
    if not isinstance(raw, dict):
        raise ValueError("Missing usage")
    if responses_api:
        raw = {
            "prompt_tokens": raw.get("input_tokens"),
            "completion_tokens": raw.get("output_tokens"),
        }
    return (
        checked_count(raw.get("prompt_tokens")),
        checked_count(raw.get("completion_tokens")),
    )


def claude_usage(raw):
    """Normalize a Claude usage mapping: (every prompt token, output tokens).

    `input_tokens` is only what was read at the full rate; cache reads and writes
    are prompt tokens too, so the prompt is their sum. `claude_cache` says how
    many of them were which, for the receipt to price apart.
    """
    if not isinstance(raw, dict):
        raise ValueError("Missing usage")
    inputs = checked_count(raw.get("input_tokens"), "Missing token counts")
    outputs = checked_count(raw.get("output_tokens"), "Missing token counts")
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        inputs += checked_count(raw.get(key, 0), "Invalid cache tokens")
    return checked_count(inputs), outputs


def claude_cache(raw):
    """(read, written, written for an hour) from a Claude usage mapping.

    A count the API did not report is none. A breakdown by lifetime
    (`cache_creation`) names the hour-long writes, which cost more; without one
    every write is the five-minute kind, the only one this platform asks for.
    """
    if not isinstance(raw, dict):
        raise ValueError("Missing usage")
    read = checked_count(raw.get("cache_read_input_tokens") or 0, "Invalid cache tokens")
    written = checked_count(raw.get("cache_creation_input_tokens") or 0, "Invalid cache tokens")
    lifetimes = raw.get("cache_creation")
    long = 0
    if isinstance(lifetimes, dict):
        long = checked_count(lifetimes.get("ephemeral_1h_input_tokens") or 0,
                             "Invalid cache tokens")
    if long > written:
        raise ValueError("Invalid cache tokens")
    return read, written, long


def claude_turn(request_id, raw):
    """One Claude turn's `TurnUsage`, its cache reads and writes kept apart."""
    inputs, outputs = claude_usage(raw)
    read, written, long = claude_cache(raw)
    return TurnUsage(request_id, inputs, outputs, read, written, long)


def receipt_usage(turn):
    """A one-shot call's usage as `ai.invoke_ai` records it (`result["usage"]`),
    its cache counts beside."""
    return {
        "prompt_tokens": turn.prompt_tokens,
        "completion_tokens": turn.completion_tokens,
        "cache_read_tokens": turn.cache_read,
        "cache_write_tokens": turn.cache_write,
        "cache_write_long_tokens": turn.cache_write_long,
    }
