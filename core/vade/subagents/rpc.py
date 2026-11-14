"""`subagent/*` RPC surface (RFC-0012 §6, appended to C-05 1.4.0).

Same shape as `vade.tasks.rpc`: `async handler(connection, params)` returning a
plain dict, `VadeError` for refusals; `install()` merges the names into
`app.session_handlers`, the one table the dispatcher reads. Both methods are
read-only observation (§6.4): an explicitly read-only session may call them.
The write channel (`subagent/message`, RFC-0007 §6.4) is NOT here — it stays
where it is, untouched.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from ..errors import VadeError

__all__ = ["SubagentRPC"]


class SubagentRPC:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def handlers(self) -> dict[str, Callable[[Any, Mapping[str, Any]], Awaitable[Any]]]:
        return {
            "subagent/list": self.subagent_list,
            "subagent/get": self.subagent_get,
        }

    # ----------------------------------------------------------- subagent/list
    async def subagent_list(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise VadeError(
                "INVALID_ARGUMENT", "session_id must be a string or null",
                data={"field": "session_id"},
            )
        return self.manager.list_runs(
            session_id=session_id,
            states=params.get("states"),
            limit=params.get("limit"),
            cursor=params.get("cursor"),
        )

    # ------------------------------------------------------------ subagent/get
    async def subagent_get(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        run_id = params.get("run_id")
        # §6.2: malformed id -> INVALID_ARGUMENT, well-formed unknown -> NOT_FOUND.
        if not isinstance(run_id, str) or not run_id:
            raise VadeError(
                "INVALID_ARGUMENT", "run_id is required", data={"field": "run_id"}
            )
        run = self.manager.require(run_id)
        return self.manager.view(run)
