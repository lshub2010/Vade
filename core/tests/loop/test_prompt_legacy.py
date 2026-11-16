"""A9 (RFC-0003 §8): `legacy` is byte-identical to a0a5062.

The golden fixture was captured from the pre-RFC code (prompt.py, tools.py,
loader.py and render.py were all unchanged between a0a5062 and the RFC-0003
worktree base, so the current legacy path IS a0a5062). Three cases:

* case_a / case_a_llm — `assemble()` with all six legacy sections filled;
* case_b — empty sections are skipped;
* case_c — a full legacy `run_turn`: every message and every tool schema the
  provider sees, hooks included.

If this file goes red, either the legacy path changed bytes (must not happen:
G5) or legacy was rerouted through the layered machinery (mutation #2).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from vade.llm.base import Delta
from vade.llm.mock import MockLLMProvider
from vade.loop.engine import LoopConfig, LoopEngine
from vade.loop.prompt import PromptParts, assemble
from vade.session.models import Turn

GOLDEN = Path(__file__).resolve().parent / "golden" / "prompt_legacy.json"


def _load() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_assemble_legacy_matches_the_golden_bytes():
    golden = _load()
    parts = PromptParts(
        persona="You are Vade.",
        tool_block="Available tools (call by exact name):\n- fs__read_file: read [risk=low]",
        skill_catalog="Skills (activate with @name):\n- kb-guide: rules",
        skill_bodies="## skill: kb-guide\nbody text",
        session_memory="- user prefers terse answers",
        user_message="hello",
        context="runtime: cwd=/tmp/x",
    )
    out = assemble(parts, injection="legacy")
    assert [m.to_dict() for m in out] == golden["case_a"]
    assert [m.to_llm() for m in out] == golden["case_a_llm"]
    assert all(m.role == "system" for m in out)


def test_assemble_legacy_skips_empty_sections():
    golden = _load()
    out = assemble(PromptParts(persona="P", tool_block="", context="X"), injection="legacy")
    assert [m.to_dict() for m in out] == golden["case_b"]


def _snapshot():
    from vade.loop.tools import ToolEntry, ToolSnapshot

    entry = ToolEntry(
        llm_name="fs__read_file", tool_id="com.example.fs/read_file", module_id="com.example.fs",
        description="read a file", input_schema={"type": "object", "properties": {}},
        risk="low", timeout_ms=2000, idempotent=True,
    )
    return ToolSnapshot(entries=(entry,))


def test_legacy_turn_matches_the_golden_provider_view():
    expected = _load()["case_c"]

    class Router:
        async def call(self, tool_id, args, **kwargs):
            from vade.router.dispatch import ToolCallResult

            return ToolCallResult(True, tool_id, result={"content": "hi"})

    async def hook(parts):
        from dataclasses import replace

        return replace(
            parts,
            skill_catalog="Skills (activate with @name):\n- kb-guide: rules",
            skill_bodies="## skill: kb-guide\nbody text",
            activated_skills=("kb-guide",),
        )

    async def scenario():
        llm = MockLLMProvider([[Delta(text="done")]])
        events: list[list] = []

        async def emit(session_id, event_type, payload=None, messages=(), turns=(), **kw):
            events.append([event_type, dict(payload or {})])
            return None

        engine = LoopEngine(
            Router(), llm, _snapshot, emit,
            prompt_hooks=lambda: [hook], config=LoopConfig(max_steps=4), memory=None,
        )
        await engine.run_turn(Turn(id="t-golden", session_id="s-golden", user_text="hello"), ())
        # Provider-side view, byte-comparable to the fixture.
        assert llm.calls and llm.calls[0]["messages"] == expected["llm_calls"][0]["messages"]
        assert llm.calls[0]["tools"] == expected["llm_calls"][0]["tools"]
        assert [[t, p] for t, p in events] == [list(pair) for pair in expected["events"]]

    asyncio.run(scenario())


def test_legacy_turn_is_byte_stable_across_runs():
    """Same inputs twice -> identical provider bytes (the prefix cache's base)."""
    golden = _load()

    class Router:
        async def call(self, tool_id, args, **kwargs):
            from vade.router.dispatch import ToolCallResult

            return ToolCallResult(True, tool_id, result={"content": "hi"})

    async def hook(parts):
        from dataclasses import replace

        return replace(
            parts,
            skill_catalog="Skills (activate with @name):\n- kb-guide: rules",
            skill_bodies="## skill: kb-guide\nbody text",
            activated_skills=("kb-guide",),
        )

    async def scenario():
        digests = []
        for _ in range(2):
            llm = MockLLMProvider([[Delta(text="done")]])

            async def emit(session_id, event_type, payload=None, messages=(), turns=(), **kw):
                return None

            engine = LoopEngine(
                Router(), llm, _snapshot, emit,
                prompt_hooks=lambda: [hook], config=LoopConfig(max_steps=4), memory=None,
            )
            await engine.run_turn(Turn(id="t-golden", session_id="s-golden", user_text="hello"), ())
            blob = json.dumps({"m": llm.calls[0]["messages"], "t": llm.calls[0]["tools"]},
                              sort_keys=True, ensure_ascii=False)
            digests.append(hashlib.sha256(blob.encode("utf-8")).hexdigest())
        assert digests[0] == digests[1]
        assert llm.calls[0]["messages"] == golden["case_c"]["llm_calls"][0]["messages"]

    asyncio.run(scenario())
