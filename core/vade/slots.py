"""C3-owned extension slots on `App` (C2's `app.py` only calls install_slots).

Four slots, defaults so late batches never hit `AttributeError`:
  * prompt_hooks        C3b appends `(PromptParts) -> PromptParts`
  * tool_failure_hooks  C3b appends `(list[str]) -> None`
  * on_session_end      C5a / C3d append `(SessionClosed) -> None`
  * approval_port       C5a assigns its ApprovalPort implementation

`install_slots` must be idempotent: `app.py` calls it on every startup and a
second call must not drop hooks already registered in this process.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["install_slots", "PROMPT_HOOKS", "TOOL_FAILURE_HOOKS", "ON_SESSION_END",
           "make_slots"]

PROMPT_HOOKS: list[Callable[..., Any]] = []
TOOL_FAILURE_HOOKS: list[Callable[..., Any]] = []
ON_SESSION_END: list[Callable[..., Any]] = []


def make_slots() -> dict[str, Any]:
    """Fresh slot values, so two `App` instances never share hook lists."""
    return {
        "prompt_hooks": [],
        "tool_failure_hooks": [],
        "on_session_end": [],
        "approval_port": None,
    }


def install_slots(app: Any) -> Any:
    for name, value in make_slots().items():
        if getattr(app, name, None) is None:
            setattr(app, name, value)
    return app
