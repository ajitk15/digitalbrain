"""Structured logging with a correlation identifier. NFR-08.

One JSON object per line, because the log shipper parses lines and a traceback
spread over twelve of them is twelve unrelated events to it.

NFR-04 forbids protected health information in application logs. The filter
below is the enforcement point rather than a convention: a formatter that sees a
value it recognises as an identifier redacts it, so a careless log call is a
redacted line and not an incident.
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    """Mint an identifier for one request, so its lines can be gathered later."""
    value = uuid.uuid4().hex
    correlation_id.set(value)
    return value


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON object, correlation identifier included."""

    def format(self, record: logging.LogRecord) -> str:
        from .security.phi import redact

        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": correlation_id.get(),
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger, replacing any handlers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
