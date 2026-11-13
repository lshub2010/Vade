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
