"""RFC-0006 §5 (A5/A6): the fields a tool card needs, and what they may leak.

`args_digest` is asserted unchanged on purpose: the preview is a display
convenience, the digest is the audit/approval anchor, and a preview that fed
back into the digest would silently break every approval comparison.
"""

from __future__ import annotations

import asyncio
import json
import re

from vade.loop.engine import LoopConfig, LoopEngine
from vade.loop.toolcard import PREVIEW_MAX_BYTES, SECRET_PATTERNS
from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.router.dispatch import ToolCallResult, digest_args
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

from ..loop.test_engine import FakeSnapshot, _entry, _turn


class Router:
    def __init__(self, result):
        self.calls = []
        self.result = result

    async def call(self, tool_id, args, *, session_id, turn_id=None, call_id=None, deadline_ms=None):
        self.calls.append((tool_id, dict(args), call_id))
        return self.result


class Recorder:
    def __init__(self):
        self.events = []

    async def __call__(self, session_id, event_type, payload=None, messages=()):
        self.events.append((event_type, dict(payload or {}), list(messages)))

    def payloads(self, event_type):
        return [payload for name, payload, _ in self.events if name == event_type]


def _llm(args: str):
    return MockLLMProvider([
        [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_file",
                                        arguments=args),))],
        [Delta(text="done")],
    ])


def _run(args: str, result, *, risk="low", snapshot=None):
    async def scenario():
        recorder = Recorder()
        factory = snapshot or (lambda: FakeSnapshot([_entry("fs__read_file", "com.example.fs/read_file",
                                                            risk=risk)]))
        engine = LoopEngine(Router(result), _llm(args), factory, recorder)
        await engine.run_turn(_turn(), ())
        return recorder

    return asyncio.run(scenario())


def test_tool_call_carries_digest_preview_time_and_batch():
    args = {"path": "a.txt", "password": "hunter2", "api_key": "sk-" + "A" * 24}
    recorder = _run(json.dumps(args), ToolCallResult(True, "com.example.fs/read_file",
                                                     result={"content": "hi"}, duration_ms=42))
    call = recorder.payloads("turn.tool_call")[0]

    # The digest is computed from the ORIGINAL args, exactly as before §5.
    assert call["args_digest"] == digest_args(args)
    assert call["args_digest"] == digest_args({"path": "a.txt", "password": "hunter2",
                                               "api_key": "sk-" + "A" * 24})
    assert call["args_preview_mode"] == "redacted"
    assert call["args_preview_redacted"] == ["password", "api_key"]
    assert "hunter2" not in call["args_preview"]
    assert "sk-" not in call["args_preview"]
    assert len(call["args_preview"].encode("utf-8")) <= PREVIEW_MAX_BYTES
    assert json.loads(call["args_preview"])["path"] == "a.txt"
    assert isinstance(call["started_at"], int)
    assert re.fullmatch(r"bat_[0-9a-f]{16}", call["batch_id"])


def test_one_batch_id_per_step_and_started_at_is_monotonic():
    """A5: two calls in one assistant step share `batch_id`, in call order."""

    def llm():
        return MockLLMProvider([
            [Delta(tool_calls=(
                ToolCallDelta(index=0, id="c1", name="fs__read_file", arguments='{"path":"a"}'),
                ToolCallDelta(index=1, id="c2", name="fs__read_file", arguments='{"path":"b"}'),
            ))],
            [Delta(text="done")],
        ])

    class TwoRouter:
        def __init__(self):
            self.calls = []

        async def call(self, tool_id, args, *, session_id, turn_id=None, call_id=None,
                       deadline_ms=None):
            self.calls.append((tool_id, dict(args), call_id))
            return ToolCallResult(True, tool_id, result={"content": args["path"]}, duration_ms=7)

    async def scenario():
        recorder = Recorder()
        engine = LoopEngine(TwoRouter(), llm(),
                            lambda: FakeSnapshot([_entry("fs__read_file",
                                                         "com.example.fs/read_file")]),
                            recorder)
        await engine.run_turn(_turn(), ())
        return recorder

    recorder = asyncio.run(scenario())
    calls = recorder.payloads("turn.tool_call")
    assert [c["call_id"] for c in calls] == ["c1", "c2"]
    assert calls[0]["batch_id"] == calls[1]["batch_id"]
    assert calls[0]["started_at"] <= calls[1]["started_at"]
    for call in calls:
        assert len(call["args_preview"].encode("utf-8")) <= PREVIEW_MAX_BYTES


def test_tool_result_reports_duration_truncation_and_a_resolvable_ref(tmp_path):
    long_result = {"content": "x" * 600}

    async def scenario():
        log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer())
        engine = LoopEngine(
            Router(ToolCallResult(True, "com.example.fs/read_file", result=long_result,
                                  duration_ms=42)),
            _llm('{"path":"a.txt"}'),
            lambda: FakeSnapshot([_entry("fs__read_file", "com.example.fs/read_file")]),
            log.emit,
            config=LoopConfig(max_steps=2),
        )
        log.set_executor(engine.run_turn)
        sess = log.create_session()
        await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        await log.wait_idle(sess.id)

        results = [e for e in log.store.read_since(sess.id, 0) if e.type == "turn.tool_result"]
        assert len(results) == 1
        payload = results[0].payload
        assert payload["duration_ms"] == 42
        assert payload["truncated"] is True          # 600 chars > the 300 window
        assert payload["summary"].endswith("x" * 10)
        assert payload["result_ref"].startswith("msg:")

        # A5: the pointer resolves to a role=tool message in the snapshot.
        message_id = payload["result_ref"].split("msg:", 1)[1]
        message = next(m for m in log.snapshot(sess.id)["messages"] if m["id"] == message_id)
        assert message["role"] == "tool"
        assert message["content"].startswith('{"content"')

    asyncio.run(scenario())


def test_a_tool_that_never_ran_reports_null_duration():
    recorder = _run('{"path":"a.txt"}', ToolCallResult(True, "x", result={}),
                    snapshot=lambda: FakeSnapshot([_entry("fs__other", "com.example.fs/other")]))
    result = recorder.payloads("turn.tool_result")[0]
    assert result["ok"] is False
    assert result["duration_ms"] is None      # never `0`, so no `—ms` placeholder
    assert result["truncated"] is False
    assert result["result_ref"].startswith("msg:")


def test_the_preview_rescan_finds_no_secret():
    """A6: the finished preview must not match any step-3 pattern again."""
    secrets = {
        "password": "hunter2",
        "token": "Bearer abcdef123456",
        "api_key": "sk-" + "B" * 20,
        "note": "ghp_" + "c" * 24,
        "blob": "AKIA" + "D" * 16,
        "pem": "-----BEGIN RSA PRIVATE KEY-----",
        "checksum": "deadbeef" * 8,
    }
    recorder = _run(json.dumps(secrets), ToolCallResult(True, "x", result={}))
    preview = recorder.payloads("turn.tool_call")[0]["args_preview"]
    for pattern in SECRET_PATTERNS:
        assert pattern.search(preview) is None, f"{pattern.pattern} leaked into {preview}"


def test_high_risk_previews_the_shape_only():
    """§5.2 step 6: risk=high → every string value is `<str:N>`, keys survive."""
    args = {"path": "secret/path.txt", "password": "hunter2", "count": 3, "dry": True}
    recorder = _run(json.dumps(args), ToolCallResult(True, "x", result={}), risk="high")
    call = recorder.payloads("turn.tool_call")[0]
    assert call["args_preview_mode"] == "typed"
    body = json.loads(call["args_preview"])
    assert body["path"] == "<str:15>"
    assert body["count"] == 3
    assert body["dry"] is True
    assert body["***"] == "***"          # the secret key name itself is masked
    assert "hunter2" not in call["args_preview"]
    assert "secret/path.txt" not in call["args_preview"]
