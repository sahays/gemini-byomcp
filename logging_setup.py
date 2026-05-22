"""
logging_setup.py
Structured JSON logging for Cloud Run / Cloud Logging.

Cloud Logging auto-promotes top-level `severity` and `message` fields; the
rest land under `jsonPayload` and are filterable in the Logs Explorer
(e.g. `jsonPayload.tool="list_miro_boards"`).

Usage:
    from logging_setup import configure_logging, log_event
    configure_logging()
    log_event("info", "tool.invoke", tool="list_miro_boards", request_id=rid)
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

# Per-request correlation id. Set by the bearer middleware; read by every log call.
current_request_id: ContextVar[str] = ContextVar("current_request_id", default="-")


_PYTHON_TO_GCP_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": _PYTHON_TO_GCP_SEVERITY.get(record.levelname, record.levelname),
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": current_request_id.get(),
        }
        extras = getattr(record, "extras", None)
        if isinstance(extras, dict):
            for k, v in extras.items():
                if k not in payload:
                    payload[k] = v
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Install JsonFormatter on the root logger, replacing any existing handlers."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def log_event(level: str, event: str, logger_name: str = "mcp", **fields: Any) -> None:
    """Emit a structured log line.

    `event` becomes the human-readable message; `fields` land in jsonPayload as
    siblings of `message` so they're filterable in Cloud Logging.
    """
    log = logging.getLogger(logger_name)
    log_level = getattr(logging, level.upper(), logging.INFO)
    log.log(log_level, event, extra={"extras": {"event": event, **fields}})


class Timer:
    """Tiny context manager to measure outbound call latency."""

    def __enter__(self) -> "Timer":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *_exc) -> None:
        self.elapsed_ms = int((time.perf_counter() - self.t0) * 1000)
