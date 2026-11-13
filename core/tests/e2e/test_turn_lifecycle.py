"""End-to-end: submit -> LLM -> tool -> reply, replay, interrupt without loss."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.loop.engine import LoopEngine
from vade.loop.tools import build_snapshot
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry
from vade.router.dispatch import Router
from vade.session.manager import SessionLog
from vade.session.models import TurnState
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore
from tests.session.helpers import make_manifest, wait_until


class FakeModule:
    def __init__(self):
        self.calls: list[dict] = []
        self.cancelled: list[str] = []

    async def call(self, target, method, params, deadline_ms):
        self.calls.append(dict(params))
        return {"ok": True, "result": {"path": params["args"].get("path"), "content": "hello"}}

    async def cancel(self, target, call_id):
        self.cancelled.append(call_id)


class FakeConnection:
    def __init__(self, connection_id: str, auth_mode: str = "device_key"):
        self.connection_id = connection_id
        self.auth_mode = auth_mode
        self.device_id = f"dev-{connection_id}"
        self.events: list[dict] = []

    async def send_event(self, frame):
        self.events.append(dict(frame["params"]))


def _registry() -> Registry:
    registry = Registry()
    registry.register(validate_manifest(make_manifest()), fingerprint="fp1", connection_id="mod-conn")
    return registry


def _build(tmp_path, llm_script, *, max_steps: int = 8):
    store = SQLiteStore(tmp_path / "sessions.db")
    log = SessionLog(store, replay=ReplayBuffer(maxlen=1000))
    registry = _registry()
    module = FakeModule()
    engine = LoopEngine(
        Router(module, registry), MockLLMProvider(llm_script),
        lambda: build_snapshot(registry), log.emit,
        history=lambda sid: (log.get_session(sid).messages if log.get_session(sid) else ()),
    )
    log.set_executor(engine.run_turn)
    return log, module, SessionRPC(log), registry


def test_full_turn_event_sequence_and_replay_chain(tmp_path):
    async def scenario():
        log, module, rpc, _ = _build(tmp_path, [
            [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_file",
                                             arguments='{"path":"a.txt"}'),))],
            [Delta(text="The file says hello.")],
        ])
        ui = FakeConnection("ui-1")
        attached = await rpc.dispatch(ui, "ui/attach", {"session_id": None, "last_seq": None})
        session_id = attached["session_id"]
        reply = await rpc.dispatch(ui, "ui/submit", {
            "session_id": session_id, "client_msg_id": "m1",
            "content": [{"type": "text", "text": "read a.txt"}],
        })
        await log.wait_idle(session_id)
        await asyncio.sleep(0.05)

        events = log.store.read_since(session_id, 0)
        seqs = [e.seq for e in events]
        assert seqs == list(range(1, len(seqs) + 1))
        types = [e.type for e in events]
        for expected in ("turn.queued", "turn.started", "turn.tool_call", "turn.tool_result",
                         "turn.delta", "turn.completed"):
            assert expected in types, f"missing {expected}: {types}"
        order = [types.index(t) for t in ("turn.queued", "turn.started", "turn.tool_call",
                                          "turn.tool_result", "turn.delta", "turn.completed")]
        assert order == sorted(order)
        assert module.calls[0]["ctx"] == {"session_id": session_id}
        completed = [e for e in events if e.type == "turn.completed"][0]
        assert completed.payload["state"] == "completed"
        assert completed.payload["stopped_reason"] == "stop"
        tool_call = [e for e in events if e.type == "turn.tool_call"][0]
        tool_result = [e for e in events if e.type == "turn.tool_result"][0]
        assert tool_call.payload["call_id"] == tool_result.payload["call_id"]
        assert tool_result.payload["ok"] is True
        messages = log.store.load(session_id).messages
        assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
        assert messages[0].content == "read a.txt"
        assert reply["turn_id"] == log.store.load(session_id).turns[0].id
        await log.close()

    asyncio.run(scenario())


def test_replay_from_a_restarted_core_reconstructs_the_call_chain(tmp_path):
    async def scenario():
        log, _, rpc, _ = _build(tmp_path, [
            [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_file",
                                             arguments='{"path":"a.txt"}'),))],
            [Delta(text="done")],
        ])
        session = log.create_session()
        await log.submit(session.id, [{"type": "text", "text": "read it"}], "m1")
        await log.wait_idle(session.id)
        await log.close()

        # New process, empty in-memory replay buffer, same SQLite file.
        store = SQLiteStore(tmp_path / "sessions.db")
        fresh = SessionLog(store, replay=ReplayBuffer(maxlen=1000))
        sink_events: list[dict] = []

        class Sink:
            async def send(self, event):
                sink_events.append(dict(event))

        result = await fresh.attach(session.id, 0, Sink())
        await asyncio.sleep(0.05)
        assert result.snapshot is None
        assert [e.seq for e in result.replay] == list(range(1, len(result.replay) + 1))
        types = [e.type for e in result.replay]
        assert "turn.tool_call" in types and "turn.tool_result" in types
        assert "turn.completed" in types
        assert [e["seq"] for e in sink_events] == list(range(1, len(sink_events) + 1))
        await fresh.close()

    asyncio.run(scenario())


def test_three_clients_see_the_same_seq_stream(tmp_path):
    async def scenario():
        log, _, rpc, _ = _build(tmp_path, [[Delta(text="ok")], [Delta(text="ok")], [Delta(text="ok")]])
        conns = [FakeConnection(f"ui-{i}") for i in range(3)]
        session_id = None
        for conn in conns:
            result = await rpc.dispatch(conn, "ui/attach", {"session_id": session_id, "last_seq": None})
            session_id = result["session_id"]
        for i, conn in enumerate(conns):
            await rpc.dispatch(conn, "ui/submit", {
                "session_id": session_id, "client_msg_id": f"m{i}",
                "content": [{"type": "text", "text": f"msg{i}"}],
            })
        await log.wait_idle(session_id)
        await asyncio.sleep(0.1)
        attach_seq = max(e["seq"] for c in conns for e in c.events
                         if e["type"] == "session.attached")
        baseline = [e["seq"] for e in conns[0].events if e["seq"] > attach_seq]
        assert baseline, "no live events captured"
        for conn in conns[1:]:
            assert [e["seq"] for e in conn.events if e["seq"] > attach_seq] == baseline
        assert baseline == list(range(attach_seq + 1, attach_seq + 1 + len(baseline)))
        user_order = [m.content for m in log.store.load(session_id).messages if m.role == "user"]
        assert user_order == ["msg0", "msg1", "msg2"]
        await log.close()

    asyncio.run(scenario())


def test_interrupt_then_next_turn_keeps_every_message(tmp_path):
    async def scenario():
        log, module, rpc, _ = _build(tmp_path, [
            [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_file",
                                             arguments='{"path":"a"}'),))],
            [Delta(text="done")],
        ])

        async def slow_call(target, method, params, deadline_ms):
            await asyncio.sleep(5)
            return {"ok": True, "result": {}}

        module.call = slow_call
        ui = FakeConnection("ui-1")
        session_id = (await rpc.dispatch(ui, "ui/attach", {"session_id": None}))["session_id"]
        first = await rpc.dispatch(ui, "ui/submit", {
            "session_id": session_id, "client_msg_id": "a",
            "content": [{"type": "text", "text": "first"}],
        })
        assert await wait_until(lambda: log.queue_state(session_id)["running"] == first["turn_id"], 1)
        began = time.monotonic()
        assert (await rpc.dispatch(ui, "ui/interrupt", {
            "session_id": session_id, "turn_id": first["turn_id"]}))["ok"] is True
        assert time.monotonic() - began < 0.5
        second = await rpc.dispatch(ui, "ui/submit", {
            "session_id": session_id, "client_msg_id": "b",
            "content": [{"type": "text", "text": "second"}],
        })
        # The follow-up turn must START immediately; it may even finish first
        # (the mock LLM answers without a tool call), so "running" is not stable.
        assert await wait_until(
            lambda: log.store.load(session_id).turns[1].started_at is not None, 0.5)
        if log.store.load(session_id).turns[1].state is TurnState.RUNNING:
            await log.interrupt(session_id, second["turn_id"])
        loaded = log.store.load(session_id)
        assert [m.content for m in loaded.messages if m.role == "user"] == ["first", "second"]
        states = [t.state for t in loaded.turns]
        assert states[0] is TurnState.INTERRUPTED
        assert states[1] in (TurnState.RUNNING, TurnState.COMPLETED, TurnState.INTERRUPTED)
        assert any(e.type == "turn.interrupted" for e in log.store.read_since(session_id, 0))
        await log.close()

    asyncio.run(scenario())
