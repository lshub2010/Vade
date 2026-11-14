"""Core-side serving of C-19 pages (RFC-0009 §7 / §9.3).

The service is the only place that turns a module's declaration into something a
client can render, and the only place a page action becomes a real RPC:

* `page(...)`   — inline pages are re-validated and returned; `ref` pages are
                  fetched from the module, checked against the sha256 pinned in
                  the manifest and re-validated. Any doubt → `MANIFEST_INVALID`.
* `state(...)`  — read-only proxy for `state/get`, cached for at most
                  `[ui].state_cache_s`, `rev` monotonic.
* `tool_call(...)` — two stages (RFC §6.2): mint an approval bound to the args
                  digest, then execute through the SAME router path as
                  `tools/call` once `approval/decide` allowed it.
* `route_action(...)` — the four kinds: `tools/call` and `ui/submit` go through
                  the existing RPC surfaces; `navigate` and `open_url` are
                  resolved and returned for the host to perform (no WebView, no
                  in-page URL ever).

Nothing here executes module code, evaluates an expression or loads a remote
resource. A page is data; the client renders it with native components.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..errors import VadeError
from ..loop.ports import ApprovalRequest
from ..registry.uipage import (
    MAX_MODULE_CACHE_BYTES,
    MAX_PAGE_CACHE_BYTES,
    MAX_REF_BYTES,
    UIPage,
    UIPageError,
    check_navigate,
    check_open_url,
    check_state,
    page_digest,
    serialize,
    validate_page,
)
from ..router.dispatch import (
    MAX_TOOL_CALL_PLAINTEXT,
    Target,
    digest_args,
    validate_arguments,
)
from .auth import (
    Caller,
    authorize_target,
    device_of,
    is_read_only,
    require_visible,
)

__all__ = ["ModulePageService", "PageLimits", "PAGE_GET_DEADLINE_MS", "STATE_GET_DEADLINE_MS"]

PAGE_GET_DEADLINE_MS = 5_000
STATE_GET_DEADLINE_MS = 5_000


@dataclass(frozen=True, slots=True)
class PageLimits:
    """Serving limits from `[ui]` (C-18); defaults mirror the schema."""

    state_cache_s: int = 5
    page_cache_bytes: int = MAX_PAGE_CACHE_BYTES
    module_cache_bytes: int = MAX_MODULE_CACHE_BYTES
    pending_calls: int = 256

    @classmethod
    def from_config(cls, cfg: Any) -> "PageLimits":
        if cfg is None:
            return cls()
        def _int(key: str, default: int) -> int:
            value = cfg.get(key, default)
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        return cls(
            state_cache_s=_int("ui.state_cache_s", 5),
            page_cache_bytes=_int("ui.page_cache_bytes", MAX_PAGE_CACHE_BYTES),
            module_cache_bytes=_int("ui.module_cache_bytes", MAX_MODULE_CACHE_BYTES),
            pending_calls=_int("ui.pending_calls", 256),
        )


@dataclass(slots=True)
class _CachedState:
    rev: int
    state: Mapping[str, Any]
    fetched_mono: float
    fetched_at: int


@dataclass(slots=True)
class _PendingCall:
    """Stage-one record for `ui/tool.call`. Holds the args digest, never a token.

    `session_id` and `approver_device_id` are the binding B2 was missing: an
    approval is a statement by one device about one session, and stage two must
    be the same device in the same session. Comparing only the args digest let
    `session-ALICE` approve a call that then executed as `session-BOB`, with the
    module receiving `session_id=session-BOB` (the value `_execute` forwards).
    """

    call_id: str
    module_id: str
    instance_id: str
    page_id: str
    tool: str
    args: Mapping[str, Any]
    digest: str
    request_id: str
    expires_at: float          # wall clock, from the approval
    created_mono: float
    session_id: str = ""
    approver_device_id: str = ""


class ModulePageService:
    def __init__(
        self,
        app: Any,
        *,
        limits: PageLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        # `app` (not the components) because install order is alphabetical:
        # `modulepage` runs before `policy` and `session`, so the approval port
        # and the router do not exist yet. They are resolved per call.
        self._app = app
        self.limits = limits or PageLimits()
        self._clock = clock
        self._wall = wall_clock
        self._state_cache: dict[tuple[str, str, str], _CachedState] = {}
        #: B5: one lock per page key. `get_state` is two awaits long (module
        #: round trip), so "compare then write" is not atomic on its own: a slow
        #: rev=3 answer could land after a fast rev=9 one and hang the OLD state
        #: on a NEWER rev. The lock makes compare-and-swap atomic per key and
        #: keeps unrelated pages from queueing behind each other.
        self._state_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        #: S3: the ref cache is keyed by the digest pinned in the manifest, so a
        #: `capabilities.update` that changes the body changes the key and the
        #: stale body can never be served (§10.3 anti-substitution).
        self._page_cache: dict[tuple[str, str, str, str], tuple[Mapping[str, Any], int]] = {}
        self._calls: dict[str, _PendingCall] = {}

    # --------------------------------------------------------------- components
    def _registry(self) -> Any:
        return getattr(self._app, "registry", None)

    def _router(self) -> Any:
        return getattr(self._app, "router", None)

    def _caller(self) -> Any:
        return getattr(self._app, "module_caller", None)

    def _approval(self) -> Any:
        port = getattr(self._app, "approval_port", None)
        return port

    def _audit(self, event: Mapping[str, Any]) -> None:
        writer = getattr(self._app, "audit", None)
        record = getattr(writer, "record", None)
        if not callable(record):
            return
        try:
            record(dict(event))
        except Exception:  # auditing must never break the call path
            pass

    # ------------------------------------------------------------------ lookups
    def _record(self, module_id: str, instance_id: str) -> Any:
        registry = self._registry()
        if registry is None:
            raise VadeError("UNAVAILABLE", "the registry is not installed")
        for record in registry.snapshot():
            if record.module_id == module_id and record.instance_id == instance_id:
                if record.manifest is None:
                    raise VadeError("NOT_FOUND", f"{module_id}#{instance_id} is registered without a manifest")
                return record
        raise VadeError("NOT_FOUND", f"no registered instance {module_id}#{instance_id}")

    def _page(self, record: Any, page_id: str) -> UIPage:
        for page in record.manifest.ui_pages:
            if page.page_id == page_id:
                return page
        # A page the registration dropped is indistinguishable from one that was
        # never declared: its entry does not exist (§7.4). No fallback card.
        raise VadeError(
            "NOT_FOUND",
            f"{record.module_id}#{record.instance_id} has no page {page_id!r}",
        )

    def pages(self, module_id: str, instance_id: str) -> tuple[dict[str, Any], ...]:
        """Entry list for one module. `()` when it declared no (valid) pages.

        FR-8.79's criterion lives here: an empty tuple is what makes the desktop
        panel and the phone "capabilities" tab show **nothing** — no empty tab,
        no greyed row (§8.2).
        """
        record = self._record(module_id, instance_id)
        return tuple(
            {
                "id": page.page_id,
                "title": page.title,
                "icon": page.icon,
                "entry": page.entry,
                "version": page.version,
                "requires": ["session"] if page.requires_session else [],
                "refresh_s": page.refresh_s,
            }
            for page in record.manifest.ui_pages
        )

    # -------------------------------------------------------------- module calls
    async def _module_rpc(
        self, record: Any, method: str, params: Mapping[str, Any], deadline_ms: int
    ) -> Mapping[str, Any]:
        caller = self._caller()
        if caller is None:
            raise VadeError("UNSUPPORTED", f"{method} is unavailable: core has no module caller")
        target = Target(
            module_id=record.module_id,
            instance_id=record.instance_id,
            connection_id=record.connection_id,
            tool=method,
            timeout_ms=deadline_ms,
        )
        try:
            reply = await asyncio.wait_for(
                caller.call(target, method, dict(params), deadline_ms),
                timeout=deadline_ms / 1000.0 + 1.0,
            )
        except asyncio.TimeoutError:
            raise VadeError("TIMEOUT", f"{record.module_id} did not answer {method} in {deadline_ms} ms") from None
        except VadeError as exc:
            if exc.code in ("NOT_FOUND", "UNSUPPORTED"):
                # §9.3: a module that does not implement the optional method is
                # UNSUPPORTED; the client shows a banner, not an error modal.
                raise VadeError("UNSUPPORTED", f"{record.module_id} does not implement {method}") from None
            raise
        if not isinstance(reply, Mapping):
            raise VadeError("UNSUPPORTED", f"{record.module_id} answered {method} with a non-object")
        return reply

    # ------------------------------------------------------------------ page/get
    async def get_page(
        self, connection: Any, module_id: str, instance_id: str, page_id: str
    ) -> dict[str, Any]:
        """Serve one page to `connection` (§9.3).

        The connection decides *whether* this caller may see the page; the
        params only say *which* page. See `modulepage/auth.py`.
        """
        caller = authorize_target(connection, module_id, instance_id)
        record = self._record(module_id, instance_id)
        require_visible(caller, record)
        page = self._page(record, page_id)
        if page.is_ref:
            body, digest = await self._ref_body(record, page)
        else:
            body = dict(page.schema)
            digest = page_digest(body)
        self._revalidate(record, page, body)
        return {"schema": body, "sha256": digest}

    async def _ref_body(self, record: Any, page: UIPage) -> tuple[Mapping[str, Any], str]:
        declared = page.ref[1] if page.ref is not None else ""
        # S3: the digest is part of the key. A `capabilities.update` that pins a
        # new body changes `declared`, so the old entry is unreachable instead of
        # being re-served for the rest of the process lifetime.
        key = (record.module_id, record.instance_id, page.page_id, declared)
        cached = self._page_cache.get(key)
        if cached is not None:
            return dict(cached[0]), declared

        reply = await self._module_rpc(
            record, "page/get", {"page_id": page.page_id}, PAGE_GET_DEADLINE_MS
        )
        body = reply.get("schema")
        if not isinstance(body, Mapping):
            raise VadeError("MANIFEST_INVALID", f"{page.pointer}/schema: page/get returned no schema object")
        size = len(serialize(body))
        ceiling = min(MAX_REF_BYTES, self.limits.page_cache_bytes)
        if size > ceiling:
            # §7.3: never truncate — truncation would disguise a failed check as
            # a working page.
            raise VadeError(
                "MANIFEST_INVALID",
                f"{page.pointer}/schema: ref body is {size} bytes > {ceiling}",
            )
        digest = page_digest(body)
        if digest != declared:
            # The manifest pinned the digest; a module cannot swap the body after
            # registration (§10.3).
            raise VadeError(
                "MANIFEST_INVALID",
                f"{page.pointer}/schema: sha256 mismatch (manifest {declared[:12]}…, body {digest[:12]}…)",
            )
        self._trim_page_cache(record.module_id, size)
        self._page_cache[key] = (dict(body), size)
        return dict(body), digest

    def _trim_page_cache(self, module_id: str, incoming: int) -> None:
        total = sum(size for (mod, _i, _p, _d), (_b, size) in self._page_cache.items() if mod == module_id)
        if total + incoming <= self.limits.module_cache_bytes:
            return
        for key in [k for k in self._page_cache if k[0] == module_id]:
            _body, size = self._page_cache.pop(key)
            total -= size
            if total + incoming <= self.limits.module_cache_bytes:
                return

    def _revalidate(self, record: Any, page: UIPage, body: Mapping[str, Any]) -> None:
        """Render-time check: the bytes we hand the client must pass C-19 now.

        Registration checked the declaration; this checks the body actually being
        returned (a ref page arrives later, and a client may be older than core).
        Failure is `MANIFEST_INVALID`, never a partial tree.

        B4: a ref body is NOT an inline declaration. Passing `allow_ref=False`
        with the default 8 KiB ceiling applied the manifest's inline budget to a
        fetched tree, so 9 770 / 15 614 / 23 406 / 29 250-byte ref pages were
        all refused — i.e. the ref mechanism could never serve anything the
        inline budget could not already hold. The body gets `MAX_REF_BYTES`
        here; the node/depth/action limits are unchanged.
        """
        declaration = {
            "id": page.page_id,
            "title": page.title,
            "version": page.version,
            "entry": page.entry,
            "icon": page.icon,
            "requires": ["session"] if page.requires_session else [],
        }
        if page.refresh_s:
            declaration["refresh_s"] = page.refresh_s
        declaration["schema"] = dict(body)
        try:
            validate_page(
                declaration,
                module_tools=record.manifest.tool_names,
                page_ids=[p.page_id for p in record.manifest.ui_pages],
                pointer=page.pointer,
                allow_ref=False,
                # The 48 KiB block budget still applies to the whole `ui` block
                # at registration; the fetched body is capped at 64 KiB by
                # `_ref_body` before it ever gets here.
                inline_max_bytes=MAX_REF_BYTES,
            )
        except UIPageError as exc:
            raise VadeError("MANIFEST_INVALID", str(exc)) from None

    # ----------------------------------------------------------------- state/get
    async def get_state(
        self, connection: Any, module_id: str, instance_id: str, page_id: str
    ) -> dict[str, Any]:
        caller = authorize_target(connection, module_id, instance_id)
        record = self._record(module_id, instance_id)
        require_visible(caller, record)
        page = self._page(record, page_id)
        key = (record.module_id, record.instance_id, page.page_id)
        now = self._clock()
        cached = self._state_cache.get(key)
        if cached is not None and self.limits.state_cache_s > 0:
            if now - cached.fetched_mono < self.limits.state_cache_s:
                return {"rev": cached.rev, "state": dict(cached.state), "fetched_at": cached.fetched_at}

        reply = await self._module_rpc(
            record, "state/get", {"page_id": page.page_id}, STATE_GET_DEADLINE_MS
        )
        raw_rev = reply.get("rev", 0)
        rev = int(raw_rev) if isinstance(raw_rev, int) and not isinstance(raw_rev, bool) and raw_rev >= 0 else 0
        try:
            state = check_state(reply.get("state") or {}, pointer=f"{page.pointer}/state")
        except UIPageError as exc:
            # The module's own state is untrusted input; a malformed blob is
            # UNAVAILABLE for this page, not a core crash and not a rendered page.
            raise VadeError("UNAVAILABLE", f"{record.module_id} returned an invalid page state: {exc.reason}") from None

        # B5: compare-and-swap under a per-key lock, and NEVER invent a rev.
        # `max(rev, cached.rev + 1)` used to fabricate a newer rev around older
        # data — worse than a plain regression, because the client trusts `rev`
        # and would render `{'v': 'old'}` as rev 10. A module's `rev` is the only
        # ordering signal core has; if it goes backwards, the older answer loses.
        lock = self._state_locks.setdefault(key, asyncio.Lock())
        async with lock:
            previous = self._state_cache.get(key)
            if previous is not None and rev <= previous.rev:
                if rev == previous.rev:
                    # Same rev, same data: refresh the TTL so the page is not
                    # re-fetched on every tick, and keep the stored value.
                    previous.fetched_mono = now
                    return {"rev": previous.rev, "state": dict(previous.state),
                            "fetched_at": previous.fetched_at, "unchanged": True}
                # Older than what we already served: drop it on the floor.
                return {"rev": previous.rev, "state": dict(previous.state),
                        "fetched_at": previous.fetched_at, "unchanged": True}
            entry = _CachedState(rev=rev, state=state, fetched_mono=now, fetched_at=int(self._wall()))
            self._state_cache[key] = entry
        return {"rev": entry.rev, "state": dict(state), "fetched_at": entry.fetched_at}

    # --------------------------------------------------------------- ui/tool.call
    async def tool_call(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        module_id = _string(params, "module_id")
        instance_id = _string(params, "instance_id")
        page_id = _string(params, "page_id")
        tool = _string(params, "tool")
        args_raw = params.get("args", {})
        if not isinstance(args_raw, Mapping):
            raise VadeError("INVALID_ARGUMENT", "args must be an object", data={"field": "args"})
        args = dict(args_raw)
        client_call_id = _string(params, "client_call_id")
        session_id = params.get("session_id") or ""
        if not isinstance(session_id, str):
            raise VadeError("INVALID_ARGUMENT", "session_id must be a string", data={"field": "session_id"})
        call_id = params.get("call_id")
        approval_id = params.get("approval_id")
        for name, value in (("call_id", call_id), ("approval_id", approval_id)):
            if value is not None and not isinstance(value, str):
                raise VadeError("INVALID_ARGUMENT", f"{name} must be a string", data={"field": name})

        # B1: the caller must own the target module. A browser may read a page
        # but never *call* one without the approval flow below — and the
        # approval is minted against the connection that asked.
        caller = authorize_target(connection, module_id, instance_id)
        # B3: `rpc.json` has declared READ_ONLY_SESSION for this method since
        # C-05 1.2.0 and nothing checked it. A read-only session is exactly the
        # one that must not be able to turn a page into a tool call: the page
        # was rendered by a viewer, not by a writer.
        if is_read_only(connection):
            raise VadeError(
                "READ_ONLY_SESSION",
                "this session is read-only; a page may not call a tool from it",
            )

        record = self._record(module_id, instance_id)
        require_visible(caller, record)
        page = self._page(record, page_id)
        spec = record.manifest.tool(tool)
        if spec is None:
            # §6.2: ownership is "the tool is declared by the module that owns the
            # page". The client's module_id only located the page; it never
            # authorised the tool.
            raise VadeError(
                "NOT_FOUND",
                f"{module_id} does not declare tool {tool!r}",
                data={"field": "tool"},
            )
        digest = digest_args(args)
        if len(serialize(args)) > MAX_TOOL_CALL_PLAINTEXT:
            raise VadeError("PAYLOAD_TOO_LARGE", "tool arguments exceed the 65519-byte frame limit")
        argument_error = validate_arguments(spec.input_schema, args)
        if argument_error is not None:
            raise VadeError("INVALID_ARGUMENT", argument_error, data={"field": "args"})

        if call_id is not None:
            return await self._second_stage(
                connection, record, page, tool, args, digest,
                call_id=call_id, approval_id=approval_id, session_id=session_id,
            )

        return await self._first_stage(
            connection, record, page, spec, tool, args, digest,
            client_call_id=client_call_id, session_id=session_id,
        )

    async def _first_stage(
        self, connection: Any, record: Any, page: UIPage, spec: Any, tool: str,
        args: Mapping[str, Any], digest: str, *, client_call_id: str, session_id: str,
    ) -> dict[str, Any]:
        approval = self._approval()
        tool_id = f"{record.module_id}/{tool}"
        risk = self._risk(tool_id, spec.risk)
        needs_approval = self._needs_approval(risk)
        if approval is None:
            if needs_approval:
                # Fail closed: no approval port means nobody can approve, so a
                # high-risk page call must not silently take the "no approval
                # needed" branch.
                raise VadeError("UNAVAILABLE", "the approval port is not installed; refusing a high-risk page call")
            return await self._execute(
                connection, record, page, tool, args, session_id=session_id,
                client_call_id=client_call_id, approval_id=None, decision="below_high",
            )
        if not needs_approval:
            return await self._execute(
                connection, record, page, tool, args, session_id=session_id,
                client_call_id=client_call_id, approval_id=None, decision="below_high",
            )

        request = ApprovalRequest(
            session_id=session_id,
            turn_id=f"page:{page.page_id}",
            tool_id=tool_id,
            args=args,
            risk=spec.risk,
            expires_at=int(self._wall()) + 60,
            emit=None,  # §9.4: a page approval is a synchronous RPC, not an event
        )
        try:
            pending, reason = await approval.open_request(request)
        except VadeError as exc:
            self._audit({
                "kind": "module_page_call", "status": "denied", "code": exc.code,
                "module_id": record.module_id, "instance_id": record.instance_id,
                "method": "ui/tool.call", "tool": tool_id, "args": dict(args),
                "args_digest": digest, "request_id": None, "session_id": session_id or None,
                "turn_id": None, "duration_ms": 0.0,
                "extra": {"origin": "module_page", "page_id": page.page_id,
                          "decision": "no_approver", "client_call_id": client_call_id},
            })
            raise
        if pending is None:
            # A `scope:"session"` grant already covers this exact tool + args.
            return await self._execute(
                connection, record, page, tool, args, session_id=session_id,
                client_call_id=client_call_id, approval_id=None, decision=reason,
            )

        call = _PendingCall(
            call_id=f"pc-{uuid.uuid4().hex[:20]}",
            module_id=record.module_id, instance_id=record.instance_id,
            page_id=page.page_id, tool=tool, args=args, digest=digest,
            request_id=pending.request_id,
            expires_at=pending.created_at + float(getattr(approval, "timeout_s", 60.0)),
            created_mono=self._clock(),
            # B2: bind the mint to the session and the device that asked. Stage
            # two compares both verbatim; see `_second_stage`.
            session_id=session_id,
            approver_device_id=device_of(connection),
        )
        self._remember_call(call)
        self._audit({
            "kind": "module_page_call", "status": "approval_required", "code": None,
            "module_id": record.module_id, "instance_id": record.instance_id,
            "method": "ui/tool.call", "tool": tool_id, "args": dict(args),
            "args_digest": digest, "request_id": pending.request_id,
            "session_id": session_id or None, "turn_id": None, "duration_ms": 0.0,
            "extra": {"origin": "module_page", "page_id": page.page_id,
                      "decision": "approval_required", "risk": pending.risk,
                      "client_call_id": client_call_id},
        })
        return {
            "call_id": call.call_id,
            "status": "approval_required",
            "approval": {
                "request_id": pending.request_id,
                "tool": tool_id,
                "args": dict(args),
                "risk": pending.risk,
                "expires_at": int(pending.created_at + float(getattr(approval, "timeout_s", 60.0))),
                "args_digest": digest,
            },
        }

    async def _second_stage(
        self, connection: Any, record: Any, page: UIPage, tool: str,
        args: Mapping[str, Any], digest: str, *, call_id: str, approval_id: str | None,
        session_id: str,
    ) -> dict[str, Any]:
        pending_call = self._calls.get(call_id)
        if pending_call is None:
            raise VadeError("NOT_FOUND", f"unknown page call {call_id!r}; start a new call", data={"field": "call_id"})
        if (pending_call.module_id, pending_call.instance_id, pending_call.page_id) != (
            record.module_id, record.instance_id, page.page_id
        ) or pending_call.tool != tool:
            raise VadeError("PERMISSION_DENIED", "call_id belongs to a different page or tool")
        # §6.2: exactly the args the approver saw. Byte-identical digest, not a
        # "close enough" comparison.
        if pending_call.digest != digest:
            raise VadeError("PERMISSION_DENIED", "args differ from the approved request (digest mismatch)")
        if approval_id != pending_call.request_id:
            raise VadeError("PERMISSION_DENIED", "approval_id does not match the minted request")
        # B2: an approval is a statement by ONE device about ONE session. The
        # args digest alone let `session-ALICE` approve a call that stage two
        # then ran as `session-BOB` — the module received `session-BOB` as its
        # `session_id`, so the human approved one conversation and the write
        # landed in another. Both halves are compared verbatim, and the field
        # name says which one failed.
        if session_id != pending_call.session_id:
            # Not consumed on refusal: the call_id + approval_id pair is a
            # capability, and burning it because *someone else* presented it
            # would let a third party cancel a legitimate approval.
            raise VadeError(
                "PERMISSION_DENIED",
                f"this call was minted for session {pending_call.session_id!r}; "
                f"redeeming it from {session_id!r} is not the approval that was granted",
                data={"field": "session_id"},
            )
        if device_of(connection) != pending_call.approver_device_id:
            raise VadeError(
                "PERMISSION_DENIED",
                "this call was minted by a different device; the approval belongs to that device",
                data={"field": "approver"},
            )
        approval = self._approval()
        if approval is None:
            raise VadeError("UNAVAILABLE", "the approval port disappeared between stages")
        decision = approval.decision_of(pending_call.request_id)
        if decision is None:
            raise VadeError("PERMISSION_DENIED", "approval is still pending; resolve it with approval/decide first")
        if decision != "allowed":
            self._calls.pop(call_id, None)
            raise VadeError("PERMISSION_DENIED", f"approval was not granted ({decision})")
        if self._wall() > pending_call.expires_at:
            self._calls.pop(call_id, None)
            raise VadeError("PERMISSION_DENIED", "approval expired before the call was executed")
        self._calls.pop(call_id, None)
        return await self._execute(
            connection, record, page, tool, args, session_id=session_id,
            client_call_id=call_id, approval_id=pending_call.request_id, decision="allowed",
        )

    def _risk(self, tool_id: str, declared: str) -> str:
        approval = self._approval()
        engine = getattr(approval, "engine", None) if approval is not None else None
        if engine is not None:
            # §6.2: risk is core's judgement; the manifest only declares. The
            # engine may only raise it (RISK_DOWNGRADE_FORBIDDEN elsewhere).
            return str(engine.risk_of(tool_id, declared))
        from ..policy.engine import effective_risk

        return effective_risk(declared, tool_id)

    def _needs_approval(self, risk: str) -> bool:
        approval = self._approval()
        engine = getattr(approval, "engine", None) if approval is not None else None
        if engine is not None:
            return bool(engine.requires_approval(risk))
        from ..policy.engine import PolicyEngine

        return PolicyEngine().requires_approval(risk)

    def _remember_call(self, call: _PendingCall) -> None:
        # Bound the table without a sweeper: drop expired calls, then refuse.
        # Refusing (OVERLOADED) matches the `[ui].pending_calls` note and the
        # C-05 error list; silently evicting the oldest would make a pending
        # approval unreachable with no signal.
        now = self._wall()
        for key in [k for k, c in self._calls.items() if c.expires_at < now]:
            self._calls.pop(key, None)
        if len(self._calls) >= self.limits.pending_calls:
            raise VadeError("OVERLOADED", "too many pending page calls; resolve or wait for them to expire")
        self._calls[call.call_id] = call

    async def _execute(
        self, connection: Any, record: Any, page: UIPage, tool: str,
        args: Mapping[str, Any], *, session_id: str, client_call_id: str,
        approval_id: str | None, decision: str,
    ) -> dict[str, Any]:
        router = self._router()
        if router is None:
            raise VadeError("UNAVAILABLE", "the tool router is not installed")
        started = self._clock()
        result = await router.call(
            f"{record.module_id}/{tool}",
            args,
            session_id=session_id or "",
            # No turn_id: a page action is user behaviour, not a turn (§6.2).
            call_id=f"page-{client_call_id}",
        )
        duration = (self._clock() - started) * 1000.0
        self._audit({
            "kind": "module_page_call", "status": "ok" if result.ok else "failed",
            "code": None if result.ok else result.code,
            "module_id": record.module_id, "instance_id": record.instance_id,
            "method": "ui/tool.call", "tool": f"{record.module_id}/{tool}",
            "args": dict(args), "args_digest": digest_args(args),
            "request_id": approval_id, "session_id": session_id or None,
            "turn_id": None, "duration_ms": round(duration, 3),
            "extra": {"origin": "module_page", "page_id": page.page_id,
                      "decision": decision, "client_call_id": client_call_id},
        })
        out: dict[str, Any] = {"call_id": client_call_id, "status": "ok" if result.ok else "failed"}
        if result.ok:
            out["result"] = result.result
        else:
            out["error"] = {
                "code": result.code or "INTERNAL",
                "message": result.message,
                "retriable": bool(result.retriable),
            }
        return out

    # ------------------------------------------------------------------ actions
    async def route_action(
        self,
        connection: Any,
        *,
        module_id: str,
        instance_id: str,
        page_id: str,
        event: str,
        node_id: str | None = None,
        session_id: str = "",
        client_msg_id: str | None = None,
        client_call_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one `on.<event>` action by its kind (RFC §6).

        `tools/call` → `tool_call` (router + approval); `ui/submit` → the
        existing `ui/submit` handler, so queue, rate limit, `READ_ONLY_SESSION`
        and audit are the same as a typed message. `navigate` and `open_url` are
        **resolved, not executed**: the host performs them, and neither can leave
        the app except through an http(s) URL the user sees.

        B1/B3: this is a fifth entry point into the same pages, so it carries the
        same two gates. `tool_call` and `ui/submit` check them again downstream;
        `navigate`/`open_url` have no downstream, so without the checks here a
        module could route another module's page — the exact hole B1 named.
        """
        caller = authorize_target(connection, module_id, instance_id)
        record = self._record(module_id, instance_id)
        require_visible(caller, record)
        page = self._page(record, page_id)
        action = self._find_action(page, event=event, node_id=node_id)
        if action is None:
            raise VadeError("NOT_FOUND", f"page {page_id!r} has no {event!r} action")

        if action.kind == "navigate":
            # Re-checked at routing time with the *current* page set: a page
            # cannot navigate to a page that was dropped or removed since.
            check_navigate(
                action.target or "",
                pointer=action.pointer,
                page_ids=[p.page_id for p in record.manifest.ui_pages],
            )
            return {"kind": "navigate", "to": action.target, "external": False}

        if action.kind == "open_url":
            check_open_url(action.target or "", pointer=action.pointer)
            return {"kind": "open_url", "to": action.target, "external": True}

        if action.kind == "tools/call":
            resolved = _resolve_value(action.args)
            if resolved is None:
                raise VadeError(
                    "INVALID_ARGUMENT",
                    "action args still contain a bind/input placeholder; the client resolves those before sending",
                )
            return await self.tool_call(connection, {
                "module_id": module_id, "instance_id": instance_id, "page_id": page_id,
                "tool": action.target, "args": resolved,
                "client_call_id": client_call_id or f"action-{uuid.uuid4().hex[:12]}",
                "session_id": session_id or None,
            })

        # ui/submit: the SAME handler a typed message uses (§6.3). Delegating
        # here is what keeps read-only sessions, rate limits and audit intact —
        # a second write path would be a second set of rules.
        content = _resolve_value(list(action.content))
        if content is None:
            raise VadeError(
                "INVALID_ARGUMENT",
                "ui/submit parts still contain a bind/input placeholder; resolve them client-side first",
            )
        handlers = getattr(self._app, "session_handlers", None) or {}
        submit = handlers.get("ui/submit")
        if submit is None:
            raise VadeError("UNAVAILABLE", "ui/submit is not installed")
        return await submit(connection, {
            "session_id": session_id,
            "client_msg_id": client_msg_id or str(uuid.uuid4()),
            "content": content,
        })

    @staticmethod
    def _find_action(page: UIPage, *, event: str, node_id: str | None) -> Any:
        for node in page.walk():
            if node_id is not None and node.id != node_id:
                continue
            for name, action in node.actions:
                if name == event:
                    return action
        return None


def _string(params: Mapping[str, Any], field: str) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value:
        raise VadeError("INVALID_ARGUMENT", f"{field} is required", data={"field": field})
    return value


def _resolve_value(value: Any) -> Any:
    """Return `value` with every `{"bind"}`/`{"input"}` resolved, or None.

    The client resolves placeholders before sending (§6.4); core never guesses a
    value for one, because "missing binding" turned into "" would let an action
    run with arguments the user never saw.
    """
    if isinstance(value, Mapping):
        if "bind" in value or "input" in value:
            return None
        out: dict[str, Any] = {}
        for key, sub in value.items():
            resolved = _resolve_value(sub)
            if resolved is None:
                return None
            out[key] = resolved
        return out
    if isinstance(value, list):
        items = []
        for sub in value:
            resolved = _resolve_value(sub)
            if resolved is None:
                return None
            items.append(resolved)
        return items
    return value
