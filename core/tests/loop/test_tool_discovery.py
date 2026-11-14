"""Tool progressive disclosure (RFC-0003 §4.3) — A5, A6.

A5: a cold tool is absent from the resident schemas(); a tools/search hit joins
the NEXT step's schemas() in the same turn, and everything still comes from the
turn-frozen snapshot (the snapshot object itself is never mutated).
A6: the same query twice returns byte-identical matches and digest.
"""

from __future__ import annotations

import asyncio

from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.loop.coretools import SKILL_LOAD_NAME, TOOLS_SEARCH_NAME, build_core_tools, resolve_tool_search, search_digest
from vade.loop.engine import LoopConfig, LoopEngine
from vade.loop.tools import ToolEntry, ToolSnapshot
from vade.session.models import Turn


def _entry(llm_name: str, tool_id: str, description: str, risk: str = "low") -> ToolEntry:
    return ToolEntry(
        llm_name=llm_name, tool_id=tool_id, module_id=tool_id.rsplit("/", 1)[0],
        description=description, input_schema={"type": "object", "properties": {}},
        risk=risk, timeout_ms=2000, idempotent=True,
    )


class _Router:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool_id, args, **kwargs):
        self.calls.append((tool_id, dict(args)))
        from vade.router.dispatch import ToolCallResult

        return ToolCallResult(True, tool_id, result={"ok": True})


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, session_id, event_type, payload=None, messages=(), turns=(), **kw):
        self.events.append((event_type, dict(payload or ())))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


def _snapshot() -> ToolSnapshot:
    # Built ONCE; the engine must treat it as frozen for the whole turn.
    return ToolSnapshot(entries=(
        _entry("fs__read_file", "com.example.fs/read_file", "read a file from disk"),
        _entry("net__fetch_url", "com.example.net/fetch_url", "fetch a URL over https"),
    ))


def _tool_names(call: dict) -> list[str]:
    return [t["function"]["name"] for t in call["tools"]]


def test_a5_cold_tool_is_absent_then_joins_after_a_search_hit():
    router = _Router()
    recorder = _Recorder()
    llm = MockLLMProvider([
        [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name=TOOLS_SEARCH_NAME,
                                         arguments='{"query": "fetch"}'),))],
        [Delta(tool_calls=(ToolCallDelta(index=0, id="c2", name="net__fetch_url",
                                         arguments='{"url": "https://x"}'),))],
        [Delta(text="done")],
    ])
    snapshot = _snapshot()
    engine = LoopEngine(
        router, llm, lambda: snapshot, recorder,
        config=LoopConfig(injection="layered", max_steps=4),
        core_tools=build_core_tools(None), memory=None,
    )
    outcome = asyncio.run(engine.run_turn(Turn(id="t1", session_id="s1", user_text="grab that page"), ()))
    assert outcome.stopped_reason == "stop"

    # Step 1: only the core tools are resident — with an empty config and no
    # sticky history, every module tool starts COLD (G3: resident tools do not
    # grow with the module count).
    names_first = _tool_names(llm.calls[0])
    assert TOOLS_SEARCH_NAME in names_first and SKILL_LOAD_NAME in names_first
    assert "net__fetch_url" not in names_first
    assert "fs__read_file" not in names_first

    # Step 2 (same turn): the discovery joined at the step boundary, sourced
    # from the SAME frozen snapshot object.
    names_second = _tool_names(llm.calls[1])
    assert "net__fetch_url" in names_second
    assert sorted(names_second) == names_second
    assert snapshot.names() == ("fs__read_file", "net__fetch_url")  # frozen, never mutated
    assert router.calls[0][0] == "com.example.net/fetch_url"

    # The discovery was announced exactly once, with the C-06 payload shape.
    discovered = [p for t, p in recorder.events if t == "turn.tool_discovered"]
    assert len(discovered) == 1
    assert discovered[0]["tool"] == "net__fetch_url"
    assert discovered[0]["tool_id"] == "com.example.net/fetch_url"
    assert len(discovered[0]["query_digest"]) == 12
    assert discovered[0]["total_matches"] == 1

    # The cold tools are still advertised in tool_block's trailer:
    system_text = "\n".join(str(m.get("content", "")) for m in llm.calls[0]["messages"]
                            if m.get("role") == "system")
    assert "+2 more tools are available" in system_text


def test_a5_tool_block_carries_the_more_tools_trailer_when_truncated():
    entries = [
        _entry(f"mod{i:02}__tool", f"com.example.mod{i:02}/tool", f"tool {i}")
        for i in range(15)
    ]
    snapshot = ToolSnapshot(entries=tuple(entries))
    recorder = _Recorder()
    llm = MockLLMProvider([[Delta(text="ok")]])
    engine = LoopEngine(
        _Router(), llm, lambda: snapshot, recorder,
        config=LoopConfig(injection="layered", max_steps=1, hot_max=12),
        core_tools=build_core_tools(None), memory=None,
    )
    asyncio.run(engine.run_turn(Turn(id="t1", session_id="s1", user_text="hi"), ()))
    system_text = "\n".join(str(m.get("content", "")) for m in llm.calls[0]["messages"]
                            if m.get("role") == "system")
    assert "+15 more tools are available" in system_text  # 17 total - 2 resident
    assert TOOLS_SEARCH_NAME in system_text
    # Resident schemas: the two core tools only (nothing made a module hot).
    assert len(llm.calls[0]["tools"]) == 2
    assert "net__fetch_url" not in _tool_names(llm.calls[0])


def test_a5_hot_layers_make_module_tools_resident():
    """Config hot list + session stickiness put module tools in schemas()."""
    snapshot = _snapshot()
    recorder = _Recorder()
    llm = MockLLMProvider([[Delta(text="ok")], [Delta(text="ok")]])
    engine = LoopEngine(
        _Router(), llm, lambda: snapshot, recorder,
        config=LoopConfig(injection="layered", max_steps=1, hot_tools=("com.example.net/fetch_url",)),
        core_tools=build_core_tools(None), memory=None,
    )
    turn = Turn(id="t1", session_id="s1", user_text="hi")
    asyncio.run(engine.run_turn(turn, ()))
    # Layer 3 (config, accepts tool_id form): the cold tool is resident now.
    assert "net__fetch_url" in _tool_names(llm.calls[0])

    # Layer 4 (sticky): after a successful call, the other module tool joins
    # the session's hot set for the NEXT turn — without any config entry.
    engine._sticky_add("s1", "fs__read_file")
    asyncio.run(engine.run_turn(Turn(id="t2", session_id="s1", user_text="hi"), ()))
    names = _tool_names(llm.calls[1])
    assert {"fs__read_file", "net__fetch_url", TOOLS_SEARCH_NAME, SKILL_LOAD_NAME} <= set(names)


def test_a6_same_query_twice_is_byte_identical():
    snapshot = _snapshot()
    tool = build_core_tools(None)[TOOLS_SEARCH_NAME]

    async def scenario():
        first = await tool.handler({"query": "fetch"}, snapshot, session_id="s", turn_id="t")
        second = await tool.handler({"query": "fetch"}, snapshot, session_id="s", turn_id="t")
        assert first["result"]["matches"] == second["result"]["matches"]
        assert first["result"]["digest"] == second["result"]["digest"]

        matches, total = resolve_tool_search(snapshot.entries, "fetch", 5)
        assert total == 1 and matches[0]["name"] == "net__fetch_url"
        assert matches[0]["description"] == "fetch a URL over https"
        assert search_digest(matches) == first["result"]["digest"]

        # Different casing / whitespace: still deterministic, still one hit.
        third = await tool.handler({"query": "  FETCH  "}, snapshot, session_id="s", turn_id="t")
        assert third["result"]["matches"] == first["result"]["matches"]

        # Limit is clamped to 1..8 and description-only matches work.
        broad, broad_total = resolve_tool_search(snapshot.entries, "file", 99)
        assert broad_total == 1 and broad[0]["name"] == "fs__read_file"

    asyncio.run(scenario())


def test_a5_off_snapshot_names_are_never_returned():
    snapshot = _snapshot()
    tool = build_core_tools(None)[TOOLS_SEARCH_NAME]

    async def scenario():
        result = await tool.handler({"query": "ghost"}, snapshot, session_id="s", turn_id="t")
        assert result["result"]["matches"] == [] and result["result"]["total"] == 0
        # A name that EXISTS but is not in this snapshot is equally invisible.
        result2 = await tool.handler({"query": "shell__run"}, snapshot, session_id="s", turn_id="t")
        assert result2["result"]["total"] == 0

    asyncio.run(scenario())
