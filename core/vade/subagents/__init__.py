"""C3 subagent component (RFC-0012): run manager, `subagent/*` RPC, wiring.

`install(app)` runs after `session` (alphabetical order is the real
dependency): the manager needs the live `SessionLog` to emit `subagent.*` on
the parent session's seq stream. The arrow points one way — the session
component never imports `vade.subagents`; it duck-types `log.subagents` for
the pre-terminal sweep (RFC-0012 §7.3), exactly like `log.tasks` for the
RFC-0007 interrupt cascade.

Registration rides the same flat `app.session_handlers` table that
`session/rpc.py` publishes and `vade.tasks` extends (RFC-0007 pattern): the
two `subagent/list` / `subagent/get` names are merged with `setdefault`, so
`session/rpc.py` remains the single dispatch surface for ui_to_core methods.
"""

from __future__ import annotations

from typing import Any

from .manager import (
    ALLOWED_TRANSITIONS,
    LABEL_MAX,
    RUN_ID_PATTERN,
    SubagentManager,
    result_digest_of,
)
from .rpc import SubagentRPC

__all__ = [
    "install", "SubagentManager", "SubagentRPC",
    "ALLOWED_TRANSITIONS", "LABEL_MAX", "RUN_ID_PATTERN", "result_digest_of",
]


def install(app: Any) -> Any:
    """Build the manager, publish it on the app, merge the two handlers."""
    log = getattr(app, "session", None) or getattr(app, "session_log", None)
    store = getattr(log, "store", None)

    manager = SubagentManager(log, store=store)
    app.subagents = manager
    app.subagent_manager = manager
    if log is not None:
        # Duck-typed hook read by `SessionLog._turn_ending` (pre-terminal chain).
        # The session component works with this attribute absent (a core that
        # predates RFC-0012, or subagents intentionally disabled).
        log.subagents = manager

    rpc = SubagentRPC(manager)
    app.subagent_rpc = rpc
    handlers = getattr(app, "session_handlers", None)
    if isinstance(handlers, dict):
        for name, handler in rpc.handlers().items():
            handlers.setdefault(name, handler)
    else:  # a bare App (tests): publish a provider the session install drains
        providers = getattr(app, "handler_providers", None)
        if isinstance(providers, list):
            providers.append(rpc.handlers)
    return app
