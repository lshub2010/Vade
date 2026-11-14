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
    "InputPort", "InputPrompt", "InputAnswer", "NullInputPort",
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


# ---------------------------------------------------------------- RFC-0008
@dataclass(frozen=True, slots=True)
class InputPrompt:
    """What the loop hands the input port: one question, no form DSL.

    `kind`, `options` and `schema` mirror `vade.input.models.InputRequest`; the
    port does not import that package (C3 stays free of C5a's module), so the
    two shapes are kept structurally compatible instead of nominally.
    """

    session_id: str
    turn_id: str
    kind: str
    prompt: str
    detail: str | None = None
    options: tuple[Mapping[str, Any], ...] = ()
    timeout_s: int | None = None
    default: Any = None
    max_length: int | None = None
    source: Mapping[str, Any] = field(default_factory=dict)
    emit: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None


@dataclass(frozen=True, slots=True)
class InputAnswer:
    """The model-visible outcome: a value, or `CANCELLED` with a reason.

    `code` is never `TIMEOUT` on purpose (C-04 marks TIMEOUT retriable and an
    implementation would auto-re-ask a question the user ignored).
    """

    ok: bool
    value: Any = None
    status: str = "cancelled"
    input_id: str | None = None
    code: str | None = None
    reason: str = ""
    message: str = ""


class InputPort(Protocol):
    async def ask(self, prompt: InputPrompt) -> InputAnswer: ...

    async def on_session_end(self, session_id: str) -> None: ...


class NullInputPort:
    """No ask-user channel installed: every question is `CANCELLED{no_ui}`.

    The same failure a headless turn gets — the model must handle "the user did
    not answer" anyway, so there is nothing to special-case.
    """

    async def ask(self, prompt: InputPrompt) -> InputAnswer:
        return InputAnswer(
            ok=False, status="cancelled", code="CANCELLED", reason="no_ui",
            message="no ask-user channel is installed",
        )

    async def on_session_end(self, session_id: str) -> None:
        return None
