"""Argument redaction + digest for approval/audit rows (C5a).

Two different things live here on purpose:

* ``digest_args`` is a stable hash of the *full* argument object. It is what
  session-scope grants key on, and what makes two `rm -rf /` calls with
  different targets distinguishable.
* ``redact_args`` is what may be persisted or shown in the audit page. Any key
  that smells like a credential is replaced wholesale; long strings are
  truncated so one tool call cannot balloon a row.

The digest is computed on the raw arguments, the redacted copy never feeds it.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

__all__ = [
    "SENSITIVE_KEY", "SAFE_METRIC_KEYS", "digest_args", "redact_args",
    "summarize_args", "MAX_STR",
]

#: Field names whose *values* never reach an audit row. Over-redaction is the
#: intended failure mode: `api_key_hint` is redacted too, and that is fine.
SENSITIVE_KEY = re.compile(
    r"(pass(word|wd|phrase)?|token|secret|credential|psk|private|"
    r"api[_-]?key|access[_-]?key|auth|session[_-]?id|cookie)",
    re.IGNORECASE,
)

#: `SENSITIVE_KEY` deliberately matches any name *containing* "token", which
#: also catches the numeric token COUNTS every `llm.call` row carries. Those
#: counts drive the usage budget (`admin/app.py` `config_usage`), so redacting
#: them would silently report 0 tokens used. Credentials are strings; counts
#: are the only names allowed through the regex.
SAFE_METRIC_KEYS = frozenset({
    "tokens", "total_tokens", "token_count", "tokens_used", "token_usage",
    "prompt_tokens", "completion_tokens", "cached_tokens",
})

MAX_STR = 200
MAX_ITEMS = 32
REDACTED = "[redacted]"

try:  # C3 owns the digest used on the event stream; reuse it when present.
    from ..loop.tools import digest_args as _c3_digest  # type: ignore
except Exception:  # pragma: no cover - import shape differs across batches
    _c3_digest = None


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)[:MAX_STR]


def _own_digest(args: Mapping[str, Any]) -> str:
    blob = json.dumps(_canonical(args), ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def digest_args(args: Mapping[str, Any] | None) -> str:
    """Stable, order-independent digest of the complete argument object."""
    payload = dict(args or {})
    if _c3_digest is not None:
        try:
            return str(_c3_digest(payload))
        except Exception:
            pass
    return _own_digest(payload)


def _redact_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[truncated]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                out["..."] = f"{len(value) - MAX_ITEMS} more"
                break
            name = str(key)
            if name in SAFE_METRIC_KEYS:
                out[name] = _redact_value(item, depth + 1)
            else:
                out[name] = REDACTED if SENSITIVE_KEY.search(name) else _redact_value(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [_redact_value(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"... {len(value) - MAX_ITEMS} more")
        return items
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR] + f"...(+{len(value) - MAX_STR})"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return repr(value)[:MAX_STR]


def redact_args(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """A JSON-safe copy with credential-bearing fields replaced by ``[redacted]``."""
    if not args:
        return {}
    return _redact_value(dict(args))  # type: ignore[return-value]


def summarize_args(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compact redacted form for the audit list page (key -> short value)."""
    redacted = redact_args(args)
    summary: dict[str, Any] = {}
    for index, (key, value) in enumerate(redacted.items()):
        if index >= 8:
            break
        if isinstance(value, (dict, list)):
            summary[key] = json.dumps(value, ensure_ascii=False)[:120]
        else:
            summary[key] = value
    return summary
