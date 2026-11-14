"""C-19 component (RFC-0009): module page serving + the three UI RPC methods.

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

    handlers = getattr(app, "session_handlers", None)
    if isinstance(handlers, dict):
        for name, handler in rpc.handlers().items():
            handlers.setdefault(name, handler)
    else:
        providers = getattr(app, "handler_providers", None)
        if not isinstance(providers, list):
            providers = []
            app.handler_providers = providers
        providers.append(rpc.handlers)
    return app
