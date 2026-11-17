"""k1-session closeout: OVERLOADED, close(), approval sweep, mark_state.

Four defects, one file each so a regression names its own bug:

* a full queue used to leave the announced turn in `queued` forever (the
  `turn.queued` write happened before `enqueue` could refuse);
* `close()` never ran `app.on_session_end` (so `session.closed` and the grant
  cleanup never fired) and resurrected a worker after cancelling it;
* a stopped turn left its `turn.approval_required` card open on every device;
* `mark_state` swallowed the queue's verdict and refused the wire string.

The OVERLOADED section pins the shape that replaced the old roll-forward
repair: capacity is CLAIMED before the first write, so a turn is never
announced and then refused. The roll-forward path (`_fail_unstarted`,
`_prior_overloaded`) is gone because the window it patched is unreachable —
see `batch-results/g3-overload.md` for the measurement.
"""

from __future__ import annotations

import asyncio
from collections import deque
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


# ------------------------------------------------------------------ OVERLOADED
#
# Capacity is decided ONCE, before the first write: `_queue_for_new_turn` CLAIMS
# a slot and `enqueue(..., claim=...)` consumes it. It used to be a bare
# `has_capacity()` sample separated from `enqueue` by an `emit` (and, for
# regenerate/edit, by `turn.superseded`/`message.edited`), and the repair for
# losing that race was roll-forward: announce the turn, get refused by the full
# queue, then terminate the announced turn `failed{OVERLOADED}` and refuse a
# replay of its `client_msg_id`.
#
# That window is unreachable in this code — `submit` performs no suspension at
# all (`emit` takes an uncontended `asyncio.Lock`, writes through a sync store
# and broadcasts to sync sinks) — so the repair is deleted rather than kept
# alive by a stubbed `enqueue`. The claim is what keeps the invariant honest if
# an `emit` ever does yield: the slot is already taken, so no announcement can
# be followed by a capacity refusal. The tests below drive real objects only.


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
        # no row exists, so a retry cannot find a corpse to hand back.
        assert "s3" not in [e.payload.get("client_msg_id") for e in _events(log, session.id)]
        assert log.store.find_turn_by_client_msg(session.id, "s3") is None
        assert log.queue_state(session.id)["running"] == first.id
        # A refused submit must not leave its slot claim behind either.
        queue = log._queues[session.id]
        assert queue._claims == {}
        assert [e.type for e in _events(log, session.id)].count("turn.failed") == 0

        executor.gate.set()
        await log.wait_idle(session.id)

        # Nothing was written, so once there is room the same key is a plain
        # submit and lands for real.
        again, _ = await log.submit(session.id, [{"type": "text", "text": "three"}], "s3")
        assert again.user_text == "three"
        await log.wait_idle(session.id)
        assert _turn_row(log, session.id, again.id).state is TurnState.COMPLETED

    asyncio.run(scenario())


def test_concurrent_submits_for_one_slot_admit_exactly_one(tmp_path):
    """Real concurrency, one free slot, six submits: one winner, five refusals.

    Nothing is stubbed — this is the case the deleted roll-forward path
    claimed to handle. Every loser must be refused by the capacity claim, so it
    announces nothing (`turn.queued`), fails nothing (`turn.failed`) and leaves
    no turn row: the `queued`-forever zombie the old code patched up afterwards
    cannot exist. If the pre-check ever stops being authoritative (an `emit`
    that yields before the slot is taken), one of these assertions breaks.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=1)
        session = log.create_session()

        first, _ = await log.submit(session.id, [{"type": "text", "text": "base"}], "base")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == first.id)
        assert log.queue_state(session.id)["pending"] == []  # the slot is free

        racers = await asyncio.gather(*[
            log.submit(session.id, [{"type": "text", "text": f"race-{i}"}], f"r{i}")
            for i in range(6)
        ], return_exceptions=True)
        winners = [result for result in racers if not isinstance(result, BaseException)]
        refusals = [result for result in racers if isinstance(result, BaseException)]
        assert len(winners) == 1, f"one slot admitted {len(winners)} turns"
        assert len(refusals) == 5
        for refusal in refusals:
            assert isinstance(refusal, VadeError)
            assert refusal.code == "OVERLOADED"
            assert refusal.data == {"reason": "queue_full"}

        winner, _pos = winners[0]
        events = _events(log, session.id)
        announced = {
            e.payload.get("client_msg_id") for e in events if e.type == "turn.queued"
        }
        assert announced == {"base", winner.client_msg_id}
        assert [e.type for e in events].count("turn.failed") == 0
        for index in range(6):
            key = f"r{index}"
            if key == winner.client_msg_id:
                assert log.store.find_turn_by_client_msg(session.id, key) is not None
            else:
                assert log.store.find_turn_by_client_msg(session.id, key) is None

        # Capacity is exactly full: one pending turn, and no claim was leaked
        # (a lost claim would silently shrink `max_pending` for good).
        queue = log._queues[session.id]
        assert queue._claims == {}
        assert not queue.has_capacity()
        assert len(log.queue_state(session.id)["pending"]) == 1

        executor.gate.set()
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_a_yielding_announcement_cannot_lose_the_claimed_slot(tmp_path):
    """The old race, reproduced on purpose: `emit` suspends mid-announcement.

    This is the window the deleted roll-forward path patched up. A hook makes
    the announcement wait (no `enqueue` stub, no monkeypatched capacity), so a
    concurrent submit runs exactly where the race used to be — and must be
    refused, because the slot was already claimed. Without the claim it takes
    the slot, and the resumed submit hits a full queue *after* `turn.queued`:
    the `queued`-forever zombie.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=1)
        session = log.create_session()
        real_emit = log.emit
        paused = asyncio.Event()   # the announcement reached its suspension
        release = asyncio.Event()  # ... and may finish

        async def slow_emit(session_id, event_type, payload=None, **kwargs):
            if event_type == "turn.queued":
                paused.set()
                await release.wait()
            return await real_emit(session_id, event_type, payload, **kwargs)

        log.emit = slow_emit
        slow = asyncio.create_task(
            log.submit(session.id, [{"type": "text", "text": "slow"}], "slow")
        )
        assert await wait_until(paused.is_set)  # it has claimed and announced

        with pytest.raises(VadeError) as caught:
            await log.submit(session.id, [{"type": "text", "text": "fast"}], "fast")
        assert caught.value.code == "OVERLOADED"
        assert caught.value.data == {"reason": "queue_full"}

        release.set()
        turn, pos = await slow
        assert pos == 1
        events = _events(log, session.id)
        assert [e.type for e in events].count("turn.failed") == 0
        assert log.store.find_turn_by_client_msg(session.id, "fast") is None
        assert log.store.find_turn_by_client_msg(session.id, "slow").id == turn.id

        executor.gate.set()
        await log.wait_idle(session.id)
        assert _turn_row(log, session.id, turn.id).state is TurnState.COMPLETED

    asyncio.run(scenario())


def test_a_failed_announcement_returns_the_claimed_slot(tmp_path):
    """The claim is released when `turn.queued` never lands.

    Losing the claim on an exception would shrink `max_pending` for the life of
    the queue — the same class of permanent damage as the `queued`-forever turn,
    just harder to see.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=1)
        session = log.create_session()
        real_emit = log.emit
        state = {"failures": 1}

        async def flaky_emit(session_id, event_type, payload=None, **kwargs):
            if event_type == "turn.queued" and state["failures"]:
                state["failures"] -= 1
                raise VadeError("INTERNAL", "store is down")
            return await real_emit(session_id, event_type, payload, **kwargs)

        log.emit = flaky_emit
        with pytest.raises(VadeError):
            await log.submit(session.id, [{"type": "text", "text": "lost"}], "f1")

        queue = log._queues[session.id]
        assert queue._claims == {}
        assert queue.has_capacity()  # the slot came back
        assert log.store.find_turn_by_client_msg(session.id, "f1") is None

        log.emit = real_emit
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "kept"}], "f2")
        executor.gate.set()
        await log.wait_idle(session.id)
        assert _turn_row(log, session.id, turn.id).state is TurnState.COMPLETED

    asyncio.run(scenario())


def test_regenerate_overloaded_writes_nothing(tmp_path):
    """Capacity is claimed before `turn.superseded`: a refusal supersedes nothing.

    The slot is taken here by an outstanding claim (a submit mid-announcement),
    not by a stubbed `enqueue` — that is the only way a full queue can coexist
    with a terminal tail turn, which `regenerate` requires.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=1)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        executor.gate.set()
        await log.wait_idle(session.id)

        queue = log._queues[session.id]
        claim = queue.claim()
        assert claim is not None
        with pytest.raises(VadeError) as caught:
            await log.regenerate(session.id, first.id, "r1")
        assert caught.value.code == "OVERLOADED"
        assert caught.value.data == {"reason": "queue_full"}

        # Roll-back in the only sense that matters: nothing was written, so the
        # old turn still points at nothing and no v2 exists.
        events = _events(log, session.id)
        assert "turn.superseded" not in [e.type for e in events]
        assert "r1" not in [e.payload.get("client_msg_id") for e in events]
        assert _turn_row(log, session.id, first.id).superseded_by is None
        assert log.store.find_turn_by_client_msg(session.id, "r1") is None

        # Same key, once the slot is free: a plain regenerate, no corpse to
        # refuse and no special retry contract.
        assert queue.release(claim) is True
        result = await log.regenerate(session.id, first.id, "r1")
        assert result["version"] == 2
        assert result["supersedes_turn_id"] == first.id
        assert _turn_row(log, session.id, first.id).superseded_by == result["turn_id"]

        executor.gate.set()
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_edit_resubmit_overloaded_writes_nothing(tmp_path):
    """Capacity is claimed before `message.edited`: a refusal edits nothing."""

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=1)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        executor.gate.set()
        await log.wait_idle(session.id)
        assert first.user_message_id is not None

        queue = log._queues[session.id]
        claim = queue.claim()
        assert claim is not None
        with pytest.raises(VadeError) as caught:
            await log.edit_resubmit(
                session.id, first.user_message_id, [{"type": "text", "text": "edited"}], "e1",
            )
        assert caught.value.code == "OVERLOADED"
        assert caught.value.data == {"reason": "queue_full"}

        events = _events(log, session.id)
        assert "message.edited" not in [e.type for e in events]
        assert "e1" not in [e.payload.get("client_msg_id") for e in events]
        reloaded = log.store.load(session.id)
        assert reloaded is not None
        message = next(m for m in reloaded.messages if m.id == first.user_message_id)
        assert message.superseded_by is None
        assert _turn_row(log, session.id, first.id).superseded_by is None

        assert queue.release(claim) is True
        result = await log.edit_resubmit(
            session.id, first.user_message_id, [{"type": "text", "text": "edited"}], "e1",
        )
        assert result["supersedes_turn_id"] == first.id
        assert _turn_row(log, session.id, first.id).superseded_by == result["turn_id"]

        executor.gate.set()
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_overloaded_after_a_started_turn_is_still_an_idempotent_result(tmp_path):
    """`OVERLOADED` from a turn that ran is an outcome, not a refusal.

    Model-resolution failure carries `error_code="OVERLOADED"` too: the request
    was dequeued (`started_at` set), so the idempotent replay returns that turn
    instead of retrying. Only a capacity refusal — which now happens before any
    row exists — is a different answer.
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
        assert row.started_at is not None  # dequeued; not a capacity refusal

        # Replay of the same request returns the original failed turn: the
        # failure is that attempt's outcome, not a silently dropped message.
        replay, pos = await log.submit(session.id, [{"type": "text", "text": "hi"}], "s1")
        assert replay.id == turn.id
        assert pos == 0

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


def test_close_terminates_turns_then_runs_hooks_dialogs_then_detach(tmp_path):
    """Order: turn terminals → `session.closed` hooks → dialog sweep → detach.

    The queue used to be shut down LAST, so every client received
    `session.closed` while its turn was still `running`: no terminal `turn.*`
    ever followed and the device waited forever.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=4)
        session = log.create_session()
        order: list[str] = []
        attached_during_hook: list[int] = []
        states_during_hook: list[str] = []
        seen: list[str] = []

        def sync_hook(session_id: str) -> None:
            order.append("hook:sync")
            seen.append(session_id)
            # The end beat reads the store and emits onto the seq bus: the sink
            # must still be there, so hooks run before `detach`.
            attached_during_hook.append(log.attached_count(session_id))
            # And the turn must already be terminal, or the beat describes a
            # session whose turn can never end.
            row = log.store.load(session_id)
            states_during_hook.extend(t.state.value for t in (row.turns if row else ()))

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
        assert order == ["shutdown", "hook:sync", "hook:async", "dialog"]
        assert seen == [session.id, session.id]
        assert attached_during_hook == [1]
        assert input_port.sessions == [session.id]
        assert log.attached_count(session.id) == 0
        assert queue._worker is None and queue._stopped is True
        assert closes == [1]
        # The turn was already terminal when the hooks ran: `session.closed`
        # describes a session whose turn ended, not one still running.
        assert states_during_hook == [TurnState.INTERRUPTED.value]

        # A second close (a transport teardown racing app shutdown) must not
        # re-publish the beat or touch the already-closed store.
        await log.close()
        assert order == ["shutdown", "hook:sync", "hook:async", "dialog"]
        assert closes == [1]

    asyncio.run(scenario())


def test_close_does_not_respawn_the_worker_for_a_pending_turn(tmp_path):
    """`_run_loop`'s `finally` used to start a new worker right after cancel.

    The queued turn is no longer left behind either: `close()` terminates it
    (`turn.interrupted{reason: shutdown}`) instead of dropping it silently.
    """

    async def scenario():
        executor = GateExecutor()
        log = _log(tmp_path, executor, max_pending=2)
        session = log.create_session()
        sink = CollectSink()
        await log.attach(session.id, None, sink)
        first, _ = await log.submit(session.id, [{"type": "text", "text": "one"}], "s1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == first.id)
        second, _ = await log.submit(session.id, [{"type": "text", "text": "two"}], "s2")
        queue = log._queues[session.id]
        assert queue._worker is not None

        await log.close()
        await asyncio.sleep(0.05)  # a resurrected worker would have started by now

        assert queue._stopped is True
        assert queue._worker is None
        # The store is closed now, so assert through the events the attached
        # sink saw: both turns got exactly one terminal write, and the queued
        # one is gone from the queue instead of being re-dequeued forever.
        interrupts = [
            event for event in sink.events
            if event["type"] == "turn.interrupted"
            and event["payload"].get("data", {}).get("reason") == "shutdown"
        ]
        assert sorted(event["payload"]["turn_id"] for event in interrupts) == sorted(
            [first.id, second.id]
        )
        assert queue._pending == deque()
        assert queue.state_of(second.id) is None
        assert {first.id, second.id} <= set(queue._finalized)
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
