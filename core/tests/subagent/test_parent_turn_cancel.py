"""RFC-0012 A7: the parent-turn terminal sweep (§7.3).

The hook chain lives in `SessionLog._turn_ending()`: the RFC-0008 input dialog
closes first, then every unfinished run of the ending turn is cancelled — all
BEFORE the turn's terminal event is written, so a client never sees a finished
parent with a running child.
"""

from __future__ import annotations

import asyncio

from vade.session.models import SubagentState

from ..session.helpers import GateExecutor, wait_until
from .helpers import attach, make_log, make_manager, payloads, settle, store_of, stream


def test_parent_turn_end_cancels_unfinished_runs(tmp_path):
    async def scenario():
        gate = GateExecutor()
        log = make_log(path=tmp_path / "sessions.db", executor=gate)
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        turn, _ = await log.submit(session.id, [{"type": "text", "text": "parent"}], "c1")
        # Two children in flight while the parent turn runs; one other-session
        # run that must NOT be swept.
        child_a = await manager.spawn(parent_session_id=session.id, parent_turn_id=turn.id,
                                      child_session_id="child-a", label="a")
        child_b = await manager.spawn(parent_session_id=session.id, parent_turn_id=turn.id,
                                      child_session_id="child-b", label="b")
        child_a = await manager.set_running(child_a.run_id)
        other_session = log.create_session()
        bystander = await manager.spawn(parent_session_id=other_session.id,
                                        parent_turn_id=turn.id, label="elsewhere")

        gate.gate.set()
        await log.wait_idle(session.id)
        await settle()

        # Both in-flight children are cancelled; the bystander is untouched.
        assert manager.require(child_a.run_id).state is SubagentState.CANCELLED
        assert manager.require(child_b.run_id).state is SubagentState.CANCELLED
        assert manager.require(bystander.run_id).state is SubagentState.SPAWNED
        swept = store_of(log).list_subagent_runs(session_id=session.id)
        assert {r.state.value for r in swept} == {"cancelled"}
        # Exactly one `subagent.result` per swept run — cancelled, not failed.
        results = payloads(sink, "subagent.result")
        assert sorted(r["run_id"] for r in results) == sorted([child_a.run_id, child_b.run_id])
        assert all(r["state"] == "cancelled" for r in results)
        await log.close()

    asyncio.run(scenario())


def test_result_precedes_turn_completed(tmp_path):
    """A7's ordering rule: `subagent.result(cancelled)` seq < `turn.completed`
    seq — the sweep runs inside the pre-terminal hook, not after it."""
    async def scenario():
        gate = GateExecutor()
        log = make_log(path=tmp_path / "sessions.db", executor=gate)
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        turn, _ = await log.submit(session.id, [{"type": "text", "text": "parent"}], "c1")
        run = await manager.spawn(parent_session_id=session.id, parent_turn_id=turn.id,
                                  child_session_id="child-1", label="child")
        gate.gate.set()
        await log.wait_idle(session.id)
        await settle()

        order = stream(sink)
        result_idx = next(i for i, (t, _) in enumerate(order) if t == "subagent.result")
        completed_idx = next(i for i, (t, _) in enumerate(order) if t == "turn.completed")
        assert result_idx < completed_idx, f"stream order: {order}"
        assert order[completed_idx][1] > order[result_idx][1]  # seq, not just index
        # The same holds for an INTERRUPTED parent: stop the turn, child swept.
        gate2 = GateExecutor()
        log2 = make_log(path=tmp_path / "s2.db", executor=gate2)
        session2 = log2.create_session()
        sink2 = await attach(log2, session2.id)
        manager2 = make_manager(log2)
        turn2, _ = await log2.submit(session2.id, [{"type": "text", "text": "slow"}], "c1")
        run2 = await manager2.spawn(parent_session_id=session2.id, parent_turn_id=turn2.id,
                                    label="child-2")
        await manager2.set_running(run2.run_id)
        # The sweep only has a turn to sweep once the queue worker actually
        # started it — wait for the executor to be entered before interrupting.
        assert await wait_until(lambda: bool(gate2.order), timeout=2.0)
        await log2.interrupt(session2.id, turn2.id, by="user")
        await settle()
        assert manager2.require(run2.run_id).state is SubagentState.CANCELLED
        order2 = stream(sink2)
        result_idx2 = next(i for i, (t, _) in enumerate(order2) if t == "subagent.result")
        interrupted_idx = next(i for i, (t, _) in enumerate(order2) if t == "turn.interrupted")
        assert result_idx2 < interrupted_idx, f"stream order: {order2}"
        await log2.close()
        await log.close()

    asyncio.run(scenario())
