"""RFC-0012 A4/A5/A6: `subagent.result` — fingerprint, not text; failure rules; idempotency."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from vade.errors import VadeError
from vade.session.models import SubagentState

from .helpers import attach, make_log, make_manager, payloads, settle

RESULT_REQUIRED = {"run_id", "session_id", "state", "result_digest", "result_chars", "finished_at"}
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def test_result_digest_is_digest_not_fulltext():
    """A4: the payload carries the sha-256 fingerprint and the length — the
    5000-char text itself must not appear under ANY key (脱敏)."""
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(parent_session_id=session.id, label="writer")
        await manager.set_running(run.run_id)
        secret = "SENSIBLE-RESULT-" + "x" * 5000 + "-END"
        finished = await manager.finish(run.run_id, "completed", result_text=secret)
        await settle()

        results = payloads(sink, "subagent.result")
        assert len(results) == 1
        payload = results[0]
        assert RESULT_REQUIRED <= set(payload)
        assert payload["state"] == "completed"
        assert payload["result_digest"] == hashlib.sha256(secret.encode("utf-8")).hexdigest()
        assert len(payload["result_digest"]) == 64
        assert payload["result_chars"] == len(secret)
        assert isinstance(payload["finished_at"], int)
        # The desensitization red line: no key anywhere in the payload carries
        # the text — not under result_digest, not under a friendly alias.
        for key, value in payload.items():
            assert "SENSIBLE-RESULT" not in str(value), f"leaked via {key!r}"
        assert set(payload) == RESULT_REQUIRED | set()  # no extra keys either
        # The record stores the fingerprint, never the text.
        assert finished.result_digest == payload["result_digest"]
        assert finished.result_chars == len(secret)
        # The manager exposes no accessor that would hand back the text.
        assert not hasattr(manager, "result_text")

    asyncio.run(scenario())


def test_failed_result_requires_error_code():
    """A6: `failed` without a C-04 code is refused; with one it carries it."""
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(parent_session_id=session.id, label="flaky")
        await manager.set_running(run.run_id)
        with pytest.raises(VadeError) as exc:
            await manager.finish(run.run_id, "failed")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data.get("field") == "error_code"
        # A code that is not a C-04 name is refused too.
        with pytest.raises(VadeError) as exc:
            await manager.finish(run.run_id, "failed", error_code="NOT_A_C04_CODE")
        assert exc.value.code == "INVALID_ARGUMENT"

        finished = await manager.finish(run.run_id, "failed", error_code="TIMEOUT")
        await settle()
        assert finished.state is SubagentState.FAILED
        assert finished.error_code == "TIMEOUT"
        results = payloads(sink, "subagent.result")
        assert len(results) == 1
        assert results[0]["error_code"] == "TIMEOUT"
        assert results[0]["state"] == "failed"

    asyncio.run(scenario())


def test_failed_and_cancelled_digest_the_empty_result():
    """A6 + §10 Q3: no result text → sha256("") — terminal digest is never null,
    so client verification has exactly one rule."""
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(parent_session_id=session.id, label="dies")
        await manager.set_running(run.run_id)
        await manager.finish(run.run_id, "failed", error_code="INTERNAL")
        swept = await manager.spawn(parent_session_id=session.id, label="swept")
        # direct cancel via finish (the parent-turn sweep path is
        # test_parent_turn_cancel.py's job)
        await manager.finish(swept.run_id, "cancelled")
        await settle()

        results = payloads(sink, "subagent.result")
        by_run = {r["run_id"]: r for r in results}
        assert by_run[run.run_id]["result_digest"] == EMPTY_DIGEST
        assert by_run[run.run_id]["result_chars"] == 0
        assert by_run[swept.run_id]["result_digest"] == EMPTY_DIGEST
        assert by_run[swept.run_id]["result_chars"] == 0

    asyncio.run(scenario())


def test_result_is_terminal_idempotent():
    """A5: a second finish (retry, late worker) returns the record unchanged
    and emits NO second `subagent.result`."""
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(parent_session_id=session.id, label="once")
        await manager.set_running(run.run_id)
        first = await manager.finish(run.run_id, "completed", result_text="v1")
        again = await manager.finish(run.run_id, "completed", result_text="v2-ignored")
        await settle()

        assert again is first
        results = payloads(sink, "subagent.result")
        assert len(results) == 1
        assert results[0]["result_digest"] == hashlib.sha256(b"v1").hexdigest()
        assert manager.require(run.run_id).finished_at == first.finished_at
        # A different terminal target on the same terminal run is refused.
        from vade.errors import VadeError as _VE

        try:
            await manager.finish(run.run_id, "failed", error_code="INTERNAL")
            raise AssertionError("different terminal target must be refused")
        except _VE:
            pass
        assert len(payloads(sink, "subagent.result")) == 1

    asyncio.run(scenario())


def test_illegal_result_transition_spawned_to_completed():
    """A5: `spawned → completed` must be refused — a run that never started
    its loop has no result. The gate rejects; nothing is written or emitted."""
    async def scenario():
        log = make_log()
        session = log.create_session()
        sink = await attach(log, session.id)
        manager = make_manager(log)

        run = await manager.spawn(parent_session_id=session.id, label="lazy")
        with pytest.raises(VadeError) as exc:
            await manager.finish(run.run_id, "completed", result_text="ghost result")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data.get("from") == "spawned"
        assert exc.value.data.get("to") == "completed"

        await settle()
        assert payloads(sink, "subagent.result") == []
        record = manager.require(run.run_id)
        assert record.state is SubagentState.SPAWNED
        assert record.result_digest is None and record.finished_at is None

    asyncio.run(scenario())
