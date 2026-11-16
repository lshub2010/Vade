"""RFC-0006 §7 (A13): `part_index` / `kind` on `turn.delta`.

Old clients ignore both fields and concatenate `text` in seq order; that result
must be byte-identical to what they got before this RFC. A new client sees the
segment boundaries ("explain → call → explain") without a new event type.
"""

from __future__ import annotations

import asyncio

from vade.loop.engine import LoopConfig, LoopEngine
from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.router.dispatch import ToolCallResult

from ..loop.test_engine import FakeRouter, FakeSnapshot, Recorder, _turn


def _call(call_id: str) -> Delta:
    return Delta(tool_calls=(ToolCallDelta(index=0, id=call_id, name="fs__read_file",
                                           arguments='{"path":"a"}'),))


def test_three_text_segments_get_three_indexes():
    async def scenario():
        llm = MockLLMProvider([
            [Delta(text="A1"), Delta(text="A2"), _call("c1")],
            [Delta(text="B1"), Delta(text="B2"), _call("c2")],
            [Delta(text="C1")],
        ])
        recorder = Recorder()
        await LoopEngine(FakeRouter(), llm, FakeSnapshot, recorder).run_turn(_turn(), ())

        deltas = [p for t, p in recorder.events if t == "turn.delta"]
        assert [d["part_index"] for d in deltas] == [0, 0, 1, 1, 2]
        assert {d["kind"] for d in deltas} == {"text"}
        # Index never decreases and the stream is unchanged for old clients.
        indexes = [d["part_index"] for d in deltas]
        assert indexes == sorted(indexes)
        assert "".join(d["text"] for d in deltas) == "A1A2B1B2C1"

    asyncio.run(scenario())


def test_the_max_steps_notice_is_its_own_notice_segment():
    async def scenario():
        llm = MockLLMProvider([[Delta(text="trying"), _call("c1")]])
        recorder = Recorder()
        engine = LoopEngine(FakeRouter(), llm, FakeSnapshot, recorder,
                            config=LoopConfig(max_steps=1))
        await engine.run_turn(_turn(), ())
        deltas = [p for t, p in recorder.events if t == "turn.delta"]
        assert [d["kind"] for d in deltas] == ["text", "notice"]
        assert deltas[0]["part_index"] == 0
        assert deltas[1]["part_index"] == 1
        assert "Stopped after 1 steps" in deltas[1]["text"]

    asyncio.run(scenario())


def test_the_no_capability_hint_is_a_notice():
    async def scenario():
        recorder = Recorder()
        engine = LoopEngine(None, MockLLMProvider([]), lambda: FakeSnapshot([]), recorder)
        await engine.run_turn(_turn(), ())
        delta = [p for t, p in recorder.events if t == "turn.delta"][0]
        assert delta["kind"] == "notice"
        assert delta["part_index"] == 0

    asyncio.run(scenario())


def test_a_step_with_no_text_skips_an_index_instead_of_reusing():
    async def scenario():
        llm = MockLLMProvider([
            [Delta(text="first"), _call("c1")],
            [_call("c2")],                      # no text in this step: no delta
            [Delta(text="last")],
        ])
        router = FakeRouter(ToolCallResult(True, "com.example.fs/read_file", result={}))
        recorder = Recorder()
        await LoopEngine(router, llm, FakeSnapshot, recorder).run_turn(_turn(), ())
        indexes = [p["part_index"] for t, p in recorder.events if t == "turn.delta"]
        # The empty middle step allocated nothing; indexes stay contiguous here
        # (the contract only promises "monotonic", clients must tolerate gaps).
        assert indexes == [0, 1]
        assert "".join(p["text"] for t, p in recorder.events if t == "turn.delta") == "firstlast"

    asyncio.run(scenario())
