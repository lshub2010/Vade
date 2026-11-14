"""Manifest generation must satisfy C-03 for real, not "looks right"."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from vade_sdk import ModuleApp, ToolSpec
from vade_sdk.app import DEFAULT_MAX_PAYLOAD

REPO = Path(__file__).resolve().parents[3]

#: One page that C-03 accepts verbatim; each ui test clones it so a mutation in
#: one test cannot leak into another through a shared nested `schema` dict.
ECHO_PAGE: dict = {
    "id": "echo",
    "title": "回声",
    "icon": "list",
    "version": 1,
    "schema": {
        "type": "column",
        "children": [
            {"type": "text", "props": {"text": "这个页面由模块的 manifest 声明（C-03 ui.pages）。"}},
        ],
    },
}


def make_page(**overrides) -> dict:
    page = copy.deepcopy(ECHO_PAGE)
    page.update(overrides)
    return page


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


# --------------------------------------------------- ui_pages (C-19 / RFC-0009 §9.2)
def test_ui_pages_are_emitted_under_ui_pages():
    manifest = make_app(ui_pages=[make_page()]).manifest()
    assert set(manifest) - set(make_app().manifest()) == {"ui"}
    assert manifest["ui"] == {"pages": [ECHO_PAGE]}


def test_ui_block_is_absent_when_no_pages_are_declared():
    """FR-8.79: no pages means no `ui` block at all — not `{"pages": []}`.

    Byte-for-byte, so a module that declares no page keeps the exact payload it
    had before `ui_pages` existed. An empty tab or a greyed row would be a
    visible regression, and `"ui": {"pages": []}` is what would produce one.
    """
    without = make_app().manifest_json()
    explicitly_empty = make_app(ui_pages=[]).manifest_json()
    assert explicitly_empty == without
    assert '"ui"' not in without


def test_print_manifest_emits_the_ui_block(capsys):
    app = make_app(ui_pages=[make_page()])
    assert app.run(["--print-manifest"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["ui"] == {"pages": [ECHO_PAGE]}
    assert printed == app.manifest_json()


def test_ui_pages_keep_declaration_order():
    pages = [make_page(id="alpha"), make_page(id="beta"), make_page(id="gamma")]
    app = make_app(ui_pages=pages)
    assert [page["id"] for page in app.manifest()["ui"]["pages"]] == ["alpha", "beta", "gamma"]
    printed = json.loads(app.manifest_json())
    assert [page["id"] for page in printed["ui"]["pages"]] == ["alpha", "beta", "gamma"]
    # sort_keys sorts the KEYS of a page, never the array itself.
    assert app.manifest_json().index('"alpha"') < app.manifest_json().index('"beta"') < app.manifest_json().index('"gamma"')


def test_bad_page_id_fails_loudly_and_names_the_page():
    """Silent dropping is the failure mode: core drops the page, the tab is
    empty, and nobody reads the warning. The constructor must refuse instead."""
    with pytest.raises(ValueError) as excinfo:
        make_app(ui_pages=[make_page(id="Echo")])
    message = str(excinfo.value)
    assert "Echo" in message and "ui_pages[0]" in message and "page id" in message

    with pytest.raises(ValueError) as excinfo:
        make_app(ui_pages=[make_page(), make_page(id="2bad")])
    message = str(excinfo.value)
    assert "ui_pages[1]" in message and "2bad" in message

    with pytest.raises(ValueError) as excinfo:
        make_app(ui_pages=[make_page(id="a" * 33)])
    assert "a" * 33 in str(excinfo.value)


def test_page_missing_a_required_key_is_named_not_dropped():
    page = make_page()
    del page["schema"]
    with pytest.raises(ValueError) as excinfo:
        make_app(ui_pages=[page])
    message = str(excinfo.value)
    assert "page id 'echo'" in message
    assert "missing required key(s) ['schema']" in message

    with pytest.raises(ValueError) as excinfo:
        make_app(ui_pages=["not a mapping"])
    assert "ui_pages[0]" in str(excinfo.value) and "mapping" in str(excinfo.value)


def test_sdk_checks_shape_only_and_leaves_the_page_schema_to_core():
    """The icon enum is C-03's rule, not the SDK's: it must pass through here.

    If this ever fails because the SDK started raising, the SDK has grown a
    second copy of `manifest.schema.json` — the drift the docstring forbids.
    """
    page = make_page(icon="not-in-the-enum", entry="auto")
    assert make_app(ui_pages=[page]).manifest()["ui"]["pages"][0]["icon"] == "not-in-the-enum"


def test_ui_pages_pass_the_frozen_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "schema" / "manifest.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(make_app(display_name="测试", ui_pages=[make_page()]).manifest(), schema)


def test_ui_pages_pass_core_validate_manifest_and_core_remains_the_judge():
    pytest.importorskip("vade")
    from vade.registry.manifest import validate_manifest

    parsed = validate_manifest(make_app(ui_pages=[make_page()]).manifest())
    assert [page.page_id for page in parsed.ui_pages] == ["echo"]
    assert parsed.ui_warnings == ()
    assert parsed.tool_names == ("echo",)

    # Core, not the SDK, owns degradation: an unknown icon costs a warning and
    # keeps the page; the module's tools register either way (RFC-0009 §7.1).
    degraded = validate_manifest(make_app(ui_pages=[make_page(icon="nope")]).manifest())
    assert [page.page_id for page in degraded.ui_pages] == ["echo"]
    assert degraded.ui_pages[0].icon == "none"
    assert degraded.ui_warnings and degraded.tool_names == ("echo",)


def test_pages_are_copied_so_later_mutation_cannot_rewrite_the_manifest():
    page = make_page()
    app = make_app(ui_pages=[page])
    page["title"] = "改过了"
    page["schema"]["type"] = "row"
    emitted = app.manifest()["ui"]["pages"][0]
    assert emitted["title"] == "回声"
    assert emitted["schema"]["type"] == "column"
    # ... and mutating the emitted manifest must not reach the stored pages.
    emitted["title"] = "又改"
    assert app.manifest()["ui"]["pages"][0]["title"] == "回声"

