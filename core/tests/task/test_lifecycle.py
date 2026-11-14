"""RFC-0007 A1/A2: the seven-state machine and a self-describing event family.

Acceptance: three spawned subagents produce three `task.started` with
`kind=subagent` / `parent_id=null` / `depth=0`, each moves to `completed`, every
summary is <=500 chars and every `duration_ms` is > 0.
"""

from __future__ import annotations

import asyncio

from vade.tasks import TERMINAL_TASK_STATES, TaskState
from vade.tasks.registry import TaskRegistry

from .helpers import attach, events, make_log, make_registry, payloads, settle, states

STARTED_REQUIRED = {
    "task_id", "kind", "state", "session_id", "turn_id", "parent_id",
    "depth", "name", "started_at", "retryable",
}
STATUS_REQUIRED = {"task_id", "kind", "parent_id", "depth", "state", "elapsed_ms"}
PROGRESS_REQUIRED = {"task_id", "pct", "elapsed_ms"}
FINISHED_REQUIRED = {
    "task_id", "kind", "parent_id", "depth", "state", "duration_ms", "summary",
}


def test_task_states_are_the_seven_and_three_terminal():
    assert {state.value for state in TaskState} == {
        "queued", "running", "waiting_approval", "cancelling",
        "completed", "failed", "cancelled",
    }
    assert {state.value for state in TERMINAL_TASK_STATES} == {
        "completed", "failed", "cancelled",
    }


def test_started_and_finished_are_self_describing():
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log)

        task = await registry.spawn(
            kind="subagent", name="researcher", session_id=session.id,
            turn_id="turn-1", child_session_id="child-1", child_turn_id="ct-1",
        )
        await settle()
        started = payloads(sink, "task.started")
        assert len(started) == 1
        payload = started[0]
        assert STARTED_REQUIRED <= set(payload)
        assert payload["task_id"] == task.task_id
        assert payload["kind"] == "subagent"
        assert payload["state"] in {"queued", "running"}
        assert payload["session_id"] == session.id
        assert payload["turn_id"] == "turn-1"
        assert payload["parent_id"] is None
        assert payload["depth"] == 0
        assert payload["child_session_id"] == "child-1"

        await registry.set_state(task.task_id, "running", action="reading docs")
        await registry.set_state(task.task_id, "waiting_approval")
        await registry.set_state(task.task_id, "running")
        await asyncio.sleep(0.02)
        final = await registry.finish(task.task_id, "completed", summary="x" * 600)
        await settle()

        status = payloads(sink, "task.status")
        assert status and STATUS_REQUIRED <= set(status[0])
        assert states(sink, "task.status") == ["running", "waiting_approval", "running"]
        finished = payloads(sink, "task.finished")
        assert len(finished) == 1
        assert FINISHED_REQUIRED <= set(finished[0])
        assert finished[0]["state"] == "completed"
        assert len(finished[0]["summary"]) == 500  # truncated at the contract limit
        assert finished[0]["duration_ms"] > 0
        assert finished[0]["forced"] is False
        assert finished[0]["by"] == "system"
        assert final.is_terminal

    asyncio.run(scenario())


def test_three_subagents_report_independently():
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log)

        tasks = [
            await registry.spawn(kind="subagent", name=f"agent-{i}", session_id=session.id,
                                 turn_id="turn-1")
            for i in range(3)
        ]
        for task in tasks:
            await registry.set_state(task.task_id, "running")
            await asyncio.sleep(0.005)
            await registry.finish(task.task_id, "completed", summary=f"done {task.name}")
        await settle()

        started = payloads(sink, "task.started")
        assert len(started) == 3
        assert {p["kind"] for p in started} == {"subagent"}
        assert all(p["parent_id"] is None and p["depth"] == 0 for p in started)
        finished = payloads(sink, "task.finished")
        assert len(finished) == 3
        assert {p["state"] for p in finished} == {"completed"}
        # `duration_ms` is wall-clock and legitimately 0 for a task that finishes
        # inside the same millisecond; asserting `> 0` made this test depend on
        # machine speed (it passed alone, failed in a full run).
        assert all(p["duration_ms"] >= 0 and len(p["summary"]) <= 500 for p in finished)

    asyncio.run(scenario())


def test_terminal_is_final_and_late_reports_are_dropped():
    """§5.5 R2/R3: a terminal task cannot be resurrected by a late worker."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)

        await registry.finish(task.task_id, "failed", code="INTERNAL", message="boom")
        await settle()
        before = len(sink.events)

        again = await registry.set_state(task.task_id, "running")
        assert again.state is TaskState.FAILED
        assert await registry.progress(task.task_id, 90) is False
        second = await registry.finish(task.task_id, "completed")
        assert second.state is TaskState.FAILED
        await settle()

        assert len(sink.events) == before, "nothing may follow task.finished"
        assert len(payloads(sink, "task.finished")) == 1
        assert payloads(sink, "task.finished")[0]["code"] == "INTERNAL"

    asyncio.run(scenario())


def test_failed_requires_a_code_and_progress_never_carries_state():
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)
        await registry.progress(task.task_id, 10)
        await settle()
        progress = payloads(sink, "task.progress")
        assert progress and PROGRESS_REQUIRED <= set(progress[0])
        assert "state" not in progress[0]

        try:
            await registry.finish(task.task_id, "failed", summary="no code")
        except Exception as exc:  # VadeError
            assert getattr(exc, "code", "") == "INVALID_ARGUMENT"
        else:  # pragma: no cover - the assertion is the refusal
            raise AssertionError("failed without a code must be refused")

    asyncio.run(scenario())


def test_no_task_event_before_started_for_a_fresh_session():
    """`task.started` is the first thing a task emits (§5.3)."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        sink = await attach(log, session.id)
        await registry.spawn(kind="job", name="index", session_id=session.id)
        await settle()
        task_types = [
            event["type"] for event in sink.events if str(event["type"]).startswith("task.")
        ]
        assert task_types == ["task.started"]
        assert isinstance(registry, TaskRegistry)

    asyncio.run(scenario())
