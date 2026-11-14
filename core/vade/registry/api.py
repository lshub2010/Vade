"""Read-only query surface for C3/C5a plus the governance write surface.

Split on purpose: the query side is safe for the router to call on every turn,
the write side requires an admin identity (enforced by C5a's listener; this
module assumes it is never exposed on the data plane).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from ..errors import VadeError
from .health import HealthState
from .table import NodeRecord, Registry

__all__ = ["RegistryAPI"]

ConcurrencyMode = Literal["exclusive", "pooled", "mirrored"]
Risk = Literal["low", "medium", "high"]


def _node_view(record: NodeRecord) -> dict[str, Any]:
    """Whitelist only. The manifest body is never forwarded (21-C2 §七)."""
    manifest = record.manifest
    return {
        "node_id": record.node_id,
        "module_id": record.module_id,
        "instance_id": record.instance_id,
        "kind": record.kind,
        "auth_mode": record.auth_mode,
        "role": record.role,
        "enabled": record.enabled,
        "health": record.health.value,
        "endpoint": record.endpoint,
        "rtt_ms": record.rtt_ms,
        "tool_count": record.tool_count,
        "version": manifest.version if manifest else None,
        "fingerprint": record.fingerprint,
        "rejected_reason": record.rejected_reason,
        # RFC-0009 §7.1: a page the C-19 validator dropped must be visible on
        # the module card ("page X failed validation"), not silently missing.
        # Counts and warnings only — page bodies never leave the manifest here.
        "ui_page_count": len(manifest.ui_pages) if manifest is not None else 0,
        "ui_page_warnings": list(manifest.ui_warnings) if manifest is not None else [],
    }


class RegistryAPI:
    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._concurrency_overrides: dict[str, str] = {}

    # ------------------------------------------------------------ read-only
    def snapshot_for_admin(self) -> list[dict[str, Any]]:
        return [_node_view(r) for r in self._registry.snapshot()]

    def tool_count(self) -> int:
        return len(self._registry.tools())

    def resolve(self, tool_name: str) -> dict[str, Any] | None:
        binding = self._registry.resolve_tool(tool_name)
        if binding is None:
            return None
        return {
            "tool_id": binding.tool_id,
            "module_id": binding.module_id,
            "instance_id": binding.instance_id,
            "risk": binding.effective_risk,
            "timeout_ms": binding.spec.timeout_ms,
            "idempotent": binding.spec.idempotent,
        }

    def health_of(self, module_id: str, instance_id: str) -> str:
        record = self._registry.get(module_id, instance_id)
        return record.health.value if record else HealthState.PENDING.value

    # ------------------------------------------------------------ governance
    async def set_enabled(self, module_id: str, instance_id: str, enabled: bool) -> None:
        """Disable = stop routing + module/drain + close conn + clear subs/approvals.

        The connection/subscription/approval side effects are wired through
        `Registry(close_connection=...)`; C3/C5a supply the hooks.
        """
        self._registry.set_enabled(module_id, instance_id, enabled)

    async def set_concurrency_override(self, module_id: str, mode: ConcurrencyMode) -> None:
        if mode not in ("exclusive", "pooled", "mirrored"):
            raise VadeError("INVALID_ARGUMENT", f"unknown concurrency mode {mode!r}")
        records = self._registry.by_module(module_id)
        if not records:
            raise VadeError("NOT_FOUND", f"{module_id} is not registered")
        # Overrides apply to future registrations; running instances keep the
        # concurrency they registered with (changing it live would silently
        # drop in-flight calls).
        self._concurrency_overrides[module_id] = mode

    async def set_risk_override(self, module_id: str, tool: str, risk: Risk) -> None:
        """Raise only. Downgrade -> RISK_DOWNGRADE_FORBIDDEN (change core.toml + restart)."""
        self._registry.set_risk_override(module_id, tool, risk)

    async def disconnect(self, module_id: str, instance_id: str) -> None:
        self._registry.disconnect(module_id, instance_id)

    _concurrency_overrides: dict[str, str] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:  # pragma: no cover
        super().__init_subclass__(**kwargs)
