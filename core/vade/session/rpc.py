"""UI-side RPC surface (T6).

Handlers follow C2's dispatcher shape: `async handler(connection, params)`.
Errors are returned as `RpcError` so the transport writes a JSON-RPC error
envelope; no traceback ever crosses the boundary.

Wiring note: C2's `make_rpc_handler` only knows `module/*`. `install(app)`
publishes `app.session_handlers` and `app.session_rpc`; one line in the
dispatcher (C2-owned) is what routes `ui/*` here.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Mapping

from ..errors import VadeError
from ..transport.connection import RpcError
from .manager import SessionLog

__all__ = ["SessionRPC", "ConnectionSink"]


class ConnectionSink:
    """Adapts a C2 `Connection` to the `EventSink` protocol."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def send(self, event: Mapping[str, Any]) -> None:
        frame = {"jsonrpc": "2.0", "method": "event", "params": dict(event)}
        send_event = getattr(self.connection, "send_event", None)
        if callable(send_event):
            await send_event(frame)
            return
        await self.connection.send_json(frame)


class SessionRPC:
    def __init__(self, log: SessionLog, approval: Callable[[], Any] | None = None) -> None:
        self.log = log
        self._approval = approval or (lambda: None)
        self._sinks: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ dispatch
    async def dispatch(self, connection: Any, method: str, params: Mapping[str, Any] | None) -> Any:
        handler = self.handlers().get(method)
        if handler is None:
            return RpcError("NOT_FOUND", f"unknown method {method!r}")
        try:
            return await handler(connection, dict(params or {}))
        except VadeError as exc:
            # `data` is part of C-04's envelope (`error.data.field`); dropping
            # it here used to erase every field pointer between the check and
            # the client (§6.4 / G7).
            return RpcError(exc.code, exc.message, dict(exc.data) if exc.data else None)
        except Exception:
            # C-04: message only, never a traceback across the boundary.
            return RpcError("INTERNAL", "session RPC failed")

    def handlers(self) -> dict[str, Callable[[Any, Mapping[str, Any]], Awaitable[Any]]]:
        return {
            "ui/attach": self.ui_attach,
            "ui/detach": self.ui_detach,
            "ui/submit": self.ui_submit,
            "ui/interrupt": self.ui_interrupt,
            "ui/queue.cancel": self.ui_queue_cancel,
            "approval/decide": self.approval_decide,
        }

    # ------------------------------------------------------------------ ui/*
    async def ui_attach(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        last_seq = params.get("last_seq")
        if session_id is not None and not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id must be a string or null")
        if last_seq is not None and (not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0):
            raise VadeError("INVALID_ARGUMENT", "last_seq must be a non-negative integer or null")
        result = await self.log.attach(session_id, last_seq, ConnectionSink(connection))
        self._sinks[(str(getattr(connection, "connection_id", "")), result.session_id)] = result.sink_id
        return {"session_id": result.session_id, "from_seq": result.from_seq,
                "snapshot": result.snapshot}

    async def ui_detach(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id is required")
        key = (str(getattr(connection, "connection_id", "")), session_id)
        sink_id = self._sinks.pop(key, None)
        if sink_id is None:
            raise VadeError("NOT_FOUND", "this connection is not attached to that session")
        return {"ok": await self.log.detach(session_id, sink_id)}

    async def ui_submit(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        content = params.get("content")
        client_msg_id = params.get("client_msg_id")
        if not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id is required",
                            data={"field": "session_id"})
        if not isinstance(client_msg_id, str) or not client_msg_id:
            raise VadeError("INVALID_ARGUMENT", "client_msg_id is required",
                            data={"field": "client_msg_id"})
        # RFC-0004 §6.4: shape (array, 1..20 parts) is checked once, in
        # `SessionLog.submit`, so a direct caller and the RPC surface answer the
        # same `data.field`. Only the identity of the submitting device is
        # resolved here — it is what §6.4 step 4 compares `file_id` ownership to.
        turn, pos = await self.log.submit(
            session_id,
            content,  # type: ignore[arg-type]
            client_msg_id,
            device_id=getattr(connection, "device_id", None) or None,
        )
        return {"turn_id": turn.id, "queue_pos": pos}

    async def ui_interrupt(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id, turn_id = self._ids(params)
        if not await self.log.interrupt(session_id, turn_id, by=self._who(connection)):
            raise VadeError("NOT_FOUND", "no running turn with that id")
        return {"ok": True}

    async def ui_queue_cancel(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id, turn_id = self._ids(params)
        if not await self.log.cancel(session_id, turn_id, by=self._who(connection)):
            raise VadeError("NOT_FOUND", "no queued turn with that id")
        return {"ok": True}

    # ------------------------------------------------------------------ approval
    async def approval_decide(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        auth_mode = getattr(connection, "auth_mode", "browser_session")
        if auth_mode not in ("device_key", "admin_local"):
            raise VadeError("APPROVER_INVALID", "browser_session cannot approve high-risk calls")
        request_id = params.get("request_id")
        decision = params.get("decision")
        scope = params.get("scope", "once")
        if not isinstance(request_id, str) or decision not in ("approve", "deny"):
            raise VadeError("INVALID_ARGUMENT", "request_id and decision are required")
        if scope not in ("once", "session"):
            raise VadeError("INVALID_ARGUMENT", "scope must be once|session")
        port = self._approval()
        if port is None or not hasattr(port, "decide"):
            raise VadeError("NOT_FOUND", "no approval port is installed")
        # Map the authenticated connection to an approver kind explicitly: the
        # port's default is fail-closed, so relying on it here would refuse a
        # legitimate device. The two modes are exactly ALLOWED_APPROVER_KINDS.
        actor_kind = "device_key" if auth_mode == "device_key" else "loopback_admin"
        resolved = port.decide(request_id, decision, scope,
                               by=getattr(connection, "device_id", None) or "admin",
                               actor_kind=actor_kind)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        ok = True
        if isinstance(resolved, tuple) and len(resolved) == 2:
            ok, resolved = resolved
        session_id = getattr(resolved, "session_id", None)
        turn_id = getattr(resolved, "turn_id", None)
        attribution: dict[str, Any] = {}
        if isinstance(resolved, Mapping):
            # RFC-0007 §7.1: `decide()` returns the attribution it resolved, so
            # the event that closes the dialog names the same task the prompt did.
            for key in ("task_id", "task_name", "depth"):
                if key in resolved:
                    attribution[key] = resolved[key]
        if session_id and turn_id:
            # Always close the dialog on every attached UI, even on a failed
            # first-wins race — otherwise the other devices spin forever.
            await self.log.emit(session_id, "turn.approval_resolved", {
                "turn_id": turn_id, "request_id": request_id, "decision": decision,
                "by": getattr(connection, "device_id", None) or "admin",
                "scope": scope, "state": "running", **attribution,
            })
        return {"ok": bool(ok)}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _ids(params: Mapping[str, Any]) -> tuple[str, str]:
        session_id, turn_id = params.get("session_id"), params.get("turn_id")
        if not isinstance(session_id, str) or not isinstance(turn_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id and turn_id are required")
        return session_id, turn_id

    @staticmethod
    def _who(connection: Any) -> str:
        return str(getattr(connection, "device_id", None) or getattr(connection, "connection_id", "user"))
