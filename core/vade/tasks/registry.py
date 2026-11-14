"""In-process task registry (RFC-0007 §5, §6, FR-8.56–8.62).

One registry per core process owns every task of every session: the record,
the seven-state machine, the progress gate/throttle, the bounded log ring, the
cancel token and the terminal broadcast. It is deliberately **not** a second
event source — every event goes through `SessionLog.emit()`, so persist-before-
broadcast, seq allocation and reconnect replay keep working untouched.

Three rules are load-bearing and easy to "fix" back into bugs:

* **Terminal first, then event** (§5.5 R1): the record is swapped before
  `task.finished` is emitted, and a late `task.status` / `task.progress` for a
  terminal task is dropped without touching the store (R2). A task never
  leaves a terminal state and never emits twice.
* **Progress is droppable, state is not** (§5.4): `task.progress` is gated on
  "some UI is attached", throttled to `tasks.progress_hz`, coalesced inside the
  window (last value wins) and is the *only* class the session burst budget may
  drop. Losing a progress event costs one repaint; losing a status event leaves
  a row spinning forever.
* **Cancel is a deadline, not a request** (§6.3): `task/cancel` returns
  immediately in `cancelling` and a timer forces `cancelled` at
  `tasks.cancel_grace_ms` (hard ceiling 3 000 ms, §5.2) with `forced:true`, so
  "正在取消…" can never be a permanent UI state.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from ..errors import VadeError

from .logs import RingLog, decode_cursor, encode_cursor
from .models import (
    TERMINAL_TASK_STATES,
    Task,
    TaskKind,
    TaskState,
    clamp_text,
    new_task_id,
    now_ms,
)

__all__ = ["TaskLimits", "TaskRegistry", "HARD_CANCEL_CEILING_MS"]

#: §6.3: "从 RPC 返回到 task.finished 的实测上限是 3s". Not configurable above
#: this — a longer grace period is the permanent spinner the RFC forbids.
HARD_CANCEL_CEILING_MS = 3000

#: §5.4: name/action/summary ceilings from §5.1.
ACTION_MAX = 120
NAME_MAX = 80
SUMMARY_MAX = 500
MESSAGE_MAX = 300


@dataclass(frozen=True, slots=True)
class TaskLimits:
    """`[tasks]` (C-18, RFC-0007 §9.4). Defaults here mirror the schema.

    `check_contracts.py` asserts the schema's defaults and apply semantics, so a
    silent change on either side goes red; this class is what the runtime reads.
    """

    max_concurrent: int = 8
    max_depth: int = 4
    ui_fold_depth: int = 3
    history_limit: int = 50
    snapshot_finished: int = 20
    log_lines: int = 200
    log_bytes: int = 65536
    log_ring_lines: int = 2000
    log_line_chars: int = 4096
    progress_hz: float = 2.0
    event_burst: int = 20
    cancel_grace_ms: int = 2000
    subagent_message: bool = False

    @classmethod
    def from_config(cls, cfg: Any) -> "TaskLimits":
        """Read `tasks.*` from a `Config` (or any `.get(dotted, default)`).

        Two values are validated at startup rather than trusted: a grace period
        above the 3 s ceiling (§6.3) and `max_depth < ui_fold_depth + 1`
        (§9.4 — otherwise FR-8.57's four-level fold can never be constructed).
        """
        get = getattr(cfg, "get", None)
        if not callable(get):
            return cls()
        # `slots=True` turns the class-level defaults into member descriptors,
        # so the fallbacks come from a real instance.
        base = cls()

        def value(key: str, default: Any) -> Any:
            raw = get(f"tasks.{key}", default)
            return default if raw is None else raw

        limits = cls(
            max_concurrent=int(value("max_concurrent", base.max_concurrent)),
            max_depth=int(value("max_depth", base.max_depth)),
            ui_fold_depth=int(value("ui_fold_depth", base.ui_fold_depth)),
            history_limit=int(value("history_limit", base.history_limit)),
            snapshot_finished=int(value("snapshot_finished", base.snapshot_finished)),
            log_lines=int(value("log_lines", base.log_lines)),
            log_bytes=int(value("log_bytes", base.log_bytes)),
            log_ring_lines=int(value("log_ring_lines", base.log_ring_lines)),
            log_line_chars=int(value("log_line_chars", base.log_line_chars)),
            progress_hz=float(value("progress_hz", base.progress_hz)),
            event_burst=int(value("event_burst", base.event_burst)),
            cancel_grace_ms=int(value("cancel_grace_ms", base.cancel_grace_ms)),
            subagent_message=bool(value("subagent_message", base.subagent_message)),
        )
        if limits.cancel_grace_ms > HARD_CANCEL_CEILING_MS:
            raise VadeError(
                "INVALID_ARGUMENT",
                f"tasks.cancel_grace_ms must be <= {HARD_CANCEL_CEILING_MS} (RFC-0007 §6.3)",
                data={"field": "tasks.cancel_grace_ms"},
            )
        if limits.max_depth < limits.ui_fold_depth + 1:
            raise VadeError(
                "INVALID_ARGUMENT",
                "tasks.max_depth must be >= tasks.ui_fold_depth + 1 (RFC-0007 §9.4)",
                data={"field": "tasks.max_depth"},
            )
        if limits.progress_hz <= 0:
            raise VadeError(
                "INVALID_ARGUMENT",
                "tasks.progress_hz must be > 0",
                data={"field": "tasks.progress_hz"},
            )
        return limits

    def for_snapshot(self) -> dict[str, int]:
        return {"max_concurrent": self.max_concurrent, "max_depth": self.max_depth}


@dataclass(slots=True)
class _Window:
    """Per-task trailing-edge coalescing window (progress or action text)."""

    window_end: float = 0.0
    pending: dict[str, Any] | None = None
    timer: "asyncio.Task[None] | None" = None


@dataclass(slots=True)
class _CancelToken:
    """The cooperative half of `task/cancel` (§6.3).

    `reason` is what the worker sees if it polls the token; `forced` is set by
    the registry, never by the worker, because only the registry knows whether
    the grace period expired.
    """

    task_id: str
    reason: str | None = None
    by: str = "user"
    cancelled: bool = False


class TaskRegistry:
    def __init__(
        self,
        log: Any = None,
        *,
        limits: TaskLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], int] = now_ms,
        max_depth: int | None = None,
    ) -> None:
        self._log = log
        self.limits = limits or TaskLimits()
        if max_depth is not None:  # test hook: same field, no config plumbing
            self.limits = replace(self.limits, max_depth=int(max_depth))
        self._clock = clock
        self._wall = wall_clock
        self._tasks: dict[str, Task] = {}
        self._logs: dict[str, RingLog] = {}
        self._tokens: dict[str, _CancelToken] = {}
        self._progress: dict[str, _Window] = {}
        self._actions: dict[str, _Window] = {}
        self._burst: dict[str, deque[float]] = {}
        self._timers: set["asyncio.Task[Any]"] = set()
        self._terminal_listeners: list[Callable[[Task], None]] = []
        self._cancel_hooks: list[Callable[[Task, _CancelToken], None]] = []
        self._message_hook: Callable[[Task, str], Awaitable[Any]] | None = None
        self.dropped_progress = 0

    # ------------------------------------------------------------------ wiring
    def set_cancel_hook(self, fn: Callable[[Task, _CancelToken], None]) -> None:
        """Called once per task when it enters `cancelling` (sync, best effort)."""
        self._cancel_hooks.append(fn)

    def add_terminal_listener(self, fn: Callable[[Task], None]) -> None:
        """Terminal broadcast (§6, FR-8.61). Clients dedupe; core announces once."""
        self._terminal_listeners.append(fn)

    def set_message_hook(self, fn: Callable[[Task, str], Awaitable[Any]]) -> None:
        self._message_hook = fn

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def require(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise VadeError("NOT_FOUND", f"task {task_id!r} does not exist")
        return task

    def all_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    # ------------------------------------------------------------------- spawn
    async def spawn(
        self,
        *,
        kind: str | TaskKind,
        name: str,
        session_id: str,
        turn_id: str | None = None,
        parent_id: str | None = None,
        child_session_id: str | None = None,
        child_turn_id: str | None = None,
        state: TaskState | str = TaskState.QUEUED,
        log_available: bool | None = None,
        retryable: bool = False,
    ) -> Task:
        """Create a task and emit `task.started` (§5.3: exactly once).

        Refusals are §5.7 exactly: `INVALID_ARGUMENT` with `data.field="depth"`
        past `tasks.max_depth`, `OVERLOADED` with `data.retry_after` past
        `tasks.max_concurrent`. Both are per session (a second session has its
        own budget).
        """
        try:
            kind_enum = kind if isinstance(kind, TaskKind) else TaskKind(str(kind))
        except ValueError as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "kind must be subagent|job", data={"field": "kind"}
            ) from exc

        parent: Task | None = None
        depth = 0
        if parent_id is not None:
            parent = self._tasks.get(parent_id)
            if parent is None:
                raise VadeError(
                    "NOT_FOUND", f"parent task {parent_id!r} does not exist",
                    data={"field": "parent_id"},
                )
            depth = parent.depth + 1
        if depth > self.limits.max_depth:
            raise VadeError(
                "INVALID_ARGUMENT",
                f"task depth {depth} exceeds tasks.max_depth ({self.limits.max_depth})",
                data={"field": "depth"},
            )

        live = sum(
            1
            for task in self._tasks.values()
            if task.session_id == session_id and not task.is_terminal
        )
        if live >= self.limits.max_concurrent:
            raise VadeError(
                "OVERLOADED",
                f"session has {live} tasks running (tasks.max_concurrent={self.limits.max_concurrent})",
                data={"retry_after": 1, "max_concurrent": self.limits.max_concurrent},
            )

        try:
            state_enum = state if isinstance(state, TaskState) else TaskState(str(state))
        except ValueError as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "state is not a task.state", data={"field": "state"}
            ) from exc
        if state_enum in TERMINAL_TASK_STATES or state_enum is TaskState.CANCELLING:
            raise VadeError(
                "INVALID_ARGUMENT",
                "task.started may only report queued|running",
                data={"field": "state"},
            )

        available = (
            (kind_enum is TaskKind.JOB) if log_available is None else bool(log_available)
        )
        task = Task(
            task_id=new_task_id(),
            kind=kind_enum,
            session_id=session_id,
            name=clamp_text(name, NAME_MAX),
            parent_id=parent_id,
            depth=depth,
            turn_id=turn_id,
            child_session_id=child_session_id,
            child_turn_id=child_turn_id,
            state=state_enum,
            started_at=self._wall(),
            retryable=bool(retryable),
            log_available=available,
        )
        self._tasks[task.task_id] = task
        if available:
            self._logs[task.task_id] = RingLog(
                max_lines=self.limits.log_ring_lines,
                line_chars=self.limits.log_line_chars,
                clock=self._wall,
            )
        await self._emit_started(task)
        return task

    # ------------------------------------------------------------------- state
    async def set_state(
        self,
        task_id: str,
        state: TaskState | str,
        *,
        action: str | None = None,
        pct: int | None = None,
    ) -> Task:
        """Transition + `task.status` (§5.3: every transition, never throttled)."""
        task = self.require(task_id)
        try:
            target = state if isinstance(state, TaskState) else TaskState(str(state))
        except ValueError as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "state is not a task.state", data={"field": "state"}
            ) from exc
        if task.is_terminal:
            # §5.5 R2: a worker that reports after the terminal event is late,
            # not authoritative. Drop without touching the record or the store.
            return task
        if target in TERMINAL_TASK_STATES:
            return await self.finish(task_id, target)
        if target is TaskState.CANCELLING:
            return await self.cancel(task_id, reason="state=cancelling")
        updated = replace(
            task,
            state=target,
            action=clamp_text(action, ACTION_MAX) if action is not None else task.action,
            pct=self._pct(pct) if pct is not None else task.pct,
        )
        self._tasks[task_id] = updated
        await self._emit_status(updated)
        return updated

    async def set_action(self, task_id: str, action: str) -> Task:
        """Action-text-only change: coalesced at ≤1/s (§5.3), latest wins."""
        task = self.require(task_id)
        if task.is_terminal:
            return task
        text = clamp_text(action, ACTION_MAX)
        self._tasks[task_id] = replace(task, action=text)
        window = self._actions.setdefault(task_id, _Window())
        remaining = window.window_end - self._clock()
        payload = self._status_payload(self._tasks[task_id])
        if remaining <= 0:
            window.window_end = self._clock() + 1.0
            window.pending = None
            await self._emit_status_payload(task.session_id, payload)
        else:
            window.pending = payload
            if window.timer is None or window.timer.done():
                window.timer = self._spawn_timer(self._flush_action(task_id, remaining))
        return self._tasks[task_id]

    async def progress(
        self,
        task_id: str,
        pct: int | None,
        *,
        detail: str | None = None,
    ) -> bool:
        """Update `pct` (always) and emit a coalesced `task.progress` (sometimes).

        Returns whether an event was emitted. Three gates, in order (§5.4):
        terminal (R2) → no UI attached (record only) → session burst budget
        (progress is the only droppable class).
        """
        task = self.require(task_id)
        if task.is_terminal:
            return False
        value = self._pct(pct)
        self._tasks[task_id] = replace(task, pct=value)
        if not self._has_sinks(task.session_id):
            return False
        if not self._burst_ok(task.session_id):
            self.dropped_progress += 1
            return False

        payload = self._progress_payload(self._tasks[task_id], detail)
        window = self._progress.setdefault(task_id, _Window())
        now = self._clock()
        remaining = window.window_end - now
        if remaining <= 0:
            window.window_end = now + self._window_s()
            window.pending = None
            await self._emit_progress_payload(payload)
            return True
        # Inside the window: remember only the newest value and let the timer
        # flush it. This is what makes 100 reports/s become ≤2 events/s.
        window.pending = payload
        if window.timer is None or window.timer.done():
            window.timer = self._spawn_timer(self._flush_progress(task_id, remaining))
        return False

    async def finish(
        self,
        task_id: str,
        state: TaskState | str = TaskState.COMPLETED,
        *,
        summary: str = "",
        code: str | None = None,
        message: str | None = None,
        forced: bool = False,
        by: str = "system",
        usage: Mapping[str, Any] | None = None,
    ) -> Task:
        """Terminal transition: record first, then exactly one `task.finished`."""
        task = self.require(task_id)
        if task.is_terminal:
            return task
        try:
            target = state if isinstance(state, TaskState) else TaskState(str(state))
        except ValueError as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "state is not a task.state", data={"field": "state"}
            ) from exc
        if target not in TERMINAL_TASK_STATES:
            raise VadeError(
                "INVALID_ARGUMENT",
                "task.finished state must be completed|failed|cancelled",
                data={"field": "state"},
            )
        if target is TaskState.FAILED and not code:
            raise VadeError(
                "INVALID_ARGUMENT", "failed tasks must carry a C-04 `code`",
                data={"field": "code"},
            )
        ended = self._wall()
        final = replace(
            task,
            state=target,
            ended_at=ended,
            summary=clamp_text(summary, SUMMARY_MAX),
            code=code,
            message=clamp_text(message, MESSAGE_MAX) or None,
            forced=bool(forced) if target is TaskState.CANCELLED else False,
        )
        # §5.5 R1: persist-before-broadcast. A crash between these two lines
        # loses one event, never invents a task that is still running.
        self._tasks[task_id] = final
        self._drop_windows(task_id)
        await self._emit_finished(final, by=by, usage=usage)
        for listener in list(self._terminal_listeners):
            try:
                listener(final)
            except Exception:  # pragma: no cover - a listener must never break finish
                pass
        if final.kind is TaskKind.SUBAGENT:
            await self._cascade_children(final.task_id)
        return final

    # ------------------------------------------------------------------ cancel
    async def cancel(
        self,
        task_id: str,
        *,
        reason: str | None = None,
        by: str = "user",
        grace_ms: int | None = None,
    ) -> Task:
        """Idempotent cancel (§6.3). Terminal in, same terminal out, no event."""
        task = self.require(task_id)
        if task.is_terminal:
            return task
        if task.state is TaskState.CANCELLING:
            return task
        token = _CancelToken(task_id=task_id, reason=reason, by=by, cancelled=True)
        self._tokens[task_id] = token
        cancelling = replace(task, state=TaskState.CANCELLING)
        self._tasks[task_id] = cancelling
        await self._emit_status(cancelling)
        for hook in list(self._cancel_hooks):
            try:
                hook(cancelling, token)
            except Exception:  # pragma: no cover - cooperative cancellation is best effort
                pass
        grace = self.limits.cancel_grace_ms if grace_ms is None else int(grace_ms)
        grace = max(0, min(grace, HARD_CANCEL_CEILING_MS))
        self._spawn_timer(self._force_cancel(task_id, grace, reason))
        return cancelling

    async def _force_cancel(self, task_id: str, grace_ms: int, reason: str | None) -> None:
        if grace_ms > 0:
            await asyncio.sleep(grace_ms / 1000.0)
        task = self._tasks.get(task_id)
        if task is None or task.is_terminal:
            return
        await self.finish(
            task_id,
            TaskState.CANCELLED,
            summary=reason or "cancelled",
            forced=True,
            by="system",
        )

    def cancel_token(self, task_id: str) -> _CancelToken | None:
        """What a cooperative worker polls: `token.cancelled`, `token.reason`."""
        return self._tokens.get(task_id)

    # ----------------------------------------------------------------- cascade
    async def cancel_turn(self, session_id: str, turn_id: str, *, by: str = "parent") -> list[str]:
        """§5.7: interrupting a turn cancels its subagents, **not** its jobs.

        Jobs are defined to have detached from the turn — the user pressing stop
        means "stop thinking for me", not "cancel the download I started ten
        minutes ago".
        """
        cancelled: list[str] = []
        for task in list(self._tasks.values()):
            if task.session_id != session_id or task.turn_id != turn_id:
                continue
            if task.kind is not TaskKind.SUBAGENT or task.is_terminal:
                continue
            await self.cancel(task.task_id, reason="parent turn interrupted", by=by)
            cancelled.append(task.task_id)
        return cancelled

    async def _cascade_children(self, parent_id: str) -> list[str]:
        cancelled: list[str] = []
        for task in list(self._tasks.values()):
            if task.parent_id != parent_id or task.is_terminal:
                continue
            if task.kind is not TaskKind.SUBAGENT:
                continue
            await self.cancel(task.task_id, reason="parent task finished", by="parent")
            cancelled.append(task.task_id)
        return cancelled

    async def close_session(self, session_id: str) -> list[str]:
        """§5.7: closing a session cancels every task (the seq space is gone)."""
        cancelled: list[str] = []
        for task in list(self._tasks.values()):
            if task.session_id != session_id or task.is_terminal:
                continue
            await self.cancel(task.task_id, reason="session closed", by="system")
            cancelled.append(task.task_id)
        return cancelled

    # ----------------------------------------------------------------- history
    def list_tasks(
        self,
        *,
        session_id: str | None = None,
        kinds: Sequence[str] | None = None,
        states: Sequence[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """`task/list` (§6.1): fixed order, opaque keyset cursor, running_count."""
        if limit is None:
            limit = self.limits.history_limit
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise VadeError(
                "INVALID_ARGUMENT", "limit must be 1..200", data={"field": "limit"}
            )
        kind_filter = self._enum_filter(kinds, TaskKind, "kinds")
        state_filter = self._enum_filter(states, TaskState, "states")

        scoped = [
            task
            for task in self._tasks.values()
            if (session_id is None or task.session_id == session_id)
            and (kind_filter is None or task.kind in kind_filter)
        ]
        running_count = sum(1 for task in scoped if not task.is_terminal)
        selected = [
            task for task in scoped if state_filter is None or task.state in state_filter
        ]
        ordered = sorted(selected, key=self._order_key)
        if cursor is not None:
            marker = self._decode_list_cursor(cursor)
            ordered = [task for task in ordered if self._order_key(task) > marker]
        page = ordered[:limit]
        next_cursor = None
        if len(ordered) > len(page) and page:
            next_cursor = self._encode_list_cursor(page[-1])
        return {
            "tasks": [task.to_view(self._wall()) for task in page],
            "next_cursor": next_cursor,
            "running_count": running_count,
            "limits": self.limits.for_snapshot(),
        }

    def snapshot(self, session_id: str) -> tuple[list[dict[str, Any]], bool]:
        """§5.6: live asc, then the newest `tasks.snapshot_finished` terminals."""
        scoped = [task for task in self._tasks.values() if task.session_id == session_id]
        live = sorted((t for t in scoped if not t.is_terminal), key=self._order_key)
        done = sorted((t for t in scoped if t.is_terminal), key=self._order_key)
        cap = max(0, self.limits.snapshot_finished)
        truncated = len(done) > cap
        keep = live + done[:cap]
        return [task.to_view(self._wall()) for task in keep], truncated

    # -------------------------------------------------------------------- logs
    def append_log(self, task_id: str, text: str, *, stream: str = "core") -> int:
        log = self._logs.get(task_id)
        if log is None:
            raise VadeError("NOT_FOUND", f"task {task_id!r} has no log")
        return log.append(text, stream=stream)

    async def log_tail(
        self,
        task_id: str,
        *,
        lines: int | None = None,
        max_bytes: int | None = None,
        cursor: str | None = None,
        follow: bool = False,
        wait_ms: int | None = None,
    ) -> dict[str, Any]:
        """§6.2. `follow` is a bounded long-poll, not a subscription."""
        task = self.require(task_id)
        log = self._logs.get(task_id)
        if log is None or not task.log_available:
            # §6.2: `log_available=false` is NOT_FOUND, not an empty list —
            # "no output yet" and "this task has no log" are different answers.
            raise VadeError("NOT_FOUND", f"task {task_id!r} has no log")

        limit = self._clamp_int(lines, self.limits.log_lines, "lines", upper=200)
        budget = self._clamp_int(max_bytes, self.limits.log_bytes, "max_bytes", upper=65536)
        # The configured value is a ceiling too: a deployment may lower
        # `tasks.log_lines` below the schema maximum, and a client's default
        # request of 200 must not walk past it.
        limit = max(1, min(limit, self.limits.log_lines, 200))
        budget = max(1, min(budget, self.limits.log_bytes, 65536))
        index: int | None = None
        if cursor is not None:
            try:
                index = decode_cursor(cursor)
            except ValueError as exc:
                raise VadeError(
                    "INVALID_ARGUMENT", "cursor is not a task log cursor",
                    data={"field": "cursor"},
                ) from exc

        if follow:
            wait = self._clamp_int(wait_ms, 10000, "wait_ms", upper=30000)
            deadline = self._clock() + wait / 1000.0
            while True:
                current = self.require(task_id)
                if index is None or log.next_index > index or current.is_terminal:
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.05, remaining))

        result = log.tail(lines=limit, max_bytes=budget, cursor=index)
        current = self.require(task_id)
        eof = current.is_terminal and result["cursor_index"] >= log.next_index
        return {
            "lines": result["rows"],
            "cursor": result["cursor"],
            "eof": bool(eof),
            "truncated": result["truncated"],
            "lost": result["lost"],
        }

    # --------------------------------------------------------------- messaging
    async def message(self, task_id: str, text: str) -> dict[str, Any]:
        """`subagent/message` (§6.4). v1 default is `tasks.subagent_message=false`."""
        if not self.limits.subagent_message:
            raise VadeError(
                "UNSUPPORTED",
                "subagent messaging is disabled (tasks.subagent_message=false)",
                data={"reason": "subagent_message_disabled"},
            )
        task = self.require(task_id)
        if task.kind is not TaskKind.SUBAGENT or not task.child_session_id:
            raise VadeError(
                "INVALID_ARGUMENT", "only a subagent with a child session accepts messages",
                data={"field": "task_id"},
            )
        body = str(text or "")
        if not 1 <= len(body) <= 2000:
            raise VadeError(
                "INVALID_ARGUMENT", "text must be 1..2000 characters",
                data={"field": "text"},
            )
        if self._message_hook is None:
            # Honest refusal: "queued" without a delivery path would be a lie.
            raise VadeError(
                "UNSUPPORTED", "no subagent delivery path is installed",
                data={"reason": "no_delivery"},
            )
        await self._message_hook(task, body)
        return {"ok": True, "turn_id": task.child_turn_id, "delivery": "queued"}

    # ------------------------------------------------------------------ events
    async def _emit_started(self, task: Task) -> None:
        if self._log is None:
            return
        self._note_event(task.session_id)
        await self._log.emit(
            task.session_id,
            "task.started",
            {
                "task_id": task.task_id,
                "kind": task.kind.value,
                "state": task.state.value,
                "session_id": task.session_id,
                "turn_id": task.turn_id,
                "parent_id": task.parent_id,
                "depth": task.depth,
                "name": task.name,
                "started_at": task.started_at,
                "retryable": task.retryable,
                "child_session_id": task.child_session_id,
                "child_turn_id": task.child_turn_id,
            },
        )

    async def _emit_status(self, task: Task) -> None:
        payload = self._status_payload(task)
        await self._emit_status_payload(task.session_id, payload)

    async def _emit_status_payload(self, session_id: str, payload: Mapping[str, Any]) -> None:
        if self._log is None:
            return
        self._note_event(session_id)
        await self._log.emit(session_id, "task.status", payload)

    async def _emit_progress_payload(self, payload: Mapping[str, Any]) -> None:
        if self._log is None:
            return
        # `coalesce=True`: replay coverage skips it (§5.5 R5 / §9.4) and the
        # wire frame carries `x-vade.coalesce`. Never carries `state`.
        session_id = str(payload.get("session_id_hint") or "")
        wire = {k: v for k, v in payload.items() if k != "session_id_hint"}
        self._note_event(session_id)
        await self._log.emit(session_id, "task.progress", wire, coalesce=True)

    async def _emit_finished(
        self, task: Task, *, by: str, usage: Mapping[str, Any] | None = None
    ) -> None:
        if self._log is None:
            return
        payload = {
            "task_id": task.task_id,
            "kind": task.kind.value,
            "parent_id": task.parent_id,
            "depth": task.depth,
            "state": task.state.value,
            "duration_ms": task.duration_ms(self._wall()),
            "summary": task.summary,
            "code": task.code,
            "message": task.message,
            "forced": bool(task.forced),
            "by": by,
        }
        if usage is not None:
            payload["usage"] = dict(usage)
        self._note_event(task.session_id)
        await self._log.emit(task.session_id, "task.finished", payload)

    # ------------------------------------------------------------- payloads
    def _status_payload(self, task: Task) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "kind": task.kind.value,
            "parent_id": task.parent_id,
            "depth": task.depth,
            "state": task.state.value,
            "elapsed_ms": task.elapsed_ms(self._wall()),
            "action": task.action,
        }
        if task.pct is not None:
            payload["pct"] = task.pct
        return payload

    def _progress_payload(self, task: Task, detail: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "pct": int(task.pct or 0),
            "elapsed_ms": task.elapsed_ms(self._wall()),
            # Internal routing hint, stripped before the event is written:
            # `task.progress` does not require `session_id` (§5.3) and adding
            # one would make the payload disagree with C-06.
            "session_id_hint": task.session_id,
        }
        if detail:
            payload["detail"] = clamp_text(detail, SUMMARY_MAX)
        return payload

    # -------------------------------------------------------------- plumbing
    def _pct(self, pct: Any) -> int | None:
        if pct is None:
            return None
        try:
            value = int(pct)
        except (TypeError, ValueError) as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "pct must be an integer 0..100",
                data={"field": "pct"},
            ) from exc
        return max(0, min(100, value))

    def _window_s(self) -> float:
        return 1.0 / float(self.limits.progress_hz)

    def _has_sinks(self, session_id: str) -> bool:
        counter = getattr(self._log, "attached_count", None)
        if not callable(counter):
            # No session log (unit tests, or a registry used as a pure index):
            # nothing can be delivered, but the records stay correct.
            return False
        try:
            return int(counter(session_id)) > 0
        except Exception:  # pragma: no cover - defensive
            return False

    def _burst_ok(self, session_id: str) -> bool:
        """§5.4 session-level budget. Only progress consults it; every task
        event counts against it, so 8 subagents cannot starve the stream."""
        budget = int(self.limits.event_burst)
        if budget <= 0:
            return True
        now = self._clock()
        bucket = self._burst.setdefault(session_id, deque())
        while bucket and bucket[0] < now - 1.0:
            bucket.popleft()
        return len(bucket) < budget

    def _note_event(self, session_id: str) -> None:
        if not session_id:
            return
        budget = int(self.limits.event_burst)
        if budget <= 0:
            return
        now = self._clock()
        bucket = self._burst.setdefault(session_id, deque())
        while bucket and bucket[0] < now - 1.0:
            bucket.popleft()
        bucket.append(now)

    def _spawn_timer(self, coro: Awaitable[None]) -> "asyncio.Task[None] | None":
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only a sync caller can get here
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]
            return None
        task = loop.create_task(coro)  # type: ignore[arg-type]
        self._timers.add(task)

        def _done(done: "asyncio.Task[None]") -> None:
            self._timers.discard(done)
            if not done.cancelled():
                done.exception()  # mark retrieved; a timer must never warn

        task.add_done_callback(_done)
        return task

    async def _flush_progress(self, task_id: str, remaining: float) -> None:
        if remaining > 0:
            await asyncio.sleep(remaining)
        window = self._progress.get(task_id)
        if window is None or window.pending is None:
            return
        task = self._tasks.get(task_id)
        payload, window.pending = window.pending, None
        if task is None or task.is_terminal:
            return
        if not self._has_sinks(task.session_id):
            return
        window.window_end = self._clock() + self._window_s()
        await self._emit_progress_payload(payload)

    async def _flush_action(self, task_id: str, remaining: float) -> None:
        if remaining > 0:
            await asyncio.sleep(remaining)
        window = self._actions.get(task_id)
        if window is None or window.pending is None:
            return
        task = self._tasks.get(task_id)
        payload, window.pending = window.pending, None
        if task is None or task.is_terminal:
            return
        window.window_end = self._clock() + 1.0
        await self._emit_status_payload(task.session_id, payload)

    def _drop_windows(self, task_id: str) -> None:
        for windows in (self._progress, self._actions):
            window = windows.pop(task_id, None)
            if window is not None and window.timer is not None and not window.timer.done():
                window.timer.cancel()
        self._tokens.pop(task_id, None)

    def _clamp_int(self, value: Any, default: int, field_name: str, *, upper: int) -> int:
        if value is None:
            return max(1, min(int(default), upper))
        if not isinstance(value, int) or isinstance(value, bool):
            raise VadeError(
                "INVALID_ARGUMENT", f"{field_name} must be an integer",
                data={"field": field_name},
            )
        if value < 1:
            raise VadeError(
                "INVALID_ARGUMENT", f"{field_name} must be >= 1",
                data={"field": field_name},
            )
        return min(value, upper)

    def _enum_filter(
        self,
        values: Sequence[str] | None,
        enum: type,
        field_name: str,
    ) -> set[Any] | None:
        if values is None:
            return None
        if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set)):
            raise VadeError(
                "INVALID_ARGUMENT", f"{field_name} must be an array",
                data={"field": field_name},
            )
        out = set()
        for item in values:
            try:
                out.add(enum(str(item)))
            except ValueError as exc:
                raise VadeError(
                    "INVALID_ARGUMENT", f"{field_name} contains {item!r}",
                    data={"field": field_name},
                ) from exc
        return out

    @staticmethod
    def _order_key(task: Task) -> tuple[int, int, str]:
        """§6.1: live by `started_at` asc first, terminal by `ended_at` desc next."""
        if task.is_terminal:
            return (1, -(task.ended_at or 0), task.task_id)
        return (0, task.started_at, task.task_id)

    def _encode_list_cursor(self, task: Task) -> str:
        primary = (task.ended_at or 0) if task.is_terminal else task.started_at
        phase = 1 if task.is_terminal else 0
        return _encode_keyset(phase, primary, task.task_id)

    def _decode_list_cursor(self, cursor: str) -> tuple[int, int, str]:
        try:
            phase, primary, task_id = _decode_keyset(cursor)
        except ValueError as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "cursor is not a task/list cursor",
                data={"field": "cursor"},
            ) from exc
        if phase == 0:
            return (0, primary, task_id)
        return (1, -primary, task_id)

    async def aclose(self) -> None:
        for timer in list(self._timers):
            timer.cancel()
        self._timers.clear()


def _encode_keyset(phase: int, primary: int, task_id: str) -> str:
    import base64

    raw = f"k1|{phase}|{int(primary)}|{task_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_keyset(cursor: str) -> tuple[int, int, str]:
    import base64
    import binascii

    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("undecodable cursor") from exc
    parts = raw.split("|", 3)
    if len(parts) != 4 or parts[0] != "k1":
        raise ValueError("unknown cursor version")
    try:
        phase = int(parts[1])
        primary = int(parts[2])
    except ValueError as exc:
        raise ValueError("non-numeric cursor") from exc
    if phase not in (0, 1) or not parts[3]:
        raise ValueError("bad cursor fields")
    return phase, primary, parts[3]
