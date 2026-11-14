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

__all__ = [
    "Message", "Role", "TurnState", "Turn", "Session", "QueueItem", "Event",
    "TERMINAL_STATES", "new_id", "now_ms",
]

Role = str  # "system" | "user" | "assistant" | "tool"


class TurnState(str, Enum):
    """Queue state machine (design §5, 22-C3 T2).

    `waiting_approval` only blocks the head of the queue; later messages keep
    queueing. Terminal states are never left again.
    """

    QUEUED = "queued"
    WAITING_APPROVAL = "waiting_approval"
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


@dataclass(frozen=True, slots=True)
class Message:
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

    def to_llm(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            payload["name"] = self.name
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [dict(c) for c in self.tool_calls]
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
    """One entry of the per-session stream. `seq` is allocated by the core."""

    session_id: str
    seq: int
    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "seq": self.seq, "type": self.type,
                "payload": dict(self.payload), "ts": self.ts}

    def to_wire(self) -> dict[str, Any]:
        """Event frame shape: `{type, seq, payload}` (session known per conn)."""
        return {"type": self.type, "seq": self.seq, "payload": dict(self.payload)}
