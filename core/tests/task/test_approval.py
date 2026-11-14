"""RFC-0007 A9/A10: approval attribution and the no-inheritance rule."""

from __future__ import annotations

import asyncio

from vade.loop.ports import ApprovalRequest
from vade.policy.approval import ApprovalManager
from vade.policy.engine import PolicyEngine

NO_RACE = 30.0  # same reasoning as core/tests/policy/test_approval.py


def _manager() -> ApprovalManager:
    return ApprovalManager(
        timeout_s=NO_RACE, engine=PolicyEngine(), audit=None, approver_probe=lambda: True
    )


def _request(recorder, *, depth: int, task_id: str | None, task_name: str | None) -> ApprovalRequest:
    async def emit(event_type, payload):
        recorder.append((event_type, dict(payload)))

    return ApprovalRequest(
        session_id="parent-session", turn_id="child-turn",
        tool_id="com.example.fs/write_file", args={"path": "a.txt"}, risk="high",
        expires_at=0, emit=emit, task_id=task_id, task_name=task_name, depth=depth,
    )


async def _pending(manager: ApprovalManager, within: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while manager.pending_count < 1:
        if loop.time() > deadline:  # pragma: no cover - only on a real hang
            raise AssertionError("approval was never queued")
        await asyncio.sleep(0)


def test_subagent_prompt_carries_task_id_name_depth_and_session():
    async def scenario():
        manager = _manager()
        seen: list[tuple[str, dict]] = []
        run = asyncio.create_task(
            manager.request(_request(seen, depth=2, task_id="task-1", task_name="writer"))
        )
        await _pending(manager)

        prompt = next(payload for kind, payload in seen if kind == "turn.approval_required")
        assert prompt["task_id"] == "task-1"
        assert prompt["task_name"] == "writer"
        assert prompt["depth"] == 2
        assert prompt["session_id"] == "parent-session", "the prompt rides the parent stream"
        assert prompt["turn_id"] == "child-turn"

        pending = manager.pending_requests()[0]
        assert pending["task_id"] == "task-1" and pending["depth"] == 2
        ok, resolved = await manager.decide(
            pending["request_id"], "approve", "once", by="dev-1", actor_kind="device_key"
        )
        decision = await run
        assert ok and decision.allowed
        assert resolved["task_id"] == "task-1" and resolved["depth"] == 2
        closed = next(payload for kind, payload in seen if kind == "turn.approval_resolved")
        assert closed["task_id"] == "task-1"
        assert closed["request_id"] == pending["request_id"]

    asyncio.run(scenario())


def test_main_turn_prompt_has_null_task_id():
    async def scenario():
        manager = _manager()
        seen: list[tuple[str, dict]] = []
        run = asyncio.create_task(
            manager.request(_request(seen, depth=0, task_id=None, task_name=None))
        )
        await _pending(manager)
        prompt = next(payload for kind, payload in seen if kind == "turn.approval_required")
        assert prompt["task_id"] is None
        assert prompt["task_name"] is None
        assert prompt["depth"] == 0
        await manager.decide(
            manager.pending_requests()[0]["request_id"], "deny", "once",
            by="dev-1", actor_kind="device_key",
        )
        await run

    asyncio.run(scenario())


def test_snapshot_pending_approvals_carry_the_task_id():
    """§5.6: a reconnecting UI must know which task owns each open prompt."""

    async def scenario():
        from vade.session import _LazyApproval
        from .helpers import make_log

        manager = _manager()
        seen: list[tuple[str, dict]] = []
        run = asyncio.create_task(
            manager.request(_request(seen, depth=1, task_id="task-9", task_name="writer"))
        )
        await _pending(manager)

        class App:
            approval_port = manager

        log = make_log()
        session = log.create_session(session_id="parent-session")
        log.approval_port = _LazyApproval(App())
        rows = log.snapshot(session.id)["pending_approvals"]
        assert len(rows) == 1
        assert rows[0]["task_id"] == "task-9"
        assert rows[0]["depth"] == 1

        await manager.decide(
            rows[0]["request_id"], "deny", "once", by="dev-1", actor_kind="device_key"
        )
        await run

    asyncio.run(scenario())


def test_session_scope_grant_is_not_inherited_by_subagents():
    """§7.2 rule 4: 'allow one write' must not become 'any spawned write'."""

    async def scenario():
        manager = _manager()
        seen: list[tuple[str, dict]] = []

        async def ask(depth: int, task_id: str | None) -> str:
            run = asyncio.create_task(
                manager.request(_request(seen, depth=depth, task_id=task_id, task_name="sub" if task_id else None))
            )
            await _pending(manager)
            request_id = manager.pending_requests()[-1]["request_id"]
            await manager.decide(
                request_id, "approve", "session", by="dev-1", actor_kind="device_key"
            )
            await run
            return request_id

        first = await ask(0, None)
        assert len(manager.grants()) == 1, "a main-turn grant is stored"

        prompts_before = sum(1 for kind, _ in seen if kind == "turn.approval_required")
        second = await ask(1, "sub-1")
        prompts_after = sum(1 for kind, _ in seen if kind == "turn.approval_required")
        assert second != first, "the subagent must be asked again with a new request_id"
        assert prompts_after == prompts_before + 1, "a subagent must produce its own prompt"
        assert len(manager.grants()) == 1, "a subagent's approval grants nothing"

        # The main turn still hits the cached grant: no third prompt.
        cached = await manager.request(_request(seen, depth=0, task_id=None, task_name=None))
        assert cached.allowed and cached.cached is True
        assert sum(1 for kind, _ in seen if kind == "turn.approval_required") == prompts_after

    asyncio.run(scenario())
