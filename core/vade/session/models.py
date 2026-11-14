"""Immutable session/turn/message/queue records.

Everything here is a frozen dataclass: the session log swaps records instead of
mutating shared state, so a concurrent reader can never observe a half-updated
turn (design §4). `Turn` is stored as a row and re-read on recovery; `QueueItem`
is the in-memory scheduling view and is rebuilt from `turns` on startup.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from .parts import parts_text

__all__ = [
    "Message", "Role", "TurnState", "Turn", "Session", "QueueItem", "Event",
    "TERMINAL_STATES", "new_id", "now_ms", "message_ref",
]


Role = str  # "system" | "user" | "assistant" | "tool"


class TurnState(str, Enum):
    """Queue state machine (design §5, 22-C3 T2).

    `waiting_approval` and `waiting_input` only block the head of the queue;
    later messages keep queueing. They are mutually exclusive: one turn has at
    most one blocking wait (RFC-0008 S1/S2). Terminal states are never left again.
    """

    QUEUED = "queued"
    WAITING_APPROVAL = "waiting_approval"
    #: RFC-0008 §6.3. Appended right after `waiting_approval` so the schema arrays
    #: and this enum stay in the same order (C-06 2.0.0 widened the enum).
    WAITING_INPUT = "waiting_input"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {TurnState.COMPLETED, TurnState.FAILED, TurnState.INTERRUPTED, TurnState.CANCELLED}
)


def new_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


def message_ref(message: Message) -> str:
    """`msg:<hex>` pointer to a persisted message (RFC-0006 §4.3/§5.1).

    One convention for every pointer the core emits (`result_ref`,
    `partial_message_id`): the message row keeps its bare `uuid4().hex` id, the
    pointer carries the `msg:` scheme so a client can tell a pointer from an
    opaque id. `split(":", 1)[1]` is the snapshot key.
    """
    return f"msg:{message.id}"


@dataclass(frozen=True, slots=True)
class Message:
    """One stored message. `parts` is the message; `content` is its projection.

    RFC-0004 §6.2: `parts` holds the ordered content parts (text / image / file)
    and `content` stays the **text projection** every existing reader
    (`queue`, `llm`, compaction, search, audit) already consumes. An old row with
    no `parts` is a text-only message — `parts` stays empty, `to_dict()` synthesises
    the single text part on the way out, and nothing is rewritten on disk.

    Why `content` is still a field and not a property: `Message(content=...)` is
    the constructor every producer uses (including `core/vade/loop/`, which is
    frozen for this RFC) and `dataclasses.replace()` re-runs `__init__` with it.
    `__post_init__` derives it whenever `parts` is present, so the two can never
    disagree in a stored message — the projection is computed, not trusted.
    """

    id: str
    session_id: str
    role: Role
    content: str
    turn_id: str | None = None
    seq: int = 0
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    created_at: int = field(default_factory=now_ms)
    #: RFC-0006 §4: an assistant message written by `interrupt`, holding the
    #: text that had already been streamed. Never re-emitted as a delta.
    partial: bool = False
    #: RFC-0006 §6.3: the message that replaced this one (edit_resubmit). The
    #: row itself is immutable — only this pointer is ever written.
    superseded_by: str | None = None
    parts: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        raw = tuple(self.parts or ())
        if raw and not all(isinstance(p, dict) for p in raw):
            raw = tuple(dict(p) for p in raw)  # accept mappings, store plain dicts
        if raw != self.parts:
            object.__setattr__(self, "parts", raw)
        if raw:
            projected = parts_text(raw)
            if projected != self.content:
                object.__setattr__(self, "content", projected)

    def wire_parts(self) -> list[dict[str, Any]]:
        """§6.3: what C-05 consumers see. Legacy rows synthesise one text part."""
        if self.parts:
            return [dict(p) for p in self.parts]
        if self.content:
            return [{"type": "text", "text": self.content}]
        return []

    def to_llm(self, *, images: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Model-side rendering (§6.3).

        Text-only messages produce exactly the pre-parts payload — `{"role",
        "content": "<str>"}` — byte for byte, because that is what the golden
        vectors and every provider adapter already expect. Only a message with
        an image/file part switches `content` to a block array. `images` maps
        `file_id → data:image/...;base64,...`; without an entry the image
        degrades to its text placeholder instead of silently disappearing or
        blowing up a caller that has no blob reader (the loop).
        """
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            payload["name"] = self.name
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [dict(c) for c in self.tool_calls]
        if not self.parts or all(p.get("type") == "text" for p in self.parts):
            return payload

        blocks: list[dict[str, Any]] = []
        for part in self.parts:
            kind = part.get("type")
            if kind == "text":
                blocks.append({"type": "text", "text": str(part.get("text") or "")})
            elif kind == "image":
                url = (images or {}).get(str(part.get("file_id") or ""))
                if url:
                    blocks.append({"type": "image_url", "image_url": {"url": url, "detail": "auto"}})
                else:
                    blocks.append({"type": "text", "text": f"[image: {part.get('name')}]"})
            elif kind == "file":
                # v1 does not inline file bodies; extraction is a tool-family
                # concern (RFC-0004 §6.3), so the model gets a hint only.
                blocks.append(
                    {"type": "text", "text": f"[file: {part.get('name')} · {part.get('mime')}]"}
                )
        payload["content"] = blocks
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "seq": self.seq,
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "created_at": self.created_at,
            "partial": self.partial,
            "superseded_by": self.superseded_by,
            "parts": self.wire_parts(),
        }


@dataclass(frozen=True, slots=True)
class Turn:
    id: str
    session_id: str
    state: TurnState = TurnState.QUEUED
    client_msg_id: str | None = None
    user_text: str = ""
    started_at: int | None = None
    ended_at: int | None = None
    stopped_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    tool_calls: tuple[dict[str, Any], ...] = ()
    # --- RFC-0006 §6.1: linear version chain (all optional, old rows read as
    # defaults; `root_turn_id`/`version` coalesce to id/1 on read).
    root_turn_id: str | None = None
    parent_turn_id: str | None = None
    version: int = 1
    superseded_by: str | None = None
    superseded_reason: str | None = None
    user_message_id: str | None = None
    #: RFC-0006 §4: the `partial=true` assistant message written on interrupt.
    partial_message_id: str | None = None
    #: RFC-0006 §6.2: content part types (`image`, `file`, …) this turn was
    #: queued with, other than `text`. Only `text` is persisted as a message,
    #: so `regenerate` cannot rebuild the rest yet and must refuse instead of
    #: silently dropping an attachment. Empty for a text-only turn.
    user_parts: tuple[str, ...] = ()

    def with_state(self, state: TurnState, **changes: Any) -> "Turn":
        return replace(self, state=state, **changes)

    @property
    def root_id(self) -> str:
        """Coalesced root: a turn created before RFC-0006 is its own root."""
        return self.root_turn_id or self.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "state": self.state.value,
            "client_msg_id": self.client_msg_id,
            "user_text": self.user_text,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stopped_reason": self.stopped_reason,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "usage": dict(self.usage),
            "tool_calls": list(self.tool_calls),
            "root_turn_id": self.root_id,
            "parent_turn_id": self.parent_turn_id,
            "version": self.version or 1,
            "superseded_by": self.superseded_by,
            "superseded_reason": self.superseded_reason,
            "user_message_id": self.user_message_id,
            "partial_message_id": self.partial_message_id,
            "user_parts": list(self.user_parts),
        }


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    created_at: int
    title: str | None = None
    seq_counter: int = 0
    state: str = "open"
    messages: tuple[Message, ...] = ()
    turns: tuple[Turn, ...] = ()

    def message_tree(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.messages]


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One queued/running turn plus its original content blocks."""

    turn: Turn
    content: tuple[dict[str, Any], ...] = ()
    enqueued_at: int = field(default_factory=now_ms)
    interrupt_requested: bool = False

    @property
    def turn_id(self) -> str:
        return self.turn.id

    @property
    def session_id(self) -> str:
        return self.turn.session_id

    def with_turn(self, turn: Turn) -> "QueueItem":
        return replace(self, turn=turn)


@dataclass(frozen=True, slots=True)
class Event:
    """One entry of the per-session stream. `seq` is allocated by the core.

    `coalesce` marks a droppable, self-contained update (RFC-0007 §5.5 R5):
    `task.progress` is an absolute value, not a delta, so replay coverage may
    ignore it and a client must not read a gap made only of such events as a
    hole. It travels on the wire as `x-vade.coalesce` and is still written to
    the `events` table — this is a coverage question, not a retention one.
    """

    session_id: str
    seq: int
    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=now_ms)
    coalesce: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "seq": self.seq, "type": self.type,
                "payload": dict(self.payload), "ts": self.ts, "coalesce": self.coalesce}

    def to_wire(self) -> dict[str, Any]:
        """Event frame shape: `{type, seq, payload}` (session known per conn)."""
        frame: dict[str, Any] = {
            "type": self.type, "seq": self.seq, "payload": dict(self.payload),
        }
        if self.coalesce:
            frame["x-vade"] = {"coalesce": True}
        return frame
