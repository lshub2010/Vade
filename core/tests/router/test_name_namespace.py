"""A13 (RFC-0003 §8): the tool-name namespaces are structurally disjoint.

Module tools are always `{prefix}__{tool}` (a double underscore, from
`router/names.py`); core built-ins are `skill_load` / `tools_search` (single
underscore, `loop/coretools.py`). No module name can ever equal a core name,
and no core name can ever be produced by the module map — including for a
module that literally registers itself as `core`.
"""

from __future__ import annotations

import json
from pathlib import Path

from vade.loop.coretools import SKILL_LOAD_NAME, TOOLS_SEARCH_NAME, build_core_tools
from vade.loop.tools import build_snapshot
from vade.router.names import ToolNameMap

RESERVED = {"skill_load", "tools_search", "tools/search"}


class _Spec:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} for tests"
        self.input_schema = {"type": "object", "properties": {}}
        self.risk = "low"
        self.timeout_ms = 1000
        self.idempotent = True


class _Manifest:
    def __init__(self, tools: list[str]) -> None:
        self.tools = [_Spec(t) for t in tools]
        self.injects: tuple[str, ...] = ()


class _Record:
    def __init__(self, module_id: str, instance_id: str, tools: list[str]) -> None:
        self.module_id = module_id
        self.instance_id = instance_id
        self.enabled = True
        self.connection_id = "conn"
        self.manifest = _Manifest(tools)

        class _Health:
            value = "online"

        self.health = _Health()


class _Registry:
    def __init__(self, records: list[_Record]) -> None:
        self._records = records

    def snapshot(self):
        return list(self._records)

    def bindings_for(self, name: str):
        return []


def test_module_llm_names_always_carry_a_double_underscore():
    pairs = [
        ("com.example.fs", "read_file"), ("mcp-bridge", "s_kb_recall"),
        ("core", "tool"),  # the hostile case: a module literally named `core`
        ("weird.module-id", "x"),
    ]
    mapping = ToolNameMap.build(pairs)
    assert len(mapping) == 4
    for llm_name in mapping.backward:
        assert "__" in llm_name, llm_name
        assert llm_name not in RESERVED, llm_name
    # `{prefix}__{tool}` even for the module named `core`:
    assert mapping.llm_name("core/tool") == "core__tool"


def test_snapshot_names_never_collide_with_core_tools():
    registry = _Registry([
        _Record("com.example.fs", "fs-01", ["read_file", "skill_load"]),
        _Record("core", "core-01", ["search"]),
    ])
    snapshot = build_snapshot(registry)
    names = snapshot.names()
    assert "fs__skill_load" in names  # a module tool named skill_load is namespaced
    for name in names:
        assert name not in RESERVED
    core = build_core_tools(None)
    assert set(core) == {SKILL_LOAD_NAME, TOOLS_SEARCH_NAME}
    for name in core.values():
        assert "__" not in name.llm_name  # the core convention: single underscore
        assert name.llm_name not in names  # the module table alone never produces them
    # ...and `with_core` grafts both without shadowing a module tool:
    from vade.loop.tools import ToolEntry

    core_entries = tuple(
        ToolEntry(llm_name=tool.llm_name, tool_id=tool.tool_id, module_id="core",
                  description=tool.description, input_schema=dict(tool.input_schema),
                  risk=tool.risk, timeout_ms=0, idempotent=True)
        for tool in core.values()
    )
    merged = snapshot.with_core(core_entries)
    merged_names = merged.names()
    assert SKILL_LOAD_NAME in merged_names and TOOLS_SEARCH_NAME in merged_names
    assert len(merged_names) == len(names) + 2
    shadowed = [e for e in merged.entries if e.llm_name == SKILL_LOAD_NAME]
    assert len(shadowed) == 1
    assert shadowed[0].tool_id == "core/skill_load"


def test_rpc_json_registers_the_reserved_names():
    schema = Path(__file__).resolve().parents[3] / "schema" / "rpc.json"
    rpc = json.loads(schema.read_text(encoding="utf-8"))
    reserved = set((rpc.get("x-vade") or {}).get("core_reserved_tools", {}).get("reserved") or [])
    assert RESERVED <= reserved
    for direction in (rpc.get("directions") or {}).values():
        assert not (RESERVED & set(direction or {}))
