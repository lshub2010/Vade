"""Layered layout: stable/context split (RFC-0003 §4.4) — A1, A2, A8.

A1: installing a skill between two turns leaves the stable prefix (persona +
tool_block + tools array) byte-identical — the catalog change travels as a
new history message instead.
A2: two consecutive turns with nothing changed produce byte-identical
stable AND context sections (pure-function property of the assembly).
A8: the stable sections never carry runtime facts; the context section does.
"""

from __future__ import annotations

import asyncio
import hashlib
import re

from vade.llm.base import Delta
from vade.llm.mock import MockLLMProvider
from vade.loop.catalog import is_catalog_message, parse_catalog_digest
from vade.loop.coretools import build_core_tools
from vade.loop.engine import LoopConfig, LoopEngine
from vade.loop.prompt import PromptParts, assemble
from vade.loop.tools import ToolEntry, ToolSnapshot
from vade.skill.loader import SkillLoader
from vade.skill.port import LayeredCatalogPort
from vade.session.models import Turn

from ..test_skill.helpers import write_skill

_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/home/|/Users/|/root/|/etc/)")


class _Cfg:
    """Minimal C-18 stand-in: the loader reads `prompt.injection` from it."""

    def __init__(self, values: dict) -> None:
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)


def _layered_loader(tmp_path) -> SkillLoader:
    loader = SkillLoader(tmp_path / "skills", config=_Cfg({"prompt.injection": "layered"}))
    loader.reload()
    return loader


def _snapshot() -> ToolSnapshot:
    entry = ToolEntry(
        llm_name="fs__read_file", tool_id="com.example.fs/read_file", module_id="com.example.fs",
        description="read a file", input_schema={"type": "object", "properties": {}},
        risk="low", timeout_ms=2000, idempotent=True,
    )
    return ToolSnapshot(entries=(entry,))


class _Router:
    async def call(self, tool_id, args, **kwargs):
        from vade.router.dispatch import ToolCallResult

        return ToolCallResult(True, tool_id, result={"content": "hi"})


class _Recorder:
    """Collects emitted events AND their persisted messages, and serves them
    back as the next turn's history — the session log's job, in miniature."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.messages: list = []

    async def __call__(self, session_id, event_type, payload=None, messages=(), turns=(), **kw):
        self.events.append((event_type, dict(payload or {})))
        self.messages.extend(messages or ())

    def history(self, session_id):
        return tuple(self.messages)

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


def _engine(loader: SkillLoader, recorder: _Recorder, llm: MockLLMProvider) -> LoopEngine:
    return LoopEngine(
        _Router(), llm, _snapshot, recorder,
        history=recorder.history,
        prompt_hooks=lambda: [loader.render],
        config=LoopConfig(injection="layered", max_steps=4),
        core_tools=build_core_tools(loader),
        catalog=LayeredCatalogPort(loader),
        memory=None,
    )


def _system_sections(messages: list[dict]) -> dict[str, list[str]]:
    """Group the system messages of a provider call by their leading line."""
    groups: dict[str, list[str]] = {}
    for message in messages:
        if message.get("role") != "system":
            continue
        head = str(message.get("content", "")).splitlines()[0]
        groups.setdefault(head, []).append(str(message.get("content", "")))
    return groups


def _stable_digest(messages: list[dict]) -> str:
    """sha256 over the stable prefix: every system section EXCEPT the
    runtime-facts context block (G1: stable + tools must not move)."""
    blob = []
    for message in messages:
        if message.get("role") != "system":
            continue
        content = str(message.get("content", ""))
        if content.startswith("Runtime facts"):
            continue
        blob.append(content)
    tools = None
    return hashlib.sha256(repr(blob).encode("utf-8")).hexdigest()


async def _run_turn(engine: LoopEngine, recorder: _Recorder, llm: MockLLMProvider, turn_id: str):
    llm_calls_before = len(llm.calls)
    await engine.run_turn(Turn(id=turn_id, session_id="s1", user_text="hello"), ())
    assert len(llm.calls) == llm_calls_before + 1
    return llm.calls[-1]


def test_a1_skill_install_leaves_the_stable_prefix_byte_identical(tmp_path):
    async def scenario():
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")], [Delta(text="ok")]])
        engine = _engine(loader, recorder, llm)

        first = await _run_turn(engine, recorder, llm, "t1")
        stable_before = _stable_digest(first["messages"])
        tools_before = first["tools"]

        # Install a skill between the turns (the triggering event of G1).
        write_skill(tmp_path / "skills", "kb-guide")
        loader.reload()

        second = await _run_turn(engine, recorder, llm, "t2")
        assert _stable_digest(second["messages"]) == stable_before
        assert second["tools"] == tools_before

        # The change must be VISIBLE to the model — as a new catalog message:
        catalog_payloads = [p for t, p in recorder.events if t == "turn.skill_catalog"]
        assert len(catalog_payloads) == 2
        assert catalog_payloads[0]["empty"] is True          # retirement entry, turn 1
        assert catalog_payloads[1]["entries"] == ["kb-guide"]  # full replacement, turn 2
        assert catalog_payloads[0]["digest"] != catalog_payloads[1]["digest"]
        # ...which is a persisted user message, not a system section:
        catalog_messages = [m for m in recorder.messages if is_catalog_message(m.content)]
        assert len(catalog_messages) == 2
        assert all(m.role == "user" for m in catalog_messages)

    asyncio.run(scenario())


def test_a2_two_unchanged_turns_are_byte_identical_and_do_not_resend(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")], [Delta(text="ok")]])
        engine = _engine(loader, recorder, llm)

        first = await _run_turn(engine, recorder, llm, "t1")
        second = await _run_turn(engine, recorder, llm, "t2")

        assert _stable_digest(first["messages"]) == _stable_digest(second["messages"])
        sections_first = _system_sections(first["messages"])
        sections_second = _system_sections(second["messages"])
        assert sections_first == sections_second

        # The catalog is visible in BOTH turns (durable message), but the
        # second turn only carries turn 1's copy — no resend (A3's base).
        assert [t for t, _ in recorder.events].count("turn.skill_catalog") == 1
        reminder_first = [m for m in first["messages"] if is_catalog_message(str(m.get("content", "")))]
        reminder_second = [m for m in second["messages"] if is_catalog_message(str(m.get("content", "")))]
        assert len(reminder_first) == 1 and len(reminder_second) == 1
        assert reminder_first[0]["content"] == reminder_second[0]["content"]

    asyncio.run(scenario())


def test_a3_digest_change_resends_a_full_replacement(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")], [Delta(text="ok")]])
        engine = _engine(loader, recorder, llm)
        await _run_turn(engine, recorder, llm, "t1")

        # Description change -> new digest -> exactly one full replacement.
        write_skill(tmp_path / "skills", "kb-guide", description="Rewritten summary")
        loader.reload()
        await _run_turn(engine, recorder, llm, "t2")

        payloads = [p for t, p in recorder.events if t == "turn.skill_catalog"]
        assert len(payloads) == 2
        assert payloads[1]["entries"] == ["kb-guide"]
        messages = [m for m in recorder.messages if is_catalog_message(m.content)]
        assert len(messages) == 2
        assert parse_catalog_digest(messages[1].content) == payloads[1]["digest"]
        assert "Rewritten summary" in messages[1].content
        # Full replacement, not a diff: both messages carry the entry line.
        assert "`kb-guide`" in messages[0].content and "`kb-guide`" in messages[1].content

    asyncio.run(scenario())


def test_a8_stable_sections_never_carry_runtime_facts(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")]])
        engine = _engine(loader, recorder, llm)
        call = await _run_turn(engine, recorder, llm, "t1")

        stable_text = "\n".join(
            str(m.get("content", "")) for m in call["messages"]
            if m.get("role") == "system" and not str(m.get("content", "")).startswith("Runtime facts")
        )
        context_text = "\n".join(
            str(m.get("content", "")) for m in call["messages"]
            if m.get("role") == "system" and str(m.get("content", "")).startswith("Runtime facts")
        )
        assert context_text, "layered must emit a runtime-facts context section"
        for pattern in (_IPV4, _ISO, _ABS_PATH):
            assert not pattern.search(stable_text), f"stable section carries a runtime fact: {pattern.pattern}"
        assert "cwd:" not in stable_text and "tokens left" not in stable_text.lower()
        # ...and the facts ARE in context, where they are allowed:
        assert "cwd:" in context_text

    asyncio.run(scenario())


def test_layered_assemble_is_pure_and_orders_stable_before_context():
    parts = PromptParts(
        persona="You are Vade.", tool_block="Available tools (call by exact name):\n- fs__read_file: read [risk=low]",
        session_memory="- prefers terse", context="Runtime facts (may change between turns):\n- cwd: /x",
    )
    first = assemble(parts, injection="layered")
    second = assemble(parts, injection="layered")
    assert [m.to_dict() for m in first] == [m.to_dict() for m in second]
    roles = [(m.role, m.id) for m in first]
    assert roles == [
        ("system", "prompt-100-persona"),
        ("system", "prompt-200-tool_block"),
        ("system", "prompt-500-session_memory"),
        ("system", "prompt-600-runtime_facts"),
    ]
    # The legacy catalog/bodies slots are NOT system channels under layered:
    filled = PromptParts(persona="P", tool_block="T", skill_catalog="C", skill_bodies="B")
    assert all(m.content not in ("C", "B") for m in assemble(filled, injection="layered"))
    # ...and a legacy hook that still fills them cannot leak into the system:
    assert all(m.role == "system" for m in assemble(filled, injection="layered"))
