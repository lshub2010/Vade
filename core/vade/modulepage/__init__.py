"""C-19 component (RFC-0009): module page serving + the four UI RPC methods.

`install(app)` runs before `policy` and `session` (component install is
alphabetical), so the service resolves the registry, the module caller, the
router and the approval port **per call** instead of pinning them here. That is
the same reason `session` wraps its approval port in a lazy object.

Handlers are published the same way `tasks` publishes its four: into
`app.session_handlers` when it already exists, otherwise through
`app.handler_providers`, which `session.install()` drains. `modulepage` sorts
before `session`, so the provider path is the normal one, not the fallback.
"""

from __future__ import annotations

from typing import Any

from .rpc import ModulePageRPC
from .service import ModulePageService, PageLimits

__all__ = ["install", "ModulePageService", "ModulePageRPC", "PageLimits"]


def install(app: Any) -> Any:
    cfg = getattr(app, "config", None)
    service = ModulePageService(app, limits=PageLimits.from_config(cfg))
    app.modulepage = service
    app.module_pages = service

    rpc = ModulePageRPC(service)
    app.modulepage_rpc = rpc

    # Both registration paths, on purpose:
    #
    #  * write into `session_handlers` when it already exists, and
    #  * ALWAYS publish a provider.
    #
    # The second half used to be an `else`. Some components install before
    # `modulepage` and create `app.session_handlers` as a dict, so the `else`
    # never ran — and `session.install()` REPLACES that dict with its own table,
    # re-adding only what `handler_providers` carries. Result on a real core
    # (`startup()` + `install_components()`): `ui/page.get` / `ui/state.get` /
    # `ui/tool.call` answered "unknown method" on a socket while every unit test
    # passed, because unit tests hand the service its table directly. Publishing
    # both ways is idempotent (`setdefault` on both sides) and keeps the methods
    # reachable whichever order the components land in.
    handlers = getattr(app, "session_handlers", None)
    if isinstance(handlers, dict):
        for name, handler in rpc.handlers().items():
            handlers.setdefault(name, handler)
    providers = getattr(app, "handler_providers", None)
    if not isinstance(providers, list):
        providers = []
        app.handler_providers = providers
    if rpc.handlers not in providers:
        providers.append(rpc.handlers)
    return app
