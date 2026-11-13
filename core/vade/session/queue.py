"""Per-session serial turn queue with real cancellation.

Guarantees:
  * exactly one running turn per session — one worker task, not a semaphore
    guess;
  * FIFO by core arrival order (never a client timestamp);
  * `interrupt` cancels the in-flight task, tells the router to send
    `tools/cancel`, and releases the queue IMMEDIATELY — it does not wait for
    the module to acknowledge or even to return. A module that ignores cancel
    must not stall the next turn;
  * every terminal transition is persisted before its event is emitted, so a
    crash mid-turn still replays the real state.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import VadeError
from .models import Message, QueueItem, Turn, TurnState, new_id, now_ms

__all__ = ["TurnQueue"]

EmitFn = Callable[..., Awaitable[Any]]
SaveTurnFn = Callable[[str, Turn], None]
Executor = Callable[[Turn, tuple[dict[str, Any], ...]], Awaitable[Any]]


class TurnQueue:
    def __init__(
        self,
        session_id: str,
        *,
        executor: Executor,
        emit: EmitFn,
        save_turn: SaveTurnFn,
        cancel_hook: Callable[[str], None] | None = None,
        max_pending: int = 100,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.session_id = session_id
        self._executor = executor
        self._emit = emit
        self._save_turn = save_turn
        self._cancel_hook = cancel_hook
        self._max_pending = max_pending
        self._clock = clock
        self._pending: deque[QueueItem] = deque()
        self._running: QueueItem | None = None
        self._worker: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[Any] | None = None
        self._finish: asyncio.Event | None = None
        self._finalized: set[str] = set()
        self._orphans: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ state
    def enqueue(self, turn: Turn, content: Sequence[Mapping[str, Any]]) -> int:
        if len(self._pending) >= self._max_pending:
            raise VadeError("OVERLOADED", f"queue is full ({self._max_pending} pending turns)")
        item = QueueItem(turn=turn, content=tuple(dict(b) for b in content), enqueued_at=self._clock())
        self._pending.append(item)
        return len(self._pending)

    def position(self, turn_id: str) -> int:
        if self._running is not None and self._running.turn_id == turn_id:
            return 0
        for pos, item in enumerate(self._pending, start=1):
            if item.turn_id == turn_id:
                return pos
        return 0

    def state(self) -> dict[str, Any]:
        return {
            "running": self._running.turn_id if self._running else None,
            "running_state": self._running.turn.state.value if self._running else None,
            "pending": [
                {
                    "turn_id": item.turn_id,
                    "state": item.turn.state.value,
                    "client_msg_id": item.turn.client_msg_id,
                    "enqueued_at": item.enqueued_at,
                }
                for item in self._pending
            ],
        }

    def state_payload(self) -> dict[str, Any]:
        payload = self.state()
        payload["session_id"] = self.session_id
        return payload

    def is_idle(self) -> bool:
        return self._running is None and not self._pending and self._worker is None

    # ------------------------------------------------------------------ worker
    def ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_loop(), name=f"queue-{self.session_id[:8]}")

    async def _run_loop(self) -> None:
        try:
            while self._pending:
                item = self._pending.popleft()
                await self._run_one(item)
        finally:
            self._worker = None
            if self._pending:  # a submit raced the loop exit
                self.ensure_worker()

    async def _run_one(self, item: QueueItem) -> None:
        turn = item.turn.with_state(TurnState.RUNNING, started_at=self._clock())
        item = item.with_turn(turn)
        self._running = item
        self._save_turn(self.session_id, turn)
        await self._emit(self.session_id, "turn.started", {"turn_id": turn.id, "state": turn.state.value})
        await self._emit(self.session_id, "queue.updated", self.state_payload())

        finish = asyncio.Event()
        self._finish = finish
        task = asyncio.create_task(self._executor(turn, item.content), name=f"turn-{turn.id[:8]}")
        self._turn_task = task
        task.add_done_callback(lambda _t: finish.set())
        try:
            await finish.wait()
        finally:
            if self._finish is finish:
                self._finish = None

        if turn.id in self._finalized:
            return  # interrupt/cancel already wrote the terminal state
        if task.cancelled():
            await self._finalize(turn, TurnState.INTERRUPTED, {"turn_id": turn.id, "by": "external",
                                                               "state": TurnState.INTERRUPTED.value})
            return
        exc = task.exception()
        if exc is not None:
            code, message = _error_of(exc)
            await self._finalize(
                turn,
                TurnState.FAILED,
                {"turn_id": turn.id, "code": code, "message": message,
                 "state": TurnState.FAILED.value},
                error_code=code,
                error_message=message,
            )
            return

        outcome = task.result()
        usage = dict(getattr(outcome, "usage", {}) or {})
        assistant_text = str(getattr(outcome, "assistant_text", "") or "")
        tool_calls = tuple(getattr(outcome, "tool_calls", ()) or ())
        stopped_reason = getattr(outcome, "stopped_reason", None)
        updated = turn.with_state(
            TurnState.COMPLETED, ended_at=self._clock(), usage=usage,
            tool_calls=tool_calls, stopped_reason=stopped_reason,
        )
        messages = []
        if assistant_text:
            messages.append(
                Message(id=new_id(), session_id=self.session_id, turn_id=turn.id,
                        role="assistant", content=assistant_text)
            )
        await self._finalize(
            updated,
            TurnState.COMPLETED,
            {"turn_id": turn.id, "usage": usage, "state": TurnState.COMPLETED.value,
             "stopped_reason": stopped_reason},
            messages=messages,
        )

    async def _finalize(
        self,
        turn: Turn,
        state: TurnState,
        payload: Mapping[str, Any],
        *,
        messages: Sequence[Message] = (),
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if turn.id in self._finalized:
            return
        self._finalized.add(turn.id)
        if state is not TurnState.COMPLETED or turn.state is not TurnState.COMPLETED:
            turn = turn.with_state(state, ended_at=turn.ended_at or self._clock())
        self._save_turn(self.session_id, turn)
        event_type = {
            TurnState.COMPLETED: "turn.completed",
            TurnState.FAILED: "turn.failed",
            TurnState.INTERRUPTED: "turn.interrupted",
            TurnState.CANCELLED: "turn.cancelled",
        }[state]
        await self._emit(self.session_id, event_type, payload, messages=messages)
        if self._running is not None and self._running.turn_id == turn.id:
            self._running = None
        await self._emit(self.session_id, "queue.updated", self.state_payload())
        if self._finish is not None and not self._finish.is_set():
            self._finish.set()

    # ------------------------------------------------------------------ control
    async def interrupt(self, turn_id: str, *, by: str = "user") -> bool:
        item = self._running
        if item is None or item.turn_id != turn_id or turn_id in self._finalized:
            return False
        self._running = item.with_turn(item.turn.with_state(TurnState.INTERRUPTED))
        if self._cancel_hook is not None:
            try:
                self._cancel_hook(turn_id)  # tools/cancel; sync + non-blocking
            except Exception:  # pragma: no cover - cancel must never raise into UI
                pass
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
            task.add_done_callback(self._consume_orphan)
        # Release the queue NOW; do not await the in-flight module.
        await self._finalize(
            item.turn, TurnState.INTERRUPTED,
            {"turn_id": turn_id, "by": by, "state": TurnState.INTERRUPTED.value},
        )
        return True

    async def cancel(self, turn_id: str, *, by: str = "user") -> bool:
        for index, item in enumerate(self._pending):
            if item.turn_id == turn_id:
                del self._pending[index]
                turn = item.turn.with_state(TurnState.CANCELLED, ended_at=self._clock())
                self._finalized.add(turn_id)
                self._save_turn(self.session_id, turn)
                await self._emit(
                    self.session_id, "turn.cancelled",
                    {"turn_id": turn_id, "by": by, "state": TurnState.CANCELLED.value},
                )
                await self._emit(self.session_id, "queue.updated", self.state_payload())
                return True
        if self._running is not None and self._running.turn_id == turn_id:
            # Running turns are stopped by interrupt, never by queue.cancel.
            raise VadeError("INVALID_ARGUMENT", "turn is running; use ui/interrupt")
        return False

    async def mark_state(self, turn_id: str, state: TurnState) -> bool:
        item = self._running
        if item is None or item.turn_id != turn_id:
            return False
        turn = item.turn.with_state(state)
        self._running = item.with_turn(turn)
        self._save_turn(self.session_id, turn)
        return True

    async def drain(self) -> None:
        while True:
            worker = self._worker
            if worker is None:
                return
            await asyncio.shield(worker)

    async def shutdown(self) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        if self._worker is not None:
            self._worker.cancel()
        for task in list(self._orphans):
            task.cancel()

    def _consume_orphan(self, task: asyncio.Task[Any]) -> None:
        """A module that ignores tools/cancel leaves a orphan task: swallow it."""
        self._orphans.add(task)
        task.add_done_callback(self._orphans.discard)
        if not task.cancelled():
            task.exception()


def _error_of(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, VadeError):
        return exc.code, exc.message
    if isinstance(exc, asyncio.TimeoutError):
        return "TIMEOUT", "the turn timed out"
    # Never surface a traceback to the user (C-04: message <= 300 chars).
    return "INTERNAL", "internal error while executing the turn"
