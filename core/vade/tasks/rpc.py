"""`task/*` RPC surface (RFC-0007 §6, appended to C-05).

Same shape as `session/rpc.py`: `async handler(connection, params)` returning a
plain dict, `VadeError` for refusals. `install()` in this package merges these
four names into `app.session_handlers`, which is the one place the dispatcher
looks — a second table would be a second entry point to keep in sync.

Read-only is decided **here**, not by hiding a button: §6.5 allows `task/list`
and `task/log_tail` on an explicitly read-only session and refuses
`task/cancel` / `subagent/message` with `READ_ONLY_SESSION`.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from ..errors import VadeError

__all__ = ["TaskRPC"]

_WAIT_DEFAULT_MS = 10000
_REASON_MAX = 200
_TEXT_MAX = 2000


def _read_only(connection: Any) -> bool:
    """Only an *explicitly* read-only session is refused (AMEND-22 #14 / D76)."""
    return bool(getattr(connection, "read_only", False))


class TaskRPC:
    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def handlers(self) -> dict[str, Callable[[Any, Mapping[str, Any]], Awaitable[Any]]]:
        return {
            "task/list": self.task_list,
            "task/log_tail": self.task_log_tail,
            "task/cancel": self.task_cancel,
            "subagent/message": self.subagent_message,
        }

    # ------------------------------------------------------------- task/list
    async def task_list(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise VadeError(
                "INVALID_ARGUMENT", "session_id must be a string or null",
                data={"field": "session_id"},
            )
        kinds = params.get("kinds")
        states = params.get("states")
        limit = params.get("limit")
        cursor = params.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise VadeError(
                "INVALID_ARGUMENT", "cursor must be a string or null",
                data={"field": "cursor"},
            )
        return self.registry.list_tasks(
            session_id=session_id,
            kinds=kinds,
            states=states,
            limit=limit,
            cursor=cursor,
        )

    # --------------------------------------------------------- task/log_tail
    async def task_log_tail(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._task_id(params)
        follow = params.get("follow", False)
        if not isinstance(follow, bool):
            raise VadeError(
                "INVALID_ARGUMENT", "follow must be a boolean", data={"field": "follow"}
            )
        cursor = params.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise VadeError(
                "INVALID_ARGUMENT", "cursor must be a string or null",
                data={"field": "cursor"},
            )
        return await self.registry.log_tail(
            task_id,
            lines=params.get("lines"),
            max_bytes=params.get("max_bytes"),
            cursor=cursor,
            follow=follow,
            wait_ms=params.get("wait_ms", _WAIT_DEFAULT_MS),
        )

    # ------------------------------------------------------------ task/cancel
    async def task_cancel(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        if _read_only(connection):
            raise VadeError(
                "READ_ONLY_SESSION", "this session is read-only; task/cancel is a write"
            )
        task_id = self._task_id(params)
        reason = params.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise VadeError(
                "INVALID_ARGUMENT", "reason must be a string", data={"field": "reason"}
            )
        if isinstance(reason, str) and len(reason) > _REASON_MAX:
            raise VadeError(
                "INVALID_ARGUMENT", f"reason must be <= {_REASON_MAX} characters",
                data={"field": "reason"},
            )
        task = await self.registry.cancel(task_id, reason=reason, by=self._who(connection))
        return {"ok": True, "state": task.state.value, "forced": bool(task.forced)}

    # ------------------------------------------------------- subagent/message
    async def subagent_message(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        if _read_only(connection):
            raise VadeError(
                "READ_ONLY_SESSION", "this session is read-only; subagent/message is a write"
            )
        task_id = self._task_id(params)
        text = params.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= _TEXT_MAX:
            raise VadeError(
                "INVALID_ARGUMENT", f"text must be 1..{_TEXT_MAX} characters",
                data={"field": "text"},
            )
        return await self.registry.message(task_id, text)

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _task_id(params: Mapping[str, Any]) -> str:
        task_id = params.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise VadeError(
                "INVALID_ARGUMENT", "task_id is required", data={"field": "task_id"}
            )
        return task_id

    @staticmethod
    def _who(connection: Any) -> str:
        return str(
            getattr(connection, "device_id", None)
            or getattr(connection, "connection_id", "user")
        )
