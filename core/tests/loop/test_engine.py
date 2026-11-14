"""Agent loop: tool chain, unknown tools, max_steps, no capability, purity.

The source-grep test below enforces that engine.py contains no concrete tool
name literals (no `f`+`s__` style prefixes and no module ids).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from vade.loop.context import BlobStore, CompactionConfig, ContextBuilder
from vade.loop.engine import LoopConfig, LoopEngine
from vade.loop.prompt import PromptParts, assemble
from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.router.dispatch import ToolCallResult
from vade.session.models import Message, Turn


class FakeRouter:
    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self.result = result or ToolCallResult(True, "com.example.fs/read_file", result={"content": "hi"})

    async def call(self, tool_id, args, *, session_id, turn_id=None, call_id=None, deadline_ms=None):
        self.calls.append((tool_id, dict(args), session_id, call_id))
        return self.result


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, session_id, event_type, payload=None, messages=()):
        self.events.append((event_type, dict(payload or {})))

    def types(self):
        return [e[0] for e in self.events]


class FakeSnapshot:
    def __init__(self, entries=None):
        self._entries = [
            _entry("fs__read_file", "com.example.fs/read_file"),
        ] if entries is None else list(entries)

    def get(self, name):
        for entry in self._entries:
            if entry.llm_name == name:
                return entry
        return None

    def names(self):
        return tuple(e.llm_name for e in self._entries)

    def schemas(self):
        return [{"type": "function", "function": {"name": e.llm_name}} for e in self._entries]

    def render_block(self):
        return "tools"

    def __bool__(self):
        return bool(self._entries)


class _Entry:
    def __init__(self, llm_name, tool_id, risk="low"):
        self.llm_name = llm_name
        self.tool_id = tool_id
        self.risk = risk
        self.description = "d"


def _entry(llm_name, tool_id, risk="low"):
    return _Entry(llm_name, tool_id, risk)


def _turn() -> Turn:
    return Turn(id="t1", session_id="s1", user_text="do it")


def test_tool_chain_emits_call_result_and_delta():
    async def scenario():
        llm = MockLLMProvider([
            [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_file",
                                             arguments='{"path":"a"}'),))],
            [Delta(text="finished")],
        ])
        router = FakeRouter()
        recorder = Recorder()
        engine = LoopEngine(router, llm, FakeSnapshot, recorder)
        outcome = await engine.run_turn(_turn(), ())
        assert recorder.types() == ["turn.tool_call", "turn.tool_result", "turn.delta"]
        assert router.calls[0][0] == "com.example.fs/read_file"
        assert router.calls[0][1] == {"path": "a"}
        assert outcome.assistant_text == "finished"
        assert outcome.stopped_reason == "stop"
        assert outcome.tool_calls[0]["ok"] is True

    asyncio.run(scenario())


def test_streamed_tool_arguments_are_concatenated_by_index():
    async def scenario():
        llm = MockLLMProvider([
            [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_", arguments='{"path":'),)),
             Delta(tool_calls=(ToolCallDelta(index=0, name="file", arguments='"a"}'),))],
            [Delta(text="ok")],
        ])
        router = FakeRouter()
        engine = LoopEngine(router, llm, FakeSnapshot, Recorder())
        await engine.run_turn(_turn(), ())
        assert router.calls[0][0] == "com.example.fs/read_file"
        assert router.calls[0][1] == {"path": "a"}

    asyncio.run(scenario())


def test_unknown_tool_does_not_crash_and_model_is_told():
    async def scenario():
        llm = MockLLMProvider([
            [Delta(tool_calls=(ToolCallDelta(index=0, id="c9", name="nope__thing", arguments="{}"),))],
            [Delta(text="fine")],
        ])
        router = FakeRouter()
        recorder = Recorder()
        await LoopEngine(router, llm, FakeSnapshot, recorder).run_turn(_turn(), ())
        assert router.calls == []
        assert "turn.tool_result" in recorder.types()
        results = [p for t, p in recorder.events if t == "turn.tool_result"]
        assert results and results[0]["ok"] is False
        second_call_messages = llm.calls[1]["messages"]
        tool_messages = [m for m in second_call_messages if m["role"] == "tool"]
        assert "does not exist" in tool_messages[0]["content"]
        assert "fs__read_file" in tool_messages[0]["content"]

    asyncio.run(scenario())


def test_max_steps_stops_with_a_readable_message():
    async def scenario():
        step = [Delta(tool_calls=(ToolCallDelta(index=0, id="c", name="fs__read_file",
                                                arguments='{"path":"a"}'),))]
        llm = MockLLMProvider([step, step, step])
        recorder = Recorder()
        outcome = await LoopEngine(FakeRouter(), llm, FakeSnapshot, recorder,
                                   config=LoopConfig(max_steps=2)).run_turn(_turn(), ())
        assert outcome.stopped_reason == "max_steps"
        assert "Stopped after 2 steps" in outcome.assistant_text
        assert len(llm.calls) == 2

    asyncio.run(scenario())


def test_no_modules_returns_readable_no_capability_message():
    async def scenario():
        llm = MockLLMProvider([[Delta(text="should not be used")]])
        recorder = Recorder()
        engine = LoopEngine(FakeRouter(), llm, lambda: FakeSnapshot([]), recorder)
        outcome = await engine.run_turn(_turn(), ())
        assert outcome.stopped_reason == "no_capability"
        assert "No capabilities" in outcome.assistant_text
        assert llm.calls == []

    asyncio.run(scenario())


def test_no_concrete_tool_names_in_loop_source():
    source = (Path(__file__).resolve().parents[3] / "core" / "vade" / "loop" / "engine.py").read_text()
    for literal in ("fs__", "shell__", "com.example", "read_file",
                    "skill_load", "tools_search", "tools/search"):
        assert literal not in source, f"agent-loop must not know {literal!r}"


def test_high_risk_is_denied_when_no_approver_is_installed():
    async def scenario():
        llm = MockLLMProvider([
            [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_file",
                                             arguments='{"path":"a"}'),))],
            [Delta(text="denied")],
        ])
        router = FakeRouter()
        entries = [_entry("fs__read_file", "com.example.fs/read_file", risk="high")]
        snapshot = FakeSnapshot(entries)
        recorder = Recorder()
        engine = LoopEngine(router, llm, lambda: snapshot, recorder)
        await engine.run_turn(_turn(), ())
        assert router.calls == []
        tool_messages = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
        assert "denied" in tool_messages[0]["content"]

    asyncio.run(scenario())


def test_assemble_is_pure_and_ordered():
    parts = PromptParts(persona="P", tool_block="T", skill_catalog="C", skill_bodies="B",
                        session_memory="M", user_message="U", context="X")
    first = assemble(parts)
    second = assemble(parts)
    assert [m.to_dict() for m in first] == [m.to_dict() for m in second]
    assert [m.content for m in first] == ["P", "T", "C", "B", "M", "X"]
    assert all(m.role == "system" for m in first)
    assert "U" not in [m.content for m in first]


def test_context_pruning_uses_config_and_creates_blobs(tmp_path):
    config = CompactionConfig(threshold=0.5, target_ratio=0.2, protect_last_n=2,
                              protect_first_n=1, proactive_prune_tokens=1, min_result_chars=50)
    builder = ContextBuilder(config, blob_store=BlobStore(tmp_path))
    messages = [
        Message(id="s", session_id="s1", role="system", content="sys"),
        Message(id="old", session_id="s1", role="tool", content="x" * 4000, name="fs__read_file"),
    ]
    messages += [Message(id=f"a{i}", session_id="s1", role="assistant", content="y" * 200) for i in range(20)]
    messages += [Message(id="last", session_id="s1", role="user", content="keep me")]
    out = builder.fit(messages, ctx_tokens=500, current_turn_id=None)
    assert builder.tokens(out) <= config.target_ratio * 500
    assert out[0].content == "sys"
    assert out[-1].content == "keep me"
    assert any("blob_id=" in m.content for m in out if m.role == "tool") or not any(
        m.role == "tool" for m in out)
