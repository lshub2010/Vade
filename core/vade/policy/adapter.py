"""`ApprovalPort` implementation handed to C3's engine."""

from __future__ import annotations

from typing import Any

from ..loop.ports import ApprovalDecision, ApprovalRequest
from .approval import ApprovalManager

__all__ = ["PolicyAdapter"]


class PolicyAdapter:
    def __init__(self, manager: ApprovalManager) -> None:
        self.manager = manager

    async def check(self, request: ApprovalRequest) -> ApprovalDecision:
        return await self.manager.request(request)

    async def on_session_end(self, session_id: str) -> None:
        await self.manager.on_session_end(session_id)

    #: C3's `approval/decide` handler calls this after authenticating the actor.
    async def decide(self, request_id: str, decision: str, scope: str = "once",
                     by: str | None = None, *, actor_kind: str = "") -> Any:
        return await self.manager.decide(request_id, decision, scope, by, actor_kind=actor_kind)
