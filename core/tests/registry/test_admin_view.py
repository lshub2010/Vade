"""Admin view + governance index regressions found by the C2 review."""

from __future__ import annotations

import pytest

from vade.errors import VadeError
from vade.registry.api import RegistryAPI
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry


def manifest(instance="fs-01", risk="low", tool="read_file"):
    return validate_manifest(
        {
            "protocol": "vade/v1",
            "module_id": "com.example.fs",
            "instance_id": instance,
            "version": "1.0.0",
            "concurrency": "pooled",
            "limits": {"max_concurrency": 3, "max_inflight": 8, "max_payload": 65519},
            "capabilities": {
                "tools": [
                    {
                        "name": tool,
                        "description": "read",
                        "risk": risk,
                        "timeout_ms": 10000,
                        "idempotent": False,
                        "input_schema": {"type": "object"},
                    }
                ]
            },
        }
    )


def test_snapshot_for_admin_survives_a_rejected_unknown_node():
    registry = Registry()
    registry.mark_rejected("com.example.stranger", "x1", "peer key is not whitelisted", fingerprint="0" * 16)

    [view] = RegistryAPI(registry).snapshot_for_admin()
    assert view["node_id"] == "com.example.stranger#x1"
    assert view["health"] == "rejected"
    assert view["rejected_reason"]
    assert view["tool_count"] == 0
    assert view["version"] is None


def test_mark_rejected_keeps_the_manifest_of_a_known_node():
    registry = Registry()
    registry.register(manifest(), fingerprint="a" * 16, connection_id="c1")
    registry.mark_rejected("com.example.fs", "fs-01", "version mismatch", fingerprint="a" * 16)
    [view] = RegistryAPI(registry).snapshot_for_admin()
    assert view["tool_count"] == 1
    assert view["version"] == "1.0.0" or view["version"] is not None


def test_disabling_one_pooled_instance_keeps_the_others_routable():
    registry = Registry()
    registry.register(manifest("fs-01"), fingerprint="a" * 16, connection_id="c1")
    registry.register(manifest("fs-02"), fingerprint="b" * 16, connection_id="c2")
    assert len(registry.bindings_for("read_file")) == 2

    registry.set_enabled("com.example.fs", "fs-01", False)

    keys = {f"{b.module_id}/{b.spec.name}@{b.instance_id}" for b in registry.bindings_for("read_file")}
    assert keys == {"com.example.fs/read_file@fs-02"}
    assert registry.resolve_tool("read_file") is not None
    assert registry.tool_count() == 1


def test_capabilities_update_compares_against_the_effective_risk():
    registry = Registry()
    registry.register(manifest(risk="low"), fingerprint="a" * 16, connection_id="c1")
    # An admin override raised the effective risk to high.
    registry.set_risk_override("com.example.fs", "read_file", "high")

    with pytest.raises(VadeError) as excinfo:
        registry.update_capabilities(manifest(risk="medium"), fingerprint="a" * 16)
    assert excinfo.value.code == "RISK_DOWNGRADE_FORBIDDEN"
    assert "high" in excinfo.value.message

    # The old manifest is intact.
    assert registry.get("com.example.fs", "fs-01").manifest.tool("read_file").risk == "low"


def test_capabilities_update_can_keep_the_same_effective_risk():
    registry = Registry()
    registry.register(manifest(risk="low"), fingerprint="a" * 16, connection_id="c1")
    epoch = registry.update_capabilities(manifest(risk="low"), fingerprint="a" * 16)
    assert epoch == 2
