"""`ui/page.get` / `ui/state.get` / `ui/tool.call` (RFC-0009 §9.3, C-05 append).

Same shape as `session/rpc.py` and `tasks/rpc.py`: `async handler(connection,
params)` returning a plain dict, `VadeError` for refusals. `install()` merges the
three names into `app.session_handlers`, the one table the dispatcher reads.

**The `connection` argument is load-bearing.** C2's dispatcher
(`app.py::make_rpc_handler`) hands every `ui/*` method to whatever socket asked,
so this file is the last place that can tell module A from module B. It used to
drop the argument on the floor for `page.get`/`state.get` — the handler signature
had it, the body did not — and that is precisely how module A read module B's
page and state. Every method now passes it to the service, which resolves the
caller in `modulepage/auth.py` and refuses a target that is not its own.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from ..errors import VadeError

__all__ = ["ModulePageRPC"]


class ModulePageRPC:
    def __init__(self, service: Any) -> None:
        self.service = service

    def handlers(self) -> dict[str, Callable[[Any, Mapping[str, Any]], Awaitable[Any]]]:
        return {
            "ui/page.get": self.page_get,
            "ui/state.get": self.state_get,
            "ui/tool.call": self.tool_call,
        }

    async def page_get(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        module_id, instance_id, page_id = _ids(params)
        return await self.service.get_page(connection, module_id, instance_id, page_id)

    async def state_get(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        module_id, instance_id, page_id = _ids(params)
        return await self.service.get_state(connection, module_id, instance_id, page_id)

    async def tool_call(self, connection: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return await self.service.tool_call(connection, params)


def _ids(params: Mapping[str, Any]) -> tuple[str, str, str]:
    out: list[str] = []
    for field in ("module_id", "instance_id", "page_id"):
        value = params.get(field)
        if not isinstance(value, str) or not value:
            raise VadeError("INVALID_ARGUMENT", f"{field} is required", data={"field": field})
        out.append(value)
    return out[0], out[1], out[2]
