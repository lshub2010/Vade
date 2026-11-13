"""C-03 manifest rejection: the six illegal classes, each pointing at a field."""

from __future__ import annotations

import copy
import json

import pytest

from vade.registry.manifest import ManifestError, validate_manifest

GOOD = {
    "module_id": "com.example.fs",
    "instance_id": "fs-01",
    "version": "1.0.0",
    "protocol": "vade/v1",
    "concurrency": "pooled",
    "role": "tool",
    "capabilities": {
        "tools": [
            {
                "name": "read_file",
                "description": "Read a UTF-8 text file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                "risk": "low",
                "idempotent": True,
                "timeout_ms": 10000,
            }
        ]
    },
    "limits": {"max_concurrency": 4, "max_payload": 65519},
}


def _mutated(**changes):
    data = copy.deepcopy(GOOD)
    for dotted, value in changes.items():
        node = data
        parts = dotted.split("__")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return data


def test_good_manifest_is_accepted():
    manifest = validate_manifest(GOOD)
    assert manifest.module_id == "com.example.fs"
    assert manifest.tool_names == ("read_file",)
    assert manifest.limits.max_payload == 65519
    assert len(manifest.digest) == 64


@pytest.mark.parametrize(
    "payload,pointer",
    [
        # 1. bad module_id (uppercase, not reverse DNS)
        (_mutated(module_id="Com.Example"), "/module_id"),
        # 2. protocol major mismatch
        (_mutated(protocol="vade/v2"), "/protocol"),
        # 3. bad tool name (hyphen)
        (_mutated(capabilities__tools=[{**GOOD["capabilities"]["tools"][0], "name": "read-file"}]), "/capabilities/tools/0/name"),
        # 4. input_schema root is not an object
        (
            _mutated(capabilities__tools=[{**GOOD["capabilities"]["tools"][0], "input_schema": {"type": "string"}}]),
            "/capabilities/tools/0/input_schema/type",
        ),
        # 5. risk outside the enum
        (_mutated(capabilities__tools=[{**GOOD["capabilities"]["tools"][0], "risk": "critical"}]), "/capabilities/tools/0/risk"),
        # 6. tool budget exceeded
        (
            _mutated(capabilities__tools=[{**GOOD["capabilities"]["tools"][0], "name": f"t{i}"} for i in range(65)]),
            "/capabilities/tools",
        ),
    ],
)
def test_six_illegal_manifest_classes_are_rejected(payload, pointer):
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(payload)
    err = excinfo.value
    assert err.code == "MANIFEST_INVALID"
    assert err.pointer.startswith(pointer), f"expected pointer starting with {pointer}, got {err.pointer}"
    # The readable message must name the offending value/rule.
    assert err.message


def test_protocol_mismatch_names_both_versions():
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(_mutated(protocol="vade/v2"))
    message = str(excinfo.value)
    assert "vade/v2" in message and "vade/v1" in message


def test_blank_description_rejected():
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(_mutated(capabilities__tools=[{**GOOD["capabilities"]["tools"][0], "description": "   "}]))
    assert excinfo.value.pointer == "/capabilities/tools/0/description"


def test_timeout_out_of_range_rejected():
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(_mutated(capabilities__tools=[{**GOOD["capabilities"]["tools"][0], "timeout_ms": 50}]))
    assert excinfo.value.pointer == "/capabilities/tools/0/timeout_ms"
    assert "100" in excinfo.value.message


def test_max_payload_above_frame_ceiling_rejected():
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(_mutated(limits={"max_concurrency": 1, "max_payload": 1048576}))
    assert excinfo.value.pointer == "/limits/max_payload"
    assert "65519" in excinfo.value.message


def test_unknown_fields_are_ignored():
    data = _mutated(future_field={"anything": True})
    manifest = validate_manifest(data)
    assert manifest.module_id == "com.example.fs"


def test_unknown_injection_source_rejected():
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(_mutated(injects=["session_id", "secrets"]))
    assert excinfo.value.pointer == "/injects/1"


def test_manifest_is_immutable():
    manifest = validate_manifest(GOOD)
    with pytest.raises(Exception):
        manifest.module_id = "other"  # type: ignore[misc]


def test_schema_file_is_valid_json():
    from vade.registry.manifest import load_schema

    schema = load_schema()
    assert schema["x-vade"]["contract"] == "C-03"
    json.dumps(schema)  # round-trips
