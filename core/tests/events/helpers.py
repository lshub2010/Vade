"""Fixtures for the C3d event tests: a real store, fake registry and socket."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from vade.events.deliver import EventBus
from vade.session.models import Event, Message, Session, Turn, TurnState, new_id, now_ms
from vade.session.sqlite_store import SQLiteStore

ALLOWED_FP = "ab" * 32


@dataclass
class FakeManifest:
    subscriptions: tuple[str, ...] = ()


@dataclass
class FakeRecord:
    module_id: str
    instance_id: str
    connection_id: str
    role: str = "evolve"
    manifest: FakeManifest = field(default_factory=FakeManifest)


class FakeRegistry:
    def __init__(self, records: Iterable[FakeRecord] = ()) -> None:
        self.records = list(records)

    def snapshot(self) -> list[FakeRecord]:
        return list(self.records)


class FakeConnection:
    """Only what the bus and the proposal service touch."""

    def __init__(self, connection_id: str, *, fingerprint: str = ALLOWED_FP,
                 module_id: str = "com.example.evolve", instance_id: str = "evo-1") -> None:
        self.connection_id = connection_id
        self.id = connection_id
        self.module_id = module_id
        self.instance_id = instance_id
        # The transport binds this from the verified key, never from anything
        # the peer sent (`transport/server.py::_bind_device_identity`).
        # `App.register_module` compares it against `module#instance` in the
        # manifest and fails closed when it is missing (review D1), so a fake
        # connection has to carry the same value a paired one would.
        self.device_id: str | None = f"{module_id}#{instance_id}"
        self.crypto = type("Crypto", (), {"remote_fingerprint": fingerprint})()
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, frame: Mapping[str, Any]) -> None:
        self.frames.append(dict(frame))

    def delivered(self) -> list[dict[str, Any]]:
        return [frame["params"] for frame in self.frames if frame.get("method") == "events/deliver"]


def make_store(tmp_path: Any) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "sessions.db")
    store.create_session(Session(id="s1", created_at=now_ms()))
    return store


def add_turn(
    store: SQLiteStore,
    *,
    session_id: str = "s1",
    user: str = "how do I roll back?",
    assistant: str = "use the rollback command",
    state: TurnState = TurnState.COMPLETED,
    terminal_type: str = "turn.completed",
    tool_calls: tuple[dict[str, Any], ...] = (),
    error_code: str | None = None,
    persist_turn: bool = True,
) -> dict[str, Any]:
    """Persist one finished turn the way C3 does: queued, then terminal + messages.

    `persist_turn=False` reproduces the real race in which the terminal event is
    committed before the `turns` row: the event exists, the payload cannot be
    built yet.
    """
    turn_id = new_id()
    queued_seq = store.next_seq(session_id)
    store.append(session_id, [], [Event(session_id=session_id, seq=queued_seq,
                                        type="turn.queued", payload={"turn_id": turn_id})])
    terminal_seq = store.next_seq(session_id)
    messages = [
        Message(id=new_id(), session_id=session_id, role="user", content=user, turn_id=turn_id),
        Message(id=new_id(), session_id=session_id, role="assistant", content=assistant,
                turn_id=turn_id),
    ]
    store.append(session_id, messages, [Event(
        session_id=session_id, seq=terminal_seq, type=terminal_type,
        payload={"turn_id": turn_id, "state": state.value, "usage": {}},
    )])
    turn = Turn(
        id=turn_id, session_id=session_id, state=state, user_text=user,
        tool_calls=tuple(tool_calls), usage={}, error_code=error_code,
        ended_at=now_ms(),
    )
    if persist_turn:
        store.update_turn(session_id, turn)
    return {"turn_id": turn_id, "seq_from": queued_seq, "seq_to": terminal_seq, "turn": turn}


def make_bus(
    store: SQLiteStore,
    *,
    records: Iterable[FakeRecord] = (),
    connections: Mapping[str, Any] | None = None,
    allow_transcript: bool = True,
    allowlist: Iterable[Any] | None = None,
    redact: Iterable[str] | None = (),
    capacity: int = 32,
) -> tuple[EventBus, FakeRegistry]:
    registry = FakeRegistry(records)
    bus = EventBus(
        store=store,
        registry=registry,
        connections=dict(connections or {}),
        allow_transcript=allow_transcript,
        allowlist=allowlist if allowlist is not None else [("com.example.evolve", ALLOWED_FP)],
        redact=redact or (),
        capacity=capacity,
        timeout_s=1.0,
    )
    return bus, registry


def pump(bus: EventBus) -> int:
    """Pump then push everything queued from a throwaway loop (no live loop here)."""
    emitted = bus.pump()
    asyncio.run(bus.drain())
    return emitted


def evolve_connection(connection_id: str = "c1", *, topics: tuple[str, ...] = ("session.turn", "session.closed"),
                      instance_id: str = "evo-1", role: str = "evolve",
                      fingerprint: str = ALLOWED_FP) -> tuple[FakeConnection, FakeRecord]:
    connection = FakeConnection(connection_id, fingerprint=fingerprint, instance_id=instance_id)
    record = FakeRecord(module_id="com.example.evolve", instance_id=instance_id,
                        connection_id=connection_id, role=role,
                        manifest=FakeManifest(subscriptions=topics))
    return connection, record
