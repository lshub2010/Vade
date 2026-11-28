"""`/admin/api/memory*` — the admin surface over the `MemoryPort` (C-16).

Every request goes through the core-owned memory adapter
(`vade.memory.MemoryAdapter` -> `memory/admin_*` private RPCs). This module never
imports `memory_module`, never opens `memory.db`, and never re-implements the
storage rules: the module owns dedup, quota, TTL and the supersedes chain.

Two constraints from the task book are enforced here rather than in the module:

* **Authorization is core's job.** The module answers whoever core asks; the
  admin `guard()` is the only gate, and a browser cookie is read-only
  (`READ_ONLY_SESSION`) like every other non-GET admin route.
* **Writes are audited, reads are not.** `promote` / `archive` / `restore` write
  one audit row each; list/get/stats do not (they change nothing).

`PATCH /admin/api/memory/{id}` is deliberately absent: C-08 declares a 405, and
there is no edit RPC. Changing an entry means archive + remember (C-16 note).
"""

from __future__ import annotations

import re

from typing import Any, Callable, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..errors import VadeError

__all__ = ["build_routes", "MAX_LIMIT"]

#: Bound on one page. The contract declares 1..200; a larger `limit` is
#: INVALID_ARGUMENT rather than a silent clamp (a clamp makes "end of list"
#: indistinguishable from "I ignored your page size").
MAX_LIMIT = 200
LAYERS = ("session", "global")
STATUSES = ("staging", "active", "archived", "superseded")
#: C-16 `reason` maxLength; the module stores the same 300 characters.
_MAX_TEXT = 300
_INTEGER = re.compile(r"[+-]?\d+")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63


# ------------------------------------------------------------------ helpers
def _port(ctx: Any) -> Any:
    port = getattr(ctx, "memory", None)
    if port is None:
        # A build without the memory component keeps the honest 501 for every
        # shape the schema declares; the UI can label it "not in this build".
        raise VadeError("UNSUPPORTED", "memory module is not installed")
    return port


def _actor(session: Any) -> str:
    return str(getattr(session, "device_id", None) or "loopback_admin")


def _audit(ctx: Any, session: Any, kind: str, **fields: Any) -> None:
    """One audit row per successful write; reads never call this."""
    ctx.write_audit(
        kind=kind, status="ok", actor=_actor(session),
        extra={key: value for key, value in fields.items() if value is not None},
    )


def _number(value: str | None, name: str) -> int | None:
    """Unix seconds as an integer.

    C-08 types `from`/`to` as `number`, but the module's C-16 contract is
    integer seconds and its dispatcher rejects a float. Sending `1700000000.0`
    made every time-filtered list a 400, so the boundary converts once, here.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    if _INTEGER.fullmatch(text):
        # Parse integer literals as int: float() rounds 2**63-1 up to 2**63,
        # which used to reject a perfectly valid int64 boundary value.
        number = int(text)
        if not _INT64_MIN <= number < _INT64_MAX:
            raise VadeError("INVALID_ARGUMENT", f"{name} is out of range")
        return number
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise VadeError("INVALID_ARGUMENT", f"{name} must be a number") from None
    if number != number or number in (float("inf"), float("-inf")):
        # `?from=inf` / `1e400` survive float() and blow up as OverflowError in
        # int(); that used to surface as a 500 instead of the contract's 400.
        raise VadeError("INVALID_ARGUMENT", f"{name} must be a finite number")
    if not _INT64_MIN <= number < _INT64_MAX:
        # SQLite INTEGER is signed 64-bit; a larger "finite" number reached the
        # module and blew up as OverflowError/INTERNAL (HTTP 500) instead of 400.
        raise VadeError("INVALID_ARGUMENT", f"{name} is out of range")
    try:
        return int(number)
    except (OverflowError, ValueError):
        raise VadeError("INVALID_ARGUMENT", f"{name} must be a finite number") from None


def _limit(value: Any) -> int:
    if value in (None, ""):
        return 50
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise VadeError("INVALID_ARGUMENT", "limit must be an integer") from None
    if parsed < 1 or parsed > MAX_LIMIT:
        raise VadeError("INVALID_ARGUMENT", f"limit must be between 1 and {MAX_LIMIT}")
    return parsed


def _one(value: Any, name: str, allowed: tuple[str, ...]) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if text not in allowed:
        raise VadeError("INVALID_ARGUMENT", f"{name} must be one of {list(allowed)}")
    return text


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VadeError("INVALID_ARGUMENT", f"{name} is required")
    text = value.strip()
    if len(text) > _MAX_TEXT:
        # C-16 caps the field; silently truncating would audit one reason and
        # store another (the module's own slice is the same length).
        raise VadeError("INVALID_ARGUMENT", f"{name} must be at most {_MAX_TEXT} characters")
    return text


def _view(item: Any) -> dict[str, Any]:
    """One `MemoryItem` (C-08 §memory) from a port item + its meta."""
    meta: Mapping[str, Any] = dict(getattr(item, "meta", None) or {})
    mem_id = str(getattr(item, "id", "") or meta.get("id") or "")
    tags = meta.get("tags")
    supersedes = meta.get("supersedes")
    status = meta.get("status")
    return {
        "id": mem_id,
        "text": str(getattr(item, "text", "") or ""),
        "layer": str(meta.get("layer") or ""),
        "namespace": str(meta.get("namespace") or ""),
        "status": str(status) if status in STATUSES else "active",
        "session_id": meta.get("session_id"),
        "turn_id": meta.get("turn_id"),
        "created_at": meta.get("created_at") or 0,
        "expires_at": meta.get("expires_at"),
        "hit_count": int(meta.get("hit_count") or 0),
        "tags": list(tags) if isinstance(tags, (list, tuple)) else [],
        "supersedes": list(supersedes) if isinstance(supersedes, (list, tuple)) else [],
    }


def _chain(item: Any) -> dict[str, Any]:
    meta: Mapping[str, Any] = dict(getattr(item, "meta", None) or {})
    status = meta.get("status")
    return {
        "id": str(getattr(item, "id", "") or meta.get("id") or ""),
        "status": str(status) if status in STATUSES else "active",
        "created_at": meta.get("created_at") or 0,
    }


def _stats(result: Mapping[str, Any]) -> dict[str, Any]:
    """Pick the five C-16 counters; never leak module-internal keys."""
    return {
        "global_count": int(result.get("global_count") or 0),
        "global_limit": int(result.get("global_limit") or 0),
        "staging_count": int(result.get("staging_count") or 0),
        "archived_count": int(result.get("archived_count") or 0),
        "oldest_at": result.get("oldest_at"),
    }


def _ok(value: Any) -> bool:
    return bool(getattr(value, "ok", value))


# ------------------------------------------------------------------- routes
def build_routes(ctx: Any, guard: Callable[..., Any],
                 read_json: Callable[..., Any]) -> list[Route]:
    """The six `/admin/api/memory*` handlers, wired onto `ctx.memory`."""

    async def list_memory(request: Request) -> Response:
        guard(request)
        port = _port(ctx)
        params = request.query_params
        q = params.get("q") or None
        if q is not None and len(q) > 200:
            raise VadeError("INVALID_ARGUMENT", "q must be at most 200 characters")
        query: dict[str, Any] = {
            "layer": _one(params.get("layer"), "layer", LAYERS),
            "namespace": params.get("namespace") or None,
            "status": _one(params.get("status"), "status", STATUSES),
            "from": _number(params.get("from"), "from"),
            "to": _number(params.get("to"), "to"),
            "q": q,
            "limit": _limit(params.get("limit")),
            "cursor": params.get("cursor") or None,
        }
        items, cursor = await port.admin_list(query)
        return JSONResponse({
            "items": [_view(item) for item in items],
            "next_cursor": cursor,
        })

    async def get_memory(request: Request) -> Response:
        guard(request)
        port = _port(ctx)
        mem_id = request.path_params["id"]
        item, chain = await port.admin_get(mem_id)
        if not getattr(item, "id", ""):
            raise VadeError("NOT_FOUND", f"no memory {mem_id!r}")
        return JSONResponse({**_view(item), "chain": [_chain(row) for row in chain]})

    async def promote_memory(request: Request) -> Response:
        session = guard(request)
        port = _port(ctx)
        mem_id = request.path_params["id"]
        by = _actor(session)
        ok = _ok(await port.admin_promote(mem_id, by))
        if ok:
            _audit(ctx, session, "memory.promote", memory_id=mem_id, by=by)
        return JSONResponse({"ok": ok})

    async def archive_memory(request: Request) -> Response:
        session = guard(request)
        port = _port(ctx)
        mem_id = request.path_params["id"]
        body = await read_json(request)
        reason = _text(body.get("reason"), "reason")
        by = _actor(session)
        ok = _ok(await port.admin_archive(mem_id, reason, by))
        if ok:
            _audit(ctx, session, "memory.archive", memory_id=mem_id, reason=reason, by=by)
        return JSONResponse({"ok": ok})

    async def restore_memory(request: Request) -> Response:
        session = guard(request)
        port = _port(ctx)
        mem_id = request.path_params["id"]
        by = _actor(session)
        ok = _ok(await port.admin_restore(mem_id, by))
        if ok:
            _audit(ctx, session, "memory.restore", memory_id=mem_id, by=by)
        return JSONResponse({"ok": ok})

    async def memory_stats(request: Request) -> Response:
        guard(request)
        port = _port(ctx)
        return JSONResponse(_stats(await port.admin_stats()))

    return [
        # `/stats` before `{id}` so it is not read as an id named "stats".
        Route("/admin/api/memory", list_memory, methods=["GET"]),
        Route("/admin/api/memory/stats", memory_stats, methods=["GET"]),
        Route("/admin/api/memory/{id}", get_memory, methods=["GET"]),
        # PATCH is intentionally not registered: C-08 declares 405.
        Route("/admin/api/memory/{id}/promote", promote_memory, methods=["POST"]),
        Route("/admin/api/memory/{id}/archive", archive_memory, methods=["POST"]),
        Route("/admin/api/memory/{id}/restore", restore_memory, methods=["POST"]),
    ]
