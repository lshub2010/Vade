"""C3d: the evolve event channel and skill proposal intake.

`install(app)` is the only entry point core calls (C2's `install_components`
looks for exactly this name). It must be idempotent: dev hot reload runs the
component scan twice.

The channel is receive-only. A module declares `subscriptions`, core checks
four permissions (allowlist, manifest, `allow_transcript`, role), and pushes
`events/deliver` notifications with a bounded per-instance queue. Nothing here
lets a module ask for a turn, a session or a skill body.

Two integration facts shape this package:

* **C3 has no per-turn callback.** `SessionLog.emit()` fans out to UI sinks only
  and `session/` is C3-owned, so `pump()` reads the events C3 already persisted
  (`read_since`) and advances a watermark. `install()` adds a prompt-hook tick so
  a live core keeps pumping without an external caller; `app.on_session_end`
  supplies the closing beat.
* **`install_components` is alphabetical, so this package installs before
  `session/`.** The doc's sketch (`app.session_store` at install time) cannot
  hold. The bus therefore resolves its providers lazily, and publishes its RPC
  handlers through `app.handler_providers`: a provider list C3 drains when it
  builds `app.session_handlers`. That is one appended callback, no logic change
  in `session/` -- the same pattern the task book uses for `on_session_end`.
* **Disconnects are per socket.** Today's transport has no app-level close slot;
  `CoreApp` passes `on_close` per `Connection`. `install()` publishes
  `EventBus.connection_closed` on `app.on_disconnect` for a transport that grows
  one, hooks the sockets already up, and every later socket is hooked by
  `on_subscribe`. A closed socket loses its subscription and queue at once.
"""

from __future__ import annotations

from typing import Any, Mapping

from .deliver import DeliveryQueue, EventBus
from .subscribe import (
    MAX_TOPICS, REASONS, TOPICS, Subscription, SubscriptionTable, validate_topics,
)
from . import transcript

__all__ = [
    "install", "wire", "EventBus", "DeliveryQueue", "Subscription",
    "SubscriptionTable", "TOPICS", "REASONS", "MAX_TOPICS", "validate_topics",
    "transcript",
]

#: Topics the bus serves. Kept here so a typo in a config check fails a test,
#: not a runtime subscribe.
HANDLERS: Mapping[str, str] = {"events/subscribe": "on_subscribe"}


class _PumpTick:
    """Prompt-hook adapter: one pump before each turn, no change to the parts.

    A prompt hook is the only per-turn callback C3 exposes to a component. The
    cost is one `read_since` that usually returns nothing; the benefit is that
    the channel is live without borrowing a C3 internal.
    """

    __slots__ = ("bus", "__name__")

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.__name__ = "events_pump_tick"

    def __call__(self, parts: Any) -> Any:
        try:
            self.bus.pump()
        except Exception:  # a push failure must never break a turn
            pass
        return parts


def wire(app: Any, bus: EventBus | None = None) -> dict[str, Any]:
    """Merge `events/*` into whatever `app.session_handlers` currently is.

    Separate from `install()` because the dict is created by C3 *after* this
    package installs. `handler_providers` covers that; `wire()` is the explicit
    version for tests and for any later caller (admin UI, dev reload).
    """
    bus = bus or getattr(app, "event_bus", None)
    if bus is None:
        return {}
    handlers = getattr(app, "session_handlers", None)
    if handlers is None:
        handlers = {}
        app.session_handlers = handlers
    for name, handler in bus.handlers().items():
        handlers.setdefault(name, handler)
    return dict(handlers)


def install(app: Any) -> None:
    """Build the bus once, then append callbacks (never replace)."""
    existing = getattr(app, "event_bus", None)
    if isinstance(existing, EventBus):
        wire(app, existing)
        return

    cfg = app.config
    bus = EventBus(
        app=app,
        allow_transcript=bool(cfg.get("evolve.allow_transcript", False)),
        allowlist=cfg.get("evolve.allowlist") or (),
        # core-side only: the redact list is never taken from a request, and a
        # request that tries to shorten it is recorded (see SubscriptionTable).
        redact=cfg.get("evolve.redact") or (),
    )
    app.event_bus = bus
    app.events = bus

    providers = getattr(app, "handler_providers", None)
    if providers is None:
        providers = []
        app.handler_providers = providers
    if not any(getattr(p, "__self__", None) is bus for p in providers):
        providers.append(bus.handlers)

    wire(app, bus)

    hooks = getattr(app, "prompt_hooks", None)
    if hooks is None:
        hooks = []
        app.prompt_hooks = hooks
    if not any(getattr(h, "bus", None) is bus for h in hooks):
        hooks.append(_PumpTick(bus))

    on_end = getattr(app, "on_session_end", None)
    if on_end is None:
        on_end = []
        app.on_session_end = on_end
    if not any(getattr(h, "__self__", None) is bus for h in on_end):
        on_end.append(bus.emit_closed)

    # Disconnect slot: the transport owns the sockets, so its close path is the
    # only place that knows a subscription died. `CoreApp` has no app-level
    # callback today (it passes `on_close` per `Connection`), so this list is
    # the explicit seam for a transport that grows one; the per-socket chain
    # below is what actually runs now.
    disconnects = getattr(app, "on_disconnect", None)
    if disconnects is None:
        disconnects = []
        app.on_disconnect = disconnects
    if isinstance(disconnects, list) and not any(
            getattr(h, "__self__", None) is bus for h in disconnects):
        disconnects.append(bus.connection_closed)
    connections = getattr(app, "_connections", None)
    live: Any = connections() if callable(connections) else connections
    if live:
        bus.watch_connections(live)

    # C3d also owns the proposal intake; it publishes through the same channel.
    from ..skill.proposal import ProposalService

    if getattr(app, "skill_proposals", None) is None:
        service = ProposalService(app=app, bus=bus)
        app.skill_proposals = service
        if not any(getattr(p, "__self__", None) is service for p in providers):
            providers.append(service.handlers)
    wire(app, bus)
