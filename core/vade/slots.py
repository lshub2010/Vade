"""Component registry holes filled by C3 (slots exist so W2 can run)."""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["install_slots", "PROMPT_HOOKS", "TOOL_FAILURE_HOOKS", "ON_SESSION_END"]

#: prompt hooks: (session, context) -> str fragments appended to the skill section (C3b)
PROMPT_HOOKS: list[Callable[..., Any]] = []
#: tool failure hooks: (module_id, tool, error) -> None, feeds skill fail_count (C3b)
TOOL_FAILURE_HOOKS: list[Callable[..., Any]] = []
#: session end hooks: (session_id) -> None, clears approvals + emits session.closed (C5a/C3d)
ON_SESSION_END: list[Callable[..., Any]] = []
#: approval port, filled by C5a; None means "no approver installed"
APPROVAL_PORT: Any = None


def install_slots(app: Any) -> Any:
    """Attach the four extension points C3/C3b/C3d/C5a append to.

    `install` must be idempotent: it replaces the lists with fresh ones only if
    they are missing, so a second call cannot drop already-registered hooks.
    """
    if getattr(app, "prompt_hooks", None) is None:
        app.prompt_hooks = []
    if getattr(app, "tool_failure_hooks", None) is None:
        app.tool_failure_hooks = []
    if getattr(app, "on_session_end", None) is None:
        app.on_session_end = []
    if getattr(app, "approval_port", None) is None:
        app.approval_port = None
    return app
