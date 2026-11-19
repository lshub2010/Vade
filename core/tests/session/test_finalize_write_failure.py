"""A failed terminal write must not leave a turn `running` forever (P1-2).

One line caused three failures: `_finalize` recorded the `_finalized` tombstone
in a `finally`, whether or not the terminal row and event actually landed.

  ① the write failed → the marker said "done" → the worker's wake-up took the
     `if turn.id in self._finalized: return` branch and never retried: the row
     stayed `running` and the client never got a terminal `turn.*`;
  ② `self._running = None` lived after the emit, so it survived the exception:
     `state()` kept reporting a live turn and `session/delete` answered
     `turn_active` for the rest of the process's life;
  ③ the exception escaped `_run_loop`, which had no `except`: the worker task
     died with nobody holding it ("Task exception was never retrieved") and its
     `finally` immediately started another worker for the same session — a
     second `running` turn row behind the one that never ended.

The fault is injected into the REAL store: `FailingTerminalWrite` raises a real
`sqlite3.OperationalError` for the first `append` that carries a terminal turn
and delegates every other call. The queue, the sinks, the SQLite schema, the
seq allocator and the asyncio scheduling are all the production ones — the
injection is one failed transaction, which is exactly what the store promises
when `append` raises.
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import time
from typing import Any, Sequence

from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog, TERMINAL_EVENT_TYPES
from vade.session.models import TurnState
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore
from .helpers import GateExecutor, wait_until


class FailingTerminalWrite:
    """Fails the next `failures` terminal-carrying `append`s, then heals.

    Everything else goes straight to the real `SQLiteStore.append`: the point is
    a write that raised (disk full / SQLITE_BUSY / a concurrent delete), not a
    fake store. `append` is atomic, so a failed call wrote nothing.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.failures = 0
        self.attempts = 0
        self.rejected = 0
        self._real = store.append
        self._sequenced = store.append_sequenced

    def _admit(self, events: Sequence[Any]) -> None:
        if any(event.type in TERMINAL_EVENT_TYPES for event in events):
            self.attempts += 1
            if self.failures:
                self.failures -= 1
                self.rejected += 1
                raise sqlite3.OperationalError("disk I/O error (injected)")

    def append(self, session_id: str, msgs: Sequence[Any], events: Sequence[Any],
               **kwargs: Any) -> None:
        self._admit(events)
        self._real(session_id, msgs, events, **kwargs)

    def append_sequenced(self, session_id: str, msgs: Sequence[Any],
                         events: Sequence[Any], **kwargs: Any) -> list[Any]:
        """The write path uses this one (`next_seq` folded into the batch)."""
        self._admit(events)
        return self._sequenced(session_id, msgs, events, **kwargs)


class LoopErrors:
    """Everything the event loop reports, including "never retrieved" tasks."""

    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []
        self._loop = asyncio.get_running_loop()
        self._previous = self._loop.get_exception_handler()
        self._loop.set_exception_handler(self._collect)

    def _collect(self, _loop: Any, context: dict[str, Any]) -> None:
        self.contexts.append(dict(context))

    def close(self) -> None:
        self._loop.set_exception_handler(self._previous)

    def unretrieved(self) -> list[dict[str, Any]]:
        """Unhandled exceptions — a cancelled task is not one of them."""
        return [
            context for context in self.contexts
            if not isinstance(context.get("exception"), asyncio.CancelledError)
        ]


class YieldingExecutor:
    """Completes after a few loop ticks, so a second worker would overlap.

    A gate would serialize the turns by itself and hide exactly the thing under
    test ("never two workers on one session"): the executor must be able to
    interleave for an overlap to be observable.
    """

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self.seen: list[str] = []

    async def __call__(self, turn: Any, content: Any) -> Any:
        self.current += 1
        self.peak = max(self.peak, self.current)
        self.seen.append(turn.id)
        try:
            for _ in range(3):
                await asyncio.sleep(0)
        finally:
            self.current -= 1
        return TurnOutcome(assistant_text=f"echo:{turn.user_text}", stopped_reason="stop")


def _build(tmp_path, executor) -> tuple[SessionLog, FailingTerminalWrite]:
    store = SQLiteStore(tmp_path / "sessions.db")
    log = SessionLog(store, replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    injector = FailingTerminalWrite(store)
    store.append = injector.append  # type: ignore[method-assign]
    # The live write path goes through `append_sequenced` (seq allocation folded
    # into the batch); the injector has to sit on both entry points or the fault
    # would never fire.
    store.append_sequenced = injector.append_sequenced  # type: ignore[method-assign]
    return log, injector


def _terminals(log: SessionLog, session_id: str) -> list[Any]:
    return [event for event in log.store.read_since(session_id, 0)
            if event.type in TERMINAL_EVENT_TYPES]


def test_a_failed_terminal_write_is_retried_and_ends_the_turn(tmp_path):
    """The write fails once: the turn still ends, exactly once, as `failed`."""

    async def scenario():
        gate = GateExecutor()
        log, injector = _build(tmp_path, gate)
        errors = LoopErrors()
        session = log.create_session()
        queue = log._queue(session.id)

        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: queue.state()["running"] == turn.id), \
            "the worker never picked the turn up"

        # Arm the fault, then let the turn finish: its terminal write is the one
        # that fails.
        injector.failures = 1
        gate.gate.set()

        started = time.monotonic()
        await log.wait_idle(session.id, timeout=0.5)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"wait_idle took {elapsed:.2f}s (2x its own timeout)"
        await asyncio.sleep(0.05)
        gc.collect()

        # ① exactly one terminal event, and it is the fallback
        terminals = _terminals(log, session.id)
        assert [event.type for event in terminals] == ["turn.failed"], terminals
        assert terminals[0].payload["code"] == "INTERNAL"
        assert terminals[0].payload["turn_id"] == turn.id

        # ② the row says so too, so a client re-reads the same story
        row = next(t for t in log.store.load(session.id).turns if t.id == turn.id)
        assert row.state is TurnState.FAILED
        assert row.error_code == "INTERNAL"

        # ③ the queue let go of the turn and can take the next one
        assert queue._running is None
        assert queue.is_idle()
        assert queue._worker_live is False
        assert errors.unretrieved() == [], errors.contexts

        # The failed attempt was really retried, not skipped.
        assert injector.rejected == 1
        assert injector.attempts == 2

        errors.close()
        await log.close()

    asyncio.run(scenario())


def test_a_persistent_write_failure_leaves_no_live_turn_and_no_exception(tmp_path):
    """Both writes fail: no terminal state, but no stuck turn and no dead task.

    The honest residue is the `running` ROW (only `reconcile_unfinished_turns`
    on the next start can repair a database that cannot be written); what must
    not survive is the in-memory turn: `_running` set forever, `is_idle()` false
    forever, or a worker task whose exception nobody retrieved.
    """

    async def scenario():
        gate = GateExecutor()
        log, injector = _build(tmp_path, gate)
        errors = LoopErrors()
        session = log.create_session()
        queue = log._queue(session.id)

        first, _ = await log.submit(session.id, [{"type": "text", "text": "one"}], "c1")
        injector.failures = 2  # the terminal write AND the fallback fail
        gate.gate.set()

        started = time.monotonic()
        await log.wait_idle(session.id, timeout=0.5)
        assert time.monotonic() - started < 1.0
        await asyncio.sleep(0.05)
        gc.collect()

        assert _terminals(log, session.id) == []
        assert queue._running is None, "a stuck _running makes the session turn_active forever"
        assert queue.is_idle(), "the queue must accept work after a failed write"
        assert errors.unretrieved() == [], errors.contexts

        row = next(t for t in log.store.load(session.id).turns if t.id == first.id)
        assert row.state is TurnState.RUNNING, "the residue is the ROW, for reconcile to fix"

        # The queue is still a working queue: the next turn runs to completion.
        log.set_executor(_ImmediateExecutor())
        second, _ = await log.submit(session.id, [{"type": "text", "text": "two"}], "c2")
        await log.wait_idle(session.id, timeout=1.0)
        completed = [event for event in _terminals(log, session.id)
                     if event.payload.get("turn_id") == second.id]
        assert [event.type for event in completed] == ["turn.completed"]
        assert queue._running is None
        assert errors.unretrieved() == [], errors.contexts

        errors.close()
        await log.close()

    asyncio.run(scenario())


class _ImmediateExecutor:
    async def __call__(self, turn: Any, content: Any) -> Any:
        return TurnOutcome(assistant_text="ok", stopped_reason="stop")


def test_a_crashed_finalize_never_runs_two_turns_at_once(tmp_path):
    """Three turns, three failed terminal writes: one turn at a time, always.

    The old bug started a second worker from the dying loop's `finally` while
    the first turn was still `running` in the store, so a session could have two
    turns executing against the same rows. The probe executor overlaps only if
    two workers dequeue at the same time, which is the invariant under test.
    """

    async def scenario():
        probe = YieldingExecutor()
        log, injector = _build(tmp_path, probe)
        errors = LoopErrors()
        session = log.create_session()
        queue = log._queue(session.id)

        turns = []
        for index in range(3):
            injector.failures = 1
            turn, _ = await log.submit(
                session.id, [{"type": "text", "text": f"m{index}"}], f"c{index}",
            )
            turns.append(turn)
            await log.wait_idle(session.id, timeout=1.0)
            await asyncio.sleep(0.01)

        gc.collect()
        assert probe.peak == 1, f"{probe.peak} turns ran at once on one session"
        assert probe.seen == [turn.id for turn in turns], "FIFO order lost"
        assert queue._worker_live is False
        assert queue._running is None

        # Every turn ended exactly once, through the fallback: one bad attempt
        # each, one retry each.
        assert injector.rejected == 3
        assert injector.attempts == 6
        per_turn: dict[str, int] = {}
        for event in _terminals(log, session.id):
            per_turn[event.payload["turn_id"]] = per_turn.get(event.payload["turn_id"], 0) + 1
        assert per_turn == {turn.id: 1 for turn in turns}, per_turn
        assert errors.unretrieved() == [], errors.contexts

        states = [row.state for row in log.store.load(session.id).turns]
        assert states == [TurnState.FAILED] * 3, states

        errors.close()
        await log.close()

    asyncio.run(scenario())
