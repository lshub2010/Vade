"""RFC-0007 A5/A6/A7: progress throttle, attach gate and replay coverage."""

from __future__ import annotations

import asyncio
import time

from vade.session.replay import ReplayBuffer
from vade.tasks.registry import TaskLimits, TaskRegistry

from ..session.helpers import CollectSink
from .helpers import attach, events, make_log, make_registry, payloads, settle


class _ManualTime:
    """The registry's whole time dependency, driven by the test.

    The throttle measures its coalescing window on `clock` and its trailing-edge
    timer waits on `sleep`. Owning both turns "inside the window" into a virtual
    interval, instead of a bet on how fast a real 500 ms timer plus the sink
    queue happen to land on a loaded machine.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self._sleepers: list[tuple[float, asyncio.Future]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, future))
        await future

    async def advance(self, seconds: float, *, turns: int = 64) -> None:
        """Move virtual time forward, wake due timers, then pump the loop.

        The leading pump lets timers created by the last call register their
        sleep at the *current* virtual instant. `turns` is a fixed number of
        event-loop iterations, never a wall-clock deadline: it only lets a
        woken timer reach the sink writer.
        """
        for _ in range(turns):
            await asyncio.sleep(0)
        self.now += seconds
        due = [item for item in self._sleepers if item[0] <= self.now]
        self._sleepers = [item for item in self._sleepers if item[0] > self.now]
        for _, future in due:
            if not future.done():
                future.set_result(None)
        for _ in range(turns):
            await asyncio.sleep(0)


def make_clocked_registry(log, **limits):
    """`make_registry`, on a manual time source instead of the wall clock."""
    time_source = _ManualTime()
    registry = TaskRegistry(
        log,
        limits=TaskLimits(**limits),
        clock=time_source.clock,
        sleep=time_source.sleep,
    )
    log.tasks = registry
    return registry, time_source


def pcts(sink: CollectSink) -> list[int]:
    return [int(payload["pct"]) for payload in payloads(sink, "task.progress")]


def test_three_rapid_updates_become_at_most_two_events():
    """§5.4: 2/s per task, coalesced inside the window, last value wins."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry, clock = make_clocked_registry(log, progress_hz=2)  # 500 ms window
        task = await registry.spawn(kind="job", name="index", session_id=session.id)

        assert await registry.progress(task.task_id, 1) is True
        assert await registry.progress(task.task_id, 2) is False  # coalesced
        assert await registry.progress(task.task_id, 3) is False  # coalesced
        await settle()
        first = payloads(sink, "task.progress")
        assert len(first) == 1 and first[0]["pct"] == 1

        # Still inside the window: the trailing edge must not leak early.
        await clock.advance(0.4)
        assert pcts(sink) == [1]

        # Cross the trailing edge: exactly one more event, carrying the last value.
        await clock.advance(0.1)
        after = payloads(sink, "task.progress")
        assert len(after) == 2, f"3 rapid updates must coalesce to <=2, got {len(after)}"
        assert after[-1]["pct"] == 3
        assert [p["pct"] for p in after] == sorted(p["pct"] for p in after)

        # The marker the client and the replay gate both key on.
        frames = events(sink, "task.progress")
        assert frames and frames[0]["x-vade"] == {"coalesce": True}

    asyncio.run(scenario())


def test_window_reopens_after_the_trailing_edge_and_idle_time_adds_nothing():
    """The window is a rate (2/s), not a one-shot: it reopens on the clock."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry, clock = make_clocked_registry(log, progress_hz=2)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)

        assert await registry.progress(task.task_id, 10) is True
        assert await registry.progress(task.task_id, 11) is False
        await clock.advance(0.5)  # trailing edge of the first window
        assert pcts(sink) == [10, 11]

        # The flush re-arms the window, so the next pair coalesces and lands
        # exactly one interval later — a rate, never a burst.
        assert await registry.progress(task.task_id, 12) is False
        assert await registry.progress(task.task_id, 13) is False
        await clock.advance(0.4)
        assert pcts(sink) == [10, 11]
        await clock.advance(0.1)
        assert pcts(sink) == [10, 11, 13], "the newest value of the window wins"

        await clock.advance(10.0)  # idle time with nothing pending: no phantom event
        assert pcts(sink) == [10, 11, 13]

    asyncio.run(scenario())


def test_sustained_rate_never_exceeds_two_per_second():
    """A task reporting ~100/s for a while still yields a handful of events."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log, progress_hz=2, event_burst=1000)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)

        started = time.monotonic()
        pct = 0
        while time.monotonic() - started < 0.6:
            pct += 1
            await registry.progress(task.task_id, min(100, pct))
            await asyncio.sleep(0.01)  # ~100 reports/s
        await asyncio.sleep(0.6)  # let the trailing flush land

        seen = payloads(sink, "task.progress")
        elapsed = time.monotonic() - started
        assert len(seen) <= 2 * (elapsed + 1), f"{len(seen)} progress events in {elapsed:.2f}s"
        assert [p["pct"] for p in seen] == sorted(p["pct"] for p in seen), "pct must be monotonic"

    asyncio.run(scenario())


def test_no_attach_means_no_progress_event_but_the_record_moves():
    """A6: the task keeps running, the record keeps the value, the wire is quiet."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, progress_hz=20)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)

        assert await registry.progress(task.task_id, 42) is False
        await settle()
        assert registry.get(task.task_id).pct == 42

        sink = await attach(log, session.id)
        view = log.snapshot(session.id)["tasks"][0]
        assert view["task_id"] == task.task_id
        assert view["pct"] == 42
        assert payloads(sink, "task.progress") == []

    asyncio.run(scenario())


def test_burst_budget_drops_progress_but_never_state():
    """§5.4: the session tap may only shed `task.progress`."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log, progress_hz=20, event_burst=3)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)

        # Three structural events fill the one-second bucket by themselves.
        await registry.set_state(task.task_id, "running")
        await registry.set_state(task.task_id, "waiting_approval")
        await registry.set_state(task.task_id, "running")
        emitted = 0
        for pct in range(5):
            emitted += int(await registry.progress(task.task_id, pct))
        await registry.finish(task.task_id, "completed", summary="done")
        await settle()

        assert emitted == 0, "progress must be the class that is dropped"
        assert registry.dropped_progress >= 1
        assert len(payloads(sink, "task.finished")) == 1, "state events are never dropped"
        assert states_of(sink) == ["running", "waiting_approval", "running"]

    def states_of(sink):
        return [p["state"] for p in payloads(sink, "task.status")]

    asyncio.run(scenario())


def test_replay_coverage_skips_coalesced_progress():
    """A7: 1000 progress rows still attach via replay, not via a snapshot."""

    async def scenario():
        log = make_log(replay=ReplayBuffer(maxlen=1000))
        session = log.create_session()
        registry = make_registry(log)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)
        for i in range(1000):
            await log.emit(
                session.id, "task.progress",
                {"task_id": task.task_id, "pct": i % 101, "elapsed_ms": i},
                coalesce=True,
            )

        sink = CollectSink()
        result = await log.attach(session.id, 0, sink)
        assert result.snapshot is None, "coalesced rows must not force a snapshot"
        assert len(result.replay) == 1001, "and read_since still returns every row"
        await settle()

        # Control: the same volume without the marker trips the 1000-event window.
        other = make_log(replay=ReplayBuffer(maxlen=1000))
        other_session = other.create_session()
        for i in range(1001):
            await other.emit(other_session.id, "turn.delta", {"turn_id": "t", "text": str(i)})
        control = await other.attach(other_session.id, 0, CollectSink())
        assert control.snapshot is not None, "a real hole must still fall back to snapshot"

    asyncio.run(scenario())
