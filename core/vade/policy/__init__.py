"""Policy component (C5a): risk judgement + the approval channel.

`install(app)` registers the `ApprovalPort` C3 reads from `app.approval_port`
and hooks session end so cached `scope=session` grants do not outlive their
session. No network listener is opened here — that is `vade.admin`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .adapter import PolicyAdapter
from .approval import ApprovalManager, ALLOWED_APPROVER_KINDS, GENERIC_EXECUTORS
from .engine import PolicyEngine, HIGH_FLOOR_TOOLS, RISK_RANK

__all__ = [
    "install", "PolicyEngine", "PolicyAdapter", "ApprovalManager",
    "HIGH_FLOOR_TOOLS", "RISK_RANK", "ALLOWED_APPROVER_KINDS", "GENERIC_EXECUTORS",
]


def install(app: Any) -> None:
    if getattr(app, "approval_port", None) is not None:
        return
    timeout_s = float(app.config.get("policy.approval_timeout_s", 60))
    engine = PolicyEngine(
        app.config.get("policy.tool_risk", {}) or {},
        medium_requires_approval=bool(app.config.get("policy.medium_requires_approval", False)),
    )
    manager = ApprovalManager(
        timeout_s=timeout_s, engine=engine, audit=getattr(app, "audit", None),
    )
    # `waiting_approval` is only observable if somebody writes it: the queue
    # owns turn state, so the hook is the same seam `input` uses. It is resolved
    # through `app.session_log` at CALL time — `policy` installs before
    # `session` (alphabetical), so capturing the log here would capture None.
    manager.set_state_hook(lambda session_id, turn_id, state: _mark_state(
        app, session_id, turn_id, state
    ))
    adapter = PolicyAdapter(manager)
    app.approval_port = adapter
    app.policy = engine
    app.approvals = manager

    def _session_closed(event: Any) -> None:
        session_id = getattr(event, "session_id", None) or (event if isinstance(event, str) else None)
        if not session_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(manager.on_session_end(str(session_id)))

    hooks = getattr(app, "on_session_end", None)
    if isinstance(hooks, list):
        hooks.append(_session_closed)


async def _mark_state(app: Any, session_id: str, turn_id: str, state: str) -> None:
    """Write one turn state into the queue, which persists and broadcasts it.

    Mirrors `vade.input._mark_state`: the policy component must not import the
    session log at install time, and an unknown/failed state must never abort an
    approval wait — the request is still answerable over `approval/decide`.
    """
    log: Any = getattr(app, "session_log", None)
    mark: Any = getattr(log, "mark_state", None)
    if not callable(mark):
        return
    from ..session.models import TurnState

    try:
        result: Any = mark(session_id, turn_id, TurnState(state))
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result
    except Exception:  # pragma: no cover - a state write is best effort
        return
