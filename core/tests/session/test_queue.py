"""Queue: one running per session, FIFO, idempotent submit, real interrupt."""

from __future__ import annotations

import asyncio
import time

import pytest

from vade.errors import VadeError
from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.loop.engine import LoopEngine, TurnOutcome
from vade.loop.tools import build_snapshot
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry
from vade.router.dispatch import Router
from vade.session.manager import SessionLog
from vade.session.models import TurnState
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore
from .helpers import CollectSink, GateExecutor, make_manifest, wait_until


def _log(tmp_path, executor) -> SessionLog:
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    return log


async def _await_settled(log: SessionLog, session_id: str, timeout: float = 2.0) -> list:
    """Join the turn task, then wait until the persisted event stream stops.

    `wait_idle` drains the queue worker only. An interrupted turn whose module
    eats `tools/cancel` outlives the worker as an orphan task, and that task —
    not the worker — is what would write a late `turn.delta`/`turn.tool_result`.
    Asserting before it is done is a false pass, so: wait for the task itself,
    then require the event count to be identical across two consecutive polls.
    `_queues`/`_turn_task` are read on purpose — `SessionLog` exposes no
    accessor for the turn task, and the point here is to join the real one.
    """
    queue = log._queues.get(session_id)
    task = getattr(queue, "_turn_task", None) if queue is not None else None
    if task is not None:
        deadline = time.monotonic() + timeout
        while not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert task.done(), "the interrupted turn task never finished"
        await asyncio.sleep(0)  # let its done-callbacks (cancel-flag clear) run
    previous = len(log.store.read_since(session_id, 0))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        current = len(log.store.read_since(session_id, 0))
        if current == previous:
            return log.store.read_since(session_id, 0)
        previous = current
    raise AssertionError("the event stream never settled: a writer is still running")


def test_submit_is_serial_and_event_seq_has_no_gap(tmp_path):
    async def scenario():
        gate = GateExecutor()
        log = _log(tmp_path, gate)
        session = log.create_session()
        turns = []
        for i in range(3):
            turn, pos = await log.submit(session.id, [{"type": "text", "text": f"m{i}"}], f"c{i}")
            turns.append(turn)
            assert pos == i + 1
        gate.gate.set()
        await log.wait_idle(session.id)
        await asyncio.sleep(0.02)
        assert gate.order == [t.id for t in turns]
        events = log.store.read_since(session.id, 0)
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert events[0].type == "turn.queued"
        loaded = log.store.load(session.id)
        assert [t.state for t in loaded.turns] == [TurnState.COMPLETED] * 3
        assert [m.content for m in loaded.messages if m.role == "user"] == ["m0", "m1", "m2"]
        await log.close()

    asyncio.run(scenario())


def test_client_msg_id_is_idempotent(tmp_path):
    async def scenario():
        gate = GateExecutor()
        gate.gate.set()
        log = _log(tmp_path, gate)
        session = log.create_session()
        first, _ = await log.submit(session.id, [{"type": "text", "text": "x"}], "same")
        second, _ = await log.submit(session.id, [{"type": "text", "text": "x"}], "same")
        assert second.id == first.id
        await log.wait_idle(session.id)
        loaded = log.store.load(session.id)
        assert len(loaded.turns) == 1
        assert sum(1 for e in log.store.read_since(session.id, 0) if e.type == "turn.queued") == 1
        await log.close()

    asyncio.run(scenario())


def test_cancel_only_works_on_queued_and_running_gets_invalid_argument(tmp_path):
    async def scenario():
        gate = GateExecutor()
        log = _log(tmp_path, gate)
        session = log.create_session()
        running, _ = await log.submit(session.id, [{"type": "text", "text": "a"}], "a")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == running.id)
        queued, _ = await log.submit(session.id, [{"type": "text", "text": "b"}], "b")
        assert log.queue_state(session.id)["pending"][0]["turn_id"] == queued.id
        assert await log.cancel(session.id, queued.id) is True
        assert log.queue_state(session.id)["pending"] == []
        assert log.store.load(session.id).turns[1].state is TurnState.CANCELLED
        with pytest.raises(VadeError) as exc:
            await log.cancel(session.id, running.id)
        assert exc.value.code == "INVALID_ARGUMENT"
        gate.gate.set()
        await log.wait_idle(session.id)
        await log.close()

    asyncio.run(scenario())


def test_interrupt_releases_queue_immediately_even_if_module_ignores_cancel(tmp_path):
    async def scenario():
        started = asyncio.Event()

        async def stubborn(turn, content):
            started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                # A module that "ignores" tools/cancel: core must not wait for it.
                await asyncio.sleep(0.6)
            return None

        log = _log(tmp_path, stubborn)
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "slow"}], "s1")
        await asyncio.wait_for(started.wait(), 1)
        began = time.monotonic()
        assert await log.interrupt(session.id, turn.id) is True
        # Wall-clock tolerance, not a latency budget: the stubborn module sleeps
        # 5s, so anything well under that proves core did not wait for it.
        assert time.monotonic() - began < 2.0

        nxt, _ = await log.submit(session.id, [{"type": "text", "text": "next"}], "s2")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == nxt.id, 2.0)

        # No message lost: both user messages persisted, interrupted turn recorded.
        loaded = log.store.load(session.id)
        assert [m.content for m in loaded.messages if m.role == "user"] == ["slow", "next"]
        assert loaded.turns[0].state is TurnState.INTERRUPTED
        types = [e.type for e in log.store.read_since(session.id, 0)]
        assert "turn.interrupted" in types
        await log.close()

    asyncio.run(scenario())


def test_interrupt_emits_cancel_hook_with_turn_id(tmp_path):
    async def scenario():
        gate = GateExecutor()
        log = _log(tmp_path, gate)
        cancelled: list[str] = []
        log.set_cancel_hook(cancelled.append)
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "x"}], "x")
        await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id)
        await log.interrupt(session.id, turn.id)
        assert cancelled == [turn.id]
        gate.gate.set()
        await log.close()

    asyncio.run(scenario())


def test_waiting_approval_blocks_only_the_head(tmp_path):
    async def scenario():
        gate = GateExecutor()
        log = _log(tmp_path, gate)
        session = log.create_session()
        head, _ = await log.submit(session.id, [{"type": "text", "text": "a"}], "a")
        await wait_until(lambda: log.queue_state(session.id)["running"] == head.id)
        await log.mark_state(session.id, head.id, TurnState.WAITING_APPROVAL)
        later, _ = await log.submit(session.id, [{"type": "text", "text": "b"}], "b")
        state = log.queue_state(session.id)
        assert state["running"] == head.id
        assert state["running_state"] == "waiting_approval"
        assert [p["turn_id"] for p in state["pending"]] == [later.id]
        assert log.store.load(session.id).turns[0].state is TurnState.WAITING_APPROVAL
        gate.gate.set()
        await log.wait_idle(session.id)
        await log.close()

    asyncio.run(scenario())


def test_queue_state_is_observable_and_ordered(tmp_path):
    async def scenario():
        gate = GateExecutor()
        log = _log(tmp_path, gate)
        session = log.create_session()
        ids = []
        for i in range(3):
            turn, pos = await log.submit(session.id, [{"type": "text", "text": str(i)}], f"c{i}")
            ids.append(turn.id)
            assert pos == i + 1
        state = log.queue_state(session.id)
        assert [p["turn_id"] for p in state["pending"]] == ids
        assert state["running"] is None
        gate.gate.set()
        await log.wait_idle(session.id)
        assert log.queue_state(session.id) == {"running": None, "running_state": None, "pending": []}
        await log.close()

    asyncio.run(scenario())


def test_broadcast_happens_after_persist(tmp_path):
    async def scenario():
        gate = GateExecutor()
        log = _log(tmp_path, gate)
        session = log.create_session()
        seen: list[int] = []

        class StoreCheckingSink:
            async def send(self, event):
                persisted = [e.seq for e in log.store.read_since(session.id, int(event["seq"]) - 1)]
                assert int(event["seq"]) in persisted, "event broadcast before it was persisted"
                seen.append(int(event["seq"]))

        await log.attach(session.id, None, StoreCheckingSink())
        await log.submit(session.id, [{"type": "text", "text": "x"}], "x")
        gate.gate.set()
        await log.wait_idle(session.id)
        await asyncio.sleep(0.05)
        assert seen == list(range(1, len(seen) + 1))
        await log.close()

    asyncio.run(scenario())


def test_six_concurrent_submits_stay_serial_fifo_and_gapless(tmp_path):
    """6 paths submit at once: one worker, FIFO by core arrival, seq 1..N."""

    async def scenario():
        counters = {"active": 0, "max_active": 0}
        executed: list[str] = []

        async def executor(turn, content):
            counters["active"] += 1
            counters["max_active"] = max(counters["max_active"], counters["active"])
            executed.append(turn.user_text)
            try:
                await asyncio.sleep(0.01)  # a real await: a second turn could sneak in
            finally:
                counters["active"] -= 1
            return TurnOutcome(assistant_text=f"echo:{turn.user_text}", stopped_reason="stop")

        log = _log(tmp_path, executor)
        session = log.create_session()
        texts = [f"m{i}" for i in range(6)]
        submitted = await asyncio.gather(*(
            log.submit(session.id, [{"type": "text", "text": text}], f"msg-{text}")
            for text in texts
        ))
        arrivals = [turn.id for turn, _pos in submitted]
        await log.wait_idle(session.id)
        await asyncio.sleep(0.05)

        assert counters["max_active"] == 1, "two turns ran at the same time in one session"
        # FIFO: execution order is core arrival order, which is gather order.
        assert executed == texts
        events = log.store.read_since(session.id, 0)
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert [e.payload["turn_id"] for e in events if e.type == "turn.queued"] == arrivals
        loaded = log.store.load(session.id)
        assert [t.id for t in loaded.turns] == arrivals
        assert [t.state for t in loaded.turns] == [TurnState.COMPLETED] * 6
        assert [m.content for m in loaded.messages if m.role == "user"] == texts
        await log.close()

    asyncio.run(scenario())


def test_interrupt_really_cancels_the_turn_and_stops_the_event_stream(tmp_path):
    """A real Router + real engine: one tools/cancel, nothing after the terminal."""

    async def scenario():
        class SlowModule:
            def __init__(self) -> None:
                self.calls: list[dict] = []
                self.cancels: list[str] = []
                self.cancelled_inflight = 0

            async def call(self, target, method, params, deadline_ms):
                self.calls.append(dict(params))
                try:
                    await asyncio.sleep(5)  # never answers in time: cancel must do it
                except asyncio.CancelledError:
                    self.cancelled_inflight += 1
                    raise
                return {"ok": True, "result": {"content": "late"}}

            async def cancel(self, target, call_id):
                self.cancels.append(call_id)

        module = SlowModule()
        registry = Registry()
        registry.register(
            validate_manifest(make_manifest()), fingerprint="fp-fs", connection_id="c-fs"
        )
        router = Router(module, registry)
        llm = MockLLMProvider([
            [Delta(tool_calls=(ToolCallDelta(index=0, id="call-1", name="fs__read_file",
                                             arguments='{"path":"a"}'),))],
            [Delta(text="late answer")],
        ])
        log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
        engine = LoopEngine(router, llm, lambda: build_snapshot(registry), log.emit)
        log.set_executor(engine.run_turn)
        log.set_cancel_hook(router.cancel_turn)
        log.set_cancel_clear_hook(router.clear_cancelled)
        session = log.create_session()

        turn, _ = await log.submit(session.id, [{"type": "text", "text": "read a"}], "m1")
        assert await wait_until(lambda: module.calls, 1.0), "the tool call never reached the module"
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id, 1.0)

        assert await log.interrupt(session.id, turn.id) is True
        # The in-flight await was really cancelled, not just abandoned.
        assert await wait_until(lambda: module.cancelled_inflight == 1, 1.0), \
            "interrupt did not cancel the running turn task"
        assert await wait_until(lambda: module.cancels, 1.0)
        assert module.cancels == ["call-1"], "the module must see exactly one tools/cancel"

        await asyncio.sleep(0.1)  # a second cancel frame would land here
        assert module.cancels == ["call-1"], "interrupt re-sent tools/cancel"

        # Drain the queue, then look at what the interrupted turn wrote.
        await log.wait_idle(session.id)
        events = await _await_settled(log, session.id)
        types = [e.type for e in events]
        assert types.count("turn.interrupted") == 1
        after = types[types.index("turn.interrupted") + 1:]
        assert "turn.tool_result" not in after, f"events after interrupt: {after}"
        assert "turn.delta" not in after, f"events after interrupt: {after}"
        assert log.store.load(session.id).turns[0].state is TurnState.INTERRUPTED
        # The per-turn cancel flag does not outlive the turn it belongs to.
        assert router.was_cancelled(turn.id) is False
        await log.close()

    asyncio.run(scenario())


def test_interrupt_emits_no_tool_result_when_the_module_answers_after_cancel(tmp_path):
    """A module that eats `tools/cancel` must not resurrect the turn.

    The module swallows the CancelledError injected into its in-flight
    `tools/call`, then answers successfully. A plain `Router` hands that reply
    back to the engine as an ordinary `ToolCallResult`, exactly as production
    sees it — the swallow point is the module coroutine, so no test double is
    needed. Without the guard the loop resumes and writes `turn.tool_result`
    (and then the next step's `turn.delta`) *after* `turn.interrupted` — a UI
    then shows a completed tool call inside an interrupted turn.
    """

    async def scenario():
        class SwallowingModule:
            def __init__(self) -> None:
                self.calls: list[dict] = []
                self.cancels: list[str] = []
                self.cancelled_inflight = 0

            async def call(self, target, method, params, deadline_ms):
                self.calls.append(dict(params))
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    # The debt: swallow the cancel, answer as if still alive.
                    self.cancelled_inflight += 1
                return {"ok": True, "result": {"content": "late"}}

            async def cancel(self, target, call_id):
                self.cancels.append(call_id)

        class RecordingAudit:
            def __init__(self) -> None:
                self.events: list[dict] = []

            def record(self, event):
                self.events.append(dict(event))

        module = SwallowingModule()
        audit = RecordingAudit()
        registry = Registry()
        registry.register(
            validate_manifest(make_manifest()), fingerprint="fp-fs", connection_id="c-fs"
        )
        router = Router(module, registry)
        llm = MockLLMProvider([
            [Delta(tool_calls=(ToolCallDelta(index=0, id="call-1", name="fs__read_file",
                                             arguments='{"path":"a"}'),))],
            [Delta(text="late answer")],
        ])
        log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
        engine = LoopEngine(router, llm, lambda: build_snapshot(registry), log.emit, audit=audit)
        log.set_executor(engine.run_turn)
        log.set_cancel_hook(router.cancel_turn)
        log.set_cancel_clear_hook(router.clear_cancelled)
        session = log.create_session()

        turn, _ = await log.submit(session.id, [{"type": "text", "text": "read a"}], "m1")
        assert await wait_until(lambda: module.calls, 1.0), "the tool call never reached the module"
        assert await log.interrupt(session.id, turn.id) is True
        assert await wait_until(lambda: module.cancelled_inflight == 1, 1.0), \
            "interrupt did not cancel the in-flight module call"
        await log.wait_idle(session.id)

        # The turn task outlives the worker here; assert on a settled stream.
        events = await _await_settled(log, session.id)
        types = [e.type for e in events]
        assert types.count("turn.interrupted") == 1
        after = types[types.index("turn.interrupted") + 1:]
        assert "turn.tool_result" not in after, f"tool_result written after interrupt: {after}"
        assert "turn.delta" not in after, f"delta written after interrupt: {after}"
        assert "turn.completed" not in after, f"turn.completed written after interrupt: {after}"

        loaded = log.store.load(session.id)
        assert loaded.turns[0].state is TurnState.INTERRUPTED
        assert all(m.role != "tool" for m in loaded.messages), \
            "the interrupted turn persisted a late tool message"

        # The module really ran, so the call must stay in the audit trail — and
        # say why it has no `turn.tool_result` to go with it.
        tool_calls = [e for e in audit.events if e["kind"] == "tool_call"]
        assert len(tool_calls) == 1, f"executed tool call lost from audit: {audit.events}"
        assert tool_calls[0]["tool"] == "com.example.fs/read_file"
        assert tool_calls[0]["aborted"] == "interrupted"
        assert [e for e in audit.events if e["kind"] == "turn_completed"] == [], \
            "an interrupted turn must not be audited as completed"
        # ... and the router forgot the turn once its task was done.
        assert router.was_cancelled(turn.id) is False
        await log.close()

    asyncio.run(scenario())


def test_interrupt_drops_a_late_delta_from_a_provider_that_swallows_the_cancel(tmp_path):
    """The stream guard must not depend on `llm.chat` re-raising the cancel.

    A provider can eat the CancelledError thrown into its in-flight
    `__anext__` and yield one more delta. The engine checks `_is_interrupted`
    on the write path, so that late delta is dropped instead of being appended
    to a turn the UI already saw as interrupted.
    """

    async def scenario():
        class LateDeltaLLM:
            def __init__(self) -> None:
                self.streaming = asyncio.Event()
                self.swallowed = 0

            async def chat(self, messages, tools=None, stream=True):
                yield Delta(text="first")
                self.streaming.set()
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    self.swallowed += 1  # the debt: the provider eats the cancel
                yield Delta(text="after the cancel")

        class IdleModule:
            async def call(self, target, method, params, deadline_ms):  # pragma: no cover
                return {"ok": True, "result": {}}

            async def cancel(self, target, call_id):  # pragma: no cover
                return None

        llm = LateDeltaLLM()
        registry = Registry()
        registry.register(
            validate_manifest(make_manifest()), fingerprint="fp-fs", connection_id="c-fs"
        )
        router = Router(IdleModule(), registry)
        log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
        engine = LoopEngine(router, llm, lambda: build_snapshot(registry), log.emit)
        log.set_executor(engine.run_turn)
        log.set_cancel_hook(router.cancel_turn)
        log.set_cancel_clear_hook(router.clear_cancelled)
        session = log.create_session()

        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "m1")
        assert await wait_until(lambda: llm.streaming.is_set(), 1.0), "the stream never started"
        assert await log.interrupt(session.id, turn.id) is True
        assert await wait_until(lambda: llm.swallowed == 1, 1.0), \
            "interrupt did not reach the provider's in-flight await"
        await log.wait_idle(session.id)

        events = await _await_settled(log, session.id)
        texts = [e.payload.get("text") for e in events if e.type == "turn.delta"]
        assert texts == ["first"], f"a late delta landed after the interrupt: {texts}"
        assert log.store.load(session.id).turns[0].state is TurnState.INTERRUPTED
        await log.close()

    asyncio.run(scenario())
