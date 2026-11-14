"""RFC-0008 A5: `turn/input.cancel` closes one dialog and the turn keeps going."""

from __future__ import annotations

import asyncio

from .helpers import build, of_type, start_turn, wait_pending


def test_cancel_closes_the_dialog_and_the_turn_continues(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="confirm", prompt="delete it?")
        executor.delta_after = "continuing without an answer"
        session = log.create_session()
        turn = await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        result = await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1")
        assert result == {"ok": True, "turn_id": turn.id, "status": "cancelled",
                          "input_id": input_id}
        await log.wait_idle(session.id)

        resolved = of_type(log, session.id, "turn.input_resolved")
        assert len(resolved) == 1
        assert resolved[0].payload["status"] == "cancelled"
        assert resolved[0].payload["by"] == "dev-1"
        assert resolved[0].payload["reason"] == "user_cancel"
        # The turn did NOT die: cancel is the "I don't want to answer" exit, it
        # is not `ui/interrupt` (§7.2).
        deltas = of_type(log, session.id, "turn.delta")
        assert [d.payload["text"] for d in deltas] == ["continuing without an answer"]
        assert resolved[0].seq < deltas[0].seq
        assert [t.state.value for t in log.store.load(session.id).turns] == ["completed"]

        outcome = executor.outcomes[0]
        assert outcome.ok is False and outcome.code == "CANCELLED"
        assert outcome.status == "cancelled"

    asyncio.run(scenario())


def test_two_cancels_emit_one_event(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="confirm", prompt="?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1")
        again = await manager.cancel(session_id=session.id, input_id=input_id, by="dev-2")
        assert again["ok"] is True and again["status"] == "cancelled"
        assert len(of_type(log, session.id, "turn.input_resolved")) == 1
        await log.wait_idle(session.id)

    asyncio.run(scenario())
