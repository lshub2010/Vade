"""RFC-0008 A10: `input_resolved` lands BEFORE `turn.interrupted`, and nothing after."""

from __future__ import annotations

import asyncio

from .helpers import build, events, start_turn, wait_pending


def test_interrupt_closes_the_dialog_before_the_terminal_event(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="free_text", prompt="still there?")
        session = log.create_session()
        turn = await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        assert await log.interrupt(session.id, turn.id, by="dev-1") is True
        await log.wait_idle(session.id)

        stream = events(log, session.id)
        names = [e.type for e in stream]
        assert "turn.input_resolved" in names and "turn.interrupted" in names
        resolved_index = names.index("turn.input_resolved")
        interrupted_index = names.index("turn.interrupted")
        assert resolved_index < interrupted_index, names

        payload = stream[resolved_index].payload
        assert payload["input_id"] == input_id
        assert payload["status"] == "cancelled"
        assert payload["by"] == "system"
        assert payload["reason"] == "turn_interrupted"
        # §6.2: the state written is the one the turn is actually in.
        assert payload["state"] == "interrupted"

        # C-06: after `turn.interrupted` there is no further `turn.*` for this turn.
        after = [e.type for e in stream[interrupted_index + 1:] if e.type.startswith("turn.")]
        assert after == []
        assert manager.pending_count == 0

    asyncio.run(scenario())


def test_interrupt_on_a_turn_that_is_not_waiting_is_unchanged(tmp_path):
    """The hook must not invent an `input_resolved` when nothing was pending."""

    async def scenario():
        log, manager, executor = build(tmp_path)
        session = log.create_session()
        turn = await start_turn(log, session.id)
        await log.wait_idle(session.id)
        assert await log.interrupt(session.id, turn.id) is False
        assert [e for e in events(log, session.id) if e.type == "turn.input_resolved"] == []

    asyncio.run(scenario())
