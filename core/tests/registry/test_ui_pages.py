"""C-19 page validation (RFC-0009 A1/A2/A5/A8/A13, core side).

Every rejection here is the *registration* checkpoint of §7.1: the page is
dropped, the manifest's tools still register, and the warning names the page.
The render/fetch checkpoints re-run the same table (`core/tests/uipage/`).
"""

from __future__ import annotations

import copy
import json

import pytest

from vade.registry.manifest import ManifestError, validate_manifest
from vade.registry.uipage import (
    MAX_ACTIONS_PER_PAGE,
    MAX_CHILDREN,
    MAX_DEPTH,
    MAX_INLINE_BYTES,
    MAX_LIST_ITEMS,
    MAX_NODES,
    MAX_PAGES,
    MAX_TEXT_CHARS,
    UIPageError,
    validate_page,
    validate_ui,
)

GOOD_TOOL = {
    "name": "refresh",
    "description": "Refresh the download queue",
    "input_schema": {"type": "object", "properties": {"all": {"type": "boolean"}}},
    "risk": "low",
    "idempotent": True,
    "timeout_ms": 10000,
}

GOOD_MANIFEST = {
    "module_id": "com.example.download",
    "instance_id": "dl-01",
    "version": "1.0.0",
    "protocol": "vade/v1",
    "capabilities": {"tools": [GOOD_TOOL]},
    "limits": {"max_concurrency": 1, "max_payload": 65519},
}

PAGE = {
    "id": "queue",
    "title": "下载队列",
    "version": 1,
    "schema": {
        "type": "column",
        "props": {"gap": "m"},
        "children": [
            {"type": "text", "data_ref": "page.state.queue_len", "props": {"style": "title", "format": "int"}},
            {"type": "text", "props": {"text": "在线"}, "visible": {"bind": "module.status.online"}},
            {
                "type": "list",
                "data_ref": "page.state.items",
                "props": {"empty_text": "队列为空"},
                "children": [
                    {
                        "type": "component",
                        "component": "ListItem",
                        "props": {
                            "title": {"bind": "item.name"},
                            "subtitle": {"bind": "item.progress", "format": "percent"},
                        },
                    }
                ],
            },
            {
                "type": "component",
                "component": "Button",
                "props": {"text": "刷新", "style": "primary"},
                "on": {"click": {"kind": "tools/call", "tool": "refresh", "args": {}}},
            },
        ],
    },
}


def with_ui(*pages: dict, **extra: object) -> dict:
    data = copy.deepcopy(GOOD_MANIFEST)
    data["ui"] = {"pages": [copy.deepcopy(p) for p in pages], **extra}
    return data


def page_with(**changes: object) -> dict:
    page = copy.deepcopy(PAGE)
    page.update(changes)
    return page


def root_of(page: dict) -> dict:
    return page["schema"]


# ------------------------------------------------------------------ happy path
def test_rfc_example_page_is_accepted():
    pages, warnings = validate_ui({"pages": [copy.deepcopy(PAGE)]}, module_tools=("refresh",))
    assert warnings == ()
    assert [p.page_id for p in pages] == ["queue"]
    assert pages[0].title == "下载队列"
    assert pages[0].component_names() == ("ListItem", "Button")
    assert [(a.kind, a.target) for a in pages[0].actions()] == [("tools/call", "refresh")]


def test_all_seven_components_are_accepted():
    children = [
        {"type": "component", "component": "Button", "props": {"text": "Go"},
         "on": {"click": {"kind": "navigate", "to": "module:queue"}}},
        {"type": "component", "component": "TextField", "props": {"label": "q", "value": {"input": "q"},
                                                                  "max_length": 100}},
        {"type": "component", "component": "Card", "props": {"title": "t"},
         "children": [{"type": "text", "data_ref": "page.state.note"}]},
        {"type": "component", "component": "ListItem", "props": {"title": "row"}},
        {"type": "component", "component": "Switch", "data_ref": "page.state.enabled",
         "on": {"change": {"kind": "tools/call", "tool": "refresh", "args": {}}}},
        {"type": "component", "component": "ToolCallCard", "props": {"tool": "refresh", "state": "ok"}},
        {"type": "component", "component": "AttachmentChip",
         "props": {"file_id": "f-1", "mime": "image/png", "name": "a.png", "state": "ready"},
         "on": {"click": {"kind": "navigate", "to": "session:current"}}},
    ]
    page = page_with(schema={"type": "column", "children": children})
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert warnings == ()
    assert pages[0].component_names() == (
        "Button", "TextField", "Card", "ListItem", "Switch", "ToolCallCard", "AttachmentChip",
    )


def test_manifest_parses_pages_and_keeps_order():
    manifest = validate_manifest(with_ui(PAGE, page_with(id="stats", title="统计")))
    assert [p.page_id for p in manifest.ui_pages] == ["queue", "stats"]
    assert manifest.ui_warnings == ()
    assert manifest.tool_names == ("refresh",)


def test_module_without_ui_has_no_pages():
    manifest = validate_manifest(copy.deepcopy(GOOD_MANIFEST))
    assert manifest.ui_pages == ()
    assert manifest.ui_warnings == ()


def test_ref_page_is_pinned_by_digest():
    page = page_with(schema={"ref": {"page_id": "queue", "sha256": "a" * 64}})
    pages, warnings = validate_ui({"pages": [page]})
    assert warnings == ()
    assert pages[0].is_ref and pages[0].ref == ("queue", "a" * 64)
    with pytest.raises(UIPageError):
        validate_page(page_with(schema={"ref": {"page_id": "other", "sha256": "a" * 64}}),
                      page_ids=["queue"])


# ------------------------------------------------------------------ fail-closed
@pytest.mark.parametrize("key", ["script", "on_click", "onClick", "on_load", "eval", "innerHTML", "template"])
def test_script_keys_reject_the_page(key):
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][0]["props"][key] = "alert(1)"
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == ()
    assert "script-shaped key" in warnings[0]


@pytest.mark.parametrize("component", ["MessageBubble", "InputBar", "ApprovalDialog", "BottomBar", "Foo", "Grid"])
def test_host_and_unknown_components_are_rejected(component):
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["component"] = component
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == ()
    assert component in warnings[0]


@pytest.mark.parametrize("node_type", ["grid", "stack", "iframe", "webview", "scroll"])
def test_unknown_node_types_are_rejected(node_type):
    page = page_with(schema={"type": node_type, "children": []})
    pages, warnings = validate_ui({"pages": [page]})
    assert pages == () and node_type in warnings[0]


@pytest.mark.parametrize("kind", ["http.request", "shell", "state.set", "rpc"])
def test_unknown_action_kinds_are_rejected(kind):
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"] = {"kind": kind, "to": "x"}
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == ()
    assert "unknown action kind" in warnings[0]


def test_action_extra_key_is_rejected():
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"]["then"] = {"kind": "navigate", "to": "session:current"}
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == () and "then" in warnings[0]


def test_unknown_event_is_rejected():
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"] = {"press": {"kind": "navigate", "to": "session:current"}}
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == () and "press" in warnings[0]


def test_page_level_unknown_key_is_rejected():
    page = page_with(script="alert(1)")
    pages, warnings = validate_ui({"pages": [page]})
    assert pages == () and "script" in warnings[0]


def test_ui_block_unknown_key_drops_every_page():
    pages, warnings = validate_ui({"pages": [copy.deepcopy(PAGE)], "on_load": "x"}, module_tools=("refresh",))
    assert pages == ()
    assert "unknown key" in warnings[0]


# --------------------------------------------------------------------- actions
@pytest.mark.parametrize("target", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "ftp://h/x"])
def test_open_url_rejects_non_http_schemes(target):
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"] = {"kind": "open_url", "to": target}
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == ()
    assert "http/https" in warnings[0]


@pytest.mark.parametrize("target", ["https://example.com/docs", "http://192.168.1.2:8080/x?y=1"])
def test_open_url_accepts_http_and_https(target):
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"] = {"kind": "open_url", "to": target}
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert warnings == ()
    assert pages[0].actions()[0].target == target


@pytest.mark.parametrize("target", ["module:missing", "https://evil.example", "settings:secrets", "session:"])
def test_navigate_out_of_bounds_is_rejected(target):
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"] = {"kind": "navigate", "to": target}
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == ()


@pytest.mark.parametrize("target", ["module:queue", "session:current", "session:s-1", "settings:appearance"])
def test_navigate_in_bounds_is_accepted(target):
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"] = {"kind": "navigate", "to": target}
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert warnings == ()


def test_tools_call_must_name_a_declared_tool():
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"] = {"kind": "tools/call", "tool": "rm_rf", "args": {}}
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == () and "not declared" in warnings[0]


def test_tools_call_arg_sources_are_closed():
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"] = {
        "kind": "tools/call", "tool": "refresh", "args": {"all": {"expr": "1==1"}},
    }
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == () and "argument source" in warnings[0]


def test_ui_submit_requires_a_session_declaration():
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][3]["on"]["click"] = {
        "kind": "ui/submit", "content": [{"type": "text", "text": "hi"}],
    }
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == () and "requires" in warnings[0]

    page["requires"] = ["session"]
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert warnings == () and pages[0].requires_session is True


def test_ui_submit_rejects_non_text_parts():
    page = copy.deepcopy(PAGE)
    page["requires"] = ["session"]
    root_of(page)["children"][3]["on"]["click"] = {
        "kind": "ui/submit",
        "content": [{"type": "image", "file_id": "f-1"}],
    }
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == () and "only type" in warnings[0]


# -------------------------------------------------------------------- bindings
@pytest.mark.parametrize(
    "path",
    ["session.messages", "audit.events", "page.other", "module.status.secret", "page.state.a.b.c.d", "Page.State.x"],
)
def test_binding_outside_the_three_roots_is_rejected(path):
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][0]["data_ref"] = path
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == ()


def test_item_binding_is_only_legal_inside_a_list_template():
    outside = copy.deepcopy(PAGE)
    root_of(outside)["children"][0]["data_ref"] = "item.name"
    pages, warnings = validate_ui({"pages": [outside]}, module_tools=("refresh",))
    assert pages == () and "list" in warnings[0]


def test_expression_shaped_binding_is_rejected():
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][0]["data_ref"] = "page.state.queue_len > 0"
    pages, _warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages == ()


# ---------------------------------------------------------------------- limits
def _nested(depth: int) -> dict:
    node: dict = {"type": "text", "props": {"text": "x"}}
    for _ in range(depth - 1):
        node = {"type": "column", "children": [node]}
    return node


def test_depth_limit_both_sides():
    assert validate_ui({"pages": [page_with(schema=_nested(MAX_DEPTH))]})[0] != ()
    assert validate_ui({"pages": [page_with(schema=_nested(MAX_DEPTH + 1))]})[0] == ()


def test_node_limit_both_sides():
    def tree(tail: int) -> dict:
        # Deliberately prop-less leaves: this test is about the node counter, and
        # 248 props would trip the 8 KiB inline budget first (a different check).
        columns = [
            {"type": "column", "children": [{"type": "text"} for _ in range(31)]}
            for _ in range(7)
        ]
        columns.append({"type": "column", "children": [{"type": "text"} for _ in range(tail)]})
        return {"type": "column", "children": columns}

    # root(1) + 8 columns + 7*31 + 30 texts = 256, exactly at the cap
    exact = tree(30)
    assert 1 + 8 + 7 * 31 + 30 == MAX_NODES
    assert validate_ui({"pages": [page_with(schema=exact)]})[0] != ()

    pages, warnings = validate_ui({"pages": [page_with(schema=tree(31))]})
    assert pages == () and "more than" in warnings[0]


def test_children_and_text_limits():
    over_children = {"type": "column", "children": [{"type": "text", "props": {"text": "x"}}
                                                    for _ in range(MAX_CHILDREN + 1)]}
    assert validate_ui({"pages": [page_with(schema=over_children)]})[0] == ()
    long_text = "x" * (MAX_TEXT_CHARS + 1)
    assert validate_ui({"pages": [page_with(schema={"type": "text", "props": {"text": long_text}})]})[0] == ()


def test_inline_size_limit():
    # 32 children of 200 chars is ~7 KiB; stacking 8 such columns crosses 8 KiB
    # while every individual node stays legal — the budget, not a field, is what
    # is being tested.
    children = [{"type": "text", "props": {"text": "z" * 200}} for _ in range(MAX_CHILDREN)]
    big = {"type": "column", "children": [{"type": "column", "children": children} for _ in range(5)]}
    assert len(json.dumps(big, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_INLINE_BYTES
    pages, warnings = validate_ui({"pages": [page_with(schema=big)]})
    assert pages == () and "inline schema" in warnings[0]


def test_action_budget_per_page():
    # 32 columns × 2 buttons = 64 actions (at the cap); one more crosses it.
    def tree(pairs: int) -> dict:
        return {"type": "column", "children": [
            {"type": "column", "children": [
                {"type": "component", "component": "Button",
                 "on": {"click": {"kind": "navigate", "to": "session:current"}}},
                {"type": "component", "component": "Button",
                 "on": {"click": {"kind": "navigate", "to": "session:current"}}},
            ]} for _ in range(pairs)
        ]}

    assert MAX_ACTIONS_PER_PAGE == 64
    assert validate_ui({"pages": [page_with(schema=tree(32))]})[0] != ()

    over = tree(32)
    over["children"][0]["children"].append(
        {"type": "component", "component": "Button",
         "on": {"click": {"kind": "navigate", "to": "session:current"}}}
    )
    pages, warnings = validate_ui({"pages": [page_with(schema=over)]})
    assert pages == () and "actions" in warnings[0]


def test_list_data_cap_is_declared():
    assert MAX_LIST_ITEMS == 512


# ------------------------------------------------------------------ structure
def test_leaf_with_children_is_rejected():
    page = page_with(schema={"type": "text", "props": {"text": "x"},
                             "children": [{"type": "text", "props": {"text": "y"}}]})
    pages, warnings = validate_ui({"pages": [page]})
    assert pages == () and "leaf" in warnings[0]


def test_list_needs_exactly_one_template_child():
    for children in ([], [{"type": "text"}, {"type": "text"}]):
        page = page_with(schema={"type": "list", "data_ref": "page.state.items", "children": children})
        pages, warnings = validate_ui({"pages": [page]})
        assert pages == () and "exactly 1" in warnings[0]


def test_tabs_child_requires_tab_title():
    page = page_with(schema={"type": "tabs", "children": [{"type": "text", "props": {"text": "x"}}]})
    pages, warnings = validate_ui({"pages": [page]})
    assert pages == () and "tab_title" in warnings[0]

    ok = page_with(schema={"type": "tabs", "children": [
        {"type": "text", "props": {"text": "x", "tab_title": "A"}},
    ]})
    assert validate_ui({"pages": [ok]})[0] != ()


def test_duplicate_node_ids_and_page_ids():
    dup_nodes = page_with(schema={"type": "column", "children": [
        {"type": "text", "id": "a", "props": {"text": "1"}},
        {"type": "text", "id": "a", "props": {"text": "2"}},
    ]})
    assert validate_ui({"pages": [dup_nodes]})[0] == ()

    pages, warnings = validate_ui({"pages": [copy.deepcopy(PAGE), copy.deepcopy(PAGE)]}, module_tools=("refresh",))
    assert len(pages) == 1 and "duplicate page id" in warnings[0]


def test_more_than_eight_pages_is_rejected():
    page = page_with(id="p")
    many = [copy.deepcopy(page) for _ in range(MAX_PAGES + 1)]
    for index, item in enumerate(many):
        item["id"] = f"p{index}"
    pages, warnings = validate_ui({"pages": many})
    assert pages == () and "pages >" in warnings[0]


def test_unknown_icon_degrades_with_a_warning():
    pages, warnings = validate_ui({"pages": [page_with(icon="rocket")]}, module_tools=("refresh",))
    assert pages[0].icon == "none"
    assert any("unknown icon" in warning for warning in warnings)


def test_unknown_format_degrades_with_a_warning():
    page = copy.deepcopy(PAGE)
    root_of(page)["children"][0]["props"]["format"] = "currency"
    pages, warnings = validate_ui({"pages": [page]}, module_tools=("refresh",))
    assert pages != ()
    assert any("currency" in warning for warning in warnings)


# ------------------------------------------------------- manifest integration
def test_one_bad_page_does_not_kill_the_module():
    bad = copy.deepcopy(PAGE)
    bad["id"] = "evil"
    root_of(bad)["children"][3]["on"]["click"] = {"kind": "shell", "to": "rm -rf /"}
    manifest = validate_manifest(with_ui(PAGE, bad))
    assert [p.page_id for p in manifest.ui_pages] == ["queue"]
    assert manifest.tool_names == ("refresh",), "tools must still register"
    assert any("evil" in warning for warning in manifest.ui_warnings)


def test_schema_file_rejects_unknown_page_keys():
    """The C-03 JSON Schema is strict exactly where `Manifest` is strict."""
    from vade._jsonschema import SchemaError, validate as js_validate
    from vade.registry.manifest import load_schema

    payload = with_ui(PAGE)
    js_validate(payload, load_schema())  # legal page passes

    payload = with_ui(page_with(on_click="x"))
    with pytest.raises(SchemaError):
        js_validate(payload, load_schema())

    payload = with_ui(copy.deepcopy(PAGE))
    payload["ui"]["pages"][0]["schema"] = {"ref": {"page_id": "queue", "sha256": "not-a-digest"}}
    with pytest.raises(SchemaError):
        js_validate(payload, load_schema())


def test_manifest_digest_covers_the_ui_block():
    a = validate_manifest(with_ui(PAGE))
    changed = copy.deepcopy(PAGE)
    changed["title"] = "另一个标题"
    b = validate_manifest(with_ui(changed))
    assert a.digest != b.digest, "a page change must change the manifest digest (capabilities.update)"


def test_cyclic_page_is_refused_instead_of_crashing():
    """A cycle cannot arrive as JSON, but an in-process manifest can carry one."""
    loop: dict = {"type": "column", "children": []}
    loop["children"].append(loop)
    pages, warnings = validate_ui({"pages": [page_with(schema=loop)]})
    assert pages == ()
    assert any("cyclic" in warning for warning in warnings)

    # A cycle directly in the ui block is refused by the size guard, not by a
    # json.dumps traceback escaping the validator.
    block: dict = {"pages": []}
    block["pages"].append(block)
    assert validate_ui(block)[0] == ()
