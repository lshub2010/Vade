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
import logging
from collections import OrderedDict, deque
from dataclasses import replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import VadeError
from .models import Message, QueueItem, Turn, TurnState, message_ref, new_id, now_ms

_log = logging.getLogger(__name__)

__all__ = ["TurnQueue"]

EmitFn = Callable[..., Awaitable[Any]]
SaveTurnFn = Callable[[str, Turn], None]
Executor = Callable[[Turn, tuple[dict[str, Any], ...]], Awaitable[Any]]

#: Terminal turns remembered so a late finisher (the worker waking up after an
#: interrupt already wrote the terminal event) cannot write a second one. Bounded
#: exactly like `SessionLog._TOMBSTONE_LIMIT`: one id per turn for the life of
#: the process is a leak, and a turn whose id fell out of the window is one that
#: can no longer be reached by any caller.
FINALIZED_LIMIT = 1024


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
        pre_terminal_hook: Callable[[str, TurnState, "str | None"], Awaitable[Any]] | None = None,
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
        #: Slots reserved by `claim()` for a turn that is still being written
        #: (the announcement emit). Counted against `max_pending` so a claim is
        #: a real capacity decision, not a hint.
        self._claims: dict[int, bool] = {}
        self._claim_seq = 0
        self._running: QueueItem | None = None
        self._worker: asyncio.Task[None] | None = None
        #: True from the first line of a `_run_loop` body to its `finally`.
        #: `_worker` alone cannot carry "one worker" (`shutdown()` clears the
        #: reference while the body can still be unwinding) and a second body
        #: would dequeue from the same `_pending`, running two turns of one
        #: session at once — the single guarantee this class exists to make.
        self._worker_live = False
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
        #: Turns whose terminal event is already written (bounded FIFO). The
        #: marker OUTLIVES `_run_one`: the worker wakes up after an interrupt
        #: has finalized the turn, and without the marker it would take the
        #: `task.cancelled()` branch and write a SECOND terminal event.
        self._finalized: OrderedDict[str, None] = OrderedDict()
        #: Terminal writes in flight. Held only for the duration of `_finalize`
        #: so a concurrent interrupt/finish cannot interleave two writes; the
        #: pair (`_finalizing`, `_finalized`) is what makes the "exactly one
        #: terminal event" promise independent of who calls `_finalize` first.
        self._finalizing: set[str] = set()
        self._orphans: set[asyncio.Task[Any]] = set()
        #: Set by `shutdown()`; makes the stop terminal (no new worker, no new
        #: turn) and `shutdown()` itself idempotent.
        self._stopped = False

    # ------------------------------------------------------------------ state
    @property
    def max_pending(self) -> int:
        return self._max_pending

    def has_capacity(self) -> bool:
        """True when one more turn can be enqueued without `OVERLOADED`."""
        return len(self._pending) + len(self._claims) < self._max_pending

    def claim(self) -> int | None:
        """Reserve one pending slot for a turn that does not exist yet, or `None`.

        A caller checks capacity and then does async work (an `emit` that
        announces the turn) before it can build the `QueueItem`; a bare
        `has_capacity()` sample would be stale by the time `enqueue` runs if
        that work ever yielded. The claim closes that gap by taking the slot at
        decision time: `enqueue(..., claim=...)` consumes it without re-checking
        capacity, so an announced turn can never be refused afterwards (a
        `queued` row that is never dequeued is invisible to every client and
        blocks the version chain with `turn_active`).

        Returns an opaque token for `release()`/`enqueue()`. The caller OWNS it
        until one of those two calls: dropping it without either loses the slot
        for the life of the queue.
        """
        if not self.has_capacity():
            return None
        self._claim_seq += 1
        self._claims[self._claim_seq] = True
        return self._claim_seq

    def release(self, claim: int) -> bool:
        """Give back a claim whose turn never reached `enqueue`.

        Returns whether the claim was still outstanding, so a release on a
        consumed (or foreign) token is a no-op instead of silently freeing
        another caller's slot.
        """
        return self._claims.pop(claim, None) is not None

    def is_finalized(self, turn_id: str) -> bool:
        return turn_id in self._finalized or turn_id in self._finalizing

    def _remember_finalized(self, turn_id: str) -> None:
        self._finalized[turn_id] = None
        self._finalized.move_to_end(turn_id)
        while len(self._finalized) > FINALIZED_LIMIT:
            self._finalized.popitem(last=False)

    def enqueue(self, turn: Turn, content: Sequence[Mapping[str, Any]], *,
                claim: int | None = None) -> int:
        """Insert `turn`; `claim` is the slot `claim()` already reserved for it.

        With a claim the capacity decision was already made (and taken), so this
        must NOT check it again — a second check is the check-then-insert race
        the claim exists to remove. Without one the queue defends itself, which
        is the path direct callers (tests, future wiring) use.
        """
        if claim is not None:
            if not self.release(claim):
                raise VadeError(
                    "INTERNAL",
                    "enqueue() with a claim that was already consumed or never taken",
                )
        elif len(self._pending) + len(self._claims) >= self._max_pending:
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
        """Nothing running, nothing waiting, and no un-consumed slot claim."""
        return (
            self._running is None
            and not self._pending
            and self._worker is None
            and not self._claims
        )

    # ------------------------------------------------------------------ worker
    def ensure_worker(self) -> None:
        # A queue that has been shut down never starts another worker: the
        # `finally` in `_run_loop` used to respawn one when `_pending` was not
        # empty, so `close()` cancelled the worker and the very next tick
        # created a new one that ran turns into a closed store.
        if self._stopped:
            return
        if self._worker_live:
            # A loop body is already serving this queue, whatever `_worker`
            # says. Creating another one here is the only way two workers can
            # ever exist for one session, and it would run two turns at once.
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_loop(), name=f"queue-{self.session_id[:8]}")

    async def _run_loop(self) -> None:
        if self._worker_live:  # pragma: no cover - guarded by `ensure_worker`
            _log.error("session %s: refused a second queue worker", self.session_id)
            return
        self._worker_live = True
        try:
            # `_stopped` in the condition: `shutdown()` empties `_pending` and
            # finalizes each turn, but a worker resumed in between must never
            # dequeue one and announce `turn.started` after its terminal event.
            while self._pending and not self._stopped:
                item = self._pending.popleft()
                try:
                    await self._run_one(item)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A turn must never take the worker with it. The exception
                    # used to escape `_run_loop`: the worker task died with
                    # nobody holding it ("Task exception was never retrieved"),
                    # `_running` stayed set (so the session reported a live turn
                    # forever and `session/delete` refused with `turn_active`),
                    # and the `finally` below started a second worker for the
                    # same session. The terminal state is still owed, so write
                    # the fallback here and keep serving the queue.
                    _log.exception(
                        "session %s: turn %s crashed while finishing",
                        self.session_id, item.turn_id,
                    )
                    await self._fail_crashed_turn(item)
        finally:
            self._worker_live = False
            self._worker = None
            if self._pending and not self._stopped:  # a submit raced the loop exit
                self.ensure_worker()

    async def _fail_crashed_turn(self, item: QueueItem) -> None:
        """Last-ditch `turn.failed{INTERNAL}` for a turn whose finalize raised.

        A retry, not a second write: `_finalize` is the only terminal writer and
        it refuses a turn that already finalized, so a turn whose terminal row
        and event did land is left untouched. What is left here is a turn that
        has no durable terminal state — it must get one, or the row stays
        `running` for the life of the process (a client sees a turn that never
        ends and `session/delete` answers `turn_active` forever).

        If even this write fails the queue stops pretending: `_running` and the
        in-flight markers are cleared so the session is not permanently active,
        and the row is left for `reconcile_unfinished_turns` on the next start.
        Nothing is raised — this runs inside the worker, and a raise here would
        recreate the dead-worker bug it exists to prevent.
        """
        running = self._running
        turn = (running.turn
                if running is not None and running.turn_id == item.turn_id
                else item.turn)
        message = "internal error while executing the turn"
        try:
            await self._finalize(
                turn,
                TurnState.FAILED,
                {"turn_id": turn.id, "code": "INTERNAL", "message": message,
                 "state": TurnState.FAILED.value},
                error_code="INTERNAL",
                error_message=message,
                usage=_unmeasured_usage(turn.model_id),
                data={"reason": "internal"},
            )
        except Exception:
            _log.exception(
                "session %s: could not persist the terminal state of turn %s",
                self.session_id, turn.id,
            )
            self._finalizing.discard(turn.id)
            if self._running is not None and self._running.turn_id == turn.id:
                self._running = None
            if self._finish is not None and not self._finish.is_set():
                self._finish.set()
            self._release_cancel(turn.id, self._turn_tasks.get(turn.id))

    async def _run_one(self, item: QueueItem) -> None:
        """Run one turn to completion.

        The `_finalized` marker is NOT released here: `_run_one` ends when the
        worker is done, which is strictly earlier than "no caller can still try
        to finalize this turn" — an `interrupt` can have written the terminal
        event and the worker then wakes up on a cancelled task. Releasing the
        marker at worker exit made that wake-up write a second terminal event.
        `_finalize` owns the release (it is the only writer), and it keeps the
        id in the bounded `_finalized` FIFO so late finishers stay refused.
        """
        await self._execute_one(item)

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
                usage=_failed_usage(exc, turn),
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
        reason: str | None = None,
    ) -> None:
        if self.is_finalized(turn.id):
            return
        # The in-flight marker is taken BEFORE the first await so a second
        # caller (a second interrupt, or the worker waking up) is refused while
        # this write is suspended. Ownership of the released marker lives here,
        # not in `_run_one`: this method is the only writer of a terminal state.
        self._finalizing.add(turn.id)
        #: `persisted` is the honest answer to "did the terminal state land?".
        #: It gates the `_finalized` tombstone AND the local completion marks:
        #: `store.append` commits the row, the messages and the event in ONE
        #: transaction, so a write that raised wrote nothing and the turn is
        #: still unfinished. The old `finally` recorded the tombstone
        #: unconditionally, which turned a failed write into "already done" —
        #: the worker woke up, saw the marker and returned, `_running` stayed
        #: set (a session that is `turn_active` forever) and the client watched
        #: a turn run until the next process start.
        persisted = False
        try:
            # RFC-0008 S5: a pending `turn.input_resolved` must be written BEFORE the
            # turn's terminal event, or the dialog is left open forever (C-06 forbids
            # any `turn.*` after `turn.interrupted`). This is the only place a turn
            # ends, so the hook lives here rather than at each caller. `reason`
            # tells the hook *why* the turn is ending (`shutdown` closes dialogs
            # as `session_closed`, per RFC-0008's close-order table).
            if self._pre_terminal_hook is not None:
                try:
                    await self._pre_terminal_hook(turn.id, state, reason)
                except Exception:  # a hook must not block finalization…
                    # …but never silently: a signature drift here once disabled
                    # every approval close on interrupt (caught only by tests).
                    _log.warning("pre-terminal hook failed for turn %s", turn.id,
                                 exc_info=True)
            if state is not TurnState.COMPLETED or turn.state is not TurnState.COMPLETED:
                turn = turn.with_state(state, ended_at=turn.ended_at or self._clock())
            if error_code is not None or error_message is not None:
                # RFC-0005 §6.1/C-04: the failure has to survive on the ROW, not
                # only in the `turn.failed` payload. `turns.error_code` /
                # `error_message` are what a client reads back after the event
                # is gone — from `ui/attach`'s snapshot and from `session/get`
                # on a restarted core — and they used to stay NULL because the
                # parameters were accepted and never applied.
                turn = replace(
                    turn,
                    error_code=error_code if error_code is not None else turn.error_code,
                    error_message=(error_message if error_message is not None
                                   else turn.error_message),
                )
            if usage is not None:
                # RFC-0005 §6.1: a failed/interrupted turn has burned tokens too.
                # When the provider never reported them the honest value is
                # `source="none"` with zero counts, not an estimate.
                turn = replace(turn, usage=dict(usage))
                payload = {**payload, "usage": dict(usage)}
            if data is not None:
                payload = {**payload, "data": dict(data)}
            event_type = {
                TurnState.COMPLETED: "turn.completed",
                TurnState.FAILED: "turn.failed",
                TurnState.INTERRUPTED: "turn.interrupted",
                TurnState.CANCELLED: "turn.cancelled",
            }[state]
            # RFC-0006 §4.2: the terminal turn row, the partial message and the
            # terminal event commit in ONE transaction. `_save_turn` + `_emit` was
            # two commits, and a crash between them left "state=interrupted" with no
            # partial text — a state no client can explain or repair. The turn row
            # rides `turns=` so there is exactly one write, not a duplicate
            # `update_turn` followed by the event's own upsert.
            await self._emit(self.session_id, event_type, payload, messages=messages,
                             turns=(turn,))
            # Durable. Only now may the turn be remembered as finalized — see
            # the `persisted` note above — and only now is it safe to forget it.
            persisted = True
            if self._running is not None and self._running.turn_id == turn.id:
                self._running = None
            try:
                await self._emit(self.session_id, "queue.updated", self.state_payload())
            except Exception:
                # The terminal row and event are already committed; this is a
                # derived snapshot the next event supersedes. Its failure must
                # not roll the turn back, re-finalize it, or tell the caller the
                # turn did not end.
                _log.warning("session %s: queue.updated after terminal write failed",
                             self.session_id, exc_info=True)
        finally:
            # Rollback: whatever happened, no terminal write is in flight any
            # more, so a retry must be allowed. `_running` is only cleared on the
            # success path above — on failure the turn is still live (the worker
            # or `_fail_crashed_turn` writes the fallback terminal state), and a
            # failure that cleared it would report the turn as finished while
            # the row still says `running`.
            self._finalizing.discard(turn.id)
            if persisted:
                self._remember_finalized(turn.id)
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
                                    task: asyncio.Task[Any] | None = None,
                                    reason: str | None = None) -> None:
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
        if reason is not None:
            # `data.reason` is the machine answer to "who stopped this turn":
            # `reconcile_unfinished_turns` already writes `restart` this way, so
            # a shutdown writes `shutdown` and a client can tell the two apart.
            payload["data"] = {"reason": reason}
            turn = replace(turn, stopped_reason=reason)
        if message is not None:
            turn = turn.with_state(TurnState.INTERRUPTED, partial_message_id=pointer)
        await self._finalize(
            turn, TurnState.INTERRUPTED, payload,
            messages=[message] if message is not None else (), task=task,
            reason=reason,
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
        if item is None or item.turn_id != turn_id or self.is_finalized(turn_id):
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
                await self._emit(
                    self.session_id, "turn.cancelled",
                    {"turn_id": turn_id, "by": by, "state": TurnState.CANCELLED.value},
                    turns=(turn,),
                )
                await self._emit(self.session_id, "queue.updated", self.state_payload())
                # A queued turn never reached the router, so there is no
                # in-flight task that could still read the flag.
                self._forget_turn(turn_id)
                # It also never reaches `_finalize`, which is what records the
                # marker: record it here so a late `interrupt` for the same id
                # stays a no-op. Bounded (FIFO), so the set cannot grow one id
                # per cancelled turn for the life of the process.
                self._remember_finalized(turn_id)
                return True
        if self._running is not None and self._running.turn_id == turn_id:
            # Running turns are stopped by interrupt, never by queue.cancel.
            raise VadeError("INVALID_ARGUMENT", "turn is running; use ui/interrupt")
        return False

    async def mark_state(self, turn_id: str, state: TurnState | str) -> bool:
        """Move the running turn between blocking states (`waiting_approval`, …).

        Emits `queue.updated` so the state is *observable*: `running_state` is
        the only contract field that carries it (there is no
        `turn.waiting_approval` event), and without the emit a client could
        never render the approval wait — the state was reachable in the store
        and invisible on the wire.
        """
        state = state if isinstance(state, TurnState) else TurnState(state)
        item = self._running
        if item is None or item.turn_id != turn_id:
            return False
        if self.is_finalized(turn_id):
            # A dialog answering/resolving after the turn was interrupted must
            # not resurrect a terminal row as `running`.
            return False
        if item.turn.state is state:
            return False  # idempotent: no event per re-check
        turn = item.turn.with_state(state)
        self._running = item.with_turn(turn)
        await self._emit(self.session_id, "queue.updated", self.state_payload(), turns=(turn,))
        return True

    async def drain(self) -> None:
        while True:
            worker = self._worker
            if worker is None:
                return
            await asyncio.shield(worker)

    async def shutdown(self) -> None:
        """Terminate every turn this queue owns, THEN stop every task.

        Order is contractual. A turn is given its terminal `turn.interrupted`
        while the store, the sinks and the seq bus are still up — `close()`
        calls this before `session.closed`, before `detach()` and before the
        store closes — and only then is its task cancelled. Cancelling first
        (the old order) killed the turn before `_execute_one` could resume, so
        a client that had already received `session.closed` waited forever for
        a terminal `turn.*` that never came. `_finalize` is the only terminal
        writer, so "exactly one" holds even when an `interrupt` — or the
        worker waking up on the cancelled task — races this method.

        The worker is awaited (as a cancelled task) so the caller returns with
        the queue actually stopped: without the join the worker was free to
        wake up after `store.close()` and write one more terminal event into a
        closed database. The turn tasks are cancelled but NOT awaited: a module
        that ignores `tools/cancel` would hold up process shutdown, and
        `_consume_orphan` already swallows whatever it returns.

        Idempotent: `_stopped` makes the second call a no-op, and every
        `_finalize` is refused by the `_finalized` marker, so a double shutdown
        writes no second event.
        """
        if self._stopped:
            return
        self._stopped = True
        # 1. Terminal states first — the event must exist before the socket
        #    learns the session is over.
        await self._terminate_turns()
        # 2. Then the tasks behind them.
        for task in list(self._turn_tasks.values()):
            if not task.done():
                task.cancel()
        self._release_pending.clear()
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
        for task in list(self._orphans):
            task.cancel()
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
            # A worker cancelled before its first step never enters the
            # coroutine body, so `_run_loop`'s `finally` (which clears this
            # reference) does not run. Own it here: a stopped queue must not
            # report a worker, not even a dead one.
            if self._worker is worker:
                self._worker = None

    async def _terminate_turns(self) -> None:
        """Write `turn.interrupted{by: system, data.reason: shutdown}` per turn.

        The running turn first (it owns the head of the queue and any
        streamed text), then every queued one in arrival order. Queued turns
        never reached the router, so they get no `tools/cancel` and no partial
        message: their row moves `queued -> interrupted` in the same
        transaction as the event.

        `_pending` is emptied BEFORE the writes: a worker waking up mid-way
        must not dequeue a turn that is already terminal and announce
        `turn.started` after its `turn.interrupted` (C-06 forbids any `turn.*`
        after the terminal one).
        """
        running = self._running
        if running is not None and not self.is_finalized(running.turn_id):
            task = self._turn_tasks.get(running.turn_id)
            if task is not None and not task.done():
                # Same courtesy `interrupt` pays: tell the module to stop
                # before the turn is closed under it. Sync + non-blocking.
                if self._cancel_hook is not None:
                    try:
                        self._cancel_hook(running.turn_id)
                    except Exception:  # pragma: no cover - cancel must never raise
                        pass
            await self._finalize_interrupted(
                running.turn, by="system", task=task, reason="shutdown",
            )
        queued = list(self._pending)
        self._pending.clear()
        for item in queued:
            if self.is_finalized(item.turn_id):
                continue
            turn = replace(item.turn, stopped_reason="shutdown")
            await self._finalize(
                turn, TurnState.INTERRUPTED,
                {"turn_id": item.turn_id, "by": "system",
                 "state": TurnState.INTERRUPTED.value,
                 "partial_message_id": None, "partial_chars": 0},
                data={"reason": "shutdown"}, reason="shutdown",
            )
            # A queued turn has no executor task: `_finalize` saw `task=None`
            # and armed `_release_pending` for a task that will never exist.
            self._forget_turn(item.turn_id)

    def _consume_orphan(self, task: asyncio.Task[Any]) -> None:
        """A module that ignores tools/cancel leaves a orphan task: swallow it."""
        self._orphans.add(task)
        task.add_done_callback(self._orphans.discard)
        if not task.cancelled():
            task.exception()


def _failed_usage(exc: BaseException, turn: Turn) -> dict[str, Any]:
    """Usage for a turn that ended in an exception (RFC-0005 §6.1).

    A turn that fails has usually been billed for the calls it did make, and
    the audit `llm.call` rows keep them (§6.2). Writing the `source="none"`
    placeholder over that made `session/usage.get` disagree with the audit
    trail by exactly the tokens a failure had burned. The loop attaches what it
    measured to the exception it raised (`loop.engine.failure_usage`); an
    executor that measures nothing attaches nothing and keeps the honest
    placeholder — core never estimates (N4/D102).
    """
    # Imported here, not at module scope: the session queue must not drag the
    # agent loop in with it (the loop imports this package's models).
    from ..loop.engine import failure_usage

    measured = failure_usage(exc)
    if measured is not None:
        return measured
    return _unmeasured_usage(turn.model_id)


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
