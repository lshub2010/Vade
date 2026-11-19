"""`ui/attach`: a replay that the sink queue accepted must stay attached.

The replay is offered into the SAME bounded queue live events use, so the room
left behind for `session.attached` — and for the `turn.completed` +
`queue.updated` pair a finalizing turn emits back to back — is a hard
precondition, not a nicety. Three things were wrong (P1-1):

  ① `ATTACH_REPLAY_RESERVE` was 2, one short of that pair: a backlog of
     `cap - 2` was accepted and the attach's own frames then overflowed the
     queue. `SinkWorker.drop()` also UNREGISTERS itself, so `ui/attach`
     returned success, `snapshot: null`, and the client received nothing at
     all — no error, no retry signal, no events for the life of the session;
  ② the reservation was never actually free: the writer task had not been
     scheduled yet, so the whole replay still sat in the queue when the
     announcement was offered — the announcement itself was the frame that
     overflowed;
  ③ nothing looked at `worker.dropped` afterwards, so a sink that died during
     the attach was still reported as attached.

Every test here is a real `SessionLog` + real `SinkWorker` + real SQLite store;
the numbers are the report's (`batch-results/r1-core-session.md`): `sink_queue=8`
with a backlog of `cap - 2`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from vade.session.manager import ATTACH_REPLAY_RESERVE, SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore
from .helpers import CollectSink, wait_until


class DyingSink:
    """A connection whose first send fails: a socket closed mid-attach."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.failures = 1

    async def send(self, event: dict[str, Any]) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionResetError("connection reset by peer")
        self.frames.append(dict(event))

    def types(self) -> list[str]:
        return [str(frame["type"]) for frame in self.frames]


def _log(tmp_path, *, sink_queue: int) -> SessionLog:
    return SessionLog(
        SQLiteStore(tmp_path / "sessions.db"),
        replay=ReplayBuffer(maxlen=500),
        sink_queue=sink_queue,
    )


def test_reserve_covers_the_finalize_pair():
    """The constant is the contract: one announcement + one terminal pair + slack."""
    assert ATTACH_REPLAY_RESERVE >= 8


def test_a_backlog_of_cap_minus_two_is_answered_with_a_snapshot(tmp_path):
    """The report's repro: sink_queue=8, 6 events (= cap-2), attach(last_seq=0).

    Old shape: `6 <= 8 - 2` accepted the replay, `session.attached` (offered a
    tick later, with the replay still queued) overflowed, `drop()` unregistered
    the sink → `attached_count == 0`, the sink received `[]`, and the client was
    told it was attached.
    """

    async def scenario():
        log = _log(tmp_path, sink_queue=8)
        session = log.create_session()
        # A backlog of cap - 2: exactly the size the old reserve arithmetic
        # declared safe.
        for index in range(6):
            await log.emit(session.id, "queue.updated", {"n": index})

        sink = CollectSink()
        result = await log.attach(session.id, 0, sink)

        assert result.snapshot is not None, "a backlog that cannot fit must not be replayed"
        assert result.replay == ()
        assert log.attached_count(session.id) == 1, "the sink must be registered, not dropped"

        assert await wait_until(lambda: len(sink.events) == 1)
        attached = sink.events[0]
        assert attached["type"] == "session.attached"
        assert attached["payload"]["snapshot"] is True

        # And the attachment is real, not just a count: the pair a finalizing
        # turn emits back to back must arrive.
        await log.emit(session.id, "turn.completed",
                       {"turn_id": "turn-1", "state": "completed"})
        await log.emit(session.id, "queue.updated", {"running": None})
        assert await wait_until(lambda: len(sink.events) == 3, 1.0)
        assert [frame["type"] for frame in sink.events] == [
            "session.attached", "turn.completed", "queue.updated",
        ]
        assert log.attached_count(session.id) == 1
        await log.close()

    asyncio.run(scenario())


def test_a_full_reservation_leaves_room_for_live_frames(tmp_path):
    """The replay still fits `cap - reserve`, and the reservation is really free.

    `sink_queue=16`, backlog `16 - 8 = 8`: the replay is served (snapshot: null),
    and eight live frames (a terminal pair plus traffic) must not drop the sink.
    On the old shape the writer had not been scheduled when the announcement
    was offered, so the queue was one frame fuller than the arithmetic assumed
    and the burst overflowed it.
    """

    async def scenario():
        log = _log(tmp_path, sink_queue=16)
        session = log.create_session()
        for index in range(8):
            await log.emit(session.id, "queue.updated", {"n": index})

        sink = CollectSink()
        result = await log.attach(session.id, 0, sink)

        assert result.snapshot is None, "a backlog that fits must be replayed, not snapshotted"
        assert len(result.replay) == 8
        worker = next(iter(log._sinks[session.id].values()))

        for index in range(8):
            await log.emit(session.id, "queue.updated", {"burst": index})

        # 8 replayed + session.attached + 8 live.
        assert await wait_until(lambda: len(sink.events) == 17, 1.0), len(sink.events)
        assert [frame["seq"] for frame in sink.events] == list(range(1, 18))
        assert sink.events[8]["type"] == "session.attached"
        assert sink.events[8]["payload"]["snapshot"] is False
        assert worker.dropped is False
        assert log.attached_count(session.id) == 1
        await log.close()

    asyncio.run(scenario())


def test_a_sink_that_dies_mid_replay_is_answered_with_a_snapshot(tmp_path):
    """A sink lost during the replay must not be reported as attached.

    This is the "silent detach" that has no client-side cure: `drop()`
    unregisters the worker, so nothing is registered and no event will ever
    arrive. The fix answers with a snapshot on a fresh worker instead.
    """

    async def scenario():
        log = _log(tmp_path, sink_queue=16)
        session = log.create_session()
        for index in range(4):
            await log.emit(session.id, "queue.updated", {"n": index})

        sink = DyingSink()
        result = await log.attach(session.id, 0, sink)

        assert result.snapshot is not None, "a dead sink cannot be handed a replay"
        assert result.replay == ()
        assert log.attached_count(session.id) == 1

        assert await wait_until(lambda: sink.types().count("session.attached") == 1, 1.0)
        attached = next(frame for frame in sink.frames if frame["type"] == "session.attached")
        assert attached["payload"]["snapshot"] is True
        assert log.attached_count(session.id) == 1
        await log.close()

    asyncio.run(scenario())
