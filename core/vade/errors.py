"""C-04 error surface: the only codes that may cross a process boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["VadeError", "ERRORS", "ERROR_CODES", "is_known_code", "retriable", "http_status"]

_SCHEMA = Path(__file__).resolve().parents[2] / "schema" / "errors.json"


def _load() -> dict[str, dict[str, Any]]:
    data = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    return {code: dict(meta) for code, meta in data["codes"].items()}


ERRORS: Mapping[str, dict[str, Any]] = _load()
ERROR_CODES: frozenset[str] = frozenset(ERRORS)


def is_known_code(code: str) -> bool:
    return code in ERROR_CODES


def retriable(code: str) -> bool:
    return bool(ERRORS.get(code, {}).get("retriable", False))


def http_status(code: str) -> int:
    return int(ERRORS.get(code, {}).get("http", 500))


class VadeError(Exception):
    """A C-04 code plus a human message (<=300 chars).

    `fatal` is NOT part of C-04: it is an attribute of the individual error
    (C-01 §5) and means the caller must drop the connection.
    """

    def __init__(self, code: str, message: str, *, data: Mapping[str, Any] | None = None, fatal: bool = False) -> None:
        if not is_known_code(code):
            raise ValueError(f"{code!r} is not a C-04 error code; modules may not invent codes")
        self.code = code
        self.message = message[:300]
        self.data = dict(data) if data else None
        self.fatal = fatal
        super().__init__(f"{code}: {self.message}")

    @property
    def retriable(self) -> bool:
        return retriable(self.code)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retriable": self.retriable}
