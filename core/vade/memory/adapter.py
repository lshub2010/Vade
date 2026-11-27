"""Core-owned `MemoryPort` over the `memory` module's private RPCs.

This adapter is the *only* place core talks to memory storage. It never imports
`memory_module`, never opens `memory.db`, and never re-implements dedup, quota or
embedding rules: those live in the module (M4a). Two consequences follow, and
both are load-bearing:

* The private RPCs (`memory/read_session`, `memory/admin_*`) are not tools. The
  router only routes `module/tool` pairs declared in a manifest, so calling them
  goes through the same `ModuleCaller` but not through `Router.call` — that is
  why this adapter selects the registered memory node itself.
* Absence is `NOT_FOUND`, not an empty result. A page that shows "no memories"
  when the module is missing is indistinguishable from data loss (22-C3 §四).

Selection uses the module-id convention the canvas already uses
(`capability_kinds` in `admin/views.py`): a node whose `module_id` contains
`memory`. The memory module registers with `concurrency="exclusive"`, so at most
one instance is eligible; ties are broken by `node_id` for determinism.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ERROR_CODES, VadeError
from ..loop.ports import MemoryItem
from ..router.dispatch import Target

__all__ = ["MemoryAdapter", "DEFAULT_TIMEOUT_MS", "READ_TIMEOUT_MS"]

#: Admin reads/writes are interactive; the module's own tools use 15 s. Keep
#: these shorter so a wedged module cannot hold an admin request open longer
#: than the UI is willing to wait.
DEFAULT_TIMEOUT_MS = 10_000
#: The prompt path runs once per turn and must not stall a turn.
READ_TIMEOUT_MS = 3_000


def _health_value(record: Any) -> str:
    health = getattr(record, "health", None)
    return str(getattr(health, "value", health) or "")


def _is_memory_node(record: Any) -> bool:
    if getattr(record, "manifest", None) is None:
        return False
    if not getattr(record, "enabled", False):
        return False
    if getattr(record, "connection_id", None) is None:
        return False
    if _health_value(record) in {"offline", "rejected"}:
        return False
    return "memory" in str(getattr(record, "module_id", "") or "").lower()


def _result(reply: Any) -> Mapping[str, Any]:
    """Unwrap `{ok, result}` when the module used the tool envelope.

    A private RPC may answer either the bare result (`{"items": [...]}`) or the
    C-04 tool envelope. Both are legal here: the adapter is the boundary, and a
    module that reuses its tool helper must not make the admin page blank.
    """
    if not isinstance(reply, Mapping):
        raise VadeError("INTERNAL", "memory module reply is not an object")
    if "ok" in reply:
        if not reply.get("ok"):
            error = reply.get("error") or {}
            code = str(error.get("code") or "INTERNAL")
            if code not in ERROR_CODES:
                code = "INTERNAL"
            raise VadeError(code, str(error.get("message") or "memory call failed"))
        if "result" in reply:
            inner = reply.get("result")
            if isinstance(inner, Mapping):
                return inner
            raise VadeError("INTERNAL", "memory module result is not an object")
    return reply


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _item(row: Mapping[str, Any]) -> MemoryItem:
    data = dict(row)
    mem_id = str(data.pop("id", "") or "")
    text = str(data.pop("text", "") or "")
    return MemoryItem(id=mem_id, text=text, meta=data)


class MemoryAdapter:
    """`MemoryPort` implementation backed by `app.module_caller` + the registry."""

    def __init__(self, app: Any) -> None:
        self._app = app

    # ------------------------------------------------------------- resolution
    def _registry(self) -> Any:
        return getattr(self._app, "registry", None)

    def _record(self) -> Any:
        registry = self._registry()
        if registry is None:
            raise VadeError("NOT_FOUND", "memory module is not installed")
        candidates = [record for record in registry.snapshot() if _is_memory_node(record)]
        if not candidates:
            raise VadeError("NOT_FOUND", "memory module is not installed")
        # `exclusive` means one instance; sorting makes a misconfigured second
        # instance deterministic instead of "whichever the table iterated first".
        candidates.sort(key=lambda record: str(getattr(record, "node_id", "")))
        return candidates[0]

    def _caller(self) -> Any:
        caller = getattr(self._app, "module_caller", None)
        if caller is not None:
            return caller
        router = getattr(self._app, "router", None)
        caller = getattr(router, "_caller", None)
        if caller is not None:
            return caller
        raise VadeError("UNAVAILABLE", "no module caller is installed")

    async def _call(self, method: str, params: Mapping[str, Any], *, deadline_ms: int) -> Mapping[str, Any]:
        record = self._record()
        target = Target(
            module_id=str(getattr(record, "module_id", "")),
            instance_id=str(getattr(record, "instance_id", "")),
            connection_id=getattr(record, "connection_id", None),
            tool=method,
        )
        reply = await self._caller().call(target, method, dict(params), deadline_ms)
        return _result(reply)

    # ----------------------------------------------------------- prompt path
    async def read_session(self, session_id: str, limit: int = 50) -> list[MemoryItem]:
        result = await self._call(
            "memory/read_session",
            {"session_id": session_id, "limit": int(limit)},
            deadline_ms=READ_TIMEOUT_MS,
        )
        return [_item(row) for row in _rows(result.get("items"))]

    # -------------------------------------------------------- management path
    async def admin_list(self, query: Any) -> tuple[list[MemoryItem], str | None]:
        params = _query_params(query)
        result = await self._call("memory/admin_list", params, deadline_ms=DEFAULT_TIMEOUT_MS)
        cursor = result.get("next_cursor")
        return ([_item(row) for row in _rows(result.get("items"))],
                str(cursor) if cursor else None)

    async def admin_get(self, mem_id: str) -> tuple[MemoryItem, list[MemoryItem]]:
        result = await self._call("memory/admin_get", {"id": mem_id}, deadline_ms=DEFAULT_TIMEOUT_MS)
        chain = _rows(result.get("chain"))
        detail: Mapping[str, Any] = result
        nested = result.get("item")
        if isinstance(nested, Mapping):
            detail = nested
            chain = chain or _rows(nested.get("chain"))
        detail = {key: value for key, value in detail.items() if key != "chain"}
        return _item(detail), [_item(row) for row in chain]

    async def admin_promote(self, mem_id: str, by: str) -> bool:
        result = await self._call(
            "memory/admin_promote", {"id": mem_id, "by": by}, deadline_ms=DEFAULT_TIMEOUT_MS,
        )
        return bool(result.get("ok"))

    async def admin_archive(self, mem_id: str, reason: str, by: str) -> bool:
        result = await self._call(
            "memory/admin_archive", {"id": mem_id, "reason": reason, "by": by},
            deadline_ms=DEFAULT_TIMEOUT_MS,
        )
        return bool(result.get("ok"))

    async def admin_restore(self, mem_id: str, by: str) -> bool:
        result = await self._call(
            "memory/admin_restore", {"id": mem_id, "by": by}, deadline_ms=DEFAULT_TIMEOUT_MS,
        )
        return bool(result.get("ok"))

    async def admin_stats(self) -> dict[str, Any]:
        result = await self._call("memory/admin_stats", {}, deadline_ms=DEFAULT_TIMEOUT_MS)
        return dict(result)


def _query_params(query: Any) -> dict[str, Any]:
    """`MemoryQuery` -> RPC params, dropping unset keys.

    Sending `null` for an unset filter is legal per C-16, but a module that
    validates with `additionalProperties: false` plus typed leaves is easier to
    get right when only the keys the caller actually asked for arrive.
    """
    source = dict(query) if isinstance(query, Mapping) else {}
    params: dict[str, Any] = {}
    for key in ("layer", "namespace", "status", "q", "cursor"):
        value = source.get(key)
        if value not in (None, ""):
            params[key] = value
    for key in ("from", "to"):
        value = source.get(key)
        if value is not None:
            params[key] = value
    limit = source.get("limit")
    params["limit"] = int(limit) if limit is not None else 50
    return params
