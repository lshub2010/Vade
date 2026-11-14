"""RFC-0007 §5.1/§5.2: the task record and its seven-state machine.

A subagent and a background job are the *same* state machine; the only
difference is `child_session_id` (a subagent has one, a job does not). Keeping
one record type is what lets the UI run one reducer, one badge counter and one
cancel path (RFC-0007 §4.1) — the alternative was six duplicated mechanisms.

`task_states` is a top-level key in C-06, not an entry under `events`, so this
enum is the code side of that enum: `tools/check_contracts.py` compares the
two sets and goes red on drift.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

__all__ = [
    "TaskKind", "TaskState", "TERMINAL_TASK_STATES", "Task",
    "new_task_id", "now_ms", "clamp_text",
]


class TaskKind(str, Enum):
    """§4.3. Immutable for the life of a task; not a permission bit."""

    SUBAGENT = "subagent"
    JOB = "job"


class TaskState(str, Enum):
    """§5.2. Terminal states are never left again."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: §5.2: `completed | failed | cancelled` and nothing else. Declared after the
#: enum so `check_contracts.py` can parse it as the second half of the same
#: assertion (schema `task_states` == code `TaskState`, terminal set == this).
TERMINAL_TASK_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
)


def new_task_id() -> str:
    """§5.1: uuid hex, same shape as a `turn_id` so UIs key on one id type."""
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


def clamp_text(value: str | None, limit: int) -> str:
    if not value:
        return ""
    text = str(value)
    return text if len(text) <= limit else text[:limit]


@dataclass(frozen=True, slots=True)
class Task:
    """One task record (RFC-0007 §5.1). Frozen: the registry swaps records.

    `pct` is `None` when progress is indeterminate, and it is updated even when
    no UI is attached (§5.4: the record keeps moving, only the *event* is
    gated) — that is what makes `snapshot.tasks[].pct` correct after a reconnect.
    """

    task_id: str
    kind: TaskKind
    session_id: str
    name: str = ""
    parent_id: str | None = None
    depth: int = 0
    turn_id: str | None = None
    child_session_id: str | None = None
    child_turn_id: str | None = None
    state: TaskState = TaskState.QUEUED
    action: str | None = None
    pct: int | None = None
    started_at: int = field(default_factory=now_ms)
    ended_at: int | None = None
    summary: str = ""
    code: str | None = None
    message: str | None = None
    retryable: bool = False
    forced: bool = False
    log_available: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TASK_STATES

    def elapsed_ms(self, now: int | None = None) -> int:
        end = self.ended_at if self.ended_at is not None else (now if now is not None else now_ms())
        return max(0, int(end) - int(self.started_at))

    def duration_ms(self, now: int | None = None) -> int:
        return self.elapsed_ms(now)

    def with_changes(self, **changes: Any) -> "Task":
        return replace(self, **changes)

    def to_view(self, now: int | None = None) -> dict[str, Any]:
        """§5.1 fields + `log_available` (§6.1 `TaskView`).

        A live task reports `elapsed_ms`; a terminal one reports `duration_ms`
        and leaves `elapsed_ms` out — the RFC says "运行中报 elapsed_ms，终态报
        duration_ms", and a UI that trusts a stale `elapsed_ms` would keep a
        finished timer ticking.
        """
        view: dict[str, Any] = {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "child_session_id": self.child_session_id,
            "child_turn_id": self.child_turn_id,
            "name": self.name,
            "state": self.state.value,
            "action": self.action,
            "pct": self.pct,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "summary": self.summary,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "forced": self.forced,
            "log_available": self.log_available,
        }
        if self.is_terminal:
            view["duration_ms"] = self.duration_ms(now)
            view["elapsed_ms"] = None
        else:
            view["elapsed_ms"] = self.elapsed_ms(now)
            view["duration_ms"] = None
        return view
