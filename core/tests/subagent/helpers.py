"""Shared fixtures for the RFC-0012 subagent tests (no network, no LLM)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore
from vade.subagents.manager import SubagentManager

from ..session.helpers import CollectSink, GateExecutor, wait_until  # noqa: F401


def store_of(log: SessionLog) -> SQLiteStore:
    """The concrete store behind the log (tests only ever pass a SQLiteStore)."""
    return log.store  # type: ignore[return-value]


def make_log(*, path: str | Path = ":memory:", executor: Any = None) -> SessionLog:
    async def echo(turn, content):
        from vade.loop.engine import TurnOutcome

        return TurnOutcome(assistant_text=f"echo:{turn.user_text}", stopped_reason="stop")

    log = SessionLog(SQLiteStore(path), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor or echo)
    return log


def make_manager(log: SessionLog) -> SubagentManager:
    """Manager wired the way `install()` wires it: one attribute each way."""
    manager = SubagentManager(log, store=log.store)
    log.subagents = manager
    return manager


async def settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def attach(log: SessionLog, session_id: str) -> CollectSink:
    sink = CollectSink()
    await log.attach(session_id, None, sink)
    await settle()
    return sink


def events(sink: CollectSink, event_type: str) -> list[dict[str, Any]]:
    return [event for event in sink.events if event.get("type") == event_type]


def payloads(sink: CollectSink, event_type: str) -> list[dict[str, Any]]:
    return [dict(event["payload"]) for event in events(sink, event_type)]


def states(sink: CollectSink, event_type: str) -> list[str]:
    return [str(payload.get("state")) for payload in payloads(sink, event_type)]


def stream(sink: CollectSink) -> list[tuple[str, int]]:
    """The (type, seq) order the client saw — the ordering assertions ride it."""
    return [(str(event.get("type")), int(event.get("seq", 0))) for event in sink.events]


class FakeConnection:
    """Just enough of a C2 `Connection` for the `subagent/*` handlers."""

    def __init__(self, *, device_id: str = "dev-1", read_only: bool = False) -> None:
        self.device_id = device_id
        self.connection_id = "conn-1"
        self.read_only = read_only
