"""Review P0-2 + P2-3: the close path is real, wired and restart-safe.

Three separate holes, one file each so a regression names its own bug:

* `App.shutdown()` never closed the session layer, so `session.closed` was
  never published on a normal stop and `sessions.db` stayed open until process
  exit (a restart relied on the kernel, not on the code).
* `session/install()` never called `set_session_end_hooks`, so even a
  `SessionLog.close()` that did run traversed an empty hook list.
* `reconcile_restarts()` had no `SQLiteStore.reconcile_unfinished_turns` to
  call: rows a dead process left in `running`/`waiting_*` stayed non-terminal
  forever, advertising a turn that runs for the life of the next process.

Review P2-3 adds the fourth: a graceful `App.shutdown()` only *cancelled* the
queue, so the turn row stayed `running`/`queued` while `session.closed` was
published — the client waited forever for a terminal `turn.*`. The shutdown now
writes `turn.interrupted{by: system, data.reason: shutdown}` for every live turn
BEFORE the closing beat, and the restart below proves it is not repaired twice.

The tests drive the **real** assembly (`App.startup()` + `install_components()`)
rather than hand-built objects: a fixture that injects a fake session would
pass with every one of the bugs above still present.
"""

from __future__ import annotations

import asyncio
from typing import Any

from vade.app import App, install_components
from vade.config import load_config
from vade.session.models import Session, Turn, TurnState, now_ms
from vade.session.sqlite_store import SQLiteStore

from ..session.helpers import CollectSink, GateExecutor, wait_until


def _config(tmp_path: Any) -> Any:
    # `llm.provider = mock` keeps the assembly hermetic; the turn that matters
    # here is blocked on a gate, never on a provider.
    return load_config(cli={"core.data_dir": str(tmp_path), "llm.provider": "mock"})


async def _boot(tmp_path: Any) -> App:
    app = App(_config(tmp_path))
    await app.startup()
    install_components(app, strict=False)
    return app


def _closed_beats(app: App, session_id: str) -> int:
    """`session.closed` entries the evolve bus journalled for one session."""
    journal = app.event_bus._journal.get(session_id) or ()
    return sum(1 for entry in journal if entry["topic"] == "session.closed")


def _closed_entry(app: App, session_id: str) -> dict[str, Any] | None:
    journal = app.event_bus._journal.get(session_id) or ()
    for entry in journal:
        if entry["topic"] == "session.closed":
            return dict(entry)
    return None


def test_shutdown_closes_the_session_layer_and_publishes_the_end_beat_once(tmp_path) -> None:
    async def scenario() -> None:
        app = await _boot(tmp_path)
        session = app.session.create_session()
        await app.session.attach(session.id, None, CollectSink())

        ended: list[str] = []
        app.on_session_end.append(ended.append)
        assert _closed_beats(app, session.id) == 0

        await app.shutdown()

        # The hook list `session/install()` stored by reference now runs: this
        # recorder appended AFTER install and still saw the beat.
        assert ended == [session.id]
        # And the real C3d beat was published exactly once, through the bus.
        assert _closed_beats(app, session.id) == 1
        assert app.session.attached_count(session.id) == 0
        assert app.session._closed is True

        # A transport teardown racing `App.shutdown` must not publish twice or
        # touch the already-closed store.
        await app.session.close()
        assert ended == [session.id]
        assert _closed_beats(app, session.id) == 1

    asyncio.run(scenario())


def test_shutdown_terminates_live_turns_before_the_closing_beat(tmp_path) -> None:
    """Review P2-3: a live turn used to be cancelled without a terminal row.

    `App.shutdown()` only cancelled the queue, so a turn that was `running` (or
    still `queued` behind it) kept its non-terminal row while `session.closed`
    went out — the client had no terminal `turn.*` to wait for and hung
    forever. The terminal event must exist, be exactly one, and take its seq
    BEFORE the closing beat's `seq_to`.
    """

    async def scenario() -> None:
        app = await _boot(tmp_path)
        session = app.session.create_session()
        sink = CollectSink()
        await app.session.attach(session.id, None, sink)
        gate = GateExecutor()
        app.session.set_executor(gate)
        turn, _ = await app.session.submit(
            session.id, [{"type": "text", "text": "long job"}], "c1"
        )
        assert await wait_until(lambda: app.session.queue_state(session.id)["running"] == turn.id)
        # A second turn queued behind it: shutdown must terminate that too, or
        # the device waits on a row that can never leave `queued`.
        queued, _ = await app.session.submit(
            session.id, [{"type": "text", "text": "next"}], "c2"
        )
        assert app.session.queue_state(session.id)["pending"][0]["turn_id"] == queued.id

        await app.shutdown()
        await asyncio.sleep(0.05)  # let the sink writer drain its queue

        store = SQLiteStore(tmp_path / "sessions.db")
        try:
            stored = store.read_since(session.id, 0)
            interrupts = [event for event in stored if event.type == "turn.interrupted"]
            assert [event.payload["turn_id"] for event in interrupts] == [turn.id, queued.id]
            for event in interrupts:
                payload = dict(event.payload)
                assert payload["by"] == "system"       # nobody pressed stop
                assert payload["state"] == "interrupted"
                assert payload["data"] == {"reason": "shutdown"}
            # Seq is the same counter the client replays: contiguous, no gap and
            # no second write behind the terminal ones.
            assert [event.seq for event in stored] == list(range(1, len(stored) + 1))

            for turn_id in (turn.id, queued.id):
                row = store.find_turn(session.id, turn_id)
                assert row is not None
                assert row.state is TurnState.INTERRUPTED
                assert row.ended_at is not None
                assert row.stopped_reason == "shutdown"
            interrupted_seq = max(event.seq for event in interrupts)
        finally:
            store.close()

        # The wire told the same story: the terminal frame reached the attached
        # device, and it is the last `turn.*` for that turn.
        assert "turn.interrupted" in sink.types()
        closed = _closed_entry(app, session.id)
        assert closed is not None
        # `session.closed` reports `seq_to = last_seq()`, so a covered terminal
        # event is exactly what proves the beat came after it.
        assert closed["seq_to"] >= interrupted_seq

        # Idempotent: the transport teardown racing `App.shutdown` — and a
        # second queue shutdown — must not write a second terminal event.
        await app.session.close()
        await app.session._queues[session.id].shutdown()
        again = SQLiteStore(tmp_path / "sessions.db")
        try:
            events = again.read_since(session.id, 0)
            assert [e.type for e in events].count("turn.interrupted") == 2
            seq_after = again.last_seq(session.id)
        finally:
            again.close()

        # A restart repairs nothing (both rows are terminal) and writes nothing:
        # the "interrupted by shutdown" story survives the process.
        restarted = await _boot(tmp_path)
        try:
            for turn_id in (turn.id, queued.id):
                row = restarted.session_store.find_turn(session.id, turn_id)
                assert row is not None and row.state is TurnState.INTERRUPTED
                assert row.stopped_reason == "shutdown"
            assert restarted.session_store.last_seq(session.id) == seq_after
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


def test_reconcile_repairs_every_non_terminal_state_and_is_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "reconcile.db")
    try:
        store.create_session(Session(id="s1", created_at=now_ms()))
        turns = (
            Turn(id="t-queued", session_id="s1", state=TurnState.QUEUED),
            Turn(id="t-running", session_id="s1", state=TurnState.RUNNING),
            Turn(id="t-waiting", session_id="s1", state=TurnState.WAITING_INPUT),
            Turn(id="t-done", session_id="s1", state=TurnState.COMPLETED, ended_at=1),
        )
        store.append("s1", (), (), turns=turns)
        seq_before = store.last_seq("s1")

        assert store.reconcile_unfinished_turns(reason="restart") == 3

        for turn_id in ("t-queued", "t-running", "t-waiting"):
            row = store.find_turn("s1", turn_id)
            assert row is not None and row.state is TurnState.INTERRUPTED
            assert row.stopped_reason == "restart"
        untouched = store.find_turn("s1", "t-done")
        assert untouched is not None
        assert untouched.state is TurnState.COMPLETED
        assert untouched.ended_at == 1  # a real ending is never rewritten

        events = store.read_since("s1", seq_before)
        assert [e.type for e in events] == ["turn.interrupted"] * 3
        assert [e.seq for e in events] == [seq_before + 1, seq_before + 2, seq_before + 3]

        # Dev hot reload installs twice; the second pass must write nothing.
        assert store.reconcile_unfinished_turns(reason="restart") == 0
        assert store.last_seq("s1") == seq_before + 3
    finally:
        store.close()
