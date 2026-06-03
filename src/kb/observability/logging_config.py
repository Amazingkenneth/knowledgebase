"""Structured logging with per-request correlation IDs.

``request_id_var`` is a contextvar set by ``RequestContextMiddleware`` for the
duration of each request. ``RequestIdFilter`` copies it onto every log record
so existing ``log.warning(...)`` calls across the codebase carry the id with no
call-site changes. Outside a request (startup, background tasks) it reads ``-``.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar

from kb.config import ObservabilityConfig

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(cfg: ObservabilityConfig) -> None:
    """Install a single root handler that carries the request id.

    Replaces existing root handlers so a `--reload` restart doesn't stack
    duplicate handlers. ``kb.*`` loggers propagate to root and pick this up.
    """
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if cfg.json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
