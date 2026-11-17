"""Per-instance circuit breaker (30s window, 5 failures or >20% timeouts).

States: closed -> open (30s) -> half_open (one probe) -> closed / open.
The router asks `allow()` before each call; registry reads `degraded()` to paint
the node, but this module never imports the registry (no reverse dependency).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

__all__ = ["CircuitBreaker", "CLOSED", "OPEN", "HALF_OPEN"]

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class _Outcome:
    ts: float
    ok: bool
    timeout: bool


class _State:
    __slots__ = ("phase", "opened_at", "events", "probe_in_flight")

    def __init__(self) -> None:
        self.phase = CLOSED
        self.opened_at = 0.0
        self.events: deque[_Outcome] = deque()
        self.probe_in_flight = False


class CircuitBreaker:
    def __init__(
        self,
        *,
        window_s: float = 30.0,
        failure_threshold: int = 5,
        timeout_rate: float = 0.2,
        open_s: float = 30.0,
        min_samples: int = 5,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[str, str, bool, float], None] | None = None,
    ) -> None:
        self._window_s = window_s
        self._failure_threshold = failure_threshold
        self._timeout_rate = timeout_rate
        self._open_s = open_s
        self._min_samples = min_samples
        self._clock = clock
        self._on_change = on_change
        self._states: dict[tuple[str, str], _State] = {}

    def _state(self, module_id: str, instance_id: str) -> _State:
        key = (module_id, instance_id)
        state = self._states.get(key)
        if state is None:
            state = _State()
            self._states[key] = state
        return state

    def _prune(self, state: _State, now: float) -> None:
        cutoff = now - self._window_s
        while state.events and state.events[0].ts < cutoff:
            state.events.popleft()

    def allow(self, module_id: str, instance_id: str) -> bool:
        state = self._state(module_id, instance_id)
        now = self._clock()
        if state.phase == CLOSED:
            return True
        if state.phase == OPEN:
            if now - state.opened_at >= self._open_s:
                state.phase = HALF_OPEN
                state.probe_in_flight = True
                self._notify(module_id, instance_id, state, timeout_rate=0.0)
                return True
            return False
        # half_open: exactly one probe
        if state.probe_in_flight:
            return False
        state.probe_in_flight = True
        return True

    def reset(self, module_id: str, instance_id: str) -> None:
        """Forget every outcome for one instance.

        A module that re-registers after a restart (or a reconnect) starts a
        NEW link: the old window is about a connection that no longer exists.
        Without this, a brief outage fills the window with five failures, the
        breaker opens for 30s, and every call is refused even though the module
        is back — the classic "circuit-open after reconnect" false outage.
        """
        self._states.pop((module_id, instance_id), None)

    def record_success(self, module_id: str, instance_id: str) -> None:
        state = self._state(module_id, instance_id)
        state.phase = CLOSED
        state.probe_in_flight = False
        state.events.clear()
        self._notify(module_id, instance_id, state, timeout_rate=0.0)

    def record_failure(self, module_id: str, instance_id: str, *, timeout: bool = False) -> None:
        state = self._state(module_id, instance_id)
        now = self._clock()
        state.probe_in_flight = False
        if state.phase == HALF_OPEN:
            self._open(state, now)
            self._notify(module_id, instance_id, state, timeout_rate=self._rate(state))
            return
        self._prune(state, now)
        state.events.append(_Outcome(ts=now, ok=False, timeout=timeout))
        failures = sum(1 for e in state.events if not e.ok)
        rate = self._rate(state)
        if failures >= self._failure_threshold or (
            len(state.events) >= self._min_samples and rate > self._timeout_rate
        ):
            self._open(state, now)
        self._notify(module_id, instance_id, state, timeout_rate=rate)

    def _open(self, state: _State, now: float) -> None:
        state.phase = OPEN
        state.opened_at = now

    def _rate(self, state: _State) -> float:
        if not state.events:
            return 0.0
        return sum(1 for e in state.events if e.timeout) / len(state.events)

    def state(self, module_id: str, instance_id: str) -> str:
        state = self._state(module_id, instance_id)
        if state.phase == OPEN and self._clock() - state.opened_at >= self._open_s:
            return HALF_OPEN
        return state.phase

    def timeout_rate(self, module_id: str, instance_id: str) -> float:
        state = self._state(module_id, instance_id)
        self._prune(state, self._clock())
        return self._rate(state)

    def degraded(self) -> tuple[tuple[str, str, str], ...]:
        now = self._clock()
        out = []
        for (module_id, instance_id), state in sorted(self._states.items()):
            phase = state.phase
            if phase == OPEN and now - state.opened_at >= self._open_s:
                phase = HALF_OPEN
            if phase != CLOSED:
                out.append((module_id, instance_id, phase))
        return tuple(out)

    def _notify(self, module_id: str, instance_id: str, state: _State, *, timeout_rate: float) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(module_id, instance_id, state.phase == OPEN, timeout_rate)
        except Exception:  # pragma: no cover - a registry hook must not break routing
            pass
