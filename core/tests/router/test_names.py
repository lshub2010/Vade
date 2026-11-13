"""LLM-side naming: `fs__read_file`, conflicts `fs2__`, snapshot stability."""

from __future__ import annotations

from vade.router.names import ToolNameMap, prefix_for


def test_prefix_is_last_module_segment():
    assert prefix_for("com.example.fs") == "fs"
    assert prefix_for("org.vade.mcp-bridge") == "mcp_bridge"


def test_roundtrip_mapping():
    names = ToolNameMap.build([("com.example.fs", "read_file"), ("com.example.fs", "write_file")])
    assert names.llm_name("com.example.fs/read_file") == "fs__read_file"
    assert names.tool_id("fs__write_file") == "com.example.fs/write_file"
    assert names.tool_id("nope__x") is None


def test_collision_gets_numeric_suffix_deterministically():
    pairs = [("com.b.fs", "read_file"), ("com.a.fs", "read_file"), ("com.c.fs", "read_file")]
    first = ToolNameMap.build(pairs)
    assert first.llm_name("com.a.fs/read_file") == "fs__read_file"
    assert first.llm_name("com.b.fs/read_file") == "fs2__read_file"
    assert first.llm_name("com.c.fs/read_file") == "fs3__read_file"
    # Input order must not matter.
    assert ToolNameMap.build(reversed(pairs)).forward == first.forward


def test_map_is_frozen_and_has_no_update_api():
    names = ToolNameMap.build([("com.example.fs", "read_file")])
    assert not hasattr(names, "add")
    assert not hasattr(names, "update")
    before = dict(names.forward)
    try:
        names.forward["com.example.fs/other"] = "fs__other"  # type: ignore[index]
    except TypeError:
        pass
    assert dict(names.forward) == before
