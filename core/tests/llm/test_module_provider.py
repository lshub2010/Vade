"""`ModuleLLMProvider` selection and translation (T5), registry/router mocked.

Selection is a contract, not a preference: the highest `priority` online
instance that declares the `chat` tool wins, ties go to the freshest heartbeat,
and a non-model module must never be picked even if it is the only one online.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from vade.errors import VadeError
from vade.llm.base import Delta
from vade.llm.mock import MockLLMProvider
from vade.llm.module import ModuleLLMProvider, MODEL_TOOL, to_deltas
from vade.registry.health import HealthState
from vade.router.dispatch import ToolCallResult


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeManifest:
    def __init__(self, module_id: str, priority: int = 0, tools: tuple[str, ...] = (MODEL_TOOL,)) -> None:
        self.module_id = module_id
        self.priority = priority
        self._tools = {name: FakeTool(name) for name in tools}

    def tool(self, name: str) -> FakeTool | None:
        return self._tools.get(name)


class FakeRecord:
    def __init__(self, module_id: str, *, priority: int = 0, instance_id: str = "m-1",
                 connection_id: str | None = "conn-1", health: Any = HealthState.ONLINE,
                 last_frame_at: float | None = 1.0, tools: tuple[str, ...] = (MODEL_TOOL,),
                 manifest: Any = ...) -> None:
        self.instance_id = instance_id
        self.connection_id = connection_id
        self.health = health
        self.last_frame_at = last_frame_at
        self.manifest = FakeManifest(module_id, priority, tools) if manifest is ... else manifest


class FakeRegistry:
    def __init__(self, records: list[FakeRecord]) -> None:
        self._records = list(records)

    def snapshot(self) -> tuple[FakeRecord, ...]:
        return tuple(self._records)


class FakeRouter:
    def __init__(self, result: ToolCallResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def call(self, tool_id, args, *, session_id, call_id, deadline_ms):
        self.calls.append({"tool_id": tool_id, "args": dict(args), "session_id": session_id,
                           "call_id": call_id, "deadline_ms": deadline_ms})
        return self.result


def _collect(provider: ModuleLLMProvider) -> list[Delta]:
    async def scenario() -> list[Delta]:
        return [delta async for delta in provider.chat([{"role": "user", "content": "hi"}],
                                                       tools=[{"type": "function"}])]

    return asyncio.run(scenario())


def _ok(payload: Any) -> ToolCallResult:
    return ToolCallResult(ok=True, tool_id="m/chat", result=payload)


def test_select_prefers_priority_then_freshest_heartbeat_and_skips_non_models():
    registry = FakeRegistry([
        FakeRecord("m.low", priority=1, instance_id="low", last_frame_at=9.0),
        FakeRecord("m.slow", priority=7, instance_id="slow", last_frame_at=1.0),
        FakeRecord("m.fast", priority=7, instance_id="fast", last_frame_at=5.0),
        FakeRecord("m.offline", priority=99, instance_id="off", health=HealthState.OFFLINE),
        FakeRecord("m.rejected", priority=99, instance_id="rej", health=HealthState.REJECTED),
        FakeRecord("m.notmodel", priority=99, instance_id="nm", tools=("other",)),
        FakeRecord("m.noconn", priority=99, instance_id="nc", connection_id=None),
        FakeRecord("m.nomanifest", priority=99, instance_id="nman", manifest=None),
    ])
    provider = ModuleLLMProvider(FakeRouter(_ok({})), registry)

    chosen = provider.select()
    assert chosen is not None
    assert chosen.instance_id == "fast", "priority wins, then the freshest heartbeat"


def test_select_honours_the_model_module_filter():
    registry = FakeRegistry([
        FakeRecord("m.a", priority=9, instance_id="a"),
        FakeRecord("m.b", priority=1, instance_id="b"),
    ])
    provider = ModuleLLMProvider(FakeRouter(_ok({})), registry, model_module="m.b")
    assert provider.select().instance_id == "b"
    assert ModuleLLMProvider(FakeRouter(_ok({})), FakeRegistry([])).select() is None


def test_chat_calls_the_router_with_no_session_and_translates_the_reply():
    result = {
        "message": {
            "content": "two plus two",
            "tool_calls": [
                {"id": "c9", "type": "function",
                 "function": {"name": "add", "arguments": {"b": 1, "a": 2}}},
            ],
        },
        "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "ignored": "x"},
    }
    router = FakeRouter(_ok(result))
    provider = ModuleLLMProvider(router, FakeRegistry([FakeRecord("m.chat")]),
                                 temperature=0.2, timeout_ms=4321)
    deltas = _collect(provider)

    assert len(deltas) == 1, "v1 is non-streaming: exactly one Delta"
    assert deltas[0].text == "two plus two"
    assert deltas[0].finish_reason == "tool_calls"
    assert deltas[0].usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    call = deltas[0].tool_calls[0]
    assert (call.index, call.id, call.name) == (0, "c9", "add")
    # 45-M6 §五: arguments must be a STRING; an object is normalised, not dropped.
    assert json.loads(call.arguments) == {"a": 2, "b": 1}
    assert call.arguments == '{"a": 2, "b": 1}'

    assert router.calls == [{
        "tool_id": "m.chat/chat",
        "args": {"messages": [{"role": "user", "content": "hi"}],
                 "tools": [{"type": "function"}], "temperature": 0.2},
        "session_id": "",
        "call_id": "llm-m-1",
        "deadline_ms": 4321,
    }]


def test_chat_falls_back_when_no_model_module_is_online():
    fallback = MockLLMProvider([[Delta(text="from the builtin")]])
    provider = ModuleLLMProvider(FakeRouter(_ok({})), FakeRegistry([]), fallback=fallback)
    deltas = _collect(provider)
    assert [d.text for d in deltas] == ["from the builtin"]
    assert fallback.calls, "the fallback provider must actually run"


def test_chat_without_candidate_or_fallback_is_unavailable():
    provider = ModuleLLMProvider(FakeRouter(_ok({})), FakeRegistry([]))
    with pytest.raises(VadeError) as exc:
        _collect(provider)
    assert exc.value.code == "UNAVAILABLE"


def test_module_error_code_is_preserved_not_wrapped_as_internal():
    router = FakeRouter(ToolCallResult(ok=False, tool_id="m/chat", code="TIMEOUT",
                                       message="the model module timed out"))
    provider = ModuleLLMProvider(router, FakeRegistry([FakeRecord("m.chat")]))
    with pytest.raises(VadeError) as exc:
        _collect(provider)
    assert exc.value.code == "TIMEOUT"
    assert "timed out" in exc.value.message


def test_to_deltas_rejects_payloads_that_cannot_be_a_message():
    with pytest.raises(VadeError) as exc:
        to_deltas(["not", "an", "object"])
    assert exc.value.code == "INTERNAL"

    with pytest.raises(VadeError):
        to_deltas({"message": {"tool_calls": "not-an-array"}})
    # No message at all is an empty answer, not a crash.
    (empty,) = to_deltas({})
    assert empty.text == "" and empty.tool_calls == ()
