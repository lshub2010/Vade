"""RFC-0008 A9: one blocking wait per turn; `waiting_input` is exclusive."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.input import InputRequest
from vade.session.models import TurnState

from .helpers import HoldExecutor, build, events, start_turn, wait_until


async def _ask(manager, log, session_id, turn_id, **kwargs):
    request = InputRequest(
        session_id=session_id, turn_id=turn_id,
        emit=lambda event_type, payload: log.emit(session_id, event_type, payload),
        **kwargs,
    )
    return await manager.request(request)


def test_waiting_approval_blocks_an_input_request(tmp_path):
    """S3: queueing would decouple the dialog order from the seq order."""

    async def scenario():
        log, manager, _executor = build(tmp_path, executor=HoldExecutor())
        session = log.create_session()
        turn = await start_turn(log, session.id)
        holder = log._queues[session.id]._executor
        await holder.started.wait()

        await log.mark_state(session.id, turn.id, TurnState.WAITING_APPROVAL)
        before = len(events(log, session.id))

        with pytest.raises(VadeError) as excinfo:
            await _ask(manager, log, session.id, turn.id, kind="confirm", prompt="ok?")
        assert excinfo.value.code == "INVALID_ARGUMENT"
        assert excinfo.value.data["reason"] == "blocked_by_approval"
        assert log.turn_state(session.id, turn.id) == "waiting_approval"
        assert len(events(log, session.id)) == before
        assert manager.pending_count == 0
        holder.release.set()
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_a_second_concurrent_request_is_refused(tmp_path):
    """S4: the slot is single; `input_pending` names the reason."""

    async def scenario():
        log, manager, holder = build(tmp_path, executor=HoldExecutor())
        session = log.create_session()
        turn = await start_turn(log, session.id)
        await holder.started.wait()

        first = asyncio.create_task(
            _ask(manager, log, session.id, turn.id, kind="confirm", prompt="first?")
        )
        assert await wait_until(lambda: manager.pending_count == 1)
        with pytest.raises(VadeError) as excinfo:
            await _ask(manager, log, session.id, turn.id, kind="confirm", prompt="second?")
        assert excinfo.value.data["reason"] == "input_pending"
        assert manager.pending_count == 1

        [pending] = manager.pending_requests()
        await manager.submit(session_id=session.id, input_id=pending["input_id"], value=True)
        outcome = await asyncio.wait_for(first, 5)
        assert outcome.ok is True
        holder.release.set()
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_waiting_input_is_entered_and_left(tmp_path):
    """S2: running -> waiting_input on the event, back to running on the answer."""

    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="confirm", prompt="?")
        session = log.create_session()
        turn = await start_turn(log, session.id)
        assert await wait_until(lambda: manager.pending_count == 1)
        assert log.turn_state(session.id, turn.id) == "waiting_input"

        required = [e for e in events(log, session.id) if e.type == "turn.input_required"][0]
        assert required.payload["state"] == "waiting_input"
        # The state event and the queue state agree: `waiting_input` is not a
        # payload-only fiction (the defect §2.2 found in the approval path).
        assert required.seq == max(e.seq for e in events(log, session.id))

        [pending] = manager.pending_requests()
        await manager.submit(session_id=session.id, input_id=pending["input_id"], value=False)
        assert log.turn_state(session.id, turn.id) == "running"
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_a_terminal_turn_cannot_be_asked_again(tmp_path):
    """S6 / §5.1: `waiting_input` never leads to `cancelled`, and terminals stay closed."""

    async def scenario():
        log, manager, executor = build(tmp_path)
        session = log.create_session()
        turn = await start_turn(log, session.id)
        await log.wait_idle(session.id)
        assert log.store.load(session.id).turns[0].state is TurnState.COMPLETED

        with pytest.raises(VadeError) as excinfo:
            await _ask(manager, log, session.id, turn.id, kind="confirm", prompt="again?")
        assert excinfo.value.code == "INVALID_ARGUMENT"
        assert excinfo.value.data["reason"] == "turn_terminal"
        assert log.store.load(session.id).turns[0].state is TurnState.COMPLETED

    asyncio.run(scenario())


def test_waiting_input_is_not_a_terminal_state():
    from vade.session.models import TERMINAL_STATES

    assert TurnState.WAITING_INPUT not in TERMINAL_STATES
    assert TurnState.WAITING_INPUT.value == "waiting_input"
