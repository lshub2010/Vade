"""RFC-0006 §4 (A1–A4): an interrupt leaves the partial answer in the store.

The point of these tests is the *storage* path: `turn.interrupted` carries a
pointer, and a client that attaches afterwards must see the same characters the
interrupted client had already rendered.
"""

from __future__ import annotations

import asyncio
import re

from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog
from vade.session.models import Message
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

from .helpers import wait_until


def _log(tmp_path, executor) -> SessionLog:
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    return log


def _interrupted(events):
    return [e for e in events if e.type == "turn.interrupted"]


def test_interrupt_persists_the_partial_answer_and_points_at_it(tmp_path):
    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "Hel"})
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "lo"})
            await asyncio.sleep(30)  # holds the turn open until interrupted
            return TurnOutcome(assistant_text="Hello", stopped_reason="stop")

        log = _log(tmp_path, executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        assert await wait_until(lambda: log.partial_text(turn.id) == "Hello", 2.0)

        assert await log.interrupt(sess.id, turn.id) is True
        events = log.store.read_since(sess.id, 0)
        terminal = _interrupted(events)
        assert len(terminal) == 1
        payload = terminal[0].payload
        assert payload["partial_chars"] == 5
        # S4: one pointer convention (`msg:<hex>`, same as `result_ref`).
        assert re.fullmatch(r"msg:[0-9a-f]{32}", payload["partial_message_id"])
        # The event is a pointer, never a second copy of the text (§4.1).
        assert "text" not in payload

        snapshot = log.snapshot(sess.id)
        message_id = payload["partial_message_id"].split(":", 1)[1]
        message = next(m for m in snapshot["messages"] if m["id"] == message_id)
        assert message["role"] == "assistant"
        assert message["partial"] is True
        assert message["content"] == "Hello"

        row = next(t for t in snapshot["turns"] if t["id"] == turn.id)
        assert row["state"] == "interrupted"
        assert row["partial_message_id"] == payload["partial_message_id"]

    asyncio.run(scenario())


def test_replaying_the_interrupt_does_not_create_a_second_bubble(tmp_path):
    """A2: the store holds ONE partial message; replay is idempotent by id."""

    async def scenario():
        log = None
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "abc"})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="abc")

        log = _log(tmp_path, executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c2")
        assert await wait_until(lambda: started.is_set(), 2.0)
        assert await wait_until(lambda: log.partial_text(turn.id) == "abc", 2.0)
        await log.interrupt(sess.id, turn.id)

        # Re-reading the stream is what a reconnect does; the client dedupes by
        # `partial_message_id`, so a second read must not add a message.
        first = log.store.read_since(sess.id, 0)
        second = log.store.read_since(sess.id, 0)
        assert [e.seq for e in first] == [e.seq for e in second]
        chunks = [c for c in log.store.read_since(sess.id, 0) if c.type == "turn.interrupted"]
        assert len(chunks) == 1
        partials = [m for m in log.snapshot(sess.id)["messages"] if m["partial"]]
        assert len(partials) == 1
        assert partials[0]["content"] == "abc"

        # A late delta cannot reopen the closed turn (§4.2 tombstone).
        before = len(log.store.read_since(sess.id, 0))
        await log.emit(sess.id, "turn.delta", {"turn_id": turn.id, "text": "late"})
        assert len(log.store.read_since(sess.id, 0)) == before

    asyncio.run(scenario())


def test_interrupt_before_the_first_delta_creates_no_empty_message(tmp_path):
    """A3: no delta yet → partial_message_id null, partial_chars 0, no bubble."""

    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="never")

        log = _log(tmp_path, executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c3")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.interrupt(sess.id, turn.id)

        payload = _interrupted(log.store.read_since(sess.id, 0))[0].payload
        assert payload["partial_message_id"] is None
        assert payload["partial_chars"] == 0
        assert [m for m in log.snapshot(sess.id)["messages"] if m["role"] == "assistant"] == []

    asyncio.run(scenario())


def test_a_tombstoned_event_takes_its_messages_with_it(tmp_path):
    """S2: a late event must not persist its messages either.

    The tombstone used to `continue` past the event only: `messages` still went
    to `store.append` with no event to anchor them, which wrote a `seq=0`
    assistant bubble into a turn that had already closed.
    """

    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="never")

        log = _log(tmp_path, executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c5")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.interrupt(sess.id, turn.id)

        before_events = len(log.store.read_since(sess.id, 0))
        before_messages = len(log.snapshot(sess.id)["messages"])

        ghost = Message(id="late-ghost", session_id=sess.id, turn_id=turn.id,
                        role="assistant", content="ghost")
        assert await log.emit(
            sess.id, "turn.delta", {"turn_id": turn.id, "text": "ghost"},
            messages=[ghost],
        ) is None
        assert await log.emit(
            sess.id, "turn.tool_call",
            {"turn_id": turn.id, "call_id": "late", "tool": "fs__read_file",
             "args_digest": "sha256:deadbeef"},
            messages=[Message(id="late-ghost-2", session_id=sess.id, turn_id=turn.id,
                              role="assistant", content="ghost")],
        ) is None

        assert len(log.store.read_since(sess.id, 0)) == before_events
        messages = log.snapshot(sess.id)["messages"]
        assert len(messages) == before_messages
        assert [m["id"] for m in messages if m["id"].startswith("late-ghost")] == []
        # No row can be written without an event: every stored message is anchored.
        assert all(m["seq"] > 0 for m in messages)
        assert all(m.seq > 0 for m in log.store.load(sess.id).messages)

    asyncio.run(scenario())


def test_interrupt_during_a_tool_step_keeps_tool_messages_and_adds_no_partial(tmp_path):
    """A4: completed tool messages stay; no empty assistant partial is written."""

    async def scenario():
        log = None
        started = asyncio.Event()

        async def executor(turn, content):
            call_id = "call-1"
            await log.emit(
                turn.session_id, "turn.tool_call",
                {"turn_id": turn.id, "call_id": call_id, "tool": "fs__read_file",
                 "args_digest": "sha256:deadbeef"},
                messages=[Message(id="m-a", session_id=turn.session_id, turn_id=turn.id,
                                  role="assistant", content="",
                                  tool_calls=({"id": call_id, "type": "function",
                                               "function": {"name": "fs__read_file",
                                                            "arguments": "{}"}},))],
            )
            await log.emit(
                turn.session_id, "turn.tool_result",
                {"turn_id": turn.id, "call_id": call_id, "ok": True, "summary": "ok"},
                messages=[Message(id="m-t", session_id=turn.session_id, turn_id=turn.id,
                                  role="tool", name="fs__read_file", tool_call_id=call_id,
                                  content="result")],
            )
            started.set()
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="x")

        log = _log(tmp_path, executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c4")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.interrupt(sess.id, turn.id)

        payload = _interrupted(log.store.read_since(sess.id, 0))[0].payload
        assert payload["partial_message_id"] is None
        messages = log.snapshot(sess.id)["messages"]
        assert [m["id"] for m in messages if m["role"] == "tool"] == ["m-t"]
        assert [m for m in messages if m["partial"]] == []

    asyncio.run(scenario())
