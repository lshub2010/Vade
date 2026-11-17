"""Review P0-2: the close path is real, wired and restart-safe.

Three separate holes, one file each so a regression names its own bug:

* `App.shutdown()` never closed the session layer, so `session.closed` was
  never published on a normal stop and `sessions.db` stayed open until process
  exit (a restart relied on the kernel, not on the code).
* `session/install()` never called `set_session_end_hooks`, so even a
  `SessionLog.close()` that did run traversed an empty hook list.
* `reconcile_restarts()` had no `SQLiteStore.reconcile_unfinished_turns` to
  call: rows a dead process left in `running`/`waiting_*` stayed non-terminal
  forever, advertising a turn that runs for the life of the next process.

The tests drive the **real** assembly (`App.startup()` + `install_components()`)
rather than hand-built objects: a fixture that injects a fake session would
pass with every one of the bugs above still present.
"""

from __future__ import annotations

import asyncio
from typing import Any

from vade.app import App, install_components
from vade.config import load_config
from vade.session.models import TERMINAL_STATES, Session, Turn, TurnState, now_ms
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


def test_a_turn_cut_by_shutdown_is_repaired_by_the_next_startup(tmp_path) -> None:
    """The real crash story: kill mid-turn, restart, the row is `interrupted`.

    A cancelled worker stops without writing a terminal row (`_execute_one`
    never resumes), which is exactly what a killed process leaves behind. The
    repair therefore has to happen on the NEXT startup — reconcile is the only
    thing that can see it.
    """

    async def scenario() -> None:
        app = await _boot(tmp_path)
        session = app.session.create_session()
        gate = GateExecutor()
        app.session.set_executor(gate)
        turn, _ = await app.session.submit(
            session.id, [{"type": "text", "text": "long job"}], "c1"
        )
        assert await wait_until(lambda: app.session.queue_state(session.id)["running"] == turn.id)

        await app.shutdown()

        # What the dead process left on disk: no terminal row, no terminal event.
        # (Asserted as "non-terminal", not "running": a future `close()` that
        # finalizes live turns itself is an improvement, and reconcile must
        # cover it either way.)
        before = SQLiteStore(tmp_path / "sessions.db")
        try:
            leftover = before.find_turn(session.id, turn.id)
            assert leftover is not None
            assert leftover.state not in TERMINAL_STATES
            assert "turn.interrupted" not in [e.type for e in before.read_since(session.id, 0)]
            seq_before = before.last_seq(session.id)
        finally:
            before.close()

        # Restart on the same data dir: `install()` reconciles before serving.
        restarted = await _boot(tmp_path)
        try:
            repaired = restarted.session_store.find_turn(session.id, turn.id)
            assert repaired is not None
            assert repaired.state is TurnState.INTERRUPTED
            assert repaired.ended_at is not None
            assert repaired.stopped_reason == "restart"

            events = restarted.session_store.read_since(session.id, 0)
            interrupts = [e for e in events if e.type == "turn.interrupted"]
            assert len(interrupts) == 1
            payload = dict(interrupts[0].payload)
            assert payload["turn_id"] == turn.id
            assert payload["state"] == "interrupted"
            # `by: "system"`, not "user": nobody pressed stop.
            assert payload["by"] == "system"
            assert payload["data"] == {"reason": "restart"}
            # The terminal event took a real seq on the same counter the
            # client's replay uses — no gap, no collision.
            assert interrupts[0].seq == seq_before + 1
            assert restarted.session_store.last_seq(session.id) == seq_before + 1
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
