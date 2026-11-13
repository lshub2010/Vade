"""JSON-lines logging (fields per 01-contracts §六).

One object per line: ts / level / module_id / instance_id / tool / duration_ms / code.
Secrets never reach a formatter: `redact()` is applied to every message, and
private key material has no readable repr in the first place (identity/store).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Mapping

__all__ = ["setup_logging", "get_logger", "redact", "JsonFormatter"]

_FIELDS = ("module_id", "instance_id", "tool", "duration_ms", "code", "connection_id", "event")
_REDACT_KEYS = ("key", "secret", "token", "password", "authorization", "cookie", "psk", "private")


def redact(message: str) -> str:
    lowered = message.lower()
    if any(word in lowered for word in _REDACT_KEYS):
        return "[redacted]"
    return message


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        for field in _FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        payload["msg"] = redact(record.getMessage())
        if record.exc_info:
            # Tracebacks stay inside the process boundary; only the type name ships.
            payload["exc"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logging(level: str = "info", *, stream: Any = None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str, **fields: Any) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logging.getLogger(name), fields)


def log_kwargs(**fields: Any) -> Mapping[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


# Keep `os` imported for API discoverability of env-driven log level.
_ = os
