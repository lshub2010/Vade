"""RFC-0012 A1/A2/A9: `subagent.spawned`, the label rule, the run_id rule, store reentrancy."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from vade.errors import VadeError
from vade.session.models import SubagentState

from .helpers import attach, make_log, make_manager, payloads, settle, store_of

SPAWNED_REQUIRED = {
    "run_id", "session_id", "parent_session_id", "parent_turn_id",
    "child_session_id", "label", "at",
}
RUN_ID_PATTERN = re.compile(r"^run_[0-9a-f]{32}$")


def test_spawned_is_self_describing_and_persisted():
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(
            parent_session_id=session.id, parent_turn_id="turn-1",
            child_session_id="child-1", label="researcher",
        )
        await settle()

        spawned = payloads(sink, "subagent.spawned")
        assert len(spawned) == 1
        payload = spawned[0]
        assert SPAWNED_REQUIRED <= set(payload)
        assert payload["run_id"] == run.run_id
        assert RUN_ID_PATTERN.match(payload["run_id"])
        # §5.1: the family broadcasts on the parent's stream — both keys present
        # and equal, so a consumer never has to walk back to build the tree.
        assert payload["session_id"] == session.id
        assert payload["parent_session_id"] == session.id
        assert payload["parent_turn_id"] == "turn-1"
        assert payload["child_session_id"] == "child-1"
        assert payload["label"] == "researcher"
        assert isinstance(payload["at"], int)
        # §4.2: the record is the truth, persisted before the event broadcast.
        assert run.state is SubagentState.SPAWNED
        row = store_of(log).get_subagent_run(run.run_id)
        assert row is not None and row.label == "researcher"
        assert store_of(log).list_subagent_runs(session_id=session.id)[0].run_id == run.run_id

    asyncio.run(scenario())


def test_label_validation_refuses_never_truncates():
    async def scenario():
        log = make_log()
        session = log.create_session()
        manager = make_manager(log)

        bad_values: tuple[Any, ...] = ("", None, 42, "x" * 65)
        for bad in bad_values:
            with pytest.raises(VadeError) as exc:
                await manager.spawn(parent_session_id=session.id, label=bad)
            assert exc.value.code == "INVALID_ARGUMENT"
            assert getattr(exc.value, "data", None) and exc.value.data.get("field") == "label"

        # A parent session id is required too — a run without a stream is a lie.
        with pytest.raises(VadeError) as exc:
            await manager.spawn(parent_session_id="", label="ok")
        assert exc.value.data.get("field") == "parent_session_id"

        # Exactly 64 characters passes — the limit is inclusive.
        run = await manager.spawn(parent_session_id=session.id, label="x" * 64)
        assert len(run.label) == 64
        # And no event leaked for any refused spawn.
        await settle()
        assert len(payloads(await attach(log, session.id), "subagent.spawned")) == 0

    asyncio.run(scenario())


def test_run_id_format_is_run_prefix_plus_32_hex():
    async def scenario():
        log = make_log()
        session = log.create_session()
        manager = make_manager(log)

        run = await manager.spawn(parent_session_id=session.id, label="a")
        assert RUN_ID_PATTERN.match(run.run_id)
        assert len(run.run_id) == 36  # "run_" + 32 hex
        # A fresh spawn never collides: two runs, two ids.
        other = await manager.spawn(parent_session_id=session.id, label="b")
        assert other.run_id != run.run_id
        # Malformed ids are refused BEFORE the lookup (§6.2: the two failures
        # are distinct — format vs unknown).
        with pytest.raises(VadeError) as exc:
            manager.require("not-a-run-id")
        assert exc.value.code == "INVALID_ARGUMENT"
        with pytest.raises(VadeError) as exc:
            manager.require(f"run_{'0' * 32}")  # well-formed, unknown
        assert exc.value.code == "NOT_FOUND"

    asyncio.run(scenario())


def test_store_table_is_reentrant(tmp_path):
    """A9: a second open of the same database file must not choke on the
    `subagent_runs` table — `CREATE TABLE IF NOT EXISTS` decides, every time."""
    async def scenario():
        db = tmp_path / "sessions.db"
        log = make_log(path=db)
        session = log.create_session()
        manager = make_manager(log)
        run = await manager.spawn(parent_session_id=session.id, label="persistent")
        await log.close()

        # A brand-new SessionLog over the same file: the table is already
        # there, the write from the first connection is readable.
        log2 = make_log(path=db)
        row = store_of(log2).get_subagent_run(run.run_id)
        assert row is not None and row.state is SubagentState.SPAWNED
        manager2 = make_manager(log2)
        run2 = await manager2.spawn(parent_session_id=session.id, label="second-open")
        assert store_of(log2).get_subagent_run(run2.run_id) is not None
        await log2.close()

    asyncio.run(scenario())
