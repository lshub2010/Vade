"""Registry package: manifest validation, instance table, health, admin API."""

from __future__ import annotations

from typing import Any

from .api import RegistryAPI
from .health import HealthState, compute_health
from .manifest import (
    MAX_FRAME_PLAINTEXT,
    MAX_TOOLS_GLOBAL,
    MAX_TOOLS_PER_MODULE,
    TOOL_NAME_RE,
    Limits,
    Manifest,
    ManifestError,
    ToolSpec,
    load_schema,
    validate_manifest,
)
from .table import NodeRecord, Registration, Registry, ToolBinding

__all__ = [
    "Registry",
    "RegistryAPI",
    "NodeRecord",
    "ToolBinding",
    "Registration",
    "HealthState",
    "compute_health",
    "Manifest",
    "ManifestError",
    "ToolSpec",
    "Limits",
    "validate_manifest",
    "load_schema",
    "TOOL_NAME_RE",
    "MAX_TOOLS_GLOBAL",
    "MAX_TOOLS_PER_MODULE",
    "MAX_FRAME_PLAINTEXT",
    "install",
]


def install(app: Any) -> None:
    if getattr(app, "registry", None) is not None:
        return
    app.registry = Registry(heartbeat_interval_s=getattr(app, "heartbeat_interval_s", 10.0))
    app.registry_api = RegistryAPI(app.registry)
