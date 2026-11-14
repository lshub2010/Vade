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
from dataclasses import replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import VadeError
from .models import Message, QueueItem, Turn, TurnState, message_ref, new_id, now_ms

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
        cancel_clear_hook: Callable[[str], None] | None = None,
        max_pending: int = 100,
        clock: Callable[[], int] = now_ms,
        resolve_model: Callable[[Turn], tuple[str | None, str | None]] | None = None,
        pre_terminal_hook: Callable[[str, TurnState], Awaitable[Any]] | None = None,
        partial_provider: Callable[[str], str] | None = None,
    ) -> None:
        self.session_id = session_id
        self._executor = executor
        self._emit = emit
        self._save_turn = save_turn
        self._cancel_hook = cancel_hook
        self._cancel_clear_hook = cancel_clear_hook
        self._pre_terminal_hook = pre_terminal_hook
        self._max_pending = max_pending
        self._clock = clock
        #: RFC-0005 §5.3: resolved at dequeue (`QUEUED -> RUNNING`), never at
        #: submit, so a switch during the queue applies to the next turn and a
        #: running turn keeps the model it started with. `None` = no resolver
        #: installed (unit tests and an unwired core keep the old behaviour).
        self._resolve_model = resolve_model
        #: RFC-0006 §4.2: reads the text already streamed for a turn so an
        #: interrupt can persist it. A callable, not a back-reference to the
        #: engine, keeps the queue free of loop-level state.
        self._partial_provider = partial_provider
        self._pending: deque[QueueItem] = deque()
        self._running: QueueItem | None = None
        self._worker: asyncio.Task[None] | None = None
        #: Kept for `shutdown()`/introspection only: decisions are turn-scoped
        #: (`_turn_tasks`), never made off this mutable attribute.
        self._turn_task: asyncio.Task[Any] | None = None
        #: turn_id -> its executor task, so a clear is bound to the turn it
        #: belongs to (a shared attribute binds it to the previous turn's task).
        self._turn_tasks: dict[str, asyncio.Task[Any]] = {}
        #: Turns interrupted between `turn.started` and `create_task`: the flag
        #: must stay (the task does not exist yet) and is armed when it appears.
        self._release_pending: set[str] = set()
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

    def state_of(self, turn_id: str) -> str | None:
        """Live state of one turn (`running`, `waiting_input`, ...), or `None`.

        RFC-0008 S2/S3 needs this: entering `waiting_input` requires the turn to
        be `running`, and a turn already in `waiting_approval` must refuse a
        second blocking wait. The queue is the only honest source for it.
        """
        item = self._running
        if item is not None and item.turn_id == turn_id:
            return item.turn.state.value
        for queued in self._pending:
            if queued.turn_id == turn_id:
                return queued.turn.state.value
        return None

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
        """Run one turn, then drop the `_finalized` marker it needed.

        The marker only covers the window in which the turn's terminal state
        may still be written twice (worker vs. `interrupt`), i.e. exactly this
        coroutine's lifetime. Leaving it in the set grows one id per turn for
        the life of the process — the same leak class as the cancel flags.
        """
        try:
            await self._execute_one(item)
        finally:
            self._finalized.discard(item.turn_id)

    async def _execute_one(self, item: QueueItem) -> None:
        turn = item.turn.with_state(TurnState.RUNNING, started_at=self._clock())
        model_source: str | None = None
        if self._resolve_model is not None:
            try:
                model_id, model_source = self._resolve_model(turn)
            except VadeError as exc:
                # A dangling explicit selection must fail loudly, never quietly
                # fall back to another model (§5.2 row 1 / A11).
                item = item.with_turn(turn)
                self._running = item
                reason = "model_unavailable" if exc.code == "UNAVAILABLE" else None
                await self._finalize(
                    turn.with_state(TurnState.RUNNING, error_code=exc.code, error_message=exc.message),
                    TurnState.FAILED,
                    {"turn_id": turn.id, "code": exc.code, "message": exc.message,
                     "state": TurnState.FAILED.value},
                    error_code=exc.code,
                    error_message=exc.message,
                    usage=_unmeasured_usage(turn.model_id),
                    data={"reason": reason} if reason else None,
                )
                return
            turn = turn.with_state(TurnState.RUNNING, model_id=model_id)
        item = item.with_turn(turn)
        self._running = item
        self._save_turn(self.session_id, turn)
        started: dict[str, Any] = {"turn_id": turn.id, "state": turn.state.value}
        if turn.model_id is not None:
            started["model_id"] = turn.model_id
            started["model_source"] = model_source
        await self._emit(self.session_id, "turn.started", started)
        await self._emit(self.session_id, "queue.updated", self.state_payload())

        finish = asyncio.Event()
        self._finish = finish
        task = asyncio.create_task(self._executor(turn, item.content), name=f"turn-{turn.id[:8]}")
        self._turn_task = task
        self._turn_tasks[turn.id] = task
        task.add_done_callback(lambda _t: finish.set())
        if turn.id in self._release_pending:
            # `interrupt` landed in the pre-task window and deliberately kept
            # the flag; it exists now, so bind the clear to THIS task.
            self._release_pending.discard(turn.id)
            self._release_cancel(turn.id, task)
        try:
            await finish.wait()
        finally:
            if self._finish is finish:
                self._finish = None

        if turn.id in self._finalized:
            return  # interrupt/cancel already wrote the terminal state
        if task.cancelled():
            await self._finalize_interrupted(turn, by="external", task=task)
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
                task=task,
                usage=_unmeasured_usage(turn.model_id),
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
             "stopped_reason": stopped_reason,
             # RFC-0002 §2: skill NAMES only, `[]` when none — the key is always
             # present (never omitted, never null); a replayed old event without
             # it is read as [] by consumers.
             "skills": list(getattr(outcome, "activated_skills", ()) or ())},
            messages=messages,
            task=task,
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
        task: asyncio.Task[Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if turn.id in self._finalized:
            return
        self._finalized.add(turn.id)
        # RFC-0008 S5: a pending `turn.input_resolved` must be written BEFORE the
        # turn's terminal event, or the dialog is left open forever (C-06 forbids
        # any `turn.*` after `turn.interrupted`). This is the only place a turn
        # ends, so the hook lives here rather than at each caller.
        if self._pre_terminal_hook is not None:
            try:
                await self._pre_terminal_hook(turn.id, state)
            except Exception:  # pragma: no cover - a hook must not block finalization
                pass
        if state is not TurnState.COMPLETED or turn.state is not TurnState.COMPLETED:
            turn = turn.with_state(state, ended_at=turn.ended_at or self._clock())
        if usage is not None:
            # RFC-0005 §6.1: a failed/interrupted turn has burned tokens too.
            # When the provider never reported them the honest value is
            # `source="none"` with zero counts, not an estimate.
            turn = replace(turn, usage=dict(usage))
            payload = {**payload, "usage": dict(usage)}
        if data is not None:
            payload = {**payload, "data": dict(data)}
        self._save_turn(self.session_id, turn)
        event_type = {
            TurnState.COMPLETED: "turn.completed",
            TurnState.FAILED: "turn.failed",
            TurnState.INTERRUPTED: "turn.interrupted",
            TurnState.CANCELLED: "turn.cancelled",
        }[state]
        # RFC-0006 §4.2: the terminal turn row, the partial message and the
        # terminal event commit in ONE transaction. `_save_turn` + `_emit` was
        # two commits, and a crash between them left "state=interrupted" with no
        # partial text — a state no client can explain or repair.
        await self._emit(self.session_id, event_type, payload, messages=messages,
                         turns=(turn,))
        if self._running is not None and self._running.turn_id == turn.id:
            self._running = None
        await self._emit(self.session_id, "queue.updated", self.state_payload())
        if self._finish is not None and not self._finish.is_set():
            self._finish.set()
        self._release_cancel(turn.id, task)

    def _partial_message(self, turn_id: str) -> tuple[str, Message | None]:
        """The already-streamed text for `turn_id` and its `partial=true` row.

        Empty text means "interrupted before the first delta" (or during a tool
        step with no text): no message is created, so an interrupt can never
        leave an empty assistant bubble behind (RFC-0006 §4.2).
        """
        text = ""
        if self._partial_provider is not None:
            try:
                text = self._partial_provider(turn_id) or ""
            except Exception:  # pragma: no cover - a provider must never break a stop
                text = ""
        if not text:
            return "", None
        return text, Message(
            id=new_id(), session_id=self.session_id, turn_id=turn_id,
            role="assistant", content=text, partial=True,
        )

    async def _finalize_interrupted(self, turn: Turn, *, by: str,
                                    task: asyncio.Task[Any] | None = None) -> None:
        text, message = self._partial_message(turn.id)
        # RFC-0006 §4.3 + schema/events.json: `partial_message_id` is a
        # `msg:<hex>` pointer, the same scheme as `result_ref` — a bare hex id
        # made clients that split on ':' fail.
        pointer = message_ref(message) if message is not None else None
        payload: dict[str, Any] = {
            "turn_id": turn.id, "by": by, "state": TurnState.INTERRUPTED.value,
            # RFC-0006 §4.3: the event carries a pointer, never the text.
            "partial_message_id": pointer,
            "partial_chars": len(text),
        }
        if message is not None:
            turn = turn.with_state(TurnState.INTERRUPTED, partial_message_id=pointer)
        await self._finalize(
            turn, TurnState.INTERRUPTED, payload,
            messages=[message] if message is not None else (), task=task,
        )

    # ------------------------------------------------------------------ cancel flag
    def _release_cancel(self, turn_id: str, task: asyncio.Task[Any] | None) -> None:
        """Let the router forget `turn_id` once nothing can consult the flag.

        The flag must outlive the terminal event: `interrupt` returns before
        the module (and therefore the turn task) is done, and the loop reads
        `was_cancelled(turn_id)` when the late reply lands to refuse writing
        into a closed turn.

        The task is a PARAMETER, never `self._turn_task`: `interrupt` can land
        while another turn's task is the current one (the pre-task window), and
        a clear bound to that task either fires at the wrong time or never —
        the flag then outlives its own turn. Three cases:
          * the turn's executor is still running: clear when it is done;
          * it is already done: clear now;
          * it does not exist yet (`interrupt` landed between `turn.started`
            and `create_task`): keep the flag and arm the clear when
            `_execute_one` creates the task — clearing now would hand the loop
            a turn whose `was_cancelled` is already False.
        """
        if task is None:
            self._release_pending.add(turn_id)
            return
        if task.done():
            self._forget_turn(turn_id)
            return
        task.add_done_callback(lambda _t, tid=turn_id: self._forget_turn(tid))

    def _forget_turn(self, turn_id: str) -> None:
        """Drop every per-turn remnant once nothing can read it any more."""
        self._turn_tasks.pop(turn_id, None)
        self._release_pending.discard(turn_id)
        self._clear_cancel(turn_id)

    def _clear_cancel(self, turn_id: str) -> None:
        if self._cancel_clear_hook is None:
            return
        try:
            self._cancel_clear_hook(turn_id)
        except Exception:  # pragma: no cover - a broken hook must not break the queue
            pass

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
        task = self._turn_tasks.get(turn_id)
        if task is not None and not task.done():
            task.cancel()
            task.add_done_callback(self._consume_orphan)
        # Release the queue NOW; do not await the in-flight module.
        await self._finalize_interrupted(item.turn, by=by, task=task)
        return True

    async def cancel(self, turn_id: str, *, by: str = "user") -> bool:
        for index, item in enumerate(self._pending):
            if item.turn_id == turn_id:
                del self._pending[index]
                turn = item.turn.with_state(TurnState.CANCELLED, ended_at=self._clock())
                self._finalized.add(turn_id)
                await self._emit(
                    self.session_id, "turn.cancelled",
                    {"turn_id": turn_id, "by": by, "state": TurnState.CANCELLED.value},
                    turns=(turn,),
                )
                await self._emit(self.session_id, "queue.updated", self.state_payload())
                # A queued turn never reached the router, so there is no
                # in-flight task that could still read the flag.
                self._forget_turn(turn_id)
                # It also never reaches `_run_one`, which is what drops the
                # `_finalized` marker: drop it here so the set stays bounded.
                self._finalized.discard(turn_id)
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
        for task in list(self._turn_tasks.values()):
            if not task.done():
                task.cancel()
        self._release_pending.clear()
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


def _unmeasured_usage(model_id: str | None) -> dict[str, Any]:
    """The usage of a turn whose provider never reported any (§6.1).

    Zero counts with `source="none"` is the only honest answer: core does not
    estimate tokens (N4/D102), and inventing a number would break the per-turn
    reconciliation against the audit trail.
    """
    provider = model_id.split("/", 1)[0] if model_id else None
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "model_id": model_id, "provider": provider, "source": "none",
            "steps": 0, "cached_tokens": 0, "reasoning_tokens": 0}


def _error_of(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, VadeError):
        return exc.code, exc.message
    if isinstance(exc, asyncio.TimeoutError):
        return "TIMEOUT", "the turn timed out"
    # Never surface a traceback to the user (C-04: message <= 300 chars).
    return "INTERNAL", "internal error while executing the turn"
