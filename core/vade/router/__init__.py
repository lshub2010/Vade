"""Router package (C3). `dispatch.Router` is the only public entry point."""

from __future__ import annotations

from typing import Any

from .names import ToolNameMap, prefix_for
from .dispatch import ModuleCaller, Router, Target, ToolCallResult, digest_args

__all__ = ["Router", "Target", "ModuleCaller", "ToolCallResult", "ToolNameMap",
           "prefix_for", "digest_args", "install"]


def install(app: Any) -> None:
    """Wire the router only if a module caller exists; otherwise leave it None.

    The connection-level client RPC (core->module request/response correlation)
    lives in C2's transport; until it exposes a pending-request table the core
    runs with `app.router = None` and the loop reports "no capability".
    """
    caller = getattr(app, "module_caller", None)
    if caller is None or getattr(app, "registry", None) is None:
        app.router = None
        return
    app.router = Router(caller, app.registry)
