"""RFC-0012 A8: the `subagent/list` / `subagent/get` observation methods."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.session.models import SubagentState

from .helpers import FakeConnection, attach, make_log, make_manager, settle
from .helpers import store_of
from vade.subagents.rpc import SubagentRPC

VIEW_KEYS = {
    "run_id", "parent_session_id", "parent_turn_id", "child_session_id",
    "label", "state", "created_at", "finished_at", "result_digest",
    "result_chars", "error_code", "result_preview", "duration_ms",
}


def _handlers(manager):
    return SubagentRPC(manager).handlers()


def test_subagent_list_returns_runs_and_filters(tmp_path):
    async def scenario():
        log = make_log(path=tmp_path / "sessions.db")
        session = log.create_session()
        await attach(log, session.id)
        manager = make_manager(log)

        run_a = await manager.spawn(parent_session_id=session.id, label="a")
        run_b = await manager.spawn(parent_session_id=session.id, label="b")
        await manager.set_running(run_b.run_id)
        await manager.finish(run_b.run_id, "completed", result_text="done")

        handlers = _handlers(manager)
        conn = FakeConnection()
        out = await handlers["subagent/list"](conn, {})
        # Same-millisecond spawns tie on created_at and break by random run_id,
        # so the ORDER is not asserted here — membership and completeness are.
        assert {r["run_id"] for r in out["runs"]} == {run_a.run_id, run_b.run_id}
        assert out["next_cursor"] is None
        # Every row is a full view; terminal rows carry the fingerprint.
        for row in out["runs"]:
            assert VIEW_KEYS <= set(row)
        done = next(r for r in out["runs"] if r["run_id"] == run_b.run_id)
        assert done["state"] == "completed"
        assert done["result_digest"] is not None and done["result_chars"] == 4
        live = next(r for r in out["runs"] if r["run_id"] == run_a.run_id)
        assert live["state"] == "spawned" and live["result_digest"] is None

        # `states` filters within the ordered set.
        filtered = await handlers["subagent/list"](conn, {"states": ["running"]})
        assert filtered["runs"] == []
        filtered = await handlers["subagent/list"](conn, {"states": ["completed"]})
        assert {r["run_id"] for r in filtered["runs"]} == {run_b.run_id}
        # session scoping keeps other sessions out.
        other = log.create_session()
        await manager.spawn(parent_session_id=other.id, label="elsewhere")
        scoped = await handlers["subagent/list"](conn, {"session_id": session.id})
        assert len(scoped["runs"]) == 2
        everything = await handlers["subagent/list"](conn, {})
        assert len(everything["runs"]) == 3
        # limit + opaque cursor pages through without repeats.
        page1 = await handlers["subagent/list"](conn, {"limit": 2})
        assert len(page1["runs"]) == 2 and page1["next_cursor"]
        page2 = await handlers["subagent/list"](conn, {"limit": 2, "cursor": page1["next_cursor"]})
        assert len(page2["runs"]) == 1
        ids1 = {r["run_id"] for r in page1["runs"]}
        ids2 = {r["run_id"] for r in page2["runs"]}
        assert ids1 & ids2 == set()
        # Read-only sessions may call it (§6.4: both methods are read-only).
        out_ro = await handlers["subagent/list"](FakeConnection(read_only=True), {})
        assert len(out_ro["runs"]) == 3

    asyncio.run(scenario())


def test_subagent_list_invalid_params(tmp_path):
    async def scenario():
        log = make_log()
        session = log.create_session()
        manager = make_manager(log)
        handlers = _handlers(manager)
        conn = FakeConnection()

        with pytest.raises(VadeError) as exc:
            await handlers["subagent/list"](conn, {"states": ["bogus"]})
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data.get("field") == "states"
        with pytest.raises(VadeError) as exc:
            await handlers["subagent/list"](conn, {"limit": 0})
        assert exc.value.data.get("field") == "limit"
        with pytest.raises(VadeError) as exc:
            await handlers["subagent/list"](conn, {"limit": 201})
        assert exc.value.data.get("field") == "limit"
        with pytest.raises(VadeError) as exc:
            await handlers["subagent/list"](conn, {"cursor": "not-a-cursor"})
        assert exc.value.data.get("field") == "cursor"
        with pytest.raises(VadeError) as exc:
            await handlers["subagent/list"](conn, {"session_id": 42})
        assert exc.value.data.get("field") == "session_id"

    asyncio.run(scenario())


def test_subagent_get_returns_full_view_with_preview(tmp_path):
    async def scenario():
        log = make_log(path=tmp_path / "sessions.db")
        session = log.create_session()
        await attach(log, session.id)
        manager = make_manager(log)
        handlers = _handlers(manager)
        conn = FakeConnection()

        # A real child session whose last assistant message becomes the preview.
        child = log.create_session()
        from vade.session.models import Message

        assistant = Message(
            id="m-assistant", session_id=child.id, role="assistant",
            content="RESULT:" + "y" * 800, created_at=1,
        )
        store_of(log).append(child.id, [assistant], [])

        run = await manager.spawn(parent_session_id=session.id, parent_turn_id="t1",
                                  child_session_id=child.id, label="researcher")
        await manager.set_running(run.run_id)
        await manager.finish(run.run_id, "completed", result_text="full result text")
        await settle()

        view = await handlers["subagent/get"](conn, {"run_id": run.run_id})
        assert VIEW_KEYS <= set(view)
        assert view["run_id"] == run.run_id
        assert view["state"] == "completed"
        assert view["parent_session_id"] == session.id
        assert view["parent_turn_id"] == "t1"
        assert view["child_session_id"] == child.id
        assert view["error_code"] is None
        assert view["duration_ms"] >= 0
        # A6/A4: the preview is the ≤500-char projection of the child's LAST
        # assistant message — the full text still never crosses.
        assert view["result_preview"] is not None
        assert len(view["result_preview"]) == 500
        assert view["result_preview"].startswith("RESULT:")
        assert "full result text" not in str(view)
        # A run without a child session has no preview, not a fake one.
        orphan = await manager.spawn(parent_session_id=session.id, label="no-child")
        await manager.set_running(orphan.run_id)
        await manager.finish(orphan.run_id, "completed", result_text="x")
        view2 = await handlers["subagent/get"](conn, {"run_id": orphan.run_id})
        assert view2["result_preview"] is None
        # Read-only sessions may call it.
        view_ro = await handlers["subagent/get"](FakeConnection(read_only=True),
                                                 {"run_id": run.run_id})
        assert view_ro["run_id"] == run.run_id

    asyncio.run(scenario())


def test_subagent_get_not_found_vs_malformed():
    async def scenario():
        log = make_log()
        session = log.create_session()
        manager = make_manager(log)
        handlers = _handlers(manager)
        conn = FakeConnection()

        # Malformed run_id → INVALID_ARGUMENT (data.field=run_id)…
        with pytest.raises(VadeError) as exc:
            await handlers["subagent/get"](conn, {"run_id": "hello"})
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data.get("field") == "run_id"
        with pytest.raises(VadeError):
            await handlers["subagent/get"](conn, {})
        # …and a well-formed unknown id → NOT_FOUND. Two different answers.
        with pytest.raises(VadeError) as exc:
            await handlers["subagent/get"](conn, {"run_id": f"run_{'a' * 32}"})
        assert exc.value.code == "NOT_FOUND"

    asyncio.run(scenario())
