"""Replay: covered -> contiguous tail; not covered -> snapshot (never a hole)."""

from __future__ import annotations

import asyncio

import pytest

from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore
from .helpers import CollectSink


def _log(tmp_path, replay) -> SessionLog:
    async def executor(turn, content):
        from vade.loop.engine import TurnOutcome

        return TurnOutcome(assistant_text="ok")

    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=replay)
    log.set_executor(executor)
    return log


def test_replay_is_contiguous_and_has_no_duplicates(tmp_path):
    async def scenario():
        log = _log(tmp_path, ReplayBuffer(maxlen=500))
        session = log.create_session()
        for i in range(30):
            await log.emit(session.id, "turn.delta", {"turn_id": "t", "text": str(i)})
        sink = CollectSink()
        result = await log.attach(session.id, 10, sink)
        assert result.snapshot is None
        assert [e.seq for e in result.replay] == list(range(11, 31))
        await asyncio.sleep(0.05)
        assert sink.seqs() == list(range(11, 32))  # 11..30 replay + attached(31)
        assert len(sink.seqs()) == len(set(sink.seqs()))
        assert sink.types()[0] == "turn.delta"
        assert sink.types()[-1] == "session.attached"
        await log.close()

    asyncio.run(scenario())


def test_attach_without_last_seq_returns_snapshot(tmp_path):
    async def scenario():
        log = _log(tmp_path, ReplayBuffer(maxlen=10))
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "c1")
        await log.wait_idle(session.id)
        sink = CollectSink()
        result = await log.attach(session.id, None, sink)
        assert result.snapshot is not None
        assert result.snapshot["session"]["id"] == session.id
        assert [m["content"] for m in result.snapshot["messages"] if m["role"] == "user"] == ["hi"]
        assert result.from_seq == result.snapshot["from_seq"]
        assert result.snapshot["queue"]["running"] is None
        assert result.snapshot["queue"]["pending"] == []
        await asyncio.sleep(0.05)
        assert sink.events[0]["type"] == "session.attached"
        assert sink.events[0]["payload"]["snapshot"] is True
        await log.close()

    asyncio.run(scenario())


def test_trimmed_buffer_forces_snapshot(tmp_path):
    async def scenario():
        log = _log(tmp_path, ReplayBuffer(maxlen=5))
        session = log.create_session()
        for i in range(12):
            await log.emit(session.id, "turn.delta", {"turn_id": "t", "text": str(i)})
        sink = CollectSink()
        result = await log.attach(session.id, 2, sink)
        assert result.snapshot is not None, "old seq is gone; replay would leave a hole"
        assert result.replay == ()
        await asyncio.sleep(0.05)
        assert sink.events[0]["type"] == "session.attached"
        assert sink.events[0]["payload"]["snapshot"] is True
        await log.close()

    asyncio.run(scenario())


def test_expired_events_force_snapshot(tmp_path):
    async def scenario():
        now = [1000.0]
        buffer = ReplayBuffer(maxlen=100, ttl_s=24 * 3600, clock=lambda: now[0])
        log = _log(tmp_path, buffer)
        session = log.create_session()
        await log.emit(session.id, "turn.delta", {"turn_id": "t", "text": "a"})
        now[0] += 24 * 3600 + 1
        result = await log.attach(session.id, 1, CollectSink())
        assert result.snapshot is not None
        await log.close()

    asyncio.run(scenario())


def test_attach_creates_a_session_when_id_is_null(tmp_path):
    async def scenario():
        log = _log(tmp_path, ReplayBuffer())
        result = await log.attach(None, None, CollectSink())
        assert result.session_id
        assert log.get_session(result.session_id) is not None
        assert result.snapshot is not None
        await log.close()

    asyncio.run(scenario())
