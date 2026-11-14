"""Shared fixtures for the RFC-0007 task tests (no network, no LLM)."""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore
from vade.tasks.registry import TaskLimits, TaskRegistry

from ..session.helpers import CollectSink, GateExecutor, wait_until  # noqa: F401


def make_log(*, replay: ReplayBuffer | None = None, executor: Any = None) -> SessionLog:
    async def echo(turn, content):  # pragma: no cover - only used by cascade test
        from vade.loop.engine import TurnOutcome

        return TurnOutcome(assistant_text="ok")

    log = SessionLog(SQLiteStore(":memory:"), replay=replay)
    log.set_executor(executor or echo)
    return log


def make_registry(log: SessionLog, **overrides: Any) -> TaskRegistry:
    """Registry wired the way `install()` wires it: one attribute each way."""
    registry = TaskRegistry(log, limits=TaskLimits(**overrides))
    log.tasks = registry
    return registry


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


class FakeConnection:
    """Just enough of a C2 `Connection` for the `task/*` handlers."""

    def __init__(self, *, device_id: str = "dev-1", read_only: bool = False) -> None:
        self.device_id = device_id
        self.connection_id = "conn-1"
        self.read_only = read_only
