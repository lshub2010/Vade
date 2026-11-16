"""k1-session closeout: OVERLOADED, close(), approval sweep, mark_state.

Four defects, one file each so a regression names its own bug:

* a full queue used to leave the announced turn in `queued` forever (the
  `turn.queued` write happened before `enqueue` could refuse);
* `close()` never ran `app.on_session_end` (so `session.closed` and the grant
  cleanup never fired) and resurrected a worker after cancelling it;
* a stopped turn left its `turn.approval_required` card open on every device;
* `mark_state` swallowed the queue's verdict and refused the wire string.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vade.errors import VadeError
from vade.session.manager import SessionLog
from vade.session.models import TurnState
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

from .helpers import CollectSink, GateExecutor, wait_until


def _log(tmp_path, executor, *, max_pending: int = 1, db: str = "k1.db") -> SessionLog:
    log = SessionLog(
        SQLiteStore(tmp_path / db), replay=ReplayBuffer(maxlen=500),
        max_pending=max_pending,
    )
    log.set_executor(executor)
    return log


def _events(log: SessionLog, session_id: str) -> list[Any]:
    return log.store.read_since(session_id, 0)


def _types(events) -> list[str]:
    return [event.type for event in events]


def _turn_row(log: SessionLog, session_id: str, turn_id: str):
    session = log.store.load(session_id)
    assert session is not None
    for turn in session.turns:
        if turn.id == turn_id:
            return turn
    raise AssertionError(f"turn {turn_id} was never persisted")


def _refuse_enqueue(queue) -> None:
    """Simulate the race: the slot filled between the pre-check and the enqueue."""

    def enqueue(turn, content):  # noqa: ANN001 - mirrors TurnQueue.enqueue
        raise VadeError("OVERLOADED", f"queue is full ({queue.max_pending} pending turns)")

    queue.enqueue = enqueue  # type: ignore[method-assign]


# ------------------------------------------------------------------ OVERLOADED
def test_submit_overloaded_refuses_before_anything_is_written(tmp_path):
    """A queue that is already full must not leave a `queued` turn behind."""

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=1)
        session = log.create_session()

        first, _ = await log.submit(session.id, [{"type": "text", "text": "one"}], "s1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == first.id)
        # The second turn fills the single pending slot while `first` runs.
        await log.submit(session.id, [{"type": "text", "text": "two"}], "s2")

        with pytest.raises(VadeError) as caught:
            await log.submit(session.id, [{"type": "text", "text": "three"}], "s3")
        assert caught.value.code == "OVERLOADED"
        assert caught.value.data == {"reason": "queue_full"}

        # Nothing of the refused turn reached the log: no event announced it and
        # no row exists, so the idempotent retry cannot hand back a corpse.
        assert "s3" not in [e.payload.get("client_msg_id") for e in _events(log, session.id)]
        assert log.store.find_turn_by_client_msg(session.id, "s3") is None
        assert log.queue_state(session.id)["running"] == first.id

        executor.gate.set()
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_submit_rolls_back_a_turn_announced_but_not_enqueued(tmp_path):
    """The emit-then-enqueue window: the announced turn must fail, not hang."""

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=1)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "one"}], "s1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == first.id)

        _refuse_enqueue(log._queues[session.id])
        with pytest.raises(VadeError) as caught:
            await log.submit(session.id, [{"type": "text", "text": "two"}], "s2")
        assert caught.value.code == "OVERLOADED"

        events = _events(log, session.id)
        queued = next(e for e in events if e.type == "turn.queued"
                      and e.payload.get("client_msg_id") == "s2")
        failed = next(e for e in events if e.type == "turn.failed"
                      and e.payload.get("turn_id") == queued.payload["turn_id"])
        assert _types(events).index("turn.queued") < _types(events).index("turn.failed")
        assert failed.payload["state"] == "failed"
        assert failed.payload["code"] == "OVERLOADED"
        assert failed.payload["data"] == {"reason": "queue_full"}
        assert failed.payload["usage"]["source"] == "none"

        row = _turn_row(log, session.id, queued.payload["turn_id"])
        assert row.state is TurnState.FAILED
        assert row.ended_at is not None
        # The failed turn is terminal, so the version chain is not blocked.
        assert row.state.value in {"failed"}

        executor.gate.set()
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_regenerate_rolls_back_the_new_version_when_enqueue_fails(tmp_path):
    """`turn.superseded` is already persisted; the new version must end `failed`."""

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        executor.gate.set()
        await log.wait_idle(session.id)

        _refuse_enqueue(log._queues[session.id])
        with pytest.raises(VadeError) as caught:
            await log.regenerate(session.id, first.id, "r1")
        assert caught.value.code == "OVERLOADED"

        events = _events(log, session.id)
        types = _types(events)
        queued_index = next(
            i for i, e in enumerate(events)
            if e.type == "turn.queued" and e.payload.get("client_msg_id") == "r1"
        )
        assert types.index("turn.superseded") < queued_index
        assert queued_index < types.index("turn.failed")
        new_id = next(e.payload["turn_id"] for e in events
                      if e.type == "turn.queued" and e.payload.get("client_msg_id") == "r1")
        assert _turn_row(log, session.id, first.id).superseded_by == new_id
        # Roll-forward, not roll-back: the chain now ends in a failed v2, which
        # is a terminal row the client can show and the user can resubmit.
        failed = _turn_row(log, session.id, new_id)
        assert failed.state is TurnState.FAILED
        assert failed.error_code == "OVERLOADED"

    asyncio.run(scenario())


def test_edit_resubmit_rolls_back_the_new_version_when_enqueue_fails(tmp_path):
    """The edit already pointed the old message at the new one — no silence."""

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        executor.gate.set()
        await log.wait_idle(session.id)
        assert first.user_message_id is not None

        _refuse_enqueue(log._queues[session.id])
        with pytest.raises(VadeError) as caught:
            await log.edit_resubmit(
                session.id, first.user_message_id, [{"type": "text", "text": "edited"}], "e1",
            )
        assert caught.value.code == "OVERLOADED"

        events = _events(log, session.id)
        types = _types(events)
        assert "message.edited" in types
        queued_index = next(
            i for i, e in enumerate(events)
            if e.type == "turn.queued" and e.payload.get("client_msg_id") == "e1"
        )
        assert queued_index < types.index("turn.failed")
        new_id = next(e.payload["turn_id"] for e in events
                      if e.type == "turn.queued" and e.payload.get("client_msg_id") == "e1")
        reloaded = log.store.load(session.id)
        assert reloaded is not None
        old_message = next(m for m in reloaded.messages if m.id == first.user_message_id)
        assert old_message.superseded_by is not None
        assert _turn_row(log, session.id, new_id).state is TurnState.FAILED

    asyncio.run(scenario())


# ------------------------------------------------------------------ close()
class _FakeInputPort:
    """Only the end hook: the RFC-0008 dialog closer."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.sessions: list[str] = []

    async def on_session_end(self, session_id: str) -> None:
        self.order.append("dialog")
        self.sessions.append(session_id)


def test_close_runs_hooks_dialogs_queue_then_detach_and_is_idempotent(tmp_path):
    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=4)
        session = log.create_session()
        order: list[str] = []
        attached_during_hook: list[int] = []
        seen: list[str] = []

        def sync_hook(session_id: str) -> None:
            order.append("hook:sync")
            seen.append(session_id)
            # The end beat reads the store and emits onto the seq bus: the sink
            # must still be there, so hooks run before `detach`.
            attached_during_hook.append(log.attached_count(session_id))

        async def async_hook(session_id: str) -> None:
            order.append("hook:async")
            seen.append(session_id)

        log.set_session_end_hooks([sync_hook, async_hook])
        input_port = _FakeInputPort(order)
        log.input_port = input_port
        await log.attach(session.id, None, CollectSink())
        assert log.attached_count(session.id) == 1
        await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")

        queue = log._queues[session.id]
        original_shutdown = queue.shutdown

        async def recording_shutdown() -> None:
            order.append("shutdown")
            await original_shutdown()

        queue.shutdown = recording_shutdown  # type: ignore[method-assign]
        closes: list[int] = []
        original_close = log.store.close
        log.store.close = lambda: (closes.append(1), original_close())  # type: ignore[method-assign]

        await log.close()
        assert order == ["hook:sync", "hook:async", "dialog", "shutdown"]
        assert seen == [session.id, session.id]
        assert attached_during_hook == [1]
        assert input_port.sessions == [session.id]
        assert log.attached_count(session.id) == 0
        assert queue._worker is None and queue._stopped is True
        assert closes == [1]

        # A second close (a transport teardown racing app shutdown) must not
        # re-publish the beat or touch the already-closed store.
        await log.close()
        assert order == ["hook:sync", "hook:async", "dialog", "shutdown"]
        assert closes == [1]

    asyncio.run(scenario())


def test_close_does_not_respawn_the_worker_for_a_pending_turn(tmp_path):
    """`_run_loop`'s `finally` used to start a new worker right after cancel."""

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=2)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "one"}], "s1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == first.id)
        second, _ = await log.submit(session.id, [{"type": "text", "text": "two"}], "s2")
        queue = log._queues[session.id]
        assert queue._worker is not None

        await log.close()
        await asyncio.sleep(0.05)  # a resurrected worker would have started by now

        assert queue._stopped is True
        assert queue._worker is None
        assert "turn.started" not in [
            e.type for e in _events(log, session.id)
            if e.payload.get("turn_id") == second.id
        ]
        queue.ensure_worker()
        assert queue._worker is None

    asyncio.run(scenario())


# ------------------------------------------------------------------ approvals
class _ApprovalPort:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.listed: list[str] = []

    def pending(self, session_id: str) -> list[dict[str, Any]]:
        self.listed.append(session_id)
        return [dict(row) for row in self.rows]


def test_turn_ending_denies_every_approval_still_pending_on_it(tmp_path):
    """A stopped turn must close its own dialogs before the terminal event."""

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor)
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "rm -rf"}], "s1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id)

        port = _ApprovalPort([
            {"request_id": "req-1", "turn_id": turn.id, "session_id": session.id,
             "task_id": "task-1", "task_name": "child", "depth": 1},
            # Another turn's dialog: not ours to close.
            {"request_id": "req-2", "turn_id": "turn-other", "session_id": session.id,
             "task_id": None, "task_name": None, "depth": 0},
        ])
        log.approval_port = port

        assert await log.interrupt(session.id, turn.id) is True
        await log.wait_idle(session.id)

        events = _events(log, session.id)
        types = _types(events)
        resolved = [e for e in events if e.type == "turn.approval_resolved"]
        assert [e.payload["request_id"] for e in resolved] == ["req-1"]
        payload = resolved[0].payload
        assert payload["decision"] == "deny"
        assert payload["by"] == "system"
        assert payload["scope"] == "once"
        assert payload["state"] == "interrupted"
        assert payload["task_id"] == "task-1" and payload["task_name"] == "child"
        assert payload["depth"] == 1
        # Contract order: the dialog closes, THEN the turn is terminal. After
        # the terminal event the tombstone drops it (C-06).
        assert types.index("turn.approval_resolved") < types.index("turn.interrupted")

        before = len(events)
        late = await log.emit(session.id, "turn.approval_resolved", {
            "turn_id": turn.id, "request_id": "req-1", "decision": "allow",
            "by": "dev-1", "scope": "once", "state": "running",
        })
        assert late is None
        assert len(_events(log, session.id)) == before
        assert port.listed == [session.id]

    asyncio.run(scenario())


# ------------------------------------------------------------------ mark_state
def test_mark_state_accepts_the_wire_string_and_returns_the_verdict(tmp_path):
    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor)
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id)

        # The RFC-0008 input channel passes the string; the approval path the enum.
        assert await log.mark_state(session.id, turn.id, "waiting_approval") is True
        assert log.queue_state(session.id)["running_state"] == "waiting_approval"
        # Idempotent: re-checking the same state publishes nothing.
        assert await log.mark_state(session.id, turn.id, TurnState.WAITING_APPROVAL) is False
        assert await log.mark_state(session.id, turn.id, TurnState.WAITING_INPUT) is True
        assert log.queue_state(session.id)["running_state"] == "waiting_input"
        # Unknown turn, unknown session: the move did not happen.
        assert await log.mark_state(session.id, "turn-nope", "running") is False
        assert await log.mark_state("session-nope", turn.id, "running") is False

        executor.gate.set()
        await log.wait_idle(session.id)
        # A finished turn can no longer move: the queue no longer holds it.
        assert await log.mark_state(session.id, turn.id, "running") is False

    asyncio.run(scenario())
