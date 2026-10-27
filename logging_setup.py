"""
logging_setup.py -- one place to configure logging for the whole tool.

CLI/entry points call configure() once at startup. Everything else just does
`logging.getLogger("agentforce_test.<area>")` and logs; nothing else touches
handlers. Level and format are driven by config (LOG_LEVEL, LOG_FORMAT env
vars), so a scheduled run can emit JSON lines for log scraping while a developer
gets readable text.
"""
from __future__ import annotations

import json
import logging
import sys

import config

ROOT = "agentforce_test"
_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure(level: str | None = None, fmt: str | None = None) -> None:
    """Idempotent: safe to call from every entry point; only the first call wins."""
    global _configured
    if _configured:
        return
    level = (level or config.LOG_LEVEL).upper()
    fmt = (fmt or config.LOG_FORMAT).lower()

    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))

    root = logging.getLogger(ROOT)
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers[:] = [handler]
    root.propagate = False
    _configured = True


def get_logger(area: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{area}")
