"""Health states (design §7.3). Registry only READS circuit state; C3 owns it."""

from __future__ import annotations

from enum import Enum

__all__ = ["HealthState", "compute_health", "STALE_FACTOR", "DEGRADED_TIMEOUT_RATE"]

STALE_FACTOR = 2.0
DEGRADED_TIMEOUT_RATE = 0.20


class HealthState(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    REJECTED = "rejected"
    PENDING = "pending"


def compute_health(
    *,
    connected: bool,
    last_frame_age_s: float | None,
    heartbeat_interval_s: float,
    circuit_open: bool = False,
    timeout_rate: float = 0.0,
) -> HealthState:
    """Five-state decision, in priority order.

    `degraded` exists because a connection that is up but timing out every call
    would otherwise show green — the most misleading thing an operator can see.
    """
    if not connected:
        return HealthState.OFFLINE
    if circuit_open or timeout_rate > DEGRADED_TIMEOUT_RATE:
        return HealthState.DEGRADED
    if last_frame_age_s is not None and last_frame_age_s > STALE_FACTOR * heartbeat_interval_s:
        return HealthState.OFFLINE
    return HealthState.ONLINE
