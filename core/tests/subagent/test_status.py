"""RFC-0012 A3/A5: the `subagent.status` state-change event and the §7.2 gate."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.session.models import SubagentState

from .helpers import attach, make_log, make_manager, payloads, settle, states

STATUS_REQUIRED = {"run_id", "session_id", "state", "at"}


def test_spawned_to_running_emits_status():
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(
            parent_session_id=session.id, parent_turn_id="turn-1",
            child_session_id="child-1", label="researcher",
        )
        updated = await manager.set_running(
            run.run_id, step="reading docs/design.md", tool_name="read_file",
        )
        await settle()

        assert updated.state is SubagentState.RUNNING
        status = payloads(sink, "subagent.status")
        assert len(status) == 1
        payload = status[0]
        assert STATUS_REQUIRED <= set(payload)
        assert payload["run_id"] == run.run_id
        assert payload["session_id"] == session.id
        assert payload["state"] == "running"
        assert payload["step"] == "reading docs/design.md"
        assert payload["tool_name"] == "read_file"
        # The record moved in the manager (and the store behind it).
        assert manager.require(run.run_id).state is SubagentState.RUNNING

    asyncio.run(scenario())


def test_status_updates_while_running():
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(parent_session_id=session.id, label="worker")
        await manager.set_running(run.run_id)
        # running → running: a step/tool_name refresh, not a transition (§7.2),
        # but still a legitimate `subagent.status` on top of the transition one.
        await settle()
        first = await manager.report(run.run_id, step="step one")
        second = await manager.report(run.run_id, step="step two", tool_name="grep")
        await settle()

        status = payloads(sink, "subagent.status")
        assert len(status) == 3  # transition + two refreshes
        assert [s["state"] for s in status] == ["running", "running", "running"]
        assert status[1]["step"] == "step one"
        assert status[2]["step"] == "step two"
        assert status[2]["tool_name"] == "grep"
        assert first.state is SubagentState.RUNNING and second.state is SubagentState.RUNNING
        # A step longer than the §5.2 ceiling is truncated, not refused.
        await manager.report(run.run_id, step="x" * 200)
        await settle()
        assert len(payloads(sink, "subagent.status")[-1]["step"]) == 120

    asyncio.run(scenario())


def test_illegal_transition_rejected_without_a_trace():
    """A5: `running → spawned` is refused; the record and the stream are
    untouched — refusal writes NOTHING (RFC-0012 §7.2)."""
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(parent_session_id=session.id, label="w")
        await manager.set_running(run.run_id)
        await settle()
        before = payloads(sink, "subagent.status")

        # running → spawned is refused through the gate (a run never goes back).
        from vade.subagents.manager import ALLOWED_TRANSITIONS

        assert SubagentState.SPAWNED not in ALLOWED_TRANSITIONS[SubagentState.RUNNING]
        # A finished run cannot be restarted either.
        await manager.finish(run.run_id, "completed", result_text="done")
        with pytest.raises(VadeError) as exc:
            await manager.set_running(run.run_id)
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data.get("from") == "completed"

        await settle()
        after = payloads(sink, "subagent.status")
        # Nothing new on the status stream: the refusals emitted nothing.
        assert len(after) == len(before) + 0
        # ...and the record never left its terminal state.
        assert manager.require(run.run_id).state is SubagentState.COMPLETED

    asyncio.run(scenario())


def test_terminal_cannot_resume_and_spawned_cannot_finish():
    """A5: the five-value machine's refused corners — `spawned → completed`
    (a loop that never ran has no result) and any exit from a terminal state."""
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        # spawned → completed: refused.
        run_a = await manager.spawn(parent_session_id=session.id, label="a")
        with pytest.raises(VadeError) as exc:
            await manager.finish(run_a.run_id, "completed", result_text="never ran")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert manager.require(run_a.run_id).state is SubagentState.SPAWNED

        # spawned → failed: refused for the same reason.
        with pytest.raises(VadeError) as exc:
            await manager.finish(run_a.run_id, "failed", error_code="INTERNAL")
        assert exc.value.code == "INVALID_ARGUMENT"

        # failed → cancelled: a terminal state is never left (same-state
        # re-finish is the only idempotent path; a different terminal target
        # on a terminal run is an illegal transition).
        run_b = await manager.spawn(parent_session_id=session.id, label="b")
        await manager.set_running(run_b.run_id)
        await manager.finish(run_b.run_id, "failed", error_code="TIMEOUT")
        with pytest.raises(VadeError) as exc:
            await manager.finish(run_b.run_id, "cancelled")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data.get("from") == "failed"
        assert exc.value.data.get("to") == "cancelled"
        # ...while a same-state retry is the idempotent no-op.
        again = await manager.finish(run_b.run_id, "failed", error_code="TIMEOUT")
        assert again is manager.require(run_b.run_id)

        await settle()
        # exactly one terminal event per run, none for the refusals
        results = payloads(sink, "subagent.result")
        assert [r["run_id"] for r in results] == [run_b.run_id]
        assert states(sink, "subagent.result") == ["failed"]

    asyncio.run(scenario())
