"""RFC-0006 §5.2: the `args_preview` shown on a tool card.

Two audiences, one rule: a preview must never become a second leak path for the
secrets that `turn.approval_required` owns. `risk=high` calls therefore only
show the *shape* (`<str:12>`), never the text — the full arguments already went
to every attached UI through the approval event, and repeating them in
`turn.tool_call` would put them into replay, exports and logs.

`args_digest` is untouched by all of this: it stays the audit/approval anchor.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

__all__ = ["build_args_preview", "redact_value", "PREVIEW_MAX_BYTES", "SECRET_PATTERNS"]

#: Byte ceiling of the serialized preview (RFC-0006 §5.1).
PREVIEW_MAX_BYTES = 512
#: `args_preview_redacted` carries at most this many dot-paths (§5.1).
MAX_REDACTED_PATHS = 16

_MASK = "***"

#: Key-name matching is two-stage, because the multi-word names below contain
#: the separators the tokenizer removes:
#:   1. normalize the WHOLE key (lowercase, separators dropped) and match the
#:      compound alternatives — `api_key`, `api-key`, `X-Api-Key`,
#:      `aws_secret_access_key` all collapse to a `…apikey` ending;
#:   2. split into tokens and match them whole-word, which is what keeps
#:      `author` out of `auth` and catches the `_token`/`_secret`/`_password`
#:      suffixes (`refresh_token`, `client_secret`, `db_password`).
#: Matching only the split tokens (the pre-fix behaviour) made every alternative
#: carrying `[_-]?` dead code: a token can never contain `_` or `-`.
_SECRET_KEY_WORD = re.compile(
    r"(?:pass(?:word|phrase)?|secret|token|credential|cookie|authorization|auth|dsn)"
)
_SECRET_KEY_COMPOUND = (
    "apikey", "accesskey", "privatekey", "sessionid", "connectionstring",
)
_KEY_SPLIT = re.compile(r"[_.\-\s]+")
#: `apiKey`/`clientSecret` split before the token pass; cheap, and the compound
#: pass already covers the leading-word forms.
_CAMEL_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


#: Applied to every string value; a hit replaces the WHOLE value, not the match,
#: so a re-scan of the finished preview cannot find the pattern again.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[bap]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"[A-Fa-f0-9]{32,}"),
    #: RFC-0006 §5.2 step 3: >=32 characters (32 chars is the common shape of a
    #: 24-byte random token). `-`/`_` are URL-safe base64, which the old
    #: `{40,}` alphabet silently let through.
    re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}"),
)


def redact_value(value: str) -> str | None:
    """`None` when clean, else the masked value (the caller records the path)."""
    for pattern in SECRET_PATTERNS:
        if pattern.search(value):
            return _MASK
    return None


def _normalize_key(key: str) -> str:
    return _NON_ALNUM.sub("", key.lower())


def _key_tokens(key: str) -> list[str]:
    return [token for token in _KEY_SPLIT.split(_CAMEL_BREAK.sub("_", key)) if token]


def _key_is_secret(key: str) -> bool:
    """Whole-key normalization first, then whole-token matching (see above)."""
    normalized = _normalize_key(key)
    if not normalized:
        return False
    if any(normalized.endswith(name) for name in _SECRET_KEY_COMPOUND):
        return True
    if _SECRET_KEY_WORD.fullmatch(normalized):
        return True
    return any(_SECRET_KEY_WORD.fullmatch(_normalize_key(token)) for token in _key_tokens(key))


def _compress(value: Any, *, depth: int, redacted: list[str], path: str,
              str_max: int, arr_max: int, obj_max: int, max_depth: int,
              typed: bool) -> Any:
    if depth > max_depth:
        return "…"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        items = list(value.items())
        for index, (raw_key, sub) in enumerate(items):
            if index >= obj_max:
                out["…"] = f"(+{len(items) - obj_max} keys)"
                break
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            if _key_is_secret(key):
                _note(redacted, child_path)
                key = _MASK
                while key in out:
                    key = key + "_"
                out[key] = _MASK
                continue
            out[key] = _compress(sub, depth=depth + 1, redacted=redacted,
                                 path=child_path, str_max=str_max, arr_max=arr_max,
                                 obj_max=obj_max, max_depth=max_depth, typed=typed)
        return out
    if isinstance(value, (list, tuple)):
        items = list(value)
        out_list: list[Any] = []
        for index, sub in enumerate(items[:arr_max]):
            out_list.append(_compress(sub, depth=depth + 1, redacted=redacted,
                                      path=f"{path}[{index}]", str_max=str_max,
                                      arr_max=arr_max, obj_max=obj_max,
                                      max_depth=max_depth, typed=typed))
        if len(items) > arr_max:
            out_list.append(f"…(+{len(items) - arr_max})")
        return out_list
    if isinstance(value, str):
        if typed:
            return f"<str:{len(value)}>"
        masked = redact_value(value)
        if masked is not None:
            _note(redacted, path)
            return masked
        if len(value) > str_max:
            return value[: str_max - 3] + "…"
        return value
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    # Anything else (datetime, dataclass, bytes) is previewed as its repr, which
    # still goes through the string rules on the next pass.
    return _compress(str(value), depth=depth, redacted=redacted, path=path,
                     str_max=str_max, arr_max=arr_max, obj_max=obj_max,
                     max_depth=max_depth, typed=typed)


def _note(redacted: list[str], path: str) -> None:
    if path not in redacted and len(redacted) < MAX_REDACTED_PATHS:
        redacted.append(path)


def _serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fits(text: str) -> bool:
    return len(text.encode("utf-8")) <= PREVIEW_MAX_BYTES


def _shrink_to_fit(text: str) -> str:
    """Last resort: a valid JSON object holding a prefix of the preview.

    Truncating the JSON text itself would leave unparseable JSON, and the
    contract says `args_preview` is a string the UI pretty-prints — so the
    prefix goes *inside* a string field instead.
    """
    lo, hi = 0, len(text)
    best = _serialize({"_": ""})
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _serialize({"_": text[:mid]})
        if len(candidate.encode("utf-8")) <= PREVIEW_MAX_BYTES:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def build_args_preview(args: Any, risk: str) -> dict[str, Any]:
    """Return the four `turn.tool_call` preview fields (never the raw args).

    Order is the RFC's, and it matters: canonicalize → key redaction → value
    scrub → shape compression → size cap. Scrubbing before compression is what
    keeps a truncated secret from looking safe.
    """
    typed = str(risk).lower() == "high"
    canonical: Any = args if isinstance(args, Mapping) else {"value": args}
    redacted: list[str] = []

    payload = _compress(
        canonical, depth=0, redacted=redacted, path="",
        str_max=80, arr_max=8, obj_max=16, max_depth=3, typed=typed,
    )
    text = _serialize(payload)
    truncated = False
    if not _fits(text):
        payload = _compress(
            canonical, depth=0, redacted=redacted, path="",
            str_max=24, arr_max=3, obj_max=6, max_depth=2, typed=typed,
        )
        text = _serialize(payload)
    if not _fits(text):
        text = _shrink_to_fit(text)
        truncated = True
    return {
        "args_preview": text,
        "args_preview_mode": "typed" if typed else "redacted",
        "args_preview_redacted": redacted,
        "args_preview_truncated": truncated,
    }
