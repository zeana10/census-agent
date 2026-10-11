"""Structured (JSON) logging with a per-request correlation id.

Every log line written while handling a chat request carries `request_id` and
`session` (set via context variables in main.py), so one user turn can be
traced across the guardrail, each model step, each SQL query and the outcome.
Fields passed through `extra={...}` become top-level JSON keys, so dashboards
can filter on `outcome`, `elapsed_s` and similar fields without parsing messages.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("request_id", default="-")
session_tag: ContextVar[str] = ContextVar("session_tag", default="-")

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id.get(),
            "session": session_tag.get(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record)} {record.levelname} {record.name} [{request_id.get()}] {record.getMessage()}"
        extras = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure(level: str, fmt: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn's access log duplicates our per-turn line and doesn't carry the request id.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
