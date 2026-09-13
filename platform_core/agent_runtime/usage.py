"""Strict provider-usage validation shared by every adapter.

The one rule this module exists to enforce: a missing, malformed or out-of-range
token count is an error, never a zero-cost accounting row. Providers bill for
requests whose usage we failed to parse, so silently recording zero would
understate real spend.
"""

from dataclasses import dataclass

TOKEN_LIMIT = 1000000
ANSWER_LIMIT = 20000
REQUEST_ID_LIMIT = 160


@dataclass(frozen=True)
class TurnUsage:
    """Billable usage for a single model turn.

    Multi-turn tool use produces one of these per model call, each with its own
    provider request ID, so `record_ai_usage` keeps its replay idempotency.
    """

    request_id: str
    prompt_tokens: int
    completion_tokens: int


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
    """Normalize a Claude usage mapping, folding cache tokens into the input count.

    Cache reads and writes are billed; the platform prices them at the configured
    ordinary input rate because there is no separate cache rate in AIConfiguration.
    """
    if not isinstance(raw, dict):
        raise ValueError("Missing usage")
    inputs = checked_count(raw.get("input_tokens"), "Missing token counts")
    outputs = checked_count(raw.get("output_tokens"), "Missing token counts")
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        inputs += checked_count(raw.get(key, 0), "Invalid cache tokens")
    return checked_count(inputs), outputs
