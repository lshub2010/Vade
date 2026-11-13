"""The real `ModuleCaller`: core->module RPC over a live C2 `Connection`.

`Router` only knows the protocol (`call(target, method, params, deadline_ms)`);
this adapter is what makes it real in production. It was missing entirely:
`app.module_caller` had no assignment anywhere, so `install()` built
`router = None` and every turn answered "no capability".

Correlation lives in `Connection.request` (pending-request table). The module
id -> connection_id mapping is read from the registry record the router already
selected, never from model input.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..errors import VadeError

__all__ = ["ConnectionCaller"]


class ConnectionCaller:
    """`ModuleCaller` over the transport's live connection table."""

    def __init__(self, connections: Callable[[], Mapping[str, Any]]) -> None:
        # A callable, not the dict: the facade swaps tables on install and the
        # caller must not pin the empty one.
        self._connections = connections

    def _connection(self, target: Any) -> Any:
        connection_id = getattr(target, "connection_id", None)
        if not connection_id:
            raise VadeError("UNAVAILABLE", f"{target.module_id}#{target.instance_id} has no live connection")
        connection = self._connections().get(connection_id)
        if connection is None:
            raise VadeError("UNAVAILABLE", f"connection {connection_id} is gone")
        return connection

    async def call(
        self,
        target: Any,
        method: str,
        params: Mapping[str, Any],
        deadline_ms: int,
    ) -> Mapping[str, Any]:
        connection = self._connection(target)
        # Slightly longer than the router's own budget so the router maps a
        # timeout to TIMEOUT instead of racing the transport for it.
        timeout_s = max(deadline_ms, 1) / 1000.0 + 1.0
        reply = await connection.request(method, dict(params), timeout_s=timeout_s)
        if not isinstance(reply, Mapping):
            raise VadeError("INTERNAL", "module reply is not an object")
        return reply

    async def cancel(self, target: Any, call_id: str) -> None:
        """Notification only: the core never waits for tools/cancel."""
        try:
            connection = self._connection(target)
        except VadeError:
            return  # already gone; nothing to cancel on the wire
        await connection.notify("tools/cancel", {"call_id": call_id})
