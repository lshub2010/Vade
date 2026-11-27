"""Vade Python SDK (C-07).

The public surface is frozen at 0.1.x. Exactly four names are exported:

    ModuleApp   lifecycle: register, serve, reconnect, drain
    ToolSpec    one tool: name, fn, description, risk, timeout_ms, idempotent
    ToolError   structured error a tool may raise
    run         ModuleApp.run() without constructing an app (CLI convenience)

Everything else (transport, standalone, logging) is internal and may change in
a patch release. Adding an optional keyword argument is allowed; changing the
meaning of an existing field is not.
"""

from __future__ import annotations

from .app import ModuleApp, current_call_context, run
from .errors import ToolError
from .spec import ToolSpec

__all__ = ["ModuleApp", "ToolSpec", "ToolError", "run", "current_call_context"]

#: SDK version. Modules pin `vade-sdk==0.1.*`; a breaking change needs 0.2.
__version__ = "0.1.0"
