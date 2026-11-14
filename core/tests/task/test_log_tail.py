"""RFC-0007 A14: `task/log_tail` caps, follow long-poll, lost marker."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from vade.errors import VadeError
from vade.tasks.logs import RingLog, decode_cursor, encode_cursor

from .helpers import make_log, make_registry


def test_ring_cursor_round_trip_and_garbage():
    assert decode_cursor(encode_cursor(7)) == 7
    with pytest.raises(ValueError):
        decode_cursor("not-a-cursor")


def test_tail_hits_both_caps_and_says_head():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)
        for i in range(5000):
            registry.append_log(task.task_id, f"line {i} " + "x" * 400)

        result = await registry.log_tail(task.task_id)
        assert len(result["lines"]) <= 200
        payload = json.dumps(result["lines"], ensure_ascii=False).encode("utf-8")
        assert len(payload) <= 65536, f"response payload is {len(payload)} bytes"
        assert result["truncated"] == "head"
        assert result["lines"][-1]["text"].startswith("line 4999")
        assert result["eof"] is False
        assert result["lost"] is False

    asyncio.run(scenario())


def test_single_long_line_is_truncated_line_wise():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, log_line_chars=64)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)
        registry.append_log(task.task_id, "y" * 4096)
        result = await registry.log_tail(task.task_id)
        assert result["truncated"] == "line"
        assert len(result["lines"][0]["text"]) == 64

    asyncio.run(scenario())


def test_follow_waits_then_returns_only_new_rows():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)
        registry.append_log(task.task_id, "first")
        first = await registry.log_tail(task.task_id)
        assert [row["text"] for row in first["lines"]] == ["first"]

        # Nothing new: the long-poll must give the call back, not hang forever.
        started = time.monotonic()
        empty = await registry.log_tail(
            task.task_id, cursor=first["cursor"], follow=True, wait_ms=120
        )
        elapsed = time.monotonic() - started
        assert empty["lines"] == []
        assert elapsed < 1.5, f"follow waited {elapsed:.2f}s for a 120ms budget"

        async def later():
            await asyncio.sleep(0.05)
            registry.append_log(task.task_id, "second")

        asyncio.create_task(later())
        followed = await registry.log_tail(
            task.task_id, cursor=first["cursor"], follow=True, wait_ms=1500
        )
        assert [row["text"] for row in followed["lines"]] == ["second"]
        assert followed["cursor"] != first["cursor"]

    asyncio.run(scenario())


def test_lost_is_reported_when_the_cursor_predates_the_ring():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, log_ring_lines=10)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)
        for i in range(50):
            registry.append_log(task.task_id, f"line {i}")
        stale = encode_cursor(0)
        result = await registry.log_tail(task.task_id, cursor=stale)
        assert result["lost"] is True
        assert len(result["lines"]) == 10
        assert result["lines"][0]["text"] == "line 40"

    asyncio.run(scenario())


def test_eof_only_when_terminal_and_drained():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)
        registry.append_log(task.task_id, "only")
        before = await registry.log_tail(task.task_id)
        assert before["eof"] is False
        await registry.finish(task.task_id, "completed", summary="ok")
        after = await registry.log_tail(task.task_id, cursor=before["cursor"])
        assert after["lines"] == []
        assert after["eof"] is True

    asyncio.run(scenario())


def test_a_task_without_a_log_is_not_found_not_empty():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log)
        subagent = await registry.spawn(kind="subagent", name="helper", session_id=session.id)
        with pytest.raises(VadeError) as caught:
            await registry.log_tail(subagent.task_id)
        assert caught.value.code == "NOT_FOUND"

    asyncio.run(scenario())


def test_configured_ceiling_wins_over_the_client_default():
    """`tasks.log_lines` may be lower than the schema maximum (200)."""

    async def scenario():
        log = make_log()
        session = log.create_session()
        registry = make_registry(log, log_lines=5)
        task = await registry.spawn(kind="job", name="index", session_id=session.id)
        for i in range(20):
            registry.append_log(task.task_id, f"line {i}")
        result = await registry.log_tail(task.task_id)  # no lines= param: default 200
        assert len(result["lines"]) == 5
        assert result["truncated"] == "head"

    asyncio.run(scenario())


def test_default_line_budget_is_the_configured_ceiling():
    ring = RingLog(max_lines=50, line_chars=100)
    for i in range(50):
        ring.append(f"row {i}")
    result = ring.tail(lines=999, max_bytes=65536)
    assert len(result["rows"]) == 50
    assert result["lost"] is False
