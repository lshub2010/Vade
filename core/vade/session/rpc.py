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
from .search import LIMIT_DEFAULT, LIMIT_MAX
from .store import UNSET

__all__ = ["SessionRPC", "ConnectionSink", "WRITE_METHODS", "CSRF_PARAM"]

#: Every method that changes durable state or drives a turn. Two gates read
#: this table (the read-only refusal and the CSRF requirement), so a new write
#: verb has ONE place to declare itself instead of two checks to remember.
#:
#: `ui/attach` is deliberately absent: registering a sink is a read. It does
#: create a session when `session_id` is null, and that branch carries its own
#: gate (`_require_writable`) — refusing attach outright would lock an
#: explicitly read-only viewer out of the history it is allowed to watch.
WRITE_METHODS: frozenset[str] = frozenset({
    "ui/submit",
    "ui/interrupt",
    "ui/queue.cancel",
    "approval/decide",
    "session/model.set",
    "session/rename",
    "session/pin",
    "session/archive",
    "session/delete",
    "session/group.create",
    "session/group.update",
    "session/group.delete",
    "session/group.assign",
    "turn/regenerate",
    "message/edit_resubmit",
    "turn/input",
    "turn/input.cancel",
})

#: Where a write frame carries its double-submit token: `params.csrf`. A
#: WebSocket frame has no headers, so the header form (`x-vade-csrf`) the HTTP
#: routes use has no analogue here; the token is popped before any handler
#: sees it, so no method schema has to know about it.
CSRF_PARAM = "csrf"

#: The two credential classes allowed to resolve a high-risk approval
#: (FR-8.53 / AC-30). Everything else — `browser_session`, `module_key`, and
#: `None` (nothing verified yet) — is `APPROVER_INVALID`, never `INTERNAL`.
APPROVER_MODES: dict[str, str] = {
    "device_key": "device_key",
    "admin_local": "loopback_admin",
    "loopback_admin": "loopback_admin",
}


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
    def __init__(self, log: SessionLog, approval: Callable[[], Any] | None = None,
                 input_port: Callable[[], Any] | None = None) -> None:
        self.log = log
        self._approval = approval or (lambda: None)
        #: RFC-0008: resolved lazily for the same reason `approval` is — the
        #: ask-user component may install before or after the session component.
        self._input = input_port or (lambda: getattr(log, "input_port", None))
        self._sinks: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ dispatch
    async def dispatch(self, connection: Any, method: str, params: Mapping[str, Any] | None) -> Any:
        handler = self.handlers().get(method)
        if handler is None:
            return RpcError("NOT_FOUND", f"unknown method {method!r}")
        try:
            arguments = dict(params or {})
            if method in WRITE_METHODS:
                # Order matters and is the one C-04 §6.4 names: "may this
                # credential write at all" (read-only) before "did it prove the
                # page it comes from" (CSRF). A read-only session that also
                # forgot its token must be told it is read-only — that is the
                # actionable fact, and the one the UI has a banner for.
                self._require_writable(connection)
                self._require_csrf(connection, arguments)
            return await handler(connection, arguments)
        except VadeError as exc:
            # `data` is part of C-04's envelope (`error.data.field`); dropping
            # it here used to erase every field pointer between the check and
            # the client (§6.4 / G7). RFC-0006 §8 rides on the same key:
            # PAYLOAD_TOO_LARGE carries {field, limit, actual, unit} verbatim.
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
            # RFC-0005 (C-05 append): read-only model surface + usage views.
            "model/list": self.model_list,
            "session/model.get": self.session_model_get,
            "session/model.set": self.session_model_set,
            "session/usage.get": self.session_usage_get,
            # RFC-0006 §6.2/§6.3 (C-05 1.1.0). `session/fork` is deliberately
            # absent: an unregistered method answers C-04 NOT_FOUND with an
            # `unknown method` message (not JSON-RPC -32601, the standard form
            # clients should also recognize), which is more honest than
            # defining it and returning UNSUPPORTED.
            "turn/regenerate": self.turn_regenerate,
            "message/edit_resubmit": self.message_edit_resubmit,
            "turn/input": self.turn_input,
            "turn/input.cancel": self.turn_input_cancel,
            # RFC-0010 §5 (C-05 1.3.0): session metadata — list / verbs / groups.
            "session/list": self.session_list,
            "session/rename": self.session_rename,
            "session/pin": self.session_pin,
            "session/archive": self.session_archive,
            "session/delete": self.session_delete,
            "session/group.list": self.session_group_list,
            "session/group.create": self.session_group_create,
            "session/group.update": self.session_group_update,
            "session/group.delete": self.session_group_delete,
            "session/group.assign": self.session_group_assign,
            # RFC-0014 §5 (C-05 1.5.0): cross-session search — titles + bodies.
            "session/search": self.session_search,
        }

    # ------------------------------------------------------------------ ui/*
    async def ui_attach(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        last_seq = params.get("last_seq")
        if session_id is not None and not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id must be a string or null")
        if last_seq is not None and (not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0):
            raise VadeError("INVALID_ARGUMENT", "last_seq must be a non-negative integer or null")
        connection_key = self._connection_key(connection)
        if session_id is None:
            # `ui/attach` is a read (it registers a sink), but the null branch
            # CREATES a session. An explicitly read-only viewer may watch; it
            # may not mint rows, and a cookie socket must still prove the page
            # it comes from.
            self._require_writable(connection)
            self._require_csrf(connection, dict(params))
        if session_id is not None:
            # A second attach for the same (connection, session) used to leave
            # the first SinkWorker registered: it kept delivering every event
            # to the same socket (double delivery — the re-attach's own
            # `session.attached` was the cheapest witness) and nothing ever
            # unregistered it (`ui/detach` only knew the newest id), so one
            # worker leaked per re-attach for the connection's whole lifetime.
            # Retire BEFORE `log.attach` registers the replacement: the pair
            # then holds exactly one worker, and the replay/snapshot below is
            # the only source of any given seq.
            self._retire_sink(connection_key, session_id)
        result = await self.log.attach(session_id, last_seq, ConnectionSink(connection))
        self._sinks[(connection_key, result.session_id)] = result.sink_id
        return {"session_id": result.session_id, "from_seq": result.from_seq,
                "snapshot": result.snapshot}

    async def ui_detach(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id is required")
        key = (self._connection_key(connection), session_id)
        sink_id = self._sinks.pop(key, None)
        if sink_id is None:
            raise VadeError("NOT_FOUND", "this connection is not attached to that session")
        return {"ok": await self.log.detach(session_id, sink_id)}

    def _retire_sink(self, connection_key: str, session_id: str) -> None:
        """Unregister and discard the sink this connection has for a session.

        `drop()` and not `SessionLog.detach()`: detach writes its stop sentinel
        BEHIND whatever is still queued, so the worker first flushes frames
        that the attach about to run is going to replay — the client would see
        those seqs twice. Dropping cancels the writer and discards that
        backlog, which the new attach's replay/snapshot then covers exactly
        once. The id leaves `SessionRPC._sinks` first, so the two maps cannot
        disagree about who is attached.
        """
        sink_id = self._sinks.pop((connection_key, session_id), None)
        if sink_id is None:
            return
        workers = getattr(self.log, "_sinks", None)
        live = workers.get(session_id) if isinstance(workers, dict) else None
        worker = live.get(sink_id) if isinstance(live, dict) else None
        if worker is not None:
            worker.drop()

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
        if self._read_only(connection):
            # B3: `rpc.json` has declared READ_ONLY_SESSION for `ui/submit` since
            # C-05, and this handler — the ONLY write path a message takes —
            # never checked it. A read-only viewer could submit a turn, including
            # through a page's `ui/submit` action, which delegates here.
            raise VadeError(
                "READ_ONLY_SESSION",
                "this session is read-only; submitting a message requires a writable session",
            )
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

    # ------------------------------------------------------------------ versions
    async def turn_regenerate(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """RFC-0006 §6.2: new turn for the same user message, old one superseded."""
        session_id, turn_id = self._ids(params)
        client_msg_id = params.get("client_msg_id")
        if not isinstance(client_msg_id, str) or not client_msg_id:
            raise VadeError("INVALID_ARGUMENT", "client_msg_id is required")
        if self._read_only(connection):
            # A regenerate appends a turn exactly like `ui/submit`, so it is a
            # write path: `rpc.json` has declared READ_ONLY_SESSION for it since
            # C-05, and leaving it unchecked let a read-only viewer write by
            # pointing at an old turn instead of submitting a message.
            raise VadeError(
                "READ_ONLY_SESSION",
                "this session is read-only; regenerating a turn requires a writable session",
            )
        return await self.log.regenerate(session_id, turn_id, client_msg_id)

    async def message_edit_resubmit(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """RFC-0006 §6.3: append a new user message, point the old one at it."""
        session_id = params.get("session_id")
        message_id = params.get("message_id")
        content = params.get("content")
        client_msg_id = params.get("client_msg_id")
        if not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id is required")
        if self._read_only(connection):
            # §6.4's validation order is explicit: write permission (①) is
            # checked before the content shape (②..⑤). An edit creates a new
            # turn, so a read-only connection must be refused here too — it is
            # the other half of the `ui/submit` gate above.
            raise VadeError(
                "READ_ONLY_SESSION",
                "this session is read-only; editing a message requires a writable session",
            )
        if not isinstance(message_id, str) or not message_id:
            raise VadeError("INVALID_ARGUMENT", "message_id is required")
        if not isinstance(content, list):
            raise VadeError("INVALID_ARGUMENT", "content must be an array of blocks")
        if not isinstance(client_msg_id, str) or not client_msg_id:
            raise VadeError("INVALID_ARGUMENT", "client_msg_id is required")
        return await self.log.edit_resubmit(session_id, message_id, content, client_msg_id)

    # ------------------------------------------------------------------ approval
    async def approval_decide(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve one pending approval — only for a principal that may approve.

        The principal comes from the credential the TRANSPORT verified
        (`connection.auth_mode`), never from a default. `getattr(connection,
        "auth_mode", "browser_session")` was the dead end: `Connection` had no
        such attribute, so the default always won and every caller — including
        the device key FR-8.53 designates as an approver — got
        `APPROVER_INVALID`. An unset mode is a refusal too, but it is an
        explicit 403 (`APPROVER_INVALID`), never an `INTERNAL` 500 and never a
        silent promotion to a privileged mode.
        """
        auth_mode = getattr(connection, "auth_mode", None)
        actor_kind = APPROVER_MODES.get(str(auth_mode)) if auth_mode else None
        if actor_kind is None:
            raise VadeError(
                "APPROVER_INVALID",
                f"{auth_mode or 'unverified'} cannot approve high-risk calls; "
                "use the device key or a loopback admin",
                data={"auth_mode": auth_mode},
            )
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
        # The actor is the identity the credential proved: `device_id` for a
        # paired key, `device_key` (the fingerprint) when the whitelist could
        # not name the pairing — both are facts about the socket, and an empty
        # `by` would make the audit row say nobody approved.
        by = (getattr(connection, "device_id", None)
              or getattr(connection, "device_key", None)
              or "admin")
        resolved = port.decide(request_id, decision, scope, by=str(by), actor_kind=actor_kind)

        if inspect.isawaitable(resolved):
            resolved = await resolved
        ok = True
        if isinstance(resolved, tuple) and len(resolved) == 2:
            ok = bool(resolved[0])
        # No `turn.approval_resolved` here. `ApprovalManager.decide` is the
        # single emitter for an allowed request — it writes through the loop's
        # turn guard, so a decision can never land after the terminal event.
        # Emitting on this side too closed every dialog twice, with two
        # different decision strings (`allow` here, `approve` there: only
        # `approve|deny` is in schema/events.json), and it also published a
        # page approval's resolution as a `turn.*` event the RFC-0009 §9.4
        # synchronous RPC deliberately does not have.
        return {"ok": bool(ok)}

    # ------------------------------------------------------------------ models
    async def model_list(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """RFC-0005 §4: what the current mode can actually route to."""
        resolver = getattr(self.log, "models", None)
        if resolver is None:
            raise VadeError("UNAVAILABLE", "the model catalog is not wired")
        if_revision = params.get("if_revision")
        if if_revision is not None and (not isinstance(if_revision, int) or isinstance(if_revision, bool)):
            raise VadeError("INVALID_ARGUMENT", "if_revision must be an integer or null")
        catalog = resolver.catalog()
        if if_revision is not None and if_revision == catalog.revision:
            # §4.4: the client reuses its cache; it must NOT clear the badges.
            return {"revision": catalog.revision, "not_modified": True, "models": None}
        default = resolver.resolve(None, catalog)
        return {
            "revision": catalog.revision,
            "not_modified": False,
            "models": catalog.to_wire(),
            "default": {"model_id": default.model_id, "source": default.source,
                        "resolved": default.resolved},
        }

    async def session_model_get(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id is required")
        selection = self.log.resolve_model(session_id)
        if selection is None:
            raise VadeError("UNAVAILABLE", "the model catalog is not wired")
        return selection.to_wire()

    async def session_model_set(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id is required")
        if "model_id" not in params:
            raise VadeError("INVALID_ARGUMENT", "model_id is required (null clears the override)")
        model_id = params.get("model_id")
        if model_id is not None and not isinstance(model_id, str):
            raise VadeError("INVALID_ARGUMENT", "model_id must be a string or null")
        resolver = getattr(self.log, "models", None)
        if resolver is None:
            raise VadeError("UNAVAILABLE", "the model catalog is not wired")
        session = self.log.get_session(session_id)
        if session is None:
            raise VadeError("NOT_FOUND", f"session {session_id!r} does not exist")
        if model_id is not None:
            if session.state != "open":
                raise VadeError("READ_ONLY_SESSION", "session is closed; its model cannot be changed")
            if "/" not in model_id or model_id.startswith("/") or model_id.endswith("/"):
                raise VadeError("INVALID_ARGUMENT", "model_id must be <provider>/<model>")
            entry = resolver.catalog().get(model_id)
            if entry is None:
                raise VadeError("NOT_FOUND", f"unknown model {model_id!r}",
                                data={"reason": "unknown_model"})
            if not entry.available:
                raise VadeError("UNAVAILABLE", f"model {model_id!r} has no healthy instance",
                                data={"reason": "model_unavailable"})
        selection = self.log.set_session_model(session_id, model_id)
        return {"model_id": selection.model_id, "source": selection.source,
                "effective_at": "next_turn"}

    async def session_usage_get(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id is required")
        session = self.log.get_session(session_id)
        if session is None:
            raise VadeError("NOT_FOUND", f"session {session_id!r} does not exist")
        accounting = getattr(self.log, "usage", None)
        if accounting is None:
            raise VadeError("UNAVAILABLE", "usage accounting is not wired")
        return accounting.report(session)
    # ------------------------------------------------------------------ input
    async def turn_input(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """`turn/input`: answer one pending question (RFC-0008 §7.1).

        `input_id` is the idempotency key and is core-issued; a value that fails
        validation leaves the turn in `waiting_input` and emits nothing (§4.4).
        """
        session_id, input_id = self._input_ids(connection, params)
        if "value" not in params:
            raise VadeError("INVALID_ARGUMENT", "value is required",
                            data={"field": "value"})
        port: Any = self._input()
        submit: Any = getattr(port, "submit", None) if port is not None else None
        if not callable(submit):
            raise VadeError("NOT_FOUND", "no ask-user channel is installed")
        result: Any = submit(session_id=session_id, input_id=input_id,
                             value=params.get("value"), by=self._who(connection))
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "turn_id": result["turn_id"], "status": result["status"],
                "value": result.get("value"), "duplicate": bool(result.get("duplicate"))}

    async def turn_input_cancel(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """`turn/input.cancel`: close one dialog, keep the turn running (§7.2)."""
        session_id, input_id = self._input_ids(connection, params)
        reason = params.get("reason", "user_cancel")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 200):
            raise VadeError("INVALID_ARGUMENT", "reason must be a string of at most 200 characters",
                            data={"field": "reason"})
        port: Any = self._input()
        cancel: Any = getattr(port, "cancel", None) if port is not None else None
        if not callable(cancel):
            raise VadeError("NOT_FOUND", "no ask-user channel is installed")
        result: Any = cancel(session_id=session_id, input_id=input_id,
                             by=self._who(connection), reason=reason or "user_cancel")
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "turn_id": result["turn_id"], "status": result["status"]}

    # ------------------------------------------------------------------ RFC-0010 session metadata
    async def session_list(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """`session/list`: one light page in FR-8.26 order (§5)."""
        limit = params.get("limit", 50)
        cursor = params.get("cursor", 0)
        archived = params.get("archived", False)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise VadeError("INVALID_ARGUMENT", "limit must be an integer in 1..200",
                            data={"field": "limit"})
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise VadeError("INVALID_ARGUMENT", "cursor must be a non-negative integer",
                            data={"field": "cursor"})
        if not isinstance(archived, bool):
            raise VadeError("INVALID_ARGUMENT", "archived must be a boolean",
                            data={"field": "archived"})
        group_id: Any = UNSET
        if "group_id" in params:
            group_id = params.get("group_id")
            if group_id is not None and not isinstance(group_id, str):
                raise VadeError("INVALID_ARGUMENT",
                                "group_id must be a string, null, or omitted",
                                data={"field": "group_id"})
        return self.log.list_sessions_page(limit=limit, cursor=cursor,
                                           group_id=group_id, archived=archived)

    async def session_rename(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise VadeError("INVALID_ARGUMENT", "session_id is required",
                            data={"field": "session_id"})
        if "title" not in params:
            raise VadeError("INVALID_ARGUMENT", "title is required",
                            data={"field": "title"})
        session = await self.log.rename_session(session_id, params.get("title"),
                                                by=self._who(connection))
        return {"ok": True, "session": session.to_wire()}

    async def session_pin(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise VadeError("INVALID_ARGUMENT", "session_id is required",
                            data={"field": "session_id"})
        if "pinned" not in params:
            raise VadeError("INVALID_ARGUMENT", "pinned is required",
                            data={"field": "pinned"})
        session = await self.log.set_pinned(session_id, params.get("pinned"),
                                            by=self._who(connection))
        return {"ok": True, "pinned_at": session.pinned_at}

    async def session_archive(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise VadeError("INVALID_ARGUMENT", "session_id is required",
                            data={"field": "session_id"})
        if "archived" not in params:
            raise VadeError("INVALID_ARGUMENT", "archived is required",
                            data={"field": "archived"})
        session = await self.log.set_archived(session_id, params.get("archived"),
                                              by=self._who(connection))
        return {"ok": True, "archived_at": session.archived_at}

    async def session_delete(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """§8.2: the double-confirm dialog already happened client-side."""
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise VadeError("INVALID_ARGUMENT", "session_id is required",
                            data={"field": "session_id"})
        await self.log.delete_session(session_id, by=self._who(connection))
        return {"ok": True}

    async def session_group_list(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"groups": self.log.list_groups()}

    async def session_group_create(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        if "parent_id" in params:
            # §7.3: one level only — the data model has nowhere to hang a parent.
            raise VadeError("INVALID_ARGUMENT", "groups cannot be nested",
                            data={"field": "parent_id", "reason": "nesting_forbidden"})
        if "name" not in params:
            raise VadeError("INVALID_ARGUMENT", "name is required",
                            data={"field": "name"})
        group = self.log.create_group(
            params.get("name"),
            icon=params.get("icon"),
            color=params.get("color"),
            sort_order=params.get("sort_order", 0),
        )
        return {"group": group.to_wire()}

    async def session_group_update(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        group_id = params.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            raise VadeError("INVALID_ARGUMENT", "group_id is required",
                            data={"field": "group_id"})
        fields = {key: params[key] for key in ("name", "icon", "color", "sort_order")
                  if key in params}
        group = self.log.update_group(group_id, fields)
        return {"group": group.to_wire()}

    async def session_group_delete(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """§8.1: members are unassigned (the count says how many) — never deleted."""
        group_id = params.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            raise VadeError("INVALID_ARGUMENT", "group_id is required",
                            data={"field": "group_id"})
        unassigned = self.log.delete_group(group_id)
        return {"ok": True, "unassigned": unassigned}

    async def session_group_assign(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise VadeError("INVALID_ARGUMENT", "session_id is required",
                            data={"field": "session_id"})
        if "group_id" not in params:
            raise VadeError("INVALID_ARGUMENT", "group_id is required (null = ungroup)",
                            data={"field": "group_id"})
        group_id = params.get("group_id")
        await self.log.assign_group(session_id, group_id, by=self._who(connection))
        return {"ok": True, "group_id": group_id}

    # ------------------------------------------------------------------ RFC-0014 session search
    async def session_search(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        """`session/search`: parameterized LIKE over titles + live bodies (§4).

        The `limit` cap is enforced HERE and nowhere else, deliberately: it is
        the single point the contract names, and a second copy in the manager
        would let a future edit delete one and leave the mutation tests green
        (RFC-0014 §8 mutation ②).
        """
        query = params.get("query")
        if not isinstance(query, str):
            raise VadeError("INVALID_ARGUMENT", "query is required and must be a string",
                            data={"field": "query"})
        limit = params.get("limit", LIMIT_DEFAULT)
        offset = params.get("offset", 0)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= LIMIT_MAX:
            raise VadeError("INVALID_ARGUMENT", f"limit must be an integer in 1..{LIMIT_MAX}",
                            data={"field": "limit"})
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise VadeError("INVALID_ARGUMENT", "offset must be a non-negative integer",
                            data={"field": "offset"})
        session_id = params.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise VadeError("INVALID_ARGUMENT", "session_id must be a non-empty string or null",
                            data={"field": "session_id"})
        # Read verb: a read_only connection searches exactly like it lists and
        # attaches (§4.8) — no READ_ONLY_SESSION refusal here.
        return self.log.search(query, limit=limit, offset=offset, session_id=session_id)

    def _input_ids(self, connection: Any, params: Mapping[str, Any]) -> tuple[str, str]:
        """Shared guard: writable session, both ids present (§7.4)."""
        session_id, input_id = params.get("session_id"), params.get("input_id")
        if not isinstance(session_id, str) or not session_id:
            raise VadeError("INVALID_ARGUMENT", "session_id is required",
                            data={"field": "session_id"})
        if not isinstance(input_id, str) or not input_id:
            raise VadeError("INVALID_ARGUMENT", "input_id is required",
                            data={"field": "input_id"})
        if self._read_only(connection):
            # AMEND-22 #14: only an EXPLICITLY read-only session is refused; a
            # cookie session is writable (D100) and may answer like anyone else.
            raise VadeError("READ_ONLY_SESSION",
                            "this session is read-only; answering requires a writable session")
        return session_id, input_id

    @staticmethod
    def _read_only(connection: Any) -> bool:
        if bool(getattr(connection, "read_only", False)):
            return True
        principal = getattr(connection, "principal", None)
        return bool(getattr(principal, "read_only", False))

    @classmethod
    def _require_writable(cls, connection: Any) -> None:
        """`READ_ONLY_SESSION` for every verb in `WRITE_METHODS`.

        The flag reaches here from the transport (`server._ws` copies the
        session row's `read_only` onto the socket), so this is the server-side
        half of FR-8.75: hiding the send button is presentation, refusing the
        frame is the control.
        """
        if not cls._read_only(connection):
            return
        raise VadeError(
            "READ_ONLY_SESSION",
            "this session is read-only; the action requires a writable session",
            data={"reason": "read_only"},
        )

    @staticmethod
    def _require_csrf(connection: Any, params: dict[str, Any]) -> None:
        """Double-submit CSRF for write frames on a cookie-authenticated socket.

        `params.csrf` is the token the page read from `__Host-ac_csrf`; the
        verdict is cached on the `Connection` (`accepts_csrf`), so a page pays
        for the hash once per socket. The token is popped before the handler
        runs: no method schema grows a credential field.

        Key-authenticated sockets are exempt — there is no ambient credential
        for a hostile page to borrow (see `Connection.csrf_required`). Fakes in
        unit tests have no `csrf_required` at all, which reads as "not a cookie
        socket" and keeps those tests about what they are testing.
        """
        token = params.pop(CSRF_PARAM, None)
        if not bool(getattr(connection, "csrf_required", False)):
            return
        accepts = getattr(connection, "accepts_csrf", None)
        if callable(accepts) and accepts(token):
            return
        raise VadeError(
            "ORIGIN_MISMATCH",
            "this write needs the session's CSRF token (params.csrf)",
            data={"reason": "csrf"},
        )


    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _connection_key(connection: Any) -> str:
        """The key `_sinks` is scoped by: one connection, not one device."""
        return str(getattr(connection, "connection_id", ""))

    @staticmethod
    def _ids(params: Mapping[str, Any]) -> tuple[str, str]:
        session_id, turn_id = params.get("session_id"), params.get("turn_id")
        if not isinstance(session_id, str) or not isinstance(turn_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id and turn_id are required")
        return session_id, turn_id

    @staticmethod
    def _who(connection: Any) -> str:
        return str(getattr(connection, "device_id", None) or getattr(connection, "connection_id", "user"))
