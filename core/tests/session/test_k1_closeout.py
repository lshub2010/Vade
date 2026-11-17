"""k1-session closeout: OVERLOADED, close(), approval sweep, mark_state.

Four defects, one file each so a regression names its own bug:

* a full queue used to leave the announced turn in `queued` forever (the
  `turn.queued` write happened before `enqueue` could refuse);
* `close()` never ran `app.on_session_end` (so `session.closed` and the grant
  cleanup never fired) and resurrected a worker after cancelling it;
* a stopped turn left its `turn.approval_required` card open on every device;
* `mark_state` swallowed the queue's verdict and refused the wire string.

The OVERLOADED section also pins the two follow-ups the enqueue-race repair
left behind:

* roll-forward, not roll-back: `superseded_by`/`message.edited` keep pointing
  at the announced v2 even when that v2 never entered the queue (events are
  append-only; the failed v2 is a real chain head);
* an idempotent retry that hits that `failed{OVERLOADED}` row is answered with
  `OVERLOADED`, never a 200 that silently drops the message.
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

        # Nothing was written, so the same key is NOT a refused corpse: once
        # there is room again the retry is a plain submit and lands.
        again, _ = await log.submit(session.id, [{"type": "text", "text": "three"}], "s3")
        assert again.user_text == "three"
        await log.wait_idle(session.id)
        assert _turn_row(log, session.id, again.id).state is TurnState.COMPLETED

    asyncio.run(scenario())


def test_submit_terminates_a_turn_announced_but_not_enqueued(tmp_path):
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


def test_submit_retry_after_an_enqueue_race_is_overloaded_not_200(tmp_path):
    """P1-2: a refused `client_msg_id` must not replay a never-queued turn.

    `find_turn_by_client_msg` finds the `failed{OVERLOADED}` corpse the race
    left behind; answering 200 with it told the client the text was accepted
    when no engine ever saw it. The retry is refused with the retriable
    `OVERLOADED` instead, and a fresh key really queues the message.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=1)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "one"}], "s1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == first.id)

        queue = log._queues[session.id]
        original_enqueue = queue.enqueue
        _refuse_enqueue(queue)
        with pytest.raises(VadeError) as caught:
            await log.submit(session.id, [{"type": "text", "text": "two"}], "s2")
        assert caught.value.code == "OVERLOADED"

        corpse = log.store.find_turn_by_client_msg(session.id, "s2")
        assert corpse is not None
        assert corpse.state is TurnState.FAILED
        assert corpse.error_code == "OVERLOADED"
        assert corpse.started_at is None

        with pytest.raises(VadeError) as retried:
            await log.submit(session.id, [{"type": "text", "text": "two"}], "s2")
        assert retried.value.code == "OVERLOADED"
        assert retried.value.data == {
            "reason": "prior_attempt_failed", "turn_id": corpse.id,
            "client_msg_id": "s2",
        }
        # No duplicate row and no duplicate announcement for the refused key.
        assert len([
            e for e in _events(log, session.id)
            if e.type == "turn.queued" and e.payload.get("client_msg_id") == "s2"
        ]) == 1

        # A fresh key (the documented retry) lands the same text for real.
        queue.enqueue = original_enqueue
        second, _ = await log.submit(session.id, [{"type": "text", "text": "two"}], "s2b")
        assert second.id != corpse.id
        assert _turn_row(log, session.id, second.id).user_text == "two"

        executor.gate.set()
        await log.wait_idle(session.id)
        assert _turn_row(log, session.id, second.id).state is TurnState.COMPLETED

    asyncio.run(scenario())


def test_overloaded_after_a_started_turn_is_still_an_idempotent_result(tmp_path):
    """Only the never-started corpse is special; a turn that started is returned.

    A model-resolution failure carries `error_code="OVERLOADED"` too, but its
    `started_at` is set: the request was dequeued and the failure is the real
    outcome of that attempt, so the idempotent replay must return it.
    """

    async def scenario():
        async def executor(turn, content):  # pragma: no cover - never reached
            raise AssertionError("no engine call expected")

        class _OverloadedModels:
            def resolve(self, model_id):
                raise VadeError("OVERLOADED", "model router is busy")

        log = _log(tmp_path, executor, db="provider.db")
        log.models = _OverloadedModels()
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        await log.wait_idle(session.id)

        row = _turn_row(log, session.id, turn.id)
        assert row.state is TurnState.FAILED
        assert row.error_code == "OVERLOADED"
        assert row.started_at is not None  # dequeued; not the enqueue corpse

        # Replay of the same request returns the original failed turn: the
        # failure is that attempt's outcome, not a silently dropped message.
        replay, pos = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        assert replay.id == turn.id
        assert pos == 0

    asyncio.run(scenario())


def test_regenerate_rolls_forward_to_a_failed_version_when_enqueue_fails(tmp_path):
    """`turn.superseded` is already persisted; the new version must end `failed`.

    Roll-forward is the chosen contract (events are append-only, so the old turn
    cannot be un-pointed): v2 stays in the chain as a terminal head. A retry
    with the SAME `client_msg_id` is refused with `OVERLOADED` instead of being
    answered 200 with the never-queued turn; a fresh key regenerates the failed
    head into v3, reusing the same user message.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        executor.gate.set()
        await log.wait_idle(session.id)

        queue = log._queues[session.id]
        original_enqueue = queue.enqueue
        _refuse_enqueue(queue)
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
        # is a terminal row the client can show and the user can work from.
        failed = _turn_row(log, session.id, new_id)
        assert failed.state is TurnState.FAILED
        assert failed.error_code == "OVERLOADED"
        assert failed.started_at is None
        assert failed.parent_turn_id == first.id
        assert failed.version == 2

        # Same key: OVERLOADED with a pointer to the corpse, never a 200 replay.
        with pytest.raises(VadeError) as retried:
            await log.regenerate(session.id, first.id, "r1")
        assert retried.value.code == "OVERLOADED"
        assert retried.value.data == {
            "reason": "prior_attempt_failed", "turn_id": new_id,
            "client_msg_id": "r1",
        }
        assert len([
            e for e in _events(log, session.id)
            if e.type == "turn.queued" and e.payload.get("client_msg_id") == "r1"
        ]) == 1

        # A fresh key on the failed head produces v3 with the same user message:
        # the text is never lost to the refused version.
        queue.enqueue = original_enqueue
        result = await log.regenerate(session.id, new_id, "r2")
        assert result["version"] == 3
        assert result["supersedes_turn_id"] == new_id
        assert _turn_row(log, session.id, new_id).superseded_by == result["turn_id"]
        v3 = _turn_row(log, session.id, result["turn_id"])
        assert v3.user_message_id == first.user_message_id

        executor.gate.set()
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_edit_resubmit_rolls_forward_to_a_failed_version_when_enqueue_fails(tmp_path):
    """The edit already pointed the old message at the new one — no silence.

    `message.edited` and `turn.superseded` stay durable and must agree with the
    announced v2 (`new_turn_id`, `superseded_by_message_id`), even though v2
    never entered the queue. The same-key retry is `OVERLOADED`; editing the
    failed edition's message with a fresh key continues the chain as v3.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        executor.gate.set()
        await log.wait_idle(session.id)
        assert first.user_message_id is not None

        queue = log._queues[session.id]
        original_enqueue = queue.enqueue
        _refuse_enqueue(queue)
        with pytest.raises(VadeError) as caught:
            await log.edit_resubmit(
                session.id, first.user_message_id, [{"type": "text", "text": "edited"}], "e1",
            )
        assert caught.value.code == "OVERLOADED"

        events = _events(log, session.id)
        types = _types(events)
        edited = next(e for e in events if e.type == "message.edited")
        queued = next(e for e in events
                      if e.type == "turn.queued" and e.payload.get("client_msg_id") == "e1")
        assert queued.payload["reason"] == "edit_resubmit"
        new_id = queued.payload["turn_id"]
        new_message_id = queued.payload["user_message_id"]
        assert types.index("message.edited") < types.index("turn.superseded")
        assert types.index("turn.superseded") < next(
            i for i, e in enumerate(events) if e is queued
        )
        assert next(i for i, e in enumerate(events) if e is queued) < types.index("turn.failed")
        # The edited bubble and the superseded turn point at the announced v2,
        # never at a turn that was not persisted.
        assert edited.payload["new_turn_id"] == new_id
        assert edited.payload["superseded_by_message_id"] == new_message_id
        reloaded = log.store.load(session.id)
        assert reloaded is not None
        old_message = next(m for m in reloaded.messages if m.id == first.user_message_id)
        assert old_message.superseded_by == new_message_id
        assert _turn_row(log, session.id, first.id).superseded_by == new_id
        failed = _turn_row(log, session.id, new_id)
        assert failed.state is TurnState.FAILED
        assert failed.error_code == "OVERLOADED"
        assert failed.started_at is None
        assert failed.user_message_id == new_message_id

        # Same key: OVERLOADED, not a 200 with the failed edition.
        with pytest.raises(VadeError) as retried:
            await log.edit_resubmit(
                session.id, first.user_message_id, [{"type": "text", "text": "edited"}], "e1",
            )
        assert retried.value.code == "OVERLOADED"
        assert retried.value.data == {
            "reason": "prior_attempt_failed", "turn_id": new_id,
            "client_msg_id": "e1",
        }

        # A fresh key edits the failed edition's message; v3 supersedes v2.
        queue.enqueue = original_enqueue
        result = await log.edit_resubmit(
            session.id, new_message_id, [{"type": "text", "text": "edited again"}], "e2",
        )
        assert result["supersedes_turn_id"] == new_id
        assert _turn_row(log, session.id, new_id).superseded_by == result["turn_id"]

        executor.gate.set()
        await log.wait_idle(session.id)

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
        # The store is closed now, so assert the in-memory queue state instead of
        # re-reading events: the queued turn must still be pending and untouched.
        assert [item.turn.id for item in queue._pending] == [second.id]
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
