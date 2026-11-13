"""Registry behaviour: concurrency, conflicts, revocation, capability updates."""

from __future__ import annotations

import copy

import pytest

from vade.errors import VadeError
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry


def manifest(instance="fs-01", module="com.example.fs", concurrency="exclusive", tool="read_file", risk="low", max_concurrency=1):
    return validate_manifest(
        {
            "module_id": module,
            "instance_id": instance,
            "version": "1.0.0",
            "protocol": "vade/v1",
            "concurrency": concurrency,
            "capabilities": {
                "tools": [
                    {
                        "name": tool,
                        "description": f"{tool} does something",
                        "input_schema": {"type": "object", "properties": {}},
                        "risk": risk,
                        "idempotent": True,
                        "timeout_ms": 1000,
                    }
                ]
            },
            "limits": {"max_concurrency": max_concurrency, "max_payload": 65519},
        }
    )


def test_first_registration_is_accepted():
    reg = Registry()
    result = reg.register(manifest(), fingerprint="a" * 16, connection_id="c1")
    assert result.accepted is True
    assert result.tool_names == ("read_file",)
    assert reg.resolve_tool("read_file").module_id == "com.example.fs"


def test_exclusive_second_instance_is_rejected():
    reg = Registry()
    reg.register(manifest(), fingerprint="a" * 16, connection_id="c1")
    with pytest.raises(VadeError) as excinfo:
        reg.register(manifest(instance="fs-02"), fingerprint="a" * 16, connection_id="c2")
    assert excinfo.value.code == "TOOL_CONFLICT"


def test_pooled_three_instances_are_all_registered():
    reg = Registry()
    for i in range(3):
        reg.register(
            manifest(instance=f"pool-{i}", concurrency="pooled", max_concurrency=4),
            fingerprint="a" * 16,
            connection_id=f"c{i}",
        )
    assert len(reg.snapshot()) == 3
    # Capability index: three bindings for the same tool, one per instance.
    assert len(reg.bindings_for("read_file")) == 3
    assert reg.tool_count() == 1


def test_pooled_beyond_max_concurrency_is_rejected():
    reg = Registry()
    reg.register(manifest(instance="pool-0", concurrency="pooled", max_concurrency=2), fingerprint="a" * 16, connection_id="c0")
    reg.register(manifest(instance="pool-1", concurrency="pooled", max_concurrency=2), fingerprint="a" * 16, connection_id="c1")
    with pytest.raises(VadeError) as excinfo:
        reg.register(manifest(instance="pool-2", concurrency="pooled", max_concurrency=2), fingerprint="a" * 16, connection_id="c2")
    assert excinfo.value.code == "TOOL_CONFLICT"


def test_tool_name_conflict_rejects_the_later_registrant():
    reg = Registry()
    reg.register(manifest(module="com.example.fs"), fingerprint="a" * 16, connection_id="c1")
    with pytest.raises(VadeError) as excinfo:
        reg.register(manifest(module="com.example.other"), fingerprint="b" * 16, connection_id="c2")
    err = excinfo.value
    assert err.code == "TOOL_CONFLICT"
    assert "/capabilities/tools/0/name" in err.message
    assert "read_file" in err.message
    # The first registrant is untouched.
    assert reg.resolve_tool("read_file").module_id == "com.example.fs"


def test_different_fingerprint_cannot_steal_an_instance():
    reg = Registry()
    reg.register(manifest(), fingerprint="a" * 16, connection_id="c1")
    with pytest.raises(VadeError) as excinfo:
        reg.register(manifest(), fingerprint="b" * 16, connection_id="c2")
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.fatal is True


def test_capabilities_update_is_atomic_and_bumps_epoch():
    reg = Registry()
    reg.register(manifest(), fingerprint="a" * 16, connection_id="c1")
    epoch = reg.cap_epoch("com.example.fs")
    updated = manifest(tool="write_file", risk="medium")
    assert reg.update_capabilities(updated, fingerprint="a" * 16) == epoch + 1
    assert reg.resolve_tool("write_file") is not None
    assert reg.resolve_tool("read_file") is None


def test_invalid_capabilities_update_keeps_the_old_manifest():
    reg = Registry()
    reg.register(manifest(), fingerprint="a" * 16, connection_id="c1")
    reg.register(manifest(module="com.example.web", instance="w-1", tool="fetch"), fingerprint="b" * 16, connection_id="c2")
    # com.example.fs tries to take over the tool name "fetch" -> rejected whole.
    conflicting = manifest(module="com.example.fs", tool="fetch")
    with pytest.raises(VadeError) as excinfo:
        reg.update_capabilities(conflicting, fingerprint="a" * 16)
    assert excinfo.value.code == "TOOL_CONFLICT"
    # Old capability still resolvable: the failed update never half-applied.
    assert reg.resolve_tool("read_file") is not None
    assert reg.resolve_tool("fetch").module_id == "com.example.web"


def test_risk_downgrade_is_forbidden():
    reg = Registry()
    reg.register(manifest(risk="high"), fingerprint="a" * 16, connection_id="c1")
    with pytest.raises(VadeError) as excinfo:
        reg.set_risk_override("com.example.fs", "read_file", "low")
    assert excinfo.value.code == "RISK_DOWNGRADE_FORBIDDEN"


def test_revoke_closes_live_connections_and_marks_rejected():
    closed: list[str] = []
    reg = Registry(close_connection=closed.append)
    reg.register(manifest(), fingerprint="a" * 16, connection_id="c1")
    assert reg.revoke_fingerprint("a" * 16) == ("c1",)
    assert closed == ["c1"]
    record = reg.get("com.example.fs", "fs-01")
    assert record.health.value == "rejected"


def test_health_transitions_to_offline_but_record_is_kept():
    now = [0.0]
    reg = Registry(clock=lambda: now[0], heartbeat_interval_s=10)
    reg.register(manifest(), fingerprint="a" * 16, connection_id="c1")
    assert reg.get("com.example.fs", "fs-01").health.value == "online"
    now[0] = 25.0
    reg.refresh_health()
    assert reg.get("com.example.fs", "fs-01").health.value == "offline"
    assert reg.get("com.example.fs", "fs-01") is not None
    reg.mark_disconnected("com.example.fs", "fs-01")
    assert reg.get("com.example.fs", "fs-01").health.value == "offline"


def test_unregistered_capabilities_update_is_not_found():
    reg = Registry()
    with pytest.raises(VadeError) as excinfo:
        reg.update_capabilities(manifest(), fingerprint="a" * 16)
    assert excinfo.value.code == "NOT_FOUND"
