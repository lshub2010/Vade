"""A12 (RFC-0003 §8): install a skill, replay from seq 0 — the catalog message
is an ordinary persisted message with its own seq, so replay carries it; two
turns with the same digest produce exactly ONE catalog message (no duplicates).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from vade.llm.base import Delta
from vade.llm.mock import MockLLMProvider
from vade.loop.coretools import build_core_tools
from vade.loop.engine import LoopConfig, LoopEngine
from vade.loop.tools import ToolEntry, ToolSnapshot
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.skill.loader import SkillLoader
from vade.skill.port import LayeredCatalogPort

from ..test_skill.helpers import write_skill


class _Router:
    async def call(self, tool_id, args, **kwargs):
        from vade.router.dispatch import ToolCallResult

        return ToolCallResult(True, tool_id, result={"content": "hi"})


def _snapshot() -> ToolSnapshot:
    entry = ToolEntry(
        llm_name="fs__read_file", tool_id="com.example.fs/read_file", module_id="m",
        description="read", input_schema={"type": "object", "properties": {}},
        risk="low", timeout_ms=1000, idempotent=True,
    )
    return ToolSnapshot(entries=(entry,))


def test_replay_carries_the_catalog_message_without_duplicates(tmp_path):
    skills = tmp_path / "skills"
    write_skill(skills, "kb-guide")

    class _Cfg:
        def __init__(self) -> None:
            self._v = {"prompt.injection": "layered"}

        def get(self, key, default=None):
            return self._v.get(key, default)

    loader = SkillLoader(skills, config=_Cfg())
    loader.reload()

    async def scenario():
        from vade.session.sqlite_store import SQLiteStore

        log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
        session = log.create_session()
        llm = MockLLMProvider([[Delta(text="one")], [Delta(text="two")]])
        engine = LoopEngine(
            _Router(), llm, _snapshot, log.emit,
            history=_history_of(log),
            prompt_hooks=lambda: [loader.render],
            config=LoopConfig(injection="layered", max_steps=2),
            core_tools=build_core_tools(loader),
            catalog=LayeredCatalogPort(loader),
            memory=None,
        )
        log.set_executor(engine.run_turn)

        turn1, _ = await log.submit(session.id, [{"type": "text", "text": "hello"}], "c1")
        await log.wait_idle(session.id)
        turn2, _ = await log.submit(session.id, [{"type": "text", "text": "again"}], "c2")
        await log.wait_idle(session.id)

        # Replay from seq 0: exactly one catalog message, digest == current.
        sink = _Sink()
        result = await log.attach(session.id, None, sink)
        catalog_rows = [
            m for m in result.snapshot["messages"]
            if m["role"] == "user" and str(m["content"]).startswith("<system-reminder>")
        ]
        assert len(catalog_rows) == 1
        assert "`kb-guide`" in catalog_rows[0]["content"]

        # The digest in the log equals the loader's current digest.
        digest = loader.catalog_digest(loader.catalog())
        assert f"digest: {digest}" in catalog_rows[0]["content"]

        # And the model saw the catalog in BOTH turns without a resend: the
        # second call's messages contain exactly one reminder (history's copy).
        reminders_per_call = [
            sum(1 for m in call["messages"]
                if m.get("role") == "user" and str(m.get("content", "")).startswith("<system-reminder>"))
            for call in llm.calls
        ]
        assert reminders_per_call == [1, 1]
        await log.close()

    asyncio.run(scenario())


def _history_of(log):
    from vade.loop.context import filter_superseded

    def history(session_id: str):
        session = log.get_session(session_id)
        if session is None:
            return ()
        return tuple(filter_superseded(session.messages, session.turns))

    return history


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send(self, event: dict) -> None:
        self.events.append(dict(event))
