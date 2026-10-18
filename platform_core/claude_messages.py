"""One-shot Claude calls through the Messages API, when the credential is an API key.

`claude_agents` drives the bundled Claude Code CLI: a subprocess per call, with
an empty home and a blanked environment. That is the only way a `claude
setup-token` OAuth token can be used, and it was how every Claude call went.
An API key needs no subprocess, so a one-shot call with one is a single
Messages request through the `anthropic` SDK instead. The prompt, the
instructions, the reply cap, the timeout and the answer and usage checks are the
CLI path's own, so a purpose reads the same either way; `ai.invoke_ai` chooses
(`uses_messages_api`), and `claude_messages_api = false` in the configuration
sends every call through the CLI again.

The credential comes only from the mounted file (CLAUDE.md, "Secrets live in
files"). The SDK would otherwise read `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`
and a profile from the environment: an explicit key and base URL stop the
first two and the third, proxies are not taken from the environment, and the
headers `ANTHROPIC_CUSTOM_HEADERS` would add to every request are dropped. No
retries: a failed call may have been billed and is never repeated for anyone.
"""

import json
import logging
import time

import anthropic
from django.conf import settings
from django.core.exceptions import ValidationError

from .agent_runtime.credentials import is_oauth_token
from .agent_runtime.prompts import INSTRUCTIONS
from .agent_runtime.runtime import ANSWER_TOKENS, evidence_payload, recent_turns
from .agent_runtime.usage import checked_answer, checked_request_id, claude_usage
from .claude_agents import TIMEOUT

logger = logging.getLogger(__name__)

BASE_URL = "https://api.anthropic.com"


def uses_messages_api(token):
    """Whether a one-shot Claude call with this credential is a Messages request.

    An API key is; an OAuth token from `claude setup-token` is accepted only by
    the CLI; and no credential at all is the development-only host login, which
    is the CLI's stored session.
    """
    token = str(token or "").strip()
    return bool(token) and not is_oauth_token(token) and settings.CLAUDE_MESSAGES_API


def _http_client(timeout):
    return anthropic.DefaultHttpxClient(follow_redirects=False, trust_env=False, timeout=timeout)


def client_for(token, timeout):
    client = anthropic.Anthropic(
        api_key=token,
        base_url=BASE_URL,
        max_retries=0,
        timeout=timeout,
        http_client=_http_client(timeout),
    )
    # Whatever ANTHROPIC_CUSTOM_HEADERS named would travel on every request,
    # credentials included; nothing a call sends may come from the environment.
    client._custom_headers = {}
    return client


def prompt_for(question, citations, history):
    """The same user turn the CLI path sends."""
    return json.dumps(
        {
            "conversation": [
                {"question": t.question, "answer": t.answer} for t in recent_turns(history)
            ],
            "question": question,
            "evidence": evidence_payload(citations),
        }
    )


def _call(
    model,
    question,
    citations,
    token,
    history,
    instructions=INSTRUCTIONS,
    max_tokens=ANSWER_TOKENS,
    timeout=TIMEOUT,
):
    deadline = time.monotonic() + timeout
    with client_for(token, timeout) as client:
        # Streamed, so a long reply arrives as it is written rather than as
        # one response the connection has to hold open; the deadline is the
        # whole call's, as `asyncio.timeout` is for the CLI.
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=instructions,
            messages=[{"role": "user", "content": prompt_for(question, citations, history)}],
        ) as stream:
            for _ in stream:
                if time.monotonic() > deadline:
                    raise TimeoutError("timed out")
            message = stream.get_final_message()
    usage = message.usage.model_dump() if message.usage is not None else None
    if isinstance(usage, dict):
        # A cache count the API did not report is absent, not zero tokens.
        usage = {key: value for key, value in usage.items() if value is not None}
    inputs, outputs = claude_usage(usage)
    answer = checked_answer(
        "".join(block.text for block in message.content if block.type == "text").strip()
    )
    return {
        "id": checked_request_id(message.id),
        "usage": {"prompt_tokens": inputs, "completion_tokens": outputs},
    }, answer


#: What a status means for the person reading it. The provider's own message is
#: never shown or logged - it may carry request details - but its status is a
#: number and is safe.
STATUS_MESSAGES = {
    401: "Claude rejected the API key for this application. Check the mounted key.",
    403: "Claude refused this request for the mounted API key. Check its permissions.",
    404: "Claude did not accept the configured model. Check the model name in AI settings.",
    429: (
        "Claude's rate limit for this API key was reached. Try again shortly; "
        "it is not retried automatically."
    ),
    529: "Claude is overloaded right now. Try again; it is not retried automatically.",
}

UNAVAILABLE = (
    "Claude response unavailable. Check the configured model, API key and limits. "
    "This request may have incurred provider charges; it is not retried by the application."
)


def completion(config, question, citations, token, history=None, **options):
    try:
        return _call(config.model, question, citations, token, history, **options)
    except anthropic.APIStatusError as failure:
        status = failure.status_code
        logger.warning(
            "claude_messages_failed",
            extra={
                "event": "claude_messages_failed",
                "failure_kind": "status",
                "api_error_status": status,
            },
        )
        raise ValidationError(
            STATUS_MESSAGES.get(status, f"{UNAVAILABLE} (HTTP {status}.)")
        ) from None
    except (anthropic.APITimeoutError, TimeoutError):
        logger.warning(
            "claude_messages_failed",
            extra={"event": "claude_messages_failed", "failure_kind": "timeout"},
        )
        raise ValidationError(
            "Claude did not respond in time. Large source sets can exceed the limit; "
            "try again, or reduce the number of sources."
        ) from None
    except (anthropic.APIError, OSError, ValueError, TypeError) as failure:
        logger.warning(
            "claude_messages_failed",
            extra={"event": "claude_messages_failed", "failure_kind": type(failure).__name__},
        )
        raise ValidationError(UNAVAILABLE) from None
