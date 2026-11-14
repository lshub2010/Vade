"""Ask-user channel: the request record, the four controls and value validation.

Approval answers *whether*; input answers *what*. The two are deliberately
separate channels (RFC-0008 §2.1): an approval's `scope:"session"` cache would
answer the next identical question for the user, and its audit row would say
`kind:"approval"` for something that was not a permission decision.

Everything a client submits is validated **here**, in core, against the record
core itself created (`schema` / `options` are core-owned, never trusted from the
wire). A rejected value must not change the turn state, emit an event or consume
the `input_id` — the client shows the error and keeps the dialog open (§4.4).

`value` is data, never code (§9.1): it is returned to the model as a tool result
and nothing in this module ever interpolates it into a command, a path or an
argument template.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import VadeError

__all__ = [
    "InputKind", "InputOption", "InputRequest", "InputOutcome", "InputConfig",
    "KINDS", "PROMPT_MAX", "DETAIL_MAX", "LABEL_MAX", "OPTION_DESC_MAX",
    "FREE_TEXT_DEFAULT_MAX", "FREE_TEXT_HARD_MAX", "OPTIONS_MIN", "OPTIONS_MAX",
    "normalize_request", "validate_value", "schema_for", "value_digest",
    "value_chars", "plain_text", "InputManagerError",
]


class InputKind:
    """The four control kinds (C-06 declares them as a string enum)."""

    FREE_TEXT = "free_text"
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    CONFIRM = "confirm"


KINDS = frozenset({InputKind.FREE_TEXT, InputKind.SINGLE_CHOICE,
                   InputKind.MULTI_CHOICE, InputKind.CONFIRM})
CHOICE_KINDS = frozenset({InputKind.SINGLE_CHOICE, InputKind.MULTI_CHOICE})

PROMPT_MAX = 500
DETAIL_MAX = 2000
LABEL_MAX = 120
OPTION_DESC_MAX = 240
OPTION_ID_MAX = 64
FREE_TEXT_DEFAULT_MAX = 4000
FREE_TEXT_HARD_MAX = 8000
OPTIONS_MIN = 2
OPTIONS_MAX = 20

#: Control characters are stripped from model-authored text before it reaches a
#: UI: `prompt`/`label` are untrusted content (RFC-0008 §9.2), and a lone `\x1b`
#: in a terminal-rendered client is an escape sequence. `\n` and `\t` survive so
#: a multi-line question still reads as one.
_CONTROL = {code: None for code in range(0x20) if code not in (0x09, 0x0A)}
_CONTROL[0x7F] = None


class InputManagerError(VadeError):
    """Kept as a named class so callers can catch one type, not five strings."""


@dataclass(frozen=True, slots=True)
class InputOption:
    id: str
    label: str
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "description": self.description}


@dataclass(frozen=True, slots=True)
class InputRequest:
    """One question, as the sender ( `ask_user` tool or a module) describes it.

    `emit` is the core-owned writer for `turn.input_required` /
    `turn.input_resolved`; the manager never opens its own event channel, so the
    pair lands on the ONE seq stream every attached UI already follows.
    """

    session_id: str
    turn_id: str
    kind: str
    prompt: str
    detail: str | None = None
    options: Sequence[InputOption | Mapping[str, Any]] = ()
    timeout_s: int | None = None
    default: Any = None
    #: `free_text` only: the sender may ask for a shorter box; core caps it at
    #: `[ask_user] max_free_text_chars` (hard ceiling 8000).
    max_length: int | None = None
    source: Mapping[str, Any] = field(default_factory=dict)
    emit: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None


@dataclass(frozen=True, slots=True)
class InputOutcome:
    """What the model gets back: a value, or an explicit `CANCELLED`.

    There is no third state and no empty value: "the user did not answer" is
    `ok=False, code="CANCELLED"`, never `""` / `None` / the `default` (§8.1).
    """

    ok: bool
    status: str                       # submitted|timeout|cancelled
    value: Any = None
    input_id: str | None = None
    by: str | None = None
    reason: str = ""
    code: str | None = None
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    duplicate: bool = False

    def tool_result(self) -> dict[str, Any]:
        """The `ask_user` tool result shape (C-05 `tool_result.result`)."""
        if self.ok:
            return {"ok": True, "value": self.value, "input_id": self.input_id,
                    "status": self.status}
        return {"ok": False, "error": {"code": self.code or "CANCELLED",
                                       "message": self.message or "the user did not answer",
                                       "input_id": self.input_id,
                                       "reason": self.reason or "cancelled"}}


@dataclass(frozen=True, slots=True)
class InputConfig:
    """`[ask_user]` (C-18), all `apply: hot`: they only affect later requests."""

    default_timeout_s: int = 120
    max_timeout_s: int = 600
    min_timeout_s: int = 5
    max_free_text_chars: int = FREE_TEXT_HARD_MAX
    max_multi_select: int = OPTIONS_MAX
    allow_subagents: bool = False
    audit_value: bool = False

    @classmethod
    def from_config(cls, cfg: Any) -> "InputConfig":
        def _int(key: str, fallback: int) -> int:
            try:
                value = cfg.get(f"ask_user.{key}") if cfg is not None else None
            except Exception:  # pragma: no cover - a stub config in tests
                value = None
            if value is None or isinstance(value, bool):
                return fallback
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        def _bool(key: str, fallback: bool) -> bool:
            try:
                value = cfg.get(f"ask_user.{key}") if cfg is not None else None
            except Exception:  # pragma: no cover
                value = None
            return fallback if value is None else bool(value)

        default = max(_int("default_timeout_s", 120), 5)
        ceiling = max(_int("max_timeout_s", 600), default)
        return cls(
            default_timeout_s=min(default, ceiling),
            max_timeout_s=ceiling,
            max_free_text_chars=min(max(_int("max_free_text_chars", FREE_TEXT_HARD_MAX), 1),
                                    FREE_TEXT_HARD_MAX),
            max_multi_select=min(max(_int("max_multi_select", OPTIONS_MAX), 1), OPTIONS_MAX),
            allow_subagents=_bool("allow_subagents", False),
            # Q5 keeps the raw value out of the audit table; the key exists so a
            # future opt-in does not change the config shape. v1 does not honour
            # it (see `InputManager._audit_response`).
            audit_value=_bool("audit_value", False),
        )


# --------------------------------------------------------------------- helpers
def plain_text(value: str, limit: int) -> str:
    """Model-authored text -> plain text: control chars dropped, hard length cap."""
    text = str(value).translate(_CONTROL)
    return text[:limit]


def _code_points(text: str) -> int:
    return len(text)


def _invalid(message: str, **data: Any) -> InputManagerError:
    payload: dict[str, Any] = {"field": "value"}
    payload.update({k: v for k, v in data.items() if v is not None})
    return InputManagerError("INVALID_ARGUMENT", message, data=payload)


def value_digest(value: Any) -> str:
    """`sha256(value)` over a canonical encoding — the audit keeps this, not the value."""
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def value_chars(value: Any) -> int:
    """Code-point count of the answer (arrays: sum of their items; bool: 0)."""
    if isinstance(value, str):
        return _code_points(value)
    if isinstance(value, (list, tuple)):
        return sum(_code_points(item) for item in value if isinstance(item, str))
    return 0


def schema_for(kind: str, option_ids: Sequence[str], *, max_length: int = FREE_TEXT_DEFAULT_MAX,
               min_select: int = 1, max_select: int | None = None) -> dict[str, Any]:
    """The fixed descriptor for one kind (§4.3). Never a form DSL, never nested."""
    if kind == InputKind.FREE_TEXT:
        return {"type": "string", "max_length": int(max_length), "multiline": True}
    if kind == InputKind.SINGLE_CHOICE:
        return {"type": "string", "enum": list(option_ids), "allow_other": False}
    if kind == InputKind.MULTI_CHOICE:
        return {
            "type": "array",
            "items": {"type": "string", "enum": list(option_ids)},
            "min_items": int(min_select),
            "max_items": int(max_select if max_select is not None else len(option_ids)),
            "allow_other": False,
        }
    return {"type": "boolean"}


# -------------------------------------------------------------------- request
def normalize_request(req: InputRequest, config: InputConfig) -> dict[str, Any]:
    """Validate the SENDER's side (§4.4.4) and derive the core-owned record.

    A bad question returns `INVALID_ARGUMENT` to the `ask_user` tool and the turn
    continues: one malformed prompt must not fail a turn.
    """
    kind = str(req.kind or "").strip().lower()
    if kind not in KINDS:
        raise InputManagerError(
            "INVALID_ARGUMENT",
            f"kind must be one of {sorted(KINDS)}",
            data={"field": "kind", "expected": sorted(KINDS)},
        )
    if not isinstance(req.prompt, str):
        raise InputManagerError("INVALID_ARGUMENT", "prompt must be a string",
                                data={"field": "prompt", "expected": "string"})
    prompt = plain_text(req.prompt, PROMPT_MAX + 1)
    if len(prompt) > PROMPT_MAX:
        raise InputManagerError("INVALID_ARGUMENT", f"prompt exceeds {PROMPT_MAX} characters",
                                data={"field": "prompt", "limit": PROMPT_MAX, "actual": len(prompt)})
    if not prompt.strip():
        raise InputManagerError("INVALID_ARGUMENT", "prompt must not be empty",
                                data={"field": "prompt", "expected": "non-empty string"})
    detail = None
    if req.detail is not None:
        if not isinstance(req.detail, str):
            raise InputManagerError("INVALID_ARGUMENT", "detail must be a string or null",
                                    data={"field": "detail", "expected": "string"})
        detail = plain_text(req.detail, DETAIL_MAX) or None

    options = _normalize_options(kind, req.options)

    requested = req.timeout_s
    if requested is None:
        timeout_s = config.default_timeout_s
    elif isinstance(requested, bool) or not isinstance(requested, (int, float)):
        raise InputManagerError("INVALID_ARGUMENT", "timeout_s must be an integer",
                                data={"field": "timeout_s", "expected": "int"})
    else:
        # Out of range is clamped, not refused: the sender asked for "long", and
        # a client-visible error would only teach it to guess our bounds (§8.1).
        timeout_s = min(max(int(requested), config.min_timeout_s), config.max_timeout_s)

    option_ids = [option["id"] for option in options]
    max_length = FREE_TEXT_DEFAULT_MAX
    if req.max_length is not None and not isinstance(req.max_length, bool):
        try:
            max_length = int(req.max_length)
        except (TypeError, ValueError):
            max_length = FREE_TEXT_DEFAULT_MAX
    max_length = min(max(max_length, 1), config.max_free_text_chars)
    schema = schema_for(kind, option_ids, max_length=max_length)

    default = req.default
    if default is not None:
        try:
            default = validate_value(kind, default, schema, option_ids,
                                     max_free_text_chars=config.max_free_text_chars)
        except VadeError:
            # A wrong default is a UI hint we can drop; refusing the whole
            # question over it would deny the user a chance to answer at all.
            default = None

    source = dict(req.source or {})
    source.setdefault("tool", "ask_user")
    source.setdefault("module_id", None)
    depth = source.get("depth", 0)
    source["depth"] = int(depth) if isinstance(depth, (int, float)) and not isinstance(depth, bool) else 0
    return {
        "kind": kind, "prompt": prompt, "detail": detail, "options": tuple(options),
        "schema": schema, "timeout_s": timeout_s, "default": default, "source": source,
    }


def _normalize_options(kind: str, raw: Sequence[Any]) -> list[dict[str, Any]]:
    items = list(raw or ())
    if kind not in CHOICE_KINDS:
        if items:
            raise InputManagerError(
                "INVALID_ARGUMENT",
                f"{kind} takes no options",
                data={"field": "options", "expected": "empty array"},
            )
        return []
    if not (OPTIONS_MIN <= len(items) <= OPTIONS_MAX):
        raise InputManagerError(
            "INVALID_ARGUMENT",
            f"{kind} requires {OPTIONS_MIN}..{OPTIONS_MAX} options",
            data={"field": "options", "expected": f"{OPTIONS_MIN}..{OPTIONS_MAX}", "actual": len(items)},
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, InputOption):
            data = item.to_dict()
        elif isinstance(item, Mapping):
            data = dict(item)
        else:
            raise InputManagerError("INVALID_ARGUMENT", "each option must be an object",
                                    data={"field": "options", "expected": "object"})
        option_id = data.get("id")
        label = data.get("label")
        if not isinstance(option_id, str) or not option_id.strip():
            raise InputManagerError("INVALID_ARGUMENT", "option.id must be a non-empty string",
                                    data={"field": "options", "expected": "id:string"})
        option_id = option_id.strip()
        if len(option_id) > OPTION_ID_MAX:
            raise InputManagerError("INVALID_ARGUMENT", f"option.id exceeds {OPTION_ID_MAX} characters",
                                    data={"field": "options", "limit": OPTION_ID_MAX})
        if option_id in seen:
            raise InputManagerError("INVALID_ARGUMENT", f"duplicate option id {option_id!r}",
                                    data={"field": "options", "expected": "unique ids"})
        seen.add(option_id)
        if not isinstance(label, str) or not label.strip():
            raise InputManagerError("INVALID_ARGUMENT", "option.label must be a non-empty string",
                                    data={"field": "options", "expected": "label:string"})
        description = data.get("description")
        if description is not None and not isinstance(description, str):
            raise InputManagerError("INVALID_ARGUMENT", "option.description must be a string or null",
                                    data={"field": "options", "expected": "description:string|null"})
        out.append({
            "id": option_id,
            "label": plain_text(label, LABEL_MAX),
            "description": plain_text(description, OPTION_DESC_MAX) if description else None,
        })
    return out


# ---------------------------------------------------------------------- value
def validate_value(kind: str, value: Any, schema: Mapping[str, Any],
                   option_ids: Sequence[str], *, max_free_text_chars: int = FREE_TEXT_HARD_MAX
                   ) -> Any:
    """Closed validation of a submitted value (§4.2). Returns the value to store.

    Raises `INVALID_ARGUMENT` (shape/range) or `PAYLOAD_TOO_LARGE` (over the hard
    text ceiling) with `data.field == "value"`; the caller guarantees the turn
    state and the `input_id` are untouched when this raises.
    """
    if kind == InputKind.FREE_TEXT:
        if not isinstance(value, str):
            raise _invalid("free_text requires a string", expected="string")
        text = value
        if not text.strip():
            # Whitespace is not an answer: "   \n" would otherwise look submitted
            # and hand the model a blank.
            raise _invalid("free_text must not be blank", expected="non-empty string")
        limit = int(schema.get("max_length") or FREE_TEXT_DEFAULT_MAX)
        limit = min(limit, max_free_text_chars)
        if _code_points(text) > limit:
            raise InputManagerError(
                "PAYLOAD_TOO_LARGE",
                f"free_text exceeds {limit} characters",
                data={"field": "value", "limit": limit, "actual": _code_points(text)},
            )
        return text

    if kind == InputKind.SINGLE_CHOICE:
        if not isinstance(value, str):
            raise _invalid("single_choice requires a string option id", expected="string")
        if value not in option_ids:
            raise _invalid("value is not one of the offered options",
                           expected=list(option_ids))
        return value

    if kind == InputKind.MULTI_CHOICE:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise _invalid("multi_choice requires an array of option ids", expected="string[]")
        chosen = list(value)
        if any(not isinstance(item, str) for item in chosen):
            raise _invalid("every selected item must be a string option id", expected="string[]")
        if len(set(chosen)) != len(chosen):
            # De-duplicating here would hide a client rendering bug: the dialog
            # showed N boxes, the user ticked N, and two of them are the same id.
            raise _invalid("multi_choice must not repeat an option id", expected="unique ids")
        unknown = [item for item in chosen if item not in option_ids]
        if unknown:
            raise _invalid("value contains ids that were not offered", expected=list(option_ids))
        low = int(schema.get("min_items") or 1)
        high = int(schema.get("max_items") or len(option_ids))
        if not (low <= len(chosen) <= high):
            raise _invalid(f"multi_choice requires {low}..{high} selections",
                           expected={"min_items": low, "max_items": high}, actual=len(chosen))
        return chosen

    if kind == InputKind.CONFIRM:
        # `isinstance(1, bool)` is False but a bool passes `isinstance(x, int)`;
        # hence the strict check, and hence `"true"` is refused: the wire type is
        # a JSON boolean, not its string spelling (§4.2).
        if not isinstance(value, bool):
            raise _invalid("confirm requires a JSON boolean", expected="boolean")
        return value

    raise _invalid(f"unknown kind {kind!r}", expected=sorted(KINDS))
