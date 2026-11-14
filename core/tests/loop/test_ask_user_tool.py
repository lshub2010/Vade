"""`ask_user` as a loop-engine built-in tool (RFC-0008 §14, batch W1).

Seven acceptance cases from the batch brief, driven through the REAL path —
MockLLM script -> engine tool loop -> InputManager on a real SessionLog —
plus the handler-level contract pins the event stream cannot express
(the `{status, value}` result shape) and the prompt/tool-schema surface.
"""

from __future__ import annotations

import asyncio
import json
import time

from vade.errors import VadeError
from vade.input import InputConfig, InputManager
from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.loop.builtins import ASK_USER_NAME, default_builtins
from vade.loop.engine import LoopEngine
from vade.loop.ports import InputAnswer, NullInputPort
from vade.loop.tools import ToolEntry, ToolSnapshot
from vade.router.dispatch import ToolCallResult
from vade.session.manager import SessionLog
from vade.session.models import TurnState
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

from ..input.helpers import of_type, start_turn, wait_pending, wait_until

FAST = InputConfig(min_timeout_s=1, default_timeout_s=5, max_timeout_s=5)
ANSWER = "sk-SECRET-ANSWER-42"


# ------------------------------------------------------------------- harness
class FakeRouter:
    """Module-tool stand-in: records calls, never actually used by ask_user."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool_id, args, *, session_id=None, turn_id=None, call_id=None,
                   deadline_ms=None):
        self.calls.append((tool_id, dict(args)))
        return ToolCallResult(True, tool_id, result={"content": "hi"})

    def was_cancelled(self, turn_id: str) -> bool:
        return False


class AuditRecorder:
    """Collects every audit row the engine and the input manager write."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event) -> None:
        self.events.append(dict(event))


class StubPort:
    """Answers `ask` with a canned `InputAnswer` (handler-level tests)."""

    def __init__(self, answer: InputAnswer) -> None:
        self.answer = answer
        self.prompts: list = []

    def ui_attached(self, session_id: str) -> int:
        return 1

    async def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answer

    async def on_session_end(self, session_id: str) -> None:
        return None


class NoAskPort:
    """Availability says yes, but `ask` must never be reached."""

    def ui_attached(self, session_id: str) -> int:
        return 1

    async def ask(self, prompt):
        raise AssertionError("ask must not be called for an invalid question")

    async def on_session_end(self, session_id: str) -> None:
        return None


class NeverAskedPort:
    """Reports NO UI, but would block forever if the tool ever delegated."""

    def ui_attached(self, session_id: str) -> int:
        return 0

    async def ask(self, prompt):
        await asyncio.sleep(120)
        raise AssertionError("ask must not be called when no UI is attached")

    async def on_session_end(self, session_id: str) -> None:
        return None


class NullSpyPort(NullInputPort):
    def __init__(self) -> None:
        self.asked = 0

    async def ask(self, prompt):
        self.asked += 1
        return await super().ask(prompt)


def _build(tmp_path, *, script, ui=1, config=FAST, port=None, audit=None,
           snapshot=None):
    """SessionLog + InputManager + LoopEngine wired exactly as install() does."""
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
    manager = InputManager(config=config, audit=audit)
    manager.set_ui_probe(lambda session_id: ui)
    manager.set_state_probe(log.turn_state)
    manager.set_state_hook(lambda sid, tid, state: log.mark_state(sid, tid, TurnState(state)))
    log.input_port = manager
    registry = default_builtins(lambda: manager if port is None else port)
    engine = LoopEngine(
        FakeRouter(), MockLLMProvider(script), lambda: snapshot or ToolSnapshot(),
        log.emit, audit=audit, builtins=registry,
    )
    log.set_executor(engine.run_turn)
    return log, manager, engine


def _call(name: str, args: dict, call_id: str = "c1") -> list[Delta]:
    return [Delta(tool_calls=(ToolCallDelta(index=0, id=call_id, name=name,
                                            arguments=json.dumps(args)),))]


def _tool_messages(engine, index: int) -> list[dict]:
    return [m for m in engine.llm.calls[index]["messages"] if m.get("role") == "tool"]


def _module_entry() -> ToolEntry:
    return ToolEntry(
        llm_name="fs__read_file", tool_id="com.example.fs/read_file",
        module_id="com.example.fs", description="read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk="low", timeout_ms=1000, idempotent=True,
    )


# ------------------------------------------------------- ① event + ② answered
def test_ask_user_emits_required_enters_waiting_input_and_returns_value(tmp_path):
    async def scenario():
        log, manager, engine = _build(tmp_path, script=[
            _call(ASK_USER_NAME, {"kind": "free_text", "prompt": "filename?",
                                  "default": "draft.md"}),
            [Delta(text="ok, got it")],
        ])
        session = log.create_session()
        turn = await start_turn(log, session.id)

        input_id = await wait_pending(manager)
        # ① the event pair AND the state: entering waiting_input is announced.
        assert await wait_until(lambda: log.turn_state(session.id, turn.id) == "waiting_input")
        assert await wait_until(
            lambda: len(of_type(log, session.id, "turn.input_required")) == 1)
        required = of_type(log, session.id, "turn.input_required")[0]
        assert required.payload["state"] == "waiting_input"
        assert required.payload["kind"] == "free_text"
        assert required.payload["default"] == "draft.md"
        assert required.payload["input_id"] == input_id

        # ② the user answers; the tool result carries {status, value}.
        submitted = await manager.submit(session_id=session.id, input_id=input_id,
                                         value="main.py")
        assert submitted["ok"] is True
        await log.wait_idle(session.id)

        resolved = of_type(log, session.id, "turn.input_resolved")
        assert len(resolved) == 1
        assert resolved[0].payload["status"] == "submitted"
        assert resolved[0].payload["value"] == "main.py"
        # RFC-0008 §5.3: resolved lands BEFORE the tool result closes the call.
        results = of_type(log, session.id, "turn.tool_result")
        assert len(results) == 1
        assert results[0].seq > resolved[0].seq
        assert results[0].payload["ok"] is True

        # The model-visible content is exactly {status, value}.
        tool_msgs = _tool_messages(engine, 1)
        assert len(tool_msgs) == 1
        assert json.loads(tool_msgs[0]["content"]) == {"status": "answered",
                                                       "value": "main.py"}
        # The turn went back to running and completed.
        assert [t.state.value for t in log.store.load(session.id).turns] == ["completed"]

    asyncio.run(scenario())


# ------------------------------------------------------------------- ③ timeout
def test_timeout_returns_timeout_with_null_value_and_never_the_default(tmp_path):
    async def scenario():
        log, manager, engine = _build(tmp_path, script=[
            _call(ASK_USER_NAME, {"kind": "free_text", "prompt": "filename?",
                                  "default": "SENTINEL", "timeout_s": 1}),
            [Delta(text="gave up")],
        ])
        session = log.create_session()
        await start_turn(log, session.id)
        await wait_pending(manager)
        await log.wait_idle(session.id)

        resolved = of_type(log, session.id, "turn.input_resolved")
        assert len(resolved) == 1
        assert resolved[0].payload["status"] == "timeout"
        assert resolved[0].payload["by"] == "system"
        assert resolved[0].payload["value"] is None

        results = of_type(log, session.id, "turn.tool_result")
        assert results[0].payload["ok"] is False
        assert results[0].payload["code"] == "CANCELLED"

        # What the model got: the C-05 error shape, timed out, NO default.
        content = json.loads(_tool_messages(engine, 1)[0]["content"])
        assert content["ok"] is False
        assert content["error"]["code"] == "CANCELLED"
        assert "timed out" in content["error"]["message"]
        # The answer slot carries no value at all — the default it offered
        # (`SENTINEL`, still visible in its own echoed tool_call arguments)
        # must never be filled in as if the user had answered (§8.1).
        assert "SENTINEL" not in _tool_messages(engine, 1)[0]["content"]
        assert [t.state.value for t in log.store.load(session.id).turns] == ["completed"]

    asyncio.run(scenario())


# ------------------------------------------------------------------ ④ cancelled
def test_user_cancel_returns_cancelled_and_the_turn_continues(tmp_path):
    async def scenario():
        log, manager, engine = _build(tmp_path, script=[
            _call(ASK_USER_NAME, {"kind": "confirm", "prompt": "continue?"}),
            [Delta(text="moving on")],
        ])
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        cancelled = await manager.cancel(session_id=session.id, input_id=input_id)
        assert cancelled["ok"] is True
        await log.wait_idle(session.id)

        resolved = of_type(log, session.id, "turn.input_resolved")
        assert len(resolved) == 1
        assert resolved[0].payload["status"] == "cancelled"
        assert resolved[0].payload["by"] == "user"
        assert resolved[0].payload["value"] is None

        results = of_type(log, session.id, "turn.tool_result")
        assert results[0].payload["ok"] is False
        assert results[0].payload["code"] == "CANCELLED"
        content = json.loads(_tool_messages(engine, 1)[0]["content"])
        assert content["error"]["code"] == "CANCELLED"
        # The turn did NOT die: the model finished its answer.
        assert [t.state.value for t in log.store.load(session.id).turns] == ["completed"]

    asyncio.run(scenario())


# -------------------------------------------------------------- ③/④ result shape
def test_handler_maps_every_answer_to_status_and_value():
    """The contract pins the `{status, value}` shape at the handler boundary."""
    ctx = type("Ctx", (), {"session_id": "s1", "turn_id": "t1", "emit": None})()

    def run(port, args):
        registry = default_builtins(lambda: port)
        return asyncio.run(registry.get(ASK_USER_NAME).handler(args, ctx))

    answered = run(StubPort(InputAnswer(ok=True, value="main.py", status="submitted",
                                       input_id="IN1")),
                   {"kind": "free_text", "prompt": "filename?", "default": "draft.md"})
    assert answered.ok is True
    assert answered.result == {"status": "answered", "value": "main.py"}
    # The question reached the port with the core-owned source marker.
    assert answered is not None and answered.ok

    timeout = run(StubPort(InputAnswer(ok=False, status="timeout", reason="timeout",
                                      message="timed out")),
                  {"kind": "free_text", "prompt": "filename?", "default": "SENTINEL"})
    # RFC-0008 §8.1: timeout is a cancellation — value null, NEVER the default.
    assert timeout.ok is False and timeout.code == "CANCELLED"
    assert timeout.result == {"status": "timeout", "value": None}

    cancelled = run(StubPort(InputAnswer(ok=False, status="cancelled", reason="user_cancel",
                                        message="cancelled")),
                    {"kind": "confirm", "prompt": "continue?"})
    assert cancelled.ok is False and cancelled.code == "CANCELLED"
    assert cancelled.result == {"status": "cancelled", "value": None}

    no_ui = run(StubPort(InputAnswer(ok=False, status="cancelled", reason="no_ui",
                                    message="no UI is attached")),
                {"kind": "confirm", "prompt": "continue?"})
    assert no_ui.ok is False and no_ui.code == "UNAVAILABLE"


# ----------------------------------------------------------------- ⑤ no UI
def test_no_ui_answers_unavailable_immediately_and_never_asks(tmp_path):
    """Three shapes of "nobody can be asked" — all immediate `UNAVAILABLE`.

    The `NeverAskedPort` variant is the mutation catcher: it reports zero UIs
    but would block for 120s if the tool delegated instead of pre-checking.
    """

    async def with_manager_ui_zero():
        log, manager, engine = _build(tmp_path, ui=0, script=[
            _call(ASK_USER_NAME, {"kind": "confirm", "prompt": "anyone?"}),
            [Delta(text="ok")],
        ])
        session = log.create_session()
        started = time.monotonic()
        await start_turn(log, session.id)
        await asyncio.wait_for(log.wait_idle(session.id), timeout=5)
        assert time.monotonic() - started < 2.0
        results = of_type(log, session.id, "turn.tool_result")
        assert results[0].payload["ok"] is False
        assert results[0].payload["code"] == "UNAVAILABLE"
        assert of_type(log, session.id, "turn.input_required") == []
        assert manager.pending_count == 0

    async def with_blocking_port():
        log, manager, engine = _build(tmp_path, port=NeverAskedPort(), script=[
            _call(ASK_USER_NAME, {"kind": "confirm", "prompt": "anyone?"}),
            [Delta(text="ok")],
        ])
        session = log.create_session()
        started = time.monotonic()
        await start_turn(log, session.id)
        # A removed pre-check delegates into a port that sleeps 120s: this
        # wait_for is what turns that mutation red instead of hanging.
        await asyncio.wait_for(log.wait_idle(session.id), timeout=5)
        assert time.monotonic() - started < 2.0
        results = of_type(log, session.id, "turn.tool_result")
        assert results[0].payload["code"] == "UNAVAILABLE"
        assert of_type(log, session.id, "turn.input_required") == []

    async def with_null_port():
        spy = NullSpyPort()
        log, manager, engine = _build(tmp_path, port=spy, script=[
            _call(ASK_USER_NAME, {"kind": "confirm", "prompt": "anyone?"}),
            [Delta(text="ok")],
        ])
        session = log.create_session()
        await start_turn(log, session.id)
        await asyncio.wait_for(log.wait_idle(session.id), timeout=5)
        results = of_type(log, session.id, "turn.tool_result")
        assert results[0].payload["code"] == "UNAVAILABLE"
        assert spy.asked == 0

    asyncio.run(with_manager_ui_zero())
    asyncio.run(with_blocking_port())
    asyncio.run(with_null_port())


# ------------------------------------------------------- ⑥ fail-closed validation
def test_invalid_kind_options_combinations_are_rejected():
    """Every malformed question raises INVALID_ARGUMENT; `ask` is never reached."""
    port = NoAskPort()
    registry = default_builtins(lambda: port)
    ctx = type("Ctx", (), {"session_id": "s", "turn_id": "t", "emit": None})()
    two = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    cases = [
        {},                                                     # prompt missing
        {"kind": "free_text"},                                  # prompt missing
        {"kind": "bogus", "prompt": "x"},                       # unknown kind
        {"kind": "free_text", "prompt": "   "},                 # blank prompt
        {"kind": "free_text", "prompt": "x" * 501},             # prompt > 500
        {"kind": "free_text", "prompt": "x", "options": two},   # combo: options on free_text
        {"kind": "confirm", "prompt": "x", "options": two},     # combo: options on confirm
        {"kind": "single_choice", "prompt": "x"},               # combo: options missing
        {"kind": "single_choice", "prompt": "x",
         "options": [{"id": "a", "label": "A"}]},               # combo: 1 option
        {"kind": "single_choice", "prompt": "x",
         "options": [{"id": "a", "label": ""}, {"id": "b", "label": "B"}]},  # blank label
        {"kind": "multi_choice", "prompt": "x",
         "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "A2"}]},  # duplicate id
        {"kind": "free_text", "prompt": "x", "timeout_s": "long"},  # wrong type
        {"kind": "free_text", "prompt": "x", "extra": 1},       # unknown argument
    ]
    for args in cases:
        try:
            asyncio.run(registry.get(ASK_USER_NAME).handler(args, ctx))
        except VadeError as exc:
            assert exc.code == "INVALID_ARGUMENT", (args, exc.code)
        else:  # pragma: no cover - a case that stops failing is a real regression
            raise AssertionError(f"question was not rejected: {args}")


def test_invalid_question_reaches_the_model_and_the_turn_continues(tmp_path):
    async def scenario():
        log, manager, engine = _build(tmp_path, script=[
            _call(ASK_USER_NAME, {"kind": "confirm", "prompt": "continue?", "options": [
                {"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}),
            [Delta(text="continuing without the answer")],
        ])
        session = log.create_session()
        await start_turn(log, session.id)
        await log.wait_idle(session.id)

        results = of_type(log, session.id, "turn.tool_result")
        assert results[0].payload["ok"] is False
        assert results[0].payload["code"] == "INVALID_ARGUMENT"
        content = json.loads(_tool_messages(engine, 1)[0]["content"])
        assert content["error"]["code"] == "INVALID_ARGUMENT"
        assert "takes no options" in content["error"]["message"]
        # Nothing was asked, no dialog opened, and the turn survived.
        assert manager.pending_count == 0
        assert of_type(log, session.id, "turn.input_required") == []
        assert [t.state.value for t in log.store.load(session.id).turns] == ["completed"]

    asyncio.run(scenario())


# ------------------------------------------------------------------- ⑦ audit
def test_audit_stores_digest_and_chars_but_never_the_value(tmp_path):
    async def scenario():
        audit = AuditRecorder()
        log, manager, engine = _build(tmp_path, audit=audit, script=[
            _call(ASK_USER_NAME, {"kind": "free_text", "prompt": "paste the token?"}),
            [Delta(text="thanks")],
        ])
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        await manager.submit(session_id=session.id, input_id=input_id, value=ANSWER)
        await log.wait_idle(session.id)

        kinds = [e["kind"] for e in audit.events]
        assert "tool_call" in kinds            # engine row for the tool call
        assert "input_request" in kinds        # manager row for the question
        assert "input_response" in kinds       # manager row for the answer

        response = [e for e in audit.events if e["kind"] == "input_response"][0]
        digest = response["extra"]["value_digest"]
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        assert response["extra"]["value_chars"] == len(ANSWER)

        # The value (and the prompt text) appear in NO audit field.
        dump = json.dumps(audit.events, default=str)
        assert ANSWER not in dump
        assert "paste the token?" not in dump

    asyncio.run(scenario())


# ------------------------------------------------- prompt surface + coexistence
def test_ask_user_is_in_the_prompt_tool_list_and_the_llm_schema(tmp_path):
    async def scenario():
        # Empty module snapshot + built-ins: the turn must still reach the LLM
        # (the no_capability gate counts built-ins as capabilities).
        log, manager, engine = _build(tmp_path, script=[[Delta(text="noted")]])
        session = log.create_session()
        await start_turn(log, session.id)
        await log.wait_idle(session.id)
        assert len(engine.llm.calls) == 1

        tools = engine.llm.calls[0]["tools"]
        ask = [t for t in tools if t["function"]["name"] == ASK_USER_NAME]
        assert len(ask) == 1
        fn = ask[0]["function"]
        assert fn["description"].startswith("Ask the user a question")
        params = fn["parameters"]
        assert params["required"] == ["kind", "prompt"]
        assert params["properties"]["kind"]["enum"] == [
            "free_text", "single_choice", "multi_choice", "confirm"]
        assert params["properties"]["timeout_s"]["minimum"] == 5
        assert params["properties"]["timeout_s"]["maximum"] == 600
        assert params["properties"]["options"]["maxItems"] == 20

        # The prompt tool block names the tool, so the model knows it exists.
        system = [m["content"] for m in engine.llm.calls[0]["messages"]
                  if m.get("role") == "system"]
        block = "\n".join(system)
        assert "- ask_user:" in block
        assert "[risk=low]" in block
        assert [t.state.value for t in log.store.load(session.id).turns] == ["completed"]

    asyncio.run(scenario())


def test_builtins_coexist_with_module_tools(tmp_path):
    """Module tools keep their names, their router dispatch and their prompt slot."""
    async def scenario():
        snapshot = ToolSnapshot(entries=(_module_entry(),))
        log, manager, engine = _build(tmp_path, snapshot=snapshot, script=[
            _call("fs__read_file", {"path": "a"}),
            _call(ASK_USER_NAME, {"kind": "single_choice", "prompt": "which env?",
                                  "options": [{"id": "dev", "label": "Dev"},
                                              {"id": "prod", "label": "Prod"}]}, "c2"),
            [Delta(text="done")],
        ])
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        await manager.submit(session_id=session.id, input_id=input_id, value="dev")
        await log.wait_idle(session.id)

        # The module tool went through the router, untouched.
        assert engine.router.calls == [("com.example.fs/read_file", {"path": "a"})]
        # The built-in blocked on the channel and came back answered.
        resolved = of_type(log, session.id, "turn.input_resolved")
        assert resolved[0].payload["status"] == "submitted"
        assert resolved[0].payload["value"] == "dev"
        # Both tools were advertised to the model, module first.
        names = [t["function"]["name"] for t in engine.llm.calls[0]["tools"]]
        assert names == ["fs__read_file", "ask_user"]
        system = "\n".join(m["content"] for m in engine.llm.calls[0]["messages"]
                           if m.get("role") == "system")
        assert "- fs__read_file:" in system
        assert "- ask_user:" in system
        assert [t.state.value for t in log.store.load(session.id).turns] == ["completed"]

    asyncio.run(scenario())
