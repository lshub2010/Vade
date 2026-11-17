"""`ApprovalPort` implementation handed to C3's engine.

The adapter is the *only* face of the approval channel that C3 sees, so every
capability the session layer needs has to be forwarded here. A missing
forwarder does not fail loudly — `getattr(port, "pending", None)` is simply not
callable — which is how the turn-end sweep silently listed nothing and left
every approval dialog open on a stopped turn.
"""

from __future__ import annotations

from typing import Any

from ..loop.ports import ApprovalDecision, ApprovalRequest
from .approval import ApprovalManager, PendingApproval

__all__ = ["PolicyAdapter"]


class PolicyAdapter:
    def __init__(self, manager: ApprovalManager) -> None:
        self.manager = manager

    @property
    def timeout_s(self) -> float:
        """`ui/tool.call` stage two measures its window against this."""
        return self.manager.timeout_s

    async def check(self, request: ApprovalRequest) -> ApprovalDecision:
        return await self.manager.request(request)

    async def open_request(self, request: ApprovalRequest) -> tuple[PendingApproval | None, str]:
        """RFC-0009 §6.2 stage one: mint without blocking a websocket."""
        return await self.manager.open_request(request)

    def decision_of(self, request_id: str) -> str | None:
        return self.manager.decision_of(request_id)

    async def on_session_end(self, session_id: str) -> None:
        await self.manager.on_session_end(session_id)

    # ------------------------------------------------------------------ listing
    def pending_requests(self) -> list[dict[str, Any]]:
        """All open requests (`vade.admin`'s approvals page)."""
        return self.manager.pending_requests()

    def pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Open requests, narrowed to one session when an id is given.

        `snapshot.pending_approvals[]` and the turn-end sweep both ask this
        question, and both used to get an empty list because the adapter only
        had `check`/`decide`.
        """
        rows = self.manager.pending_requests()
        if session_id is None:
            return rows
        return [row for row in rows if row.get("session_id") == session_id]

    async def close_turn(
        self, session_id: str, turn_id: str, *,
        reason: str = "turn_interrupted", by: str = "system",
    ) -> list[dict[str, Any]]:
        """Close and dequeue every approval still open on a turn that is ending."""
        return await self.manager.close_turn(session_id, turn_id, reason=reason, by=by)

    #: C3's `approval/decide` handler calls this after authenticating the actor.
    async def decide(self, request_id: str, decision: str, scope: str = "once",
                     by: str | None = None, *, actor_kind: str = "") -> Any:
        return await self.manager.decide(request_id, decision, scope, by, actor_kind=actor_kind)
