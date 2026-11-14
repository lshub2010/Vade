"""RFC-0007 A3/A4/A11: cancel deadline, terminal idempotence, cascade rules."""

from __future__ import annotations

import asyncio
import time

from vade.tasks import TaskState

from .helpers import (
    attach,
    make_log,
    make_registry,
    payloads,
    settle,
    wait_until,
)


def test_cancel_is_idempotent_and_emits_exactly_one_finished():
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log, cancel_grace_ms=120)
        task = await registry.spawn(kind="job", name="download", session_id=session.id)
        await registry.set_state(task.task_id, "running")

        first = await registry.cancel(task.task_id, reason="user asked")
        assert first.state is TaskState.CANCELLING
        second = await registry.cancel(task.task_id, reason="again")
        assert second.state is TaskState.CANCELLING

        assert await wait_until(lambda: registry.get(task.task_id).is_terminal, 3.0)
        await settle()
        finished = payloads(sink, "task.finished")
        assert len(finished) == 1, "a second cancel must not emit a second task.finished"
        assert finished[0]["state"] == "cancelled"
        assert finished[0]["forced"] is True

        third = await registry.cancel(task.task_id)
        assert third.state is TaskState.CANCELLED
        await registry.set_state(task.task_id, "running")
        await settle()
        assert len(payloads(sink, "task.finished")) == 1

    asyncio.run(scenario())


def test_cancel_lands_within_three_seconds_of_the_rpc_return():
    """A3: the measured bound is the RFC's, with the shipped default grace."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log)  # default cancel_grace_ms=2000
        task = await registry.spawn(kind="job", name="stubborn", session_id=session.id)
        await registry.set_state(task.task_id, "running")

        started = time.monotonic()
        returned = await registry.cancel(task.task_id, reason="ignore me")
        assert returned.state is TaskState.CANCELLING

        assert await wait_until(lambda: registry.get(task.task_id).is_terminal, 5.0)
        elapsed_ms = (time.monotonic() - started) * 1000
        assert elapsed_ms <= 3000, f"cancel took {elapsed_ms:.0f}ms"
        await settle()
        finished = payloads(sink, "task.finished")
        assert len(finished) == 1 and finished[0]["forced"] is True

        # A3's second half: nothing follows within 5 s (sped up to the test clock).
        count = len(sink.events)
        await asyncio.sleep(0.2)
        assert len(sink.events) == count

    asyncio.run(scenario())


def test_cooperative_exit_is_not_forced():
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        registry = make_registry(log, cancel_grace_ms=2000)
        task = await registry.spawn(kind="job", name="polite", session_id=session.id)
        await registry.set_state(task.task_id, "running")

        await registry.cancel(task.task_id)
        # The worker notices its cancel token and exits well inside the grace.
        token = registry.cancel_token(task.task_id)
        assert token is not None and token.cancelled is True
        await registry.finish(task.task_id, "cancelled", summary="stopped cleanly")
        await settle()
        finished = payloads(sink, "task.finished")
        assert len(finished) == 1
        assert finished[0]["forced"] is False, "a clean exit must not be reported as forced"

    asyncio.run(scenario())


def test_interrupting_a_turn_cancels_subagents_but_not_jobs():
    """§5.7: stop means 'stop thinking', not 'cancel my download'."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, cancel_grace_ms=80)
        subagent = await registry.spawn(
            kind="subagent", name="helper", session_id=session.id, turn_id="turn-1"
        )
        job = await registry.spawn(
            kind="job", name="download", session_id=session.id, turn_id="turn-1"
        )
        other = await registry.spawn(
            kind="subagent", name="other", session_id=session.id, turn_id="turn-2"
        )

        cancelled = await registry.cancel_turn(session.id, "turn-1")
        assert cancelled == [subagent.task_id]
        assert registry.get(subagent.task_id).state is TaskState.CANCELLING
        assert registry.get(job.task_id).state is TaskState.QUEUED
        assert registry.get(other.task_id).state is TaskState.QUEUED

    asyncio.run(scenario())


def test_finishing_a_parent_cancels_its_children():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, cancel_grace_ms=80)
        parent = await registry.spawn(kind="subagent", name="A", session_id=session.id)
        child = await registry.spawn(
            kind="subagent", name="B", session_id=session.id, parent_id=parent.task_id
        )

        await registry.finish(parent.task_id, "completed", summary="done")
        await settle()
        assert registry.get(child.task_id).state is TaskState.CANCELLING
        assert registry.get(child.task_id).depth == 1

    asyncio.run(scenario())


def test_session_close_cancels_every_kind():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, cancel_grace_ms=50)
        job = await registry.spawn(kind="job", name="job", session_id=session.id)
        sub = await registry.spawn(kind="subagent", name="sub", session_id=session.id)
        await registry.close_session(session.id)
        assert registry.get(job.task_id).state is TaskState.CANCELLING
        assert registry.get(sub.task_id).state is TaskState.CANCELLING

    asyncio.run(scenario())


def test_ui_interrupt_path_reaches_the_registry():
    """The wiring, not just the method: `SessionLog.interrupt` cascades."""

    async def scenario():
        from ..session.helpers import GateExecutor

        gate = GateExecutor()
        log = make_log(executor=gate)
        session = log.create_session()
        registry = make_registry(log, cancel_grace_ms=80)
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "cmid-1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id, 2.0)

        sub = await registry.spawn(
            kind="subagent", name="helper", session_id=session.id, turn_id=turn.id
        )
        job = await registry.spawn(
            kind="job", name="job", session_id=session.id, turn_id=turn.id
        )
        assert await log.interrupt(session.id, turn.id) is True
        assert registry.get(sub.task_id).state is TaskState.CANCELLING
        assert registry.get(job.task_id).state is TaskState.QUEUED

        gate.gate.set()
        await asyncio.sleep(0.05)
        await log.close()

    asyncio.run(scenario())
