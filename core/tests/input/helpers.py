"""Shared harness for RFC-0008: a real SessionLog + real queue + InputManager.

Nothing here fakes the event stream. The tests assert on the events the core
actually persisted (`log.store.read_since`) and on the turn states the queue
actually holds, because both bugs this RFC guards against — a dialog that never
closes and a state that is written but never announced — are invisible to a
mock.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from vade.input import InputConfig, InputManager, InputRequest
from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog
from vade.session.models import TurnState
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore


class AskExecutor:
    """Turn executor that asks one queued question, then finishes the turn."""

    def __init__(self) -> None:
        self.log: Any = None
        self.manager: Any = None
        self.questions: list[dict[str, Any]] = []
        self.outcomes: list[Any] = []
        self.error: BaseException | None = None
        self.delta_after: str | None = None
        self.asked = asyncio.Event()

    def ask(self, **kwargs: Any) -> None:
        self.questions.append(kwargs)

    async def __call__(self, turn: Any, content: Any) -> Any:
        if not self.questions:
            return TurnOutcome(assistant_text="no question")
        kwargs = self.questions.pop(0)
        request = InputRequest(
            session_id=turn.session_id, turn_id=turn.id,
            emit=lambda event_type, payload: self.log.emit(turn.session_id, event_type, payload),
            **kwargs,
        )
        self.asked.set()
        try:
            outcome = await self.manager.request(request)
        except BaseException as exc:  # noqa: BLE001 - the test asserts on it
            self.error = exc
            raise
        self.outcomes.append(outcome)
        if self.delta_after:
            await self.log.emit(turn.session_id, "turn.delta",
                                {"turn_id": turn.id, "text": self.delta_after})
        return TurnOutcome(assistant_text="done")


class HoldExecutor:
    """Turn executor that holds the turn open until the test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, turn: Any, content: Any) -> Any:
        self.started.set()
        await self.release.wait()
        return TurnOutcome(assistant_text="held")


def build(tmp_path, *, ui: int = 1, config: InputConfig | None = None,
          audit: Any = None, executor: Any = None):
    """Wire a SessionLog and an InputManager exactly as `install()` does."""
    executor = executor or AskExecutor()
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    manager = InputManager(
        config=config or InputConfig(min_timeout_s=1, default_timeout_s=5, max_timeout_s=5),
        audit=audit,
    )
    manager.set_ui_probe(lambda session_id: ui)
    manager.set_state_probe(log.turn_state)
    manager.set_state_hook(lambda sid, tid, state: log.mark_state(sid, tid, TurnState(state)))
    log.input_port = manager
    executor.log = log
    executor.manager = manager
    return log, manager, executor


async def start_turn(log: SessionLog, session_id: str, text: str = "go") -> Any:
    turn, _pos = await log.submit(
        session_id, [{"type": "text", "text": text}], f"c-{uuid.uuid4().hex}"
    )
    return turn


async def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return bool(predicate())


async def wait_pending(manager: InputManager, count: int = 1) -> str:
    ok = await wait_until(lambda: manager.pending_count >= count)
    assert ok, f"expected {count} pending input(s), saw {manager.pending_count}"
    rows = manager.pending_requests()
    return rows[-1]["input_id"]


def events(log: SessionLog, session_id: str) -> list[Any]:
    return list(log.store.read_since(session_id, 0))


def types(log: SessionLog, session_id: str) -> list[str]:
    return [event.type for event in events(log, session_id)]


def of_type(log: SessionLog, session_id: str, event_type: str) -> list[Any]:
    return [event for event in events(log, session_id) if event.type == event_type]
