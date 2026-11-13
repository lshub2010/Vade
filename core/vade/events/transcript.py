"""Assemble a `session.turn` / `session.closed` payload from the session store.

Two rules drive everything here:

* **No second copy of the transcript.** Payloads are built by reading the rows
  C3 already persists (`turns`, `messages`) for one seq range. Storing a
  parallel evolve-side transcript is how the two copies drift.
* **Text stays split.** `user_text` and `assistant_text` are separate fields
  (AMEND-19). A consumer that receives `messages[]` has to guess which side it
  is reading, and guessing wrong once puts the model's own conclusion into
  memory as if the user had said it.

`session.closed` carries `tail` (last 10 turns, hard-truncated here) and never
the whole session and never a rolling summary: the consumer accumulates its own
summary in the pass that already touches every turn, so recomputing it in core
would pay for the same tokens twice.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "TERMINAL_TYPES", "TAIL_TURNS", "TAIL_TEXT_MAX", "TURN_TEXT_MAX",
    "digest_args", "turn_payload", "closed_payload", "redact_payload",
    "turn_state", "is_terminal",
]

#: A turn is "finished" for the evolve channel on any terminal state. `state`
#: travels in the payload, so M4b can tell a failure from a clean completion.
TERMINAL_TYPES: frozenset[str] = frozenset(
    {"turn.completed", "turn.failed", "turn.interrupted", "turn.cancelled"}
)

#: AMEND-19: exactly the last ten turns of original text, truncated by core.
TAIL_TURNS = 10
TAIL_TEXT_MAX = 2000
TURN_TEXT_MAX = 16000

_TRUNCATED = "\n[truncated by core]"

#: `[evolve] redact` names a class of content, not a key. `tool_args` is the
#: full argument object (a digest is already the redacted form, so hashing the
#: digest again would only destroy information), `file_content` is file bytes.
_REDACT_KEYS: Mapping[str, tuple[str, ...]] = {
    "tool_args": ("args", "arguments", "tool_args"),
    "file_content": ("content", "file_content", "file_contents", "diff", "patch"),
}


def digest_args(args: Any) -> str:
    """Same shape as `router.dispatch.digest_args`: `sha256:` + canonical JSON."""
    canonical = json.dumps(args if args is not None else {}, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + _TRUNCATED, True


def turn_state(turn: Any, fallback: str | None = None) -> str:
    state = getattr(turn, "state", None)
    value = getattr(state, "value", state)
    return str(value if value is not None else (fallback or "completed"))


def is_terminal(event_type: str) -> bool:
    return event_type in TERMINAL_TYPES


def _tool_calls(turn: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for call in getattr(turn, "tool_calls", ()) or ():
        if not isinstance(call, Mapping):
            continue
        tool = call.get("tool") or call.get("name") or ""
        digest = call.get("args_digest")
        if not digest:
            digest = digest_args(call.get("args", call.get("arguments")))
        ok = call.get("ok")
        out.append({
            "tool": str(tool),
            "args_digest": str(digest),
            "ok": None if ok is None else bool(ok),
        })
    return out


def _negative(turn: Any) -> bool:
    """A turn went badly. Derived when C3 has no explicit field.

    C-14 only promises the flag exists; C3's `Turn` does not store it yet, so a
    failed / interrupted turn (or one carrying an error code) is the honest
    proxy. A false `true` here only makes M4b more cautious, never less.
    """
    explicit = getattr(turn, "negative", None)
    if explicit is not None:
        return bool(explicit)
    state = turn_state(turn)
    return bool(getattr(turn, "error_code", None)) or state in ("failed", "interrupted")


def _self_corrected(turn: Any) -> bool:
    """C3 owns this judgement (`self_corrected`); we only pass it through.

    If the field is absent the answer is `false` -- never a guess. A wrong
    `true` would teach M4b that a correction trace is the recommended practice.
    """
    for holder in (turn, getattr(turn, "usage", None)):
        if isinstance(holder, Mapping):
            if "self_corrected" in holder:
                return bool(holder["self_corrected"])
        elif holder is not None and hasattr(holder, "self_corrected"):
            return bool(getattr(holder, "self_corrected"))
    return False


def _messages_of(session: Any, turn_id: str) -> list[Any]:
    return [m for m in (getattr(session, "messages", ()) or ())
            if getattr(m, "turn_id", None) == turn_id]


def _find_turn(session: Any, turn_id: str) -> Any:
    for turn in getattr(session, "turns", ()) or ():
        if getattr(turn, "id", None) == turn_id:
            return turn
    return None


def _assistant_text(messages: Sequence[Any]) -> str:
    text = ""
    for message in messages:
        if getattr(message, "role", "") == "assistant":
            text = str(getattr(message, "content", "") or "")
    return text


def _user_text(messages: Sequence[Any], turn: Any) -> str:
    stored = str(getattr(turn, "user_text", "") or "")
    if stored:
        return stored
    for message in messages:
        if getattr(message, "role", "") == "user":
            return str(getattr(message, "content", "") or "")
    return ""


def turn_payload(store: Any, session_id: str, turn_id: str, *, state: str | None = None) -> dict[str, Any] | None:
    """Build the `session.turn` payload, or `None` when the turn is not stored yet.

    `None` is not "drop it": the caller keeps the watermark where it is, so the
    next pump retries once the row lands. Silence here would be a silent hole.
    """
    session = store.load(session_id)
    if session is None:
        return None
    turn = _find_turn(session, turn_id)
    if turn is None:
        return None
    messages = _messages_of(session, turn_id)
    user_text, user_cut = _clip(_user_text(messages, turn), TURN_TEXT_MAX)
    assistant_text, assistant_cut = _clip(_assistant_text(messages), TURN_TEXT_MAX)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "turn_id": turn_id,
        "state": state or turn_state(turn),
        "user_text": user_text,
        "assistant_text": assistant_text,
        "tool_calls": _tool_calls(turn),
    }
    if user_cut or assistant_cut:
        payload["truncated"] = True
    return payload


def closed_payload(store: Any, session_id: str, *, tail: int = TAIL_TURNS,
                   closed_at: int | None = None) -> dict[str, Any] | None:
    """Build the `session.closed` payload: per-turn metadata plus the last `tail` texts."""
    session = store.load(session_id)
    if session is None:
        return None
    turns = list(getattr(session, "turns", ()) or ())
    rows: list[dict[str, Any]] = []
    tails: list[dict[str, Any]] = []
    for turn in turns:
        turn_id = str(getattr(turn, "id", ""))
        messages = _messages_of(session, turn_id)
        rows.append({
            "turn_id": turn_id,
            "state": turn_state(turn),
            "tool_calls": _tool_calls(turn),
            "negative": _negative(turn),
            "self_corrected": _self_corrected(turn),
        })
        user_text, _ = _clip(_user_text(messages, turn), TAIL_TEXT_MAX)
        assistant_text, _ = _clip(_assistant_text(messages), TAIL_TEXT_MAX)
        tails.append({"turn_id": turn_id, "user_text": user_text,
                      "assistant_text": assistant_text})
    return {
        "session_id": session_id,
        "turns_total": len(turns),
        "turns": rows,
        # The tail is the last N *turns*, not the last N rows of anything else:
        # a turn without an assistant message still belongs in the window.
        "tail": tails[-tail:] if tail > 0 else [],
        "closed_at": int(closed_at if closed_at is not None else 0),
    }


def _keys_for(field: str) -> tuple[str, ...]:
    return _REDACT_KEYS.get(field, (field,))


def _hash(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def redact_payload(payload: Mapping[str, Any], fields: Iterable[str]) -> tuple[dict[str, Any], bool]:
    """Replace redacted fields with `sha256(value)[:16]`; return `(payload, hit)`.

    A module cannot weaken this: `fields` comes from core config. When something
    was replaced the payload says so (`redacted: true`) instead of pretending it
    is complete -- a consumer must be able to tell "empty text" from "withheld".
    """
    wanted = {str(f) for f in fields or ()}
    if not wanted:
        return dict(payload), False
    keys = {key for field in wanted for key in _keys_for(field)}
    hit = False

    def walk(node: Any) -> Any:
        nonlocal hit
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key in keys and value not in (None, "", [], {}):
                    out[key] = _hash(value)
                    hit = True
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    redacted = walk(payload)
    if hit:
        redacted["redacted"] = True
    return redacted, hit
