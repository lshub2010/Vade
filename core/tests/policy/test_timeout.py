"""High-risk tools must be denied by silence (31-C5a §六: 审批超时 → 拒绝).

The clock is injected / the timeout is shrunk to milliseconds: the test suite
never waits 60 real seconds.
"""

from __future__ import annotations

import asyncio

from vade.audit.reader import AuditReader
from vade.audit.writer import AuditWriter
from vade.loop.engine import LoopConfig, LoopEngine
from vade.loop.ports import ApprovalRequest
from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.policy.adapter import PolicyAdapter
from vade.policy.approval import ApprovalManager
from vade.policy.engine import PolicyEngine
from vade.router.dispatch import ToolCallResult
from vade.session.models import Turn


def _writer(tmp_path):
    writer = AuditWriter(tmp_path / "audit" / "audit.db", sync=True)
    return writer


def test_high_risk_without_a_decision_is_denied_at_the_deadline(tmp_path):
    async def scenario():
        writer = _writer(tmp_path)
        manager = ApprovalManager(timeout_s=0.05, engine=PolicyEngine(), audit=writer)
        events: list[tuple[str, dict]] = []

        async def emit(event_type, payload):
            events.append((event_type, dict(payload)))

        decision = await manager.request(ApprovalRequest(
            session_id="s1", turn_id="t1", tool_id="com.example.fs/write_file",
            args={"path": "/etc/passwd", "content": "x"}, risk="high",
            expires_at=0, emit=emit,
        ))

        assert decision.allowed is False
        assert "审批超时" in decision.reason
        assert decision.by == "system"
        # The UI saw the full arguments, not a digest.
        assert events[0][0] == "turn.approval_required"
        assert events[0][1]["args"] == {"path": "/etc/passwd", "content": "x"}
        assert events[0][1]["risk"] == "high"

        rows = AuditReader(writer.path).query(kind="approval_timeout")["items"]
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "denied"
        assert row["code"] == "PERMISSION_DENIED"
        assert row["actor"] == "system"
        assert row["request_id"] == decision.request_id
        assert row["args_digest"]
        assert row["args_redacted"] == {"path": "/etc/passwd", "content": "x"}
        assert row["duration_ms"] >= 45
        writer.close()

    asyncio.run(scenario())


def test_the_denial_reason_reaches_the_model_as_a_tool_result(tmp_path):
    """End-to-end through C3's engine: `ok=false` and the model is told why."""

    class Snapshot:
        def __init__(self):
            self._entry = type("Entry", (), {
                "llm_name": "fs__write_file", "tool_id": "com.example.fs/write_file",
                "risk": "high", "description": "d",
            })()

        def get(self, name):
            return self._entry if name == "fs__write_file" else None

        def names(self):
            return ("fs__write_file",)

        def schemas(self):
            return []

        def render_block(self):
            return "tools"

        def __bool__(self):
            return True

    class Router:
        def __init__(self):
            self.calls = []

        async def call(self, *args, **kwargs):
            self.calls.append(args)
            return ToolCallResult(True, "com.example.fs/write_file", result={})

    async def scenario():
        writer = _writer(tmp_path)
        manager = ApprovalManager(timeout_s=0.05, engine=PolicyEngine(), audit=writer)
        llm = MockLLMProvider([
            [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__write_file",
                                             arguments='{"path":"a"}'),))],
            [Delta(text="ok, skipped")],
        ])
        router = Router()
        engine = LoopEngine(router, llm, Snapshot, Recorder(), approval=PolicyAdapter(manager),
                            audit=writer, config=LoopConfig(approval_timeout_s=0.05))
        outcome = await engine.run_turn(Turn(id="t1", session_id="s1", user_text="write"), ())

        assert router.calls == []              # the tool never ran
        assert outcome.tool_calls[0]["ok"] is False
        tool_messages = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
        assert "审批超时" in tool_messages[0]["content"]
        assert "Tool call denied" in tool_messages[0]["content"]
        writer.close()

    asyncio.run(scenario())


class Recorder:
    def __init__(self):
        self.events = []

    async def __call__(self, session_id, event_type, payload=None, messages=()):
        self.events.append((event_type, dict(payload or {})))

    def types(self):
        return [e[0] for e in self.events]
