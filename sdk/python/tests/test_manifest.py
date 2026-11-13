"""Manifest generation must satisfy C-03 for real, not "looks right"."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vade_sdk import ModuleApp, ToolSpec
from vade_sdk.app import DEFAULT_MAX_PAYLOAD

REPO = Path(__file__).resolve().parents[3]


def echo(text: str) -> dict:
    return {"text": text}


def make_app(**kwargs) -> ModuleApp:
    app = ModuleApp("com.example.test", "1.2.3", concurrency="pooled", **kwargs)
    app.add_tool(ToolSpec(name="echo", fn=echo, description="echo text", timeout_ms=5000))
    return app


def test_manifest_required_fields_and_defaults():
    manifest = make_app().manifest()
    assert manifest["module_id"] == "com.example.test"
    assert manifest["instance_id"] == "main"
    assert manifest["protocol"] == "vade/v1"
    assert manifest["role"] == "tool"
    assert manifest["concurrency"] == "pooled"
    assert manifest["limits"] == {"max_concurrency": 4, "max_payload": DEFAULT_MAX_PAYLOAD}
    entry = manifest["capabilities"]["tools"][0]
    assert set(entry) == {"name", "description", "input_schema", "risk", "timeout_ms", "idempotent"}
    assert entry["risk"] == "low" and entry["idempotent"] is True
    assert entry["input_schema"]["required"] == ["text"]


def test_manifest_is_deterministic_and_print_matches_register_payload(capsys):
    app = make_app()
    first = app.manifest_json()
    assert app.manifest_json() == first
    assert app.run(["--print-manifest"]) == 0
    captured = capsys.readouterr().out
    assert json.loads(captured) == json.loads(first)
    assert captured == first


def test_manifest_passes_the_frozen_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "schema" / "manifest.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(make_app(display_name="测试").manifest(), schema)


def test_manifest_passes_core_validate_manifest():
    """Cross-check against C2's implementation — the judge, not a copy of it."""
    pytest.importorskip("vade")
    from vade.registry.manifest import validate_manifest

    parsed = validate_manifest(make_app(display_name="测试").manifest())
    assert parsed.tool_names == ("echo",)
    assert parsed.protocol_major == 1


def test_check_reports_problems_not_tracebacks():
    app = ModuleApp("com.example.test", "1.0.0")
    problems = app.check()
    assert any("empty" in p for p in problems)


def test_bad_constructor_arguments_fail_fast():
    with pytest.raises(ValueError, match="module_id"):
        ModuleApp("Not-Reverse-DNS", "1.0.0")
    with pytest.raises(ValueError, match="version"):
        ModuleApp("com.example.x", "1.0")
    with pytest.raises(ValueError, match="concurrency"):
        ModuleApp("com.example.x", "1.0.0", concurrency="sharded")
    with pytest.raises(ValueError, match="capability"):
        ModuleApp("com.example.x", "1.0.0", capabilities=["tools", "prompts"])
