"""Allowlisted structured logging: never serialize request bodies, URLs or exceptions."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_context = ContextVar("request_id", default="")


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "application_event"),
        }
        # Fixed internal labels only; nothing here ever carries user data.
        #
        # `exception_type` is a class name and nothing else - the reason it is
        # allowed where a message and a traceback are not. It was being passed
        # by callers and silently dropped here, which made a transient failure
        # and a permanent one identical in the log: precisely the distinction
        # document_worker added it to record.
        for field in (
            "request_id",
            "route",
            "status",
            "duration_ms",
            "lane",
            "count",
            "exception_type",
            # A failed Claude CLI result's structured fields: SDK labels and
            # numbers, never the provider's prose (see claude_agents).
            "subtype",
            "terminal_reason",
            "api_error_status",
            "stop_reason",
            "duration_api_ms",
            "num_turns",
            "error_count",
            "prompt_bytes",
            "max_tokens",
            "api_error_kind",
            "output_tokens",
        ):
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["exception_type"] = record.exc_info[0].__name__
        return json.dumps(entry, separators=(",", ":"))
