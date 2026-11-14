"""Approval channel: suspend, time out, multi-UI race, session-scope grants.

Design constraints that are easy to get wrong (31-C5a §T2):

* **Timeout denies.** There is no "assume yes" path, no TTY prompt, and no
  waiting for a device to come online. 60s of silence is a `no`.
* **Only a device key or a loopback admin may approve.** A `browser_session`
  (XSS-controllable) approving `high` would hand HITL to an attacker.
* **Session scope keys on `(session_id, tool_id, args_digest)`.** Keying on the
  tool alone would let one approved `ls` unlock `rm -rf /`.
* Generic executors (`shell`, `exec`, ...) are `once`-only, always.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from ..audit.redact import digest_args, summarize_args
from ..errors import VadeError
from ..loop.ports import ApprovalDecision, ApprovalRequest
from .engine import PolicyEngine

__all__ = [
    "ApprovalManager", "PendingApproval", "new_request_id",
    "ALLOWED_APPROVER_KINDS", "GENERIC_EXECUTORS",
]

#: Anything else — `browser_session`, `ui`, `browser`, a module — is refused
#: with `APPROVER_INVALID`, which is exactly the C-04 code for this.
ALLOWED_APPROVER_KINDS = frozenset({"device_key", "loopback_admin"})

#: Executors that can run arbitrary commands: session scope would be a bypass.
GENERIC_EXECUTORS = frozenset({
    "shell", "shell_exec", "exec", "run", "run_command", "terminal", "bash", "sh", "powershell", "cmd",
})

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RID_LOCK = threading.Lock()
_RID_SEQ = 0


def new_request_id() -> str:
    """ULID-shaped id (26 chars) so `turn.approval_required` sorts by creation."""
    global _RID_SEQ
    millis = int(time.time() * 1000)
    with _RID_LOCK:
        _RID_SEQ = (_RID_SEQ + 1) & 0xFFFF
        seq = _RID_SEQ
    import os

    value = (millis << 80) | ((int.from_bytes(os.urandom(8), "big") << 16 | seq) & ((1 << 80) - 1))
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


@dataclass(slots=True)
class PendingApproval:
    request_id: str
    session_id: str
    turn_id: str
    tool_id: str
    args: Mapping[str, Any]
    risk: str
    deadline: float                 # monotonic
    started: float                  # monotonic
    created_at: float               # wall clock, for the event payload
    scope_key: tuple[str, str, str]
    future: "asyncio.Future[tuple[bool, str, str, str]]"
    emit: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None
    #: RFC-0007 §7.1: who is asking. `depth == 0` means the main turn.
    task_id: str | None = None
    task_name: str | None = None
    depth: int = 0


class ApprovalManager:
    def __init__(
        self,
        *,
        timeout_s: float = 60.0,
        engine: PolicyEngine | None = None,
        audit: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        approver_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.timeout_s = float(timeout_s)
        self.engine = engine or PolicyEngine()
        self._audit = audit
        self._clock = clock
        self._wall = wall_clock
        self._approver_probe = approver_probe
        self._pending: dict[str, PendingApproval] = {}
        self._resolved: dict[str, str] = {}
        self._grants: dict[tuple[str, str, str], float] = {}
        self._lock = asyncio.Lock()
        self._ui_count = 0

    # ---------------------------------------------------------------- approvers
    def note_ui_attached(self, delta: int = 1) -> None:
        self._ui_count = max(0, self._ui_count + delta)

    @property
    def ui_count(self) -> int:
        return self._ui_count

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def set_approver_probe(self, probe: Callable[[], bool] | None) -> None:
        self._approver_probe = probe

    # ------------------------------------------------------------------ request
    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        risk = self.engine.risk_of(req.tool_id, req.risk)
        if not self.engine.requires_approval(risk):
            return ApprovalDecision(True, reason="below_high")

        depth = int(getattr(req, "depth", 0) or 0)
        scope_key = (req.session_id, req.tool_id, digest_args(req.args))
        now = self._clock()
        if depth == 0:
            # RFC-0007 §7.2 rule 4: a `scope:"session"` grant covers the main
            # turn of this session only. A subagent that hits the same tool with
            # the same arguments must ask again — otherwise "allow one file
            # write" is amplified into "any write, from any spawned agent".
            async with self._lock:
                expiry = self._grants.get(scope_key)
                if expiry is not None and expiry > self._wall():
                    return ApprovalDecision(True, reason="session_grant", cached=True)

        if self._approver_probe is not None and not self._approver_probe():
            # No UI attached: deny now. Queueing would stall the turn until the
            # timeout and look like a hang to the user (31 §T2).
            self._write_audit(
                kind="approval", status="denied", code="PERMISSION_DENIED",
                request_id=None, actor=None, tool=req.tool_id, risk=risk,
                args=req.args, session_id=req.session_id, turn_id=req.turn_id,
                duration_ms=0.0, reason="no_approver", scope="once",
            )
            return ApprovalDecision(False, reason="no_approver")

        rid = new_request_id()
        loop = asyncio.get_running_loop()
        pending = PendingApproval(
            request_id=rid, session_id=req.session_id, turn_id=req.turn_id,
            tool_id=req.tool_id, args=dict(req.args or {}), risk=risk,
            deadline=now + self.timeout_s, started=now, created_at=self._wall(),
            scope_key=scope_key, future=loop.create_future(), emit=req.emit,
            task_id=getattr(req, "task_id", None),
            task_name=getattr(req, "task_name", None),
            depth=depth,
        )
        async with self._lock:
            self._pending[rid] = pending
        if req.emit is not None:
            try:
                await req.emit("turn.approval_required", {
                    "turn_id": req.turn_id, "request_id": rid, "tool": req.tool_id,
                    "args": dict(req.args or {}), "risk": risk,
                    "expires_at": int(pending.created_at + self.timeout_s),
                    # §7.1: `session_id` was already on the wire before C-06
                    # declared it (a drift `check_contracts.py` could not see,
                    # since it only verifies that required ⊆ payload). It is
                    # registered now, together with the attribution triple.
                    "session_id": req.session_id,
                    "task_id": pending.task_id,
                    "task_name": pending.task_name,
                    "depth": pending.depth,
                })
            except Exception:  # a broken UI must not abort the approval wait
                pass
        try:
            # `shield` is load-bearing: plain `wait_for` cancels the very future
            # the approver resolves, so a decision landing at the deadline used
            # to leave a done-but-unclaimed entry in `_pending` and `decide()`
            # answered "already resolved" instead of a clean timeout. With the
            # shield the deadline never touches the future, and the claim below
            # happens under the same lock `decide()` takes.
            allowed, by, scope, reason = await asyncio.wait_for(
                asyncio.shield(pending.future), self.timeout_s
            )
        except asyncio.TimeoutError:
            raced: tuple[bool, str, str, str] | None = None
            async with self._lock:
                if pending.future.done() and not pending.future.cancelled():
                    # The approver answered in the same tick the deadline fired;
                    # a real decision beats a timeout.
                    raced = pending.future.result()
                elif not pending.future.done():
                    pending.future.cancel()
                    self._resolved[rid] = "timeout"
                    self._pending.pop(rid, None)
                else:
                    self._pending.pop(rid, None)
            if raced is not None:
                allowed, by, scope, reason = raced
            else:
                elapsed = (self._clock() - pending.started) * 1000.0
                self._write_audit(
                    kind="approval_timeout", status="denied", code="PERMISSION_DENIED",
                    request_id=rid, actor="system", tool=req.tool_id, risk=risk,
                    args=req.args, session_id=req.session_id, turn_id=req.turn_id,
                    duration_ms=elapsed,
                    reason=f"审批超时（{self.timeout_s:g}s 内无人确认）", scope="once",
                )
                return ApprovalDecision(
                    False, reason=f"审批超时（{self.timeout_s:g}s 内无人确认，approval_timeout）",
                    request_id=rid, scope="once", by="system",
                )
        except asyncio.CancelledError:
            # A cancelled turn must not strand the entry: `pending_requests()`
            # would keep advertising a request nobody can resolve.
            async with self._lock:
                if not pending.future.done():
                    pending.future.cancel()
                self._resolved[rid] = "cancelled"
                self._pending.pop(rid, None)
            raise
        if not allowed:
            return ApprovalDecision(False, reason=reason or "denied", request_id=rid, scope=scope, by=by)
        return ApprovalDecision(True, reason=reason, request_id=rid, scope=scope, by=by,
                                cached=(reason == "session_grant"))

    # ------------------------------------------------------------------- decide
    async def decide(
        self,
        request_id: str,
        decision: str,
        scope: str = "once",
        by: str | None = None,
        *,
        actor_kind: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        """Called by C3's `approval/decide` handler after it authenticated the caller.

        `actor_kind` defaults to the empty string on purpose: a caller that
        forgets to say who is approving gets `APPROVER_INVALID`, not the
        permissive `device_key` branch. Secure defaults fail closed.

        Raises `APPROVER_INVALID` for a browser/UI actor or an unspecified one,
        `NOT_FOUND` for an unknown id and `INVALID_ARGUMENT` when the id is
        already resolved.
        """
        if actor_kind not in ALLOWED_APPROVER_KINDS:
            self._write_audit(
                kind="approval", status="denied", code="APPROVER_INVALID",
                request_id=request_id, actor=by or actor_kind, tool=None, risk=None,
                args=None, session_id=None, turn_id=None, duration_ms=0.0,
                reason=f"approver kind {actor_kind!r} may not resolve high-risk approvals",
                scope=scope,
            )
            raise VadeError(
                "APPROVER_INVALID",
                f"{actor_kind!r} cannot approve a high-risk tool call; use the device key or a loopback admin",
            )
        verdict = str(decision).lower()
        if verdict not in {"allow", "deny", "denied", "approve", "approved"}:
            raise VadeError("INVALID_ARGUMENT", "decision must be allow|deny")
        allow = verdict in {"allow", "approve", "approved"}
        scope = str(scope or "once").lower()
        if scope not in {"once", "session"}:
            raise VadeError("INVALID_ARGUMENT", "scope must be once|session")

        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                if request_id in self._resolved:
                    raise VadeError("INVALID_ARGUMENT", "approval request already resolved")
                raise VadeError("NOT_FOUND", "unknown approval request")
            if pending.future.done():
                raise VadeError("INVALID_ARGUMENT", "approval request already resolved")
            if scope == "session":
                bare = pending.tool_id.rsplit("/", 1)[-1]
                if bare in GENERIC_EXECUTORS:
                    raise VadeError(
                        "INVALID_ARGUMENT",
                        f"{bare!r} is a generic executor; session scope would approve arbitrary commands",
                    )
            pending.future.set_result((allow, by or actor_kind, scope, "" if allow else "denied by approver"))
            self._resolved[request_id] = "allowed" if allow else "denied"
            self._pending.pop(request_id, None)
            if allow and scope == "session" and pending.depth == 0:
                # Session grants die with the session (on_session_end) and are
                # bounded by an 8h ceiling so a forgotten session cannot live
                # forever in memory. A subagent's approval stores nothing:
                # RFC-0007 §7.2 rule 4, no downwards inheritance.
                self._grants[pending.scope_key] = self._wall() + 8 * 3600

        elapsed = (self._clock() - pending.started) * 1000.0
        self._write_audit(
            kind="approval", status="allowed" if allow else "denied",
            code=None if allow else "PERMISSION_DENIED", request_id=request_id,
            actor=by or actor_kind, tool=pending.tool_id, risk=pending.risk,
            args=pending.args, session_id=pending.session_id, turn_id=pending.turn_id,
            duration_ms=elapsed, reason="approved" if allow else "denied by approver",
            scope=scope,
        )
        if allow and scope == "session" and pending.depth == 0:
            self._write_audit(
                kind="approval_scope_granted", status="allowed", code=None,
                request_id=request_id, actor=by or actor_kind, tool=pending.tool_id,
                risk=pending.risk, args=pending.args, session_id=pending.session_id,
                turn_id=pending.turn_id, duration_ms=elapsed, reason="session scope",
                scope="session",
            )
        resolved = {
            "request_id": request_id, "decision": "allow" if allow else "deny",
            "by": by or actor_kind, "scope": scope, "state": "running",
            # §7.1: the same attribution the prompt carried, so a device that
            # missed `turn.approval_required` can still close the right card.
            "task_id": pending.task_id, "task_name": pending.task_name,
            "depth": pending.depth,
        }
        if allow and pending.emit is not None:
            # On denial C3's engine emits `turn.approval_resolved` itself; on
            # allow nobody else does, so other attached UIs would keep their
            # dialog open (31 §T2 "两端同时收到").
            try:
                await pending.emit("turn.approval_resolved", {"turn_id": pending.turn_id, **resolved})
            except Exception:
                pass
        return True, resolved

    # ------------------------------------------------------------- session ends
    async def on_session_end(self, session_id: str) -> None:
        async with self._lock:
            for key in [k for k in self._grants if k[0] == session_id]:
                self._grants.pop(key, None)
            stranded = [p for p in self._pending.values() if p.session_id == session_id]
            for pending in stranded:
                if not pending.future.done():
                    pending.future.set_result((False, "system", "once", "session_ended"))
                self._resolved[pending.request_id] = "session_ended"
                self._pending.pop(pending.request_id, None)

    def grants(self) -> dict[tuple[str, str, str], float]:
        return dict(self._grants)

    def pending_requests(self) -> list[dict[str, Any]]:
        """Snapshot for the admin UI; args are included because the approver must
        see exactly what will run (`31 §T2`). Nothing here is persisted."""
        return [
            {
                "request_id": p.request_id, "session_id": p.session_id, "turn_id": p.turn_id,
                "tool": p.tool_id, "args": dict(p.args), "risk": p.risk,
                "created_at": p.created_at, "deadline": p.deadline,
                "task_id": p.task_id, "task_name": p.task_name, "depth": p.depth,
            }
            for p in self._pending.values()
        ]

    # ------------------------------------------------------------------ auditing
    def _write_audit(
        self, *, kind: str, status: str, code: str | None, request_id: str | None,
        actor: str | None, tool: str | None, risk: str | None, args: Mapping[str, Any] | None,
        session_id: str | None, turn_id: str | None, duration_ms: float, reason: str, scope: str,
    ) -> None:
        if self._audit is None:
            return
        event = {
            "kind": kind, "status": status, "code": code, "request_id": request_id,
            "actor": actor, "tool": tool, "session_id": session_id, "turn_id": turn_id,
            "duration_ms": round(float(duration_ms), 3),
            "args": dict(args or {}) if args else None,
            "args_summary": summarize_args(args),
            "extra": {"decision": status, "risk": risk, "reason": reason, "scope": scope},
        }
        try:
            self._audit.record(event)
        except Exception:
            pass
