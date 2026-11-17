"""Approval authority, session scope and duplicate decisions.

Timing note: nothing here sleeps for a fixed handful of milliseconds and then
asserts. A fixed sleep made the decision race the manager's *real* deadline —
on a stalled box the 1s timeout fired between `create_task` and `decide`, the
future was cancelled under the entry, and the test failed with
`INVALID_ARGUMENT: approval request already resolved`. Tests now (a) wait for
the request to be queued, and (b) give decision-driven managers a deadline far
beyond the test, shrinking `timeout_s` only where a timeout is the assertion.
"""

from __future__ import annotations

import asyncio

import pytest

from vade.audit.reader import AuditReader
from vade.audit.writer import AuditWriter
from vade.errors import VadeError
from vade.loop.ports import ApprovalRequest
from vade.policy.approval import ApprovalManager, GENERIC_EXECUTORS
from vade.policy.engine import PolicyEngine

#: Long enough that a stalled event loop cannot trip it mid-test, short enough
#: that a genuinely stuck request still fails the suite in seconds.
NO_RACE = 30.0


def _request(tool_id="com.example.fs/read_file", args=None, risk="high", session_id="s1"):
    async def emit(event_type, payload):
        return None

    return ApprovalRequest(
        session_id=session_id, turn_id="t1", tool_id=tool_id,
        args=args or {"path": "a"}, risk=risk, expires_at=0, emit=emit,
    )


def _manager(tmp_path, timeout_s=NO_RACE, probe=None):
    writer = AuditWriter(tmp_path / "audit" / "audit.db", sync=True)
    return ApprovalManager(timeout_s=timeout_s, engine=PolicyEngine(), audit=writer,
                           approver_probe=probe), writer


async def _pending(manager, count=1, within=10.0):
    """Wait until `count` approvals are queued; no wall-clock guesswork."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while manager.pending_count < count:
        if loop.time() > deadline:
            raise AssertionError(
                f"expected {count} pending approval(s), saw {manager.pending_count}"
            )
        await asyncio.sleep(0)
    return manager.pending_requests()


def test_low_and_medium_risk_do_not_prompt(tmp_path):
    async def scenario():
        manager, writer = _manager(tmp_path)
        low = await manager.request(_request(risk="low"))
        medium = await manager.request(_request(risk="medium"))
        assert low.allowed and low.reason == "below_high"
        assert medium.allowed
        writer.close()

    asyncio.run(scenario())


def test_a_manifest_cannot_downgrade_a_high_floor_tool():
    engine = PolicyEngine({"write_file": "low"})
    assert engine.risk_of("com.example.fs/write_file", "low") == "high"
    assert engine.risk_of("com.example.fs/shell", "medium") == "high"
    # An override may raise, never lower: high -> medium is refused.
    engine.set_override("com.example.fs/read_file", "high")
    with pytest.raises(VadeError) as excinfo:
        engine.set_override("com.example.fs/read_file", "medium")
    assert excinfo.value.code == "RISK_DOWNGRADE_FORBIDDEN"


def test_browser_session_cannot_approve_a_high_risk_call(tmp_path):
    async def scenario():
        manager, writer = _manager(tmp_path)
        task = asyncio.create_task(manager.request(_request()))
        [pending] = await _pending(manager)
        with pytest.raises(VadeError) as excinfo:
            await manager.decide(pending["request_id"], "allow", by="browser", actor_kind="browser_session")
        assert excinfo.value.code == "APPROVER_INVALID"
        # The request is still open: a refused approver must not consume it.
        assert manager.pending_requests()[0]["request_id"] == pending["request_id"]
        ok, resolved = await manager.decide(pending["request_id"], "allow", by="dev-1",
                                            actor_kind="device_key")
        decision = await asyncio.wait_for(task, 10)
        assert ok and decision.allowed and decision.by == "dev-1"
        assert resolved["decision"] == "approve", "wire vocabulary is approve|deny"
        rows = AuditReader(writer.path).query(kind="approval")["items"]
        # Newest first in the audit page, so read the two rows oldest-first.
        assert [r["status"] for r in reversed(rows)] == ["denied", "allowed"]
        assert rows[1]["code"] == "APPROVER_INVALID"
        assert rows[0]["actor"] == "dev-1"
        writer.close()

    asyncio.run(scenario())


def test_an_unspecified_approver_is_refused(tmp_path):
    """B-3: the default is fail-closed, not the permissive `device_key`.

    C3's `approval/decide` handler is the only caller that can forget to say who
    is approving. If it does, the request must stay pending and answer
    `APPROVER_INVALID` rather than silently approving a high-risk call.
    """

    async def scenario():
        manager, writer = _manager(tmp_path)
        task = asyncio.create_task(manager.request(_request()))
        [pending] = await _pending(manager)
        with pytest.raises(VadeError) as excinfo:
            await manager.decide(pending["request_id"], "allow", by="forgetful")
        assert excinfo.value.code == "APPROVER_INVALID"
        # Refused, not consumed: a real approver can still resolve it.
        assert manager.pending_requests()[0]["request_id"] == pending["request_id"]
        ok, _ = await manager.decide(pending["request_id"], "allow", by="dev-1",
                                     actor_kind="device_key")
        assert ok and (await asyncio.wait_for(task, 10)).allowed
        writer.close()

    asyncio.run(scenario())


def test_the_adapter_does_not_default_the_approver_kind(tmp_path):
    """The C3 seam (`PolicyAdapter`) must not re-introduce a permissive default."""
    from vade.policy.adapter import PolicyAdapter

    async def scenario():
        manager, writer = _manager(tmp_path)
        adapter = PolicyAdapter(manager)
        task = asyncio.create_task(manager.request(_request()))
        [pending] = await _pending(manager)
        with pytest.raises(VadeError) as excinfo:
            await adapter.decide(pending["request_id"], "allow", by="forgetful")
        assert excinfo.value.code == "APPROVER_INVALID"
        ok, _ = await adapter.decide(pending["request_id"], "allow", by="dev-1",
                                    actor_kind="device_key")
        assert ok and (await asyncio.wait_for(task, 10)).allowed
        writer.close()

    asyncio.run(scenario())


def test_a_timed_out_request_leaves_nothing_pending(tmp_path):
    """Contract: a timeout retires the request in the same lock that decide() takes.

    The stranded-entry race itself is timing dependent (it lives in the window
    where `wait_for` cancelled the future before the handler ran); this pins the
    observable contract — nothing left to advertise, and a retired id answers a
    clean `already resolved` rather than `NOT_FOUND` or a silent success.
    """

    async def scenario():
        manager, writer = _manager(tmp_path, timeout_s=0.02)
        decision = await asyncio.wait_for(manager.request(_request()), timeout=10.0)
        assert decision.allowed is False and "审批超时" in decision.reason
        request_id = decision.request_id
        assert manager.pending_count == 0 and manager.pending_requests() == []
        with pytest.raises(VadeError) as excinfo:
            await manager.decide(request_id, "allow", by="dev-1", actor_kind="device_key")
        assert excinfo.value.code == "INVALID_ARGUMENT"
        assert "already resolved" in excinfo.value.message
        writer.close()

    asyncio.run(scenario())


def test_session_scope_is_keyed_by_arguments_not_just_the_tool(tmp_path):
    async def scenario():
        manager, writer = _manager(tmp_path)
        first = asyncio.create_task(manager.request(_request(args={"path": "a"})))
        [pending] = await _pending(manager)
        await manager.decide(pending["request_id"], "allow", scope="session", by="dev-1", actor_kind="device_key")
        assert (await asyncio.wait_for(first, 10)).allowed

        cached = await manager.request(_request(args={"path": "a"}))
        assert cached.allowed and cached.cached is True

        # Different arguments must not inherit the grant: `rm -rf /` after `ls`.
        # Only this negative case waits on the deadline, so shrink it here
        # rather than making every step of the test race 50ms.
        manager.timeout_s = 0.05
        other = await manager.request(_request(args={"path": "b"}))
        assert other.allowed is False
        assert "审批超时" in other.reason
        manager.timeout_s = NO_RACE
        writer.close()

    asyncio.run(scenario())


def test_generic_executors_are_once_only(tmp_path):
    async def scenario():
        manager, writer = _manager(tmp_path)
        task = asyncio.create_task(manager.request(_request(tool_id="com.example.shell/shell", args={"cmd": "ls"})))
        [pending] = await _pending(manager)
        with pytest.raises(VadeError) as excinfo:
            await manager.decide(pending["request_id"], "allow", scope="session", by="dev-1", actor_kind="device_key")
        assert excinfo.value.code == "INVALID_ARGUMENT"
        assert "shell" in GENERIC_EXECUTORS
        await manager.decide(pending["request_id"], "allow", scope="once", by="dev-1", actor_kind="device_key")
        assert (await asyncio.wait_for(task, 10)).allowed
        writer.close()

    asyncio.run(scenario())


def test_a_second_decision_is_rejected(tmp_path):
    async def scenario():
        manager, writer = _manager(tmp_path)
        task = asyncio.create_task(manager.request(_request()))
        [pending] = await _pending(manager)
        await manager.decide(pending["request_id"], "deny", by="dev-1", actor_kind="device_key")
        with pytest.raises(VadeError) as excinfo:
            await manager.decide(pending["request_id"], "allow", by="dev-1", actor_kind="device_key")
        assert excinfo.value.code == "INVALID_ARGUMENT"
        assert "already resolved" in excinfo.value.message
        decision = await asyncio.wait_for(task, 10)
        assert decision.allowed is False and decision.reason == "denied by approver"
        writer.close()

    asyncio.run(scenario())


def test_no_attached_ui_denies_immediately_instead_of_waiting(tmp_path):
    async def scenario():
        manager, writer = _manager(tmp_path, timeout_s=5.0, probe=lambda: False)
        decision = await asyncio.wait_for(manager.request(_request()), timeout=1.0)
        assert decision.allowed is False
        assert decision.reason == "no_approver"
        rows = AuditReader(writer.path).query(kind="approval")["items"]
        assert rows[0]["status"] == "denied" and rows[0]["duration_ms"] < 1000
        writer.close()

    asyncio.run(scenario())


def test_session_end_drops_grants_and_unblocks_waiters(tmp_path):
    async def scenario():
        manager, writer = _manager(tmp_path)
        task = asyncio.create_task(manager.request(_request()))
        await _pending(manager)
        await manager.on_session_end("s1")
        decision = await asyncio.wait_for(task, timeout=10.0)
        assert decision.allowed is False and decision.reason == "session_ended"
        assert manager.grants() == {}
        writer.close()

    asyncio.run(scenario())
