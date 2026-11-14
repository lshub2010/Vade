"""RFC-0007 A16: `task/list` ordering, keyset cursor, filters and counts."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError

from .helpers import make_log, make_registry, settle


def test_terminal_history_pages_by_cursor():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        for i in range(60):
            task = await registry.spawn(kind="job", name=f"job-{i}", session_id=session.id)
            await registry.finish(task.task_id, "completed", summary=f"done {i}")

        page = registry.list_tasks(
            session_id=session.id,
            states=["completed", "failed", "cancelled"],
            limit=50,
        )
        assert len(page["tasks"]) == 50
        assert page["next_cursor"]
        ended = [row["ended_at"] for row in page["tasks"]]
        assert ended == sorted(ended, reverse=True), "terminal rows are newest-first"
        assert page["running_count"] == 0
        assert page["limits"] == {"max_concurrent": 8, "max_depth": 4}

        rest = registry.list_tasks(
            session_id=session.id,
            states=["completed", "failed", "cancelled"],
            limit=50,
            cursor=page["next_cursor"],
        )
        assert len(rest["tasks"]) == 10
        assert rest["next_cursor"] is None

        seen = {row["task_id"] for row in page["tasks"]} | {row["task_id"] for row in rest["tasks"]}
        assert len(seen) == 60, "paging must neither duplicate nor drop a row"

    asyncio.run(scenario())


def test_live_tasks_come_first_and_running_count_ignores_the_state_filter():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        done = await registry.spawn(kind="job", name="done", session_id=session.id)
        await registry.finish(done.task_id, "completed", summary="ok")
        live = await registry.spawn(kind="subagent", name="live", session_id=session.id)
        await registry.set_state(live.task_id, "running")

        everything = registry.list_tasks(session_id=session.id)
        assert [row["task_id"] for row in everything["tasks"]][0] == live.task_id
        assert everything["running_count"] == 1

        terminal_only = registry.list_tasks(
            session_id=session.id, states=["completed", "failed", "cancelled"]
        )
        assert [row["task_id"] for row in terminal_only["tasks"]] == [done.task_id]
        assert terminal_only["running_count"] == 1, "running_count is not filtered by `states`"

    asyncio.run(scenario())


def test_kind_filter_and_cross_session_default():
    async def scenario():
        log = make_log()
        first = log.create_session()
        second = log.create_session()
        registry = make_registry(log)
        sub = await registry.spawn(kind="subagent", name="sub", session_id=first.id)
        job = await registry.spawn(kind="job", name="job", session_id=second.id)
        await settle()

        jobs = registry.list_tasks(kinds=["job"])
        assert [row["task_id"] for row in jobs["tasks"]] == [job.task_id]
        both = registry.list_tasks()
        assert {row["task_id"] for row in both["tasks"]} == {sub.task_id, job.task_id}
        assert both["running_count"] == 2

    asyncio.run(scenario())


def test_bad_cursor_and_bad_limit_are_invalid_argument():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        await registry.spawn(kind="job", name="job", session_id=session.id)

        with pytest.raises(VadeError) as cursor:
            registry.list_tasks(session_id=session.id, cursor="not-a-cursor")
        assert cursor.value.code == "INVALID_ARGUMENT"
        assert cursor.value.data.get("field") == "cursor"

        with pytest.raises(VadeError) as limit:
            registry.list_tasks(session_id=session.id, limit=0)
        assert limit.value.code == "INVALID_ARGUMENT"

        with pytest.raises(VadeError):
            registry.list_tasks(session_id=session.id, states=["not-a-state"])

    asyncio.run(scenario())


def test_task_view_carries_the_task_contract():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        task = await registry.spawn(
            kind="subagent", name="helper", session_id=session.id, turn_id="t1",
            child_session_id="child",
        )
        view = registry.list_tasks(session_id=session.id)["tasks"][0]
        for key in (
            "task_id", "kind", "parent_id", "depth", "session_id", "turn_id",
            "child_session_id", "child_turn_id", "name", "state", "action", "pct",
            "started_at", "ended_at", "elapsed_ms", "duration_ms", "summary", "code",
            "message", "retryable", "forced", "log_available",
        ):
            assert key in view, f"TaskView is missing {key}"
        assert view["task_id"] == task.task_id
        assert view["child_session_id"] == "child"
        assert view["log_available"] is False

        await registry.finish(task.task_id, "completed", summary="ok")
        done = registry.list_tasks(session_id=session.id)["tasks"][0]
        assert done["duration_ms"] is not None and done["elapsed_ms"] is None

    asyncio.run(scenario())


def test_snapshot_holds_live_tasks_plus_a_bounded_terminal_tail():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, snapshot_finished=2)
        for i in range(5):
            task = await registry.spawn(kind="job", name=f"job-{i}", session_id=session.id)
            await registry.finish(task.task_id, "completed", summary=f"done {i}")
        live = await registry.spawn(kind="job", name="live", session_id=session.id)
        await registry.set_state(live.task_id, "running")

        views = log.snapshot(session.id)["tasks"]
        assert log.snapshot(session.id)["tasks_truncated"] is True
        assert views[0]["task_id"] == live.task_id
        assert len([v for v in views if v["state"] == "completed"]) == 2
        assert len(views) == 3

    asyncio.run(scenario())


def test_session_without_the_component_has_no_tasks_key():
    """§5.6: a missing `tasks` key is the documented 'core predates RFC-0007'."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        snapshot = log.snapshot(session.id)
        assert "tasks" not in snapshot and "tasks_truncated" not in snapshot

    asyncio.run(scenario())
