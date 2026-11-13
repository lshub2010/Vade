"""Error codes (C-04) and the one exception a tool may raise.

A module may ONLY return codes from `schema/errors.json`. The table below is a
frozen copy so standalone runs do not need the repository; when the schema file
is reachable the two are compared at import time (fail loudly instead of
drifting). `FORBIDDEN` is deliberately absent: it appeared in an older task
book, is not in C-04, and core maps unknown codes to INTERNAL.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

__all__ = ["ERROR_CODES", "ToolError", "RETRIABLE", "schema_codes"]

#: code -> retriable, verbatim from schema/errors.json (C-04).
ERROR_CODES: dict[str, bool] = {
    "INVALID_ARGUMENT": False,
    "NOT_FOUND": False,
    "PERMISSION_DENIED": False,
    "TIMEOUT": True,
    "CANCELLED": False,
    "UNAVAILABLE": True,
    "OVERLOADED": True,
    "MEMORY_FULL": True,
    "PAYLOAD_TOO_LARGE": False,
    "UNSUPPORTED": False,
    "PROTOCOL_MISMATCH": False,
    "MANIFEST_INVALID": False,
    "TOOL_CONFLICT": False,
    "RISK_DOWNGRADE_FORBIDDEN": False,
    "APPROVER_INVALID": False,
    "ORIGIN_MISMATCH": False,
    "READ_ONLY_SESSION": False,
    "BIND_UNAUTHORIZED": False,
    "BIND_FINGERPRINT_MISMATCH": False,
    "BIND_HELLO_SIG_INVALID": False,
    "BIND_DEVICE_LIMIT": False,
    "BIND_CLOCK_SKEW": False,
    "BIND_KEY_UNSUPPORTED": False,
    "BIND_SETUP_LOCKED": True,
    "AUTH_FAILED": True,
    "ADMIN_LOCKED": True,
    "SETUP_UNAUTHORIZED": True,
    "MODULE_DRAINING": True,
    "INTERNAL": False,
    "CONFIRM_REQUIRED": False,
    "UNKNOWN_CONFIG_KEY": False,
    "CODE_INVALID": False,
    "CODE_EXPIRED": False,
}

RETRIABLE = {code for code, retriable in ERROR_CODES.items() if retriable}

_MAX_MESSAGE = 300


def schema_codes(schema_path: Path | str | None = None) -> dict[str, bool] | None:
    """Read C-04 from disk, or return None when it is not reachable."""
    candidates: list[Path] = []
    if schema_path is not None:
        candidates.append(Path(schema_path))
    env = os.environ.get("VADE_SCHEMA_DIR")
    if env:
        candidates.append(Path(env) / "errors.json")
    # repo layout: sdk/python/vade_sdk/errors.py -> ../../../schema/errors.json
    candidates.append(Path(__file__).resolve().parents[3] / "schema" / "errors.json")
    for candidate in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        table = raw.get("codes", raw)
        if not isinstance(table, Mapping):
            continue
        return {str(k): bool(v.get("retriable", False)) if isinstance(v, Mapping) else bool(v) for k, v in table.items()}
    return None


class ToolError(Exception):
    """A structured tool failure. Only C-04 codes are accepted.

    `retriable` defaults to the code's own property; passing a different value
    here does not let a module change core's policy — core uses the table — it
    only documents intent. Unknown codes raise ValueError at construction so a
    typo surfaces at import time, not as an INTERNAL on a live call.
    """

    def __init__(self, code: str, message: str, retriable: bool | None = None, data: Mapping[str, Any] | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(
                f"{code!r} is not a C-04 error code; modules may only use the codes in schema/errors.json"
            )
        self.code = code
        self.message = str(message)[:_MAX_MESSAGE]
        self.retriable = ERROR_CODES[code] if retriable is None else bool(retriable)
        self.data = dict(data) if data else None
        super().__init__(f"{code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
        }
        if self.data:
            payload["data"] = self.data
        return payload


_checked = False


def _check_against_schema() -> None:
    """Import-time sanity check: the frozen copy must equal C-04 when present."""
    global _checked
    if _checked:
        return
    _checked = True
    table = schema_codes()
    if table is None:
        return
    if table != ERROR_CODES:
        missing = sorted(set(table) - set(ERROR_CODES))
        extra = sorted(set(ERROR_CODES) - set(table))
        raise RuntimeError(
            "vade_sdk error table drifted from schema/errors.json "
            f"(missing={missing}, extra={extra}); update vade_sdk/errors.py"
        )


_check_against_schema()
