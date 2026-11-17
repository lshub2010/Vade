"""The approval channel at a turn's end: close once, close for real.

Two defects, one contract: `turn.approval_resolved` is the ONLY signal that
closes an approval dialog on another device, so a turn that stops must publish
exactly one per still-open request and must leave nothing behind in the pending
table — `pending_requests()` / `snapshot.pending_approvals[]` are what a
reconnecting client renders, and an entry nobody can answer re-opens a dead
dialog.

These tests use the real objects the app wires together (`ApprovalManager` →
`PolicyAdapter` → the session's `_LazyApproval` → `SessionLog`), not a fake
port: the original P0 was a missing forwarder on the adapter, which no
fake-port test could see.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vade.errors import VadeError
from vade.loop.ports import ApprovalRequest
from vade.policy.adapter import PolicyAdapter
from vade.policy.approval import ApprovalManager
from vade.policy.engine import PolicyEngine
from vade.session import _LazyApproval
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore

from .helpers import GateExecutor, wait_until

TOOL = "com.example.fs/write_file"
#: Long enough that a stalled box cannot time the request out mid-test.
NO_RACE = 30.0


class _App:
    def __init__(self, port: Any) -> None:
        self.approval_port = port


def _stack(tmp_path, executor, *, db: str = "approval.db"):
    """The production wiring: manager ← adapter ← lazy port ← session log."""
    manager = ApprovalManager(
        timeout_s=NO_RACE, engine=PolicyEngine(), approver_probe=lambda: True
    )
    adapter = PolicyAdapter(manager)
    log = SessionLog(SQLiteStore(tmp_path / db), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    log.approval_port = _LazyApproval(_App(adapter))
    return manager, adapter, log


def _events(log: SessionLog, session_id: str) -> list[Any]:
    return log.store.read_since(session_id, 0)


def _resolved(log: SessionLog, session_id: str) -> list[Any]:
    return [e for e in _events(log, session_id) if e.type == "turn.approval_resolved"]


class _AwaitingExecutor:
    """A turn blocked inside the approval channel, like `LoopEngine`."""

    def __init__(self, log: SessionLog, port: PolicyAdapter) -> None:
        self.log = log
        self.port = port
        self.decision = None

    async def __call__(self, turn: Any, content: Any) -> Any:
        from vade.loop.engine import TurnOutcome

        async def emit(kind: str, payload: Any) -> None:
            await self.log.emit(turn.session_id, kind, payload)

        self.decision = await self.port.check(ApprovalRequest(
            session_id=turn.session_id, turn_id=turn.id, tool_id=TOOL,
            args={"path": "notes.md"}, risk="high", expires_at=0, emit=emit,
        ))
        return TurnOutcome(assistant_text="done", stopped_reason="stop")


def test_a_minted_approval_is_listed_closed_once_and_dequeued(tmp_path):
    """RFC-0009 §6.2: the page path mints without a waiter; the sweep owns it.

    `still_pending == 1` after the interrupt is the bug: the deny was emitted
    (so the dialog did close) but the row stayed in the table forever, and the
    next snapshot re-opened the card.
    """

    async def scenario():
        executor = GateExecutor()
        manager, adapter, log = _stack(tmp_path, executor)
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "purge"}], "c1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id)

        pending, reason = await adapter.open_request(ApprovalRequest(
            session_id=session.id, turn_id=turn.id, tool_id=TOOL,
            args={"path": "notes.md"}, risk="high", expires_at=0,
            emit=None,                      # §9.4: a page approval is a sync RPC
            task_id="task-7", task_name="writer", depth=1,
        ))
        assert reason == "approval_required" and pending is not None
        assert manager.pending_count == 1
        # P0-1: the adapter must answer the session layer's question. Without
        # `pending`/`pending_requests` this list was always empty.
        [row] = log.snapshot(session.id)["pending_approvals"]
        assert row["request_id"] == pending.request_id
        assert row["tool"] == TOOL and row["risk"] == "high"
        assert row["task_id"] == "task-7" and row["depth"] == 1

        assert await log.interrupt(session.id, turn.id) is True
        await log.wait_idle(session.id)

        resolved = _resolved(log, session.id)
        assert len(resolved) == 1, "a stopped turn closes its dialog exactly once"
        payload = resolved[0].payload
        assert payload["request_id"] == pending.request_id
        assert payload["decision"] == "deny" and payload["by"] == "system"
        assert payload["state"] == "interrupted" and payload["scope"] == "once"
        assert payload["session_id"] == session.id and payload["turn_id"] == turn.id
        assert payload["task_id"] == "task-7" and payload["task_name"] == "writer"
        assert payload["depth"] == 1
        # Order is contractual: close the dialog, THEN end the turn.
        types = [e.type for e in _events(log, session.id)]
        assert types.index("turn.approval_resolved") < types.index("turn.interrupted")

        # P0-1's other half: nothing left to advertise, and a late approval is
        # refused as already resolved instead of reopening a closed turn.
        assert manager.pending_count == 0
        assert manager.pending_requests() == []
        assert log.snapshot(session.id)["pending_approvals"] == []
        with pytest.raises(VadeError) as caught:
            await adapter.decide(pending.request_id, "approve", by="dev-1",
                                 actor_kind="device_key")
        assert caught.value.code == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_an_awaited_approval_is_closed_once_after_the_interrupt(tmp_path):
    """The blocking path loses its row to CancelledError; the sweep must still
    announce it — otherwise the cancel closed the table entry and nobody ever
    told the other devices (the card stayed up forever)."""

    async def scenario():
        executor = _AwaitingExecutor(None, None)  # log/port bound below
        manager, adapter, log = _stack(tmp_path, executor, db="waited.db")
        executor.log, executor.port = log, adapter
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "purge"}], "c1")
        assert await wait_until(lambda: manager.pending_count == 1)
        request_id = manager.pending_requests()[0]["request_id"]

        assert await log.interrupt(session.id, turn.id) is True
        await log.wait_idle(session.id)

        resolved = _resolved(log, session.id)
        assert len(resolved) == 1
        payload = resolved[0].payload
        assert payload["decision"] == "deny" and payload["by"] == "system"
        assert payload["state"] == "interrupted"
        assert payload["request_id"] == request_id
        assert manager.pending_count == 0
        assert log.snapshot(session.id)["pending_approvals"] == []
        # The waiter itself was cancelled mid-`check` — that is exactly why the
        # manager has to hand the row to the sweep instead of finishing it —
        # so the turn ends `interrupted`, never `failed`.
        types = [e.type for e in _events(log, session.id)]
        assert "turn.interrupted" in types and "turn.failed" not in types

    asyncio.run(scenario())


class _Connection:
    auth_mode = "device_key"
    device_id = "dev-1"


def test_approval_decide_publishes_one_resolved_event_in_approve_vocabulary(tmp_path):
    """allow used to be emitted twice: `allow` from the manager, `approve` from
    `approval/decide`. One emission point, one vocabulary (schema/events.json)."""

    async def scenario():
        executor = _AwaitingExecutor(None, None)
        manager, adapter, log = _stack(tmp_path, executor, db="decide.db")
        executor.log, executor.port = log, adapter
        rpc = SessionRPC(log, approval=lambda: adapter)
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "purge"}], "c1")
        assert await wait_until(lambda: manager.pending_count == 1)
        request_id = manager.pending_requests()[0]["request_id"]

        reply = await rpc.approval_decide(_Connection(), {
            "request_id": request_id, "decision": "approve", "scope": "once",
        })
        assert reply == {"ok": True}
        await log.wait_idle(session.id)

        resolved = _resolved(log, session.id)
        assert len(resolved) == 1, "the dialog closes once, not twice"
        payload = resolved[0].payload
        assert payload["decision"] == "approve"
        assert payload["request_id"] == request_id
        assert payload["by"] == "dev-1" and payload["scope"] == "once"
        assert payload["session_id"] == session.id and payload["turn_id"] == turn.id
        assert payload["state"] == "running"
        assert manager.pending_count == 0
        assert executor.decision is not None and executor.decision.allowed is True

    asyncio.run(scenario())
