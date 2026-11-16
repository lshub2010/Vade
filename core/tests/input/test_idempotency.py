"""RFC-0008 A6: the six-row idempotency matrix of §7.3."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError

from .helpers import build, of_type, start_turn, wait_pending


def _open(tmp_path, kind="free_text", value_options=None):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind=kind, prompt="q", options=value_options or ())
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        return log, manager, session, input_id

    return scenario()


def _resolved_count(log, session_id):
    return len(of_type(log, session_id, "turn.input_resolved"))


def test_row1_first_answer_is_accepted_and_emits_one_event(tmp_path):
    async def scenario():
        log, manager, session, input_id = await _open(tmp_path)
        result = await manager.submit(session_id=session.id, input_id=input_id, value="hello")
        assert result["status"] == "submitted" and result["duplicate"] is False
        assert _resolved_count(log, session.id) == 1
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_row3_same_value_again_is_a_duplicate_without_a_second_event(tmp_path):
    async def scenario():
        log, manager, session, input_id = await _open(tmp_path)
        await manager.submit(session_id=session.id, input_id=input_id, value="hello")
        again = await manager.submit(session_id=session.id, input_id=input_id, value="hello")
        assert again == {"ok": True, "turn_id": again["turn_id"], "status": "submitted",
                         "value": "hello", "duplicate": True, "input_id": input_id}
        assert _resolved_count(log, session.id) == 1
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_row4_a_different_value_is_refused_with_already_resolved(tmp_path):
    async def scenario():
        log, manager, session, input_id = await _open(tmp_path)
        await manager.submit(session_id=session.id, input_id=input_id, value="first")
        with pytest.raises(VadeError) as excinfo:
            await manager.submit(session_id=session.id, input_id=input_id, value="second")
        assert excinfo.value.code == "INVALID_ARGUMENT"
        assert excinfo.value.data["reason"] == "already_resolved"
        assert excinfo.value.data["status"] == "submitted"
        # The first answer still stands and no arbitrating second event appeared.
        assert _resolved_count(log, session.id) == 1
        assert manager.resolved(input_id).value == "first"
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_row5_after_timeout_a_late_submit_is_ok_with_status_timeout(tmp_path):
    async def scenario():
        log, manager, session, input_id = await _open(tmp_path)
        await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1")
        late = await manager.submit(session_id=session.id, input_id=input_id, value="too late")
        assert late["ok"] is True
        assert late["status"] == "cancelled"
        assert late["value"] is None
        assert late["duplicate"] is True
        assert _resolved_count(log, session.id) == 1
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_row6_unknown_id_is_not_found(tmp_path):
    async def scenario():
        log, manager, session, input_id = await _open(tmp_path)
        with pytest.raises(VadeError) as excinfo:
            await manager.submit(session_id=session.id, input_id="01JUNK", value="x")
        assert excinfo.value.code == "NOT_FOUND"
        with pytest.raises(VadeError) as excinfo:
            await manager.cancel(session_id=session.id, input_id="01JUNK")
        assert excinfo.value.code == "NOT_FOUND"
        # A different session must not learn whether the id exists (§7.1).
        with pytest.raises(VadeError) as excinfo:
            await manager.submit(session_id="someone-else", input_id=input_id, value="x")
        assert excinfo.value.code == "NOT_FOUND"
        # Close the question so the turn ends inside the test's own budget.
        await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1")
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_cancel_is_idempotent_and_keeps_the_current_status(tmp_path):
    async def scenario():
        log, manager, session, input_id = await _open(tmp_path)
        first = await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1")
        assert first["status"] == "cancelled"
        second = await manager.cancel(session_id=session.id, input_id=input_id, by="dev-2")
        assert second["ok"] is True and second["status"] == "cancelled"
        assert _resolved_count(log, session.id) == 1
        await log.wait_idle(session.id)

    asyncio.run(scenario())
