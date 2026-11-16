"""`module.status` emitter (C-06, 22-C3 T6).

The registry is synchronous and may be mutated from the event loop (module WS
handlers) or an admin thread (C5a). This bridges the two: a change callback
schedules one `SessionLog.emit("module.status", ...)` per session that has a
live sink. That keeps the per-session seq contract (every event is persisted
before it is broadcast) and gives a reconnecting UI the same stream.

Sessions without sinks are skipped on purpose: `module.status` is a trigger for
the admin surface to re-pull `registry_api.snapshot_for_admin()`, not state a
late client has to replay.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from ..errors import VadeError
from ..logging import get_logger

__all__ = ["ModuleStatusPublisher"]

log = get_logger("vade.session.status")


class ModuleStatusPublisher:
    def __init__(self, log_: Any) -> None:
        self._log = log_

    # ------------------------------------------------------------------ wiring
    def attach(self, registry: Any) -> None:
        add = getattr(registry, "add_listener", None)
        if callable(add):
            add(self.on_registry_change)

    # ------------------------------------------------------------------ callback
    def on_registry_change(self, payload: Mapping[str, Any]) -> None:
        """Sync by contract (registry calls it under its lock)."""
        loop = getattr(self._log, "loop", None)
        if loop is None or loop.is_closed():
            return  # nobody attached yet: nothing to broadcast
        snapshot = dict(payload)

        def _spawn() -> None:
            asyncio.ensure_future(self.publish(snapshot))

        try:
            if asyncio.get_running_loop() is loop:
                _spawn()
            else:
                loop.call_soon_threadsafe(_spawn)
        except RuntimeError:  # no loop on this thread (admin thread)
            loop.call_soon_threadsafe(_spawn)

    async def publish(self, payload: Mapping[str, Any]) -> int:
        """Broadcast one change to every attached session; returns event count."""
        emitted = 0
        for session_id in self._log.attached_sessions():
            try:
                # `coalesce=True`: this event is a re-pull trigger, not state a
                # late client has to replay. Counting it as a continuity witness
                # let a burst of module churn exhaust the replay window and turn
                # every reconnect into a full snapshot (RFC-0007 §5.5 R5).
                await self._log.emit(session_id, "module.status", dict(payload),
                                     coalesce=True)
                emitted += 1
            except VadeError as exc:  # session deleted mid-flight
                log.warning("module.status dropped", extra={"code": exc.code, "session_id": session_id,
                                                            "msg": exc.message})
        return emitted
