"""Structured JSON logging for DART.

All DART processes (API, worker, scheduler) route logs through this
formatter so that request attempts, dispatch latency, status codes, and
circuit breaker transitions are machine-parseable in aggregation tooling
(e.g. Datadog, CloudWatch Logs Insights, Loki) rather than free-text.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Final

# Attributes that `logging.LogRecord` always sets internally. Anything a
# caller passes via `extra={...}` will NOT be in this set, which is how we
# distinguish "standard" fields from structured extras to merge in.
_RESERVED_LOG_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


class JSONFormatter(logging.Formatter):
    """Renders each `LogRecord` as a single-line JSON object.

    Standard fields (timestamp, level, logger name, message) are always
    present. Any extra fields passed via
    `logger.info(..., extra={"status_code": 503, "latency_ms": 812.4})`
    are merged directly into the top-level JSON object, enabling
    structured capture of dispatch attempts without a custom log schema.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install the JSON formatter on the root logger's stdout handler.

    Idempotent: safe to call once at API startup and again at worker
    startup (or in tests) without accumulating duplicate handlers, since
    any pre-existing handlers on the root logger are removed first.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for existing_handler in list(root.handlers):
        root.removeHandler(existing_handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger. Formatting is applied at the root
    logger via `configure_logging`, so callers don't attach handlers."""
    return logging.getLogger(name)
