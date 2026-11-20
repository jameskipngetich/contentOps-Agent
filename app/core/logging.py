"""Structured logging.

One JSON object per line by default, which is what container log collectors
expect. ``LOG_FORMAT=console`` switches to a readable format for local debugging.
Every request gets an id that is attached to its log records and echoed in errors.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}

# Attributes we add ourselves; they have their own JSON fields.
_EXTRA_FIELDS = {
    "method",
    "path",
    "status_code",
    "duration_ms",
    "client",
    "error_code",
    "exc_type",
    "detail",
    "platform",
    "report_id",
    "service",
    "attempt",
    "check",
    "errors",
    "event",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_ctx.get()),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in _RESERVED:
                continue
            if key in _EXTRA_FIELDS or key not in _RESERVED:
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} "
            f"{record.levelname:<7} [{request_id_ctx.get()}] "
            f"{record.name}: {record.getMessage()}"
        )
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and key not in {"request_id"}
        }
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class RequestIdFilter(logging.Filter):
    """Ensure every record carries the current request id."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(record, "request_id", request_id_ctx.get())
        return True


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Uvicorn access logs duplicate our middleware access log.
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
