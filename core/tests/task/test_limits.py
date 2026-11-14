"""RFC-0007 A12 + §9.4: depth ceiling, concurrency ceiling, config validation."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.tasks.registry import TaskLimits

from .helpers import make_log, make_registry, settle


def test_five_levels_fit_and_the_sixth_is_refused():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, max_depth=4)
        parent = None
        depths = []
        for level in range(5):
            task = await registry.spawn(
                kind="subagent", name=f"level-{level}", session_id=session.id,
                parent_id=parent.task_id if parent else None,
            )
            depths.append(task.depth)
            parent = task
        assert depths == [0, 1, 2, 3, 4]

        with pytest.raises(VadeError) as caught:
            await registry.spawn(
                kind="subagent", name="level-5", session_id=session.id, parent_id=parent.task_id
            )
        assert caught.value.code == "INVALID_ARGUMENT"
        assert caught.value.data.get("field") == "depth"

    asyncio.run(scenario())


def test_ninth_live_task_is_overloaded_with_retry_after():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, max_concurrent=8)
        tasks = [
            await registry.spawn(kind="job", name=f"job-{i}", session_id=session.id)
            for i in range(8)
        ]
        with pytest.raises(VadeError) as caught:
            await registry.spawn(kind="job", name="job-9", session_id=session.id)
        assert caught.value.code == "OVERLOADED"
        assert caught.value.data.get("retry_after", 0) > 0
        assert caught.value.data.get("max_concurrent") == 8

        # Finishing one frees exactly one slot.
        await registry.finish(tasks[0].task_id, "completed", summary="ok")
        await settle()
        replacement = await registry.spawn(kind="job", name="job-9", session_id=session.id)
        assert replacement.task_id

    asyncio.run(scenario())


def test_concurrency_is_per_session():
    async def scenario():
        log = make_log()
        first = log.create_session()
        second = log.create_session()
        registry = make_registry(log, max_concurrent=1)
        await registry.spawn(kind="job", name="a", session_id=first.id)
        other = await registry.spawn(kind="job", name="b", session_id=second.id)
        assert other.session_id == second.id

    asyncio.run(scenario())


def test_bad_kind_and_terminal_started_state_are_refused():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        with pytest.raises(VadeError) as kind:
            await registry.spawn(kind="daemon", name="x", session_id=session.id)
        assert kind.value.data.get("field") == "kind"
        with pytest.raises(VadeError) as state:
            await registry.spawn(kind="job", name="x", session_id=session.id, state="completed")
        assert state.value.data.get("field") == "state"

    asyncio.run(scenario())


def test_limits_defaults_match_the_rfc():
    limits = TaskLimits()
    assert limits.max_concurrent == 8
    assert limits.max_depth == 4
    assert limits.ui_fold_depth == 3
    assert limits.history_limit == 50
    assert limits.snapshot_finished == 20
    assert limits.log_lines == 200
    assert limits.log_bytes == 65536
    assert limits.log_ring_lines == 2000
    assert limits.log_line_chars == 4096
    assert limits.progress_hz == 2.0
    assert limits.event_burst == 20
    assert limits.cancel_grace_ms == 2000
    assert limits.subagent_message is False


def test_config_loader_refuses_a_grace_above_three_seconds():
    with pytest.raises(VadeError) as caught:
        TaskLimits.from_config({"tasks.cancel_grace_ms": 3001})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.data.get("field") == "tasks.cancel_grace_ms"


def test_config_loader_refuses_a_fold_deeper_than_the_tree():
    with pytest.raises(VadeError) as caught:
        TaskLimits.from_config({"tasks.max_depth": 2, "tasks.ui_fold_depth": 3})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.data.get("field") == "tasks.max_depth"


def test_config_loader_reads_overrides():
    limits = TaskLimits.from_config(
        {"tasks.max_concurrent": 3, "tasks.progress_hz": 4, "tasks.subagent_message": True}
    )
    assert limits.max_concurrent == 3
    assert limits.progress_hz == 4.0
    assert limits.subagent_message is True
