"""Ports C3 consumes and other batches implement (no reverse imports).

`ApprovalPort` / `AuditPort` are C5a's; `MemoryPort` is M4a's. C3 only defines
the shape, so `vade.policy` / `vade.audit` / `vade.memory` never appear here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

__all__ = [
    "ApprovalPort", "ApprovalRequest", "ApprovalDecision", "NullApprovalPort",
    "AuditPort", "NullAuditPort", "MemoryPort", "MemoryItem", "EventSink",
]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    session_id: str
    turn_id: str
    tool_id: str
    args: Mapping[str, Any]
    risk: str
    expires_at: int
    #: Core-owned emit. The port uses it to publish `turn.approval_required`
    #: on the ONE seq stream; it must not open its own event channel.
    emit: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None
    #: RFC-0007 §7.1 attribution. `task_id` is the task that raised the call
    #: (`None` for a main-turn call), `depth` is its level in the task tree and
    #: is what §7.2 rule 4 uses to refuse grant inheritance.
    task_id: str | None = None
    task_name: str | None = None
    depth: int = 0


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    allowed: bool
    reason: str = ""
    request_id: str | None = None
    scope: str = "once"
    by: str | None = None
    #: True when the decision came from a cached session-scope grant.
    cached: bool = False


class ApprovalPort(Protocol):
    async def check(self, request: ApprovalRequest) -> ApprovalDecision: ...

    async def on_session_end(self, session_id: str) -> None: ...


class NullApprovalPort:
    """Fail-closed when C5a is not installed: high risk is denied immediately."""

    async def check(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.risk == "high":
            return ApprovalDecision(False, reason="no_approver")
        return ApprovalDecision(True, reason="below_high")

    async def on_session_end(self, session_id: str) -> None:
        return None


class AuditPort(Protocol):
    def record(self, event: Mapping[str, Any]) -> None: ...


class NullAuditPort:
    def record(self, event: Mapping[str, Any]) -> None:
        return None


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    text: str
    meta: Mapping[str, Any] = field(default_factory=dict)


class MemoryPort(Protocol):
    async def read_session(self, session_id: str, limit: int = 50) -> list[MemoryItem]: ...

    # Management surface (AMEND-16) forwarded to `memory/admin_*`; only C5a's
    # admin layer calls these. C3 performs no authorization here.
    async def admin_list(self, query: Any) -> tuple[list[MemoryItem], str | None]: ...
    async def admin_get(self, mem_id: str) -> tuple[MemoryItem, list[MemoryItem]]: ...
    async def admin_promote(self, mem_id: str, by: str) -> bool: ...
    async def admin_archive(self, mem_id: str, reason: str, by: str) -> bool: ...
    async def admin_restore(self, mem_id: str, by: str) -> bool: ...
    async def admin_stats(self) -> dict[str, Any]: ...


class EventSink(Protocol):
    async def send(self, event: Mapping[str, Any]) -> None: ...
