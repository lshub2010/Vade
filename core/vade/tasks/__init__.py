"""C3 task component (RFC-0007): registry, `task/*` RPC, snapshot hook.

`install(app)` runs after `session` (alphabetical, and the dependency is real):
the registry needs the live `SessionLog` to emit on the session's own seq stream
and the log needs a registry to fill `snapshot.tasks`. Both directions are set
here, in one place, so a half-wired pair cannot exist in a running core.

Nothing in this package writes events directly: every `task.*` goes through
`SessionLog.emit()`, which is what keeps persist-before-broadcast, seq
allocation and reconnect replay working without a second event source.
"""

from __future__ import annotations

from typing import Any

from .models import (
    TERMINAL_TASK_STATES,
    Task,
    TaskKind,
    TaskState,
    new_task_id,
    now_ms,
)
from .logs import RingLog, decode_cursor, encode_cursor
from .registry import HARD_CANCEL_CEILING_MS, TaskLimits, TaskRegistry
from .rpc import TaskRPC

__all__ = [
    "install", "TaskRegistry", "TaskLimits", "TaskRPC", "Task", "TaskState",
    "TaskKind", "TERMINAL_TASK_STATES", "RingLog", "HARD_CANCEL_CEILING_MS",
    "new_task_id", "now_ms", "decode_cursor", "encode_cursor",
]


def install(app: Any) -> Any:
    """Build the registry, publish it on the app and merge the four handlers."""
    cfg = getattr(app, "config", None)
    limits = TaskLimits.from_config(cfg) if cfg is not None else TaskLimits()
    log = getattr(app, "session", None) or getattr(app, "session_log", None)

    registry = TaskRegistry(log, limits=limits)
    app.tasks = registry
    app.task_registry = registry
    if log is not None:
        # One attribute, read by `SessionLog.snapshot()` and `interrupt()`.
        # The session component never imports `vade.tasks` — the arrow points
        # one way, so `session` still works with the tasks component absent.
        log.tasks = registry

    rpc = TaskRPC(registry)
    app.task_rpc = rpc
    handlers = getattr(app, "session_handlers", None)
    if isinstance(handlers, dict):
        for name, handler in rpc.handlers().items():
            handlers.setdefault(name, handler)
    else:  # a bare App (tests): publish a provider the session install drains
        providers = getattr(app, "handler_providers", None)
        if isinstance(providers, list):
            providers.append(rpc.handlers)
    return app
