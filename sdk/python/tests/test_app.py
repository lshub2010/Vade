"""Dispatch semantics: envelope shapes, timeouts, cancellation, live swaps."""

from __future__ import annotations

import asyncio

import pytest

from vade_sdk import ModuleApp, ToolSpec, current_call_context
from vade_sdk.errors import ToolError


def build() -> ModuleApp:
    app = ModuleApp("com.example.dispatch", "1.0.0", max_concurrency=2)

    def echo(text: str) -> dict:
        return {"text": text, "len": len(text)}

    async def slow(ms: int) -> dict:
        await asyncio.sleep(ms / 1000)
        return {"slept_ms": ms}

    def bad() -> bytes:
        return b"raw"

    def crash() -> dict:
        raise RuntimeError("secret path /etc/shadow is in this traceback")

    app.add_tool(ToolSpec(name="echo", fn=echo, description="echo"))
    app.add_tool(ToolSpec(name="slow", fn=slow, description="sleep", timeout_ms=200))
    app.add_tool(ToolSpec(name="bad", fn=bad, description="returns bytes"))
    app.add_tool(ToolSpec(name="crash", fn=crash, description="raises"))
    return app


async def test_ok_envelope():
    envelope = await build().call_tool("echo", {"text": "hi"})
    assert envelope["ok"] is True
    assert envelope["result"] == {"text": "hi", "len": 2}
    assert isinstance(envelope["duration_ms"], int)


async def test_unknown_tool_is_not_found():
    envelope = await build().call_tool("nope", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "NOT_FOUND"


async def test_extra_and_missing_arguments_are_invalid():
    app = build()
    extra = await app.call_tool("echo", {"text": "hi", "path": "../../etc/passwd"})
    assert extra["error"]["code"] == "INVALID_ARGUMENT"
    missing = await app.call_tool("echo", {})
    assert missing["error"]["code"] == "INVALID_ARGUMENT"


async def test_timeout_uses_the_spec_budget():
    envelope = await build().call_tool("slow", {"ms": 1000})
    assert envelope["ok"] is False
    assert envelope["error"] == {"code": "TIMEOUT", "message": envelope["error"]["message"], "retriable": True}


async def test_cancel_interrupts_an_async_tool():
    app = build()
    task = asyncio.create_task(app.call_tool("slow", {"ms": 3000}, call_id="call-1"))
    # Wait until the tool is actually running: cancelling before dispatch would
    # test the test, not the module (the call id is not registered yet).
    for _ in range(200):
        if "call-1" in app._tasks:  # noqa: SLF001 - white-box on purpose
            break
        await asyncio.sleep(0.01)
    assert app.cancel("call-1") is True
    envelope = await task
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "CANCELLED"


async def test_bytes_return_is_refused():
    envelope = await build().call_tool("bad", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INTERNAL"
    assert "base64" in envelope["error"]["message"]


async def test_unexpected_exception_is_internal_and_hides_the_traceback():
    envelope = await build().call_tool("crash", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INTERNAL"
    assert "/etc/shadow" not in envelope["error"]["message"]


async def test_add_tool_is_refused_once_live():
    app = build()
    app._transport = object()

    def other() -> dict:
        return {}

    with pytest.raises(RuntimeError, match="replace_tools"):
        app.add_tool(ToolSpec(name="other", fn=other, description="x"))


async def test_replace_tools_keeps_the_old_table_when_core_refuses():
    app = build()
    app._transport = _RefusingTransport("RISK_DOWNGRADE_FORBIDDEN")

    def other() -> dict:
        return {}

    with pytest.raises(ToolError) as excinfo:
        await app.replace_tools([ToolSpec(name="other", fn=other, description="x")])
    assert excinfo.value.code == "MANIFEST_INVALID"
    assert "RISK_DOWNGRADE_FORBIDDEN" in excinfo.value.message
    assert app.tool_names == ("echo", "slow", "bad", "crash")


async def test_replace_tools_swaps_after_acceptance():
    app = build()

    class Accepting:
        async def request(self, method, params, timeout=None):  # noqa: ANN001
            assert method == "module/capabilities.update"
            assert params["capabilities"]["tools"][0]["name"] == "only"
            return {"accepted": True, "cap_epoch": 3, "tool_names": ["only"]}

    app._transport = Accepting()

    def only(text: str) -> dict:
        return {"text": text}

    result = await app.replace_tools([ToolSpec(name="only", fn=only, description="x")])
    assert result["cap_epoch"] == 3
    assert app.tool_names == ("only",)


async def test_draining_refuses_new_calls():
    app = build()
    app.begin_drain(100)
    envelope = await app.call_tool("echo", {"text": "hi"})
    assert envelope["error"]["code"] == "MODULE_DRAINING"


async def test_tool_context_reaches_sync_and_async_tools_and_is_reset():
    app = ModuleApp("com.example.context", "1.0.0")

    def sync_tool() -> dict:
        return {"session_id": current_call_context().get("session_id")}

    async def async_tool() -> dict:
        return {"session_id": current_call_context().get("session_id")}

    app.add_tool(ToolSpec(name="sync", fn=sync_tool, description="sync"))
    app.add_tool(ToolSpec(name="async", fn=async_tool, description="async"))

    sync_envelope = await app.call_tool("sync", {}, context={"session_id": "s-1"})
    async_envelope = await app.call_tool("async", {}, context={"session_id": "s-2"})
    assert sync_envelope["result"] == {"session_id": "s-1"}
    assert async_envelope["result"] == {"session_id": "s-2"}
    assert current_call_context() == {}


async def test_private_rpc_handlers_are_not_tools_and_dispatch_sync_or_async():
    app = ModuleApp("com.example.rpc", "1.0.0")
    app.on_rpc("memory/admin_stats", lambda params: {"ok": True, "params": dict(params)})

    async def read_session(params):
        return {"items": [], "session_id": params.get("session_id")}

    app.on_rpc("memory/read_session", read_session)
    assert await app.dispatch_rpc("memory/admin_stats", {"x": 1}) == {
        "ok": True,
        "params": {"x": 1},
    }
    assert await app.dispatch_rpc("memory/read_session", {"session_id": "s"}) == {
        "items": [],
        "session_id": "s",
    }
    assert app.tool_names == ()
    with pytest.raises(ToolError) as excinfo:
        await app.dispatch_rpc("memory/unknown", {})
    assert excinfo.value.code == "NOT_FOUND"


async def test_event_handler_receives_topic_and_payload():
    app = ModuleApp("com.example.events", "1.0.0")
    seen = []

    async def on_event(topic, payload):
        seen.append((topic, payload))

    app.on_event(on_event)
    assert await app.dispatch_event("session.turn", {"turn_id": "t"}) == {"ok": True}
    assert seen == [("session.turn", {"turn_id": "t"})]


class _RefusingTransport:
    def __init__(self, code: str) -> None:
        self.code = code

    async def request(self, method, params, timeout=None):  # noqa: ANN001
        raise _RpcError(self.code)


class _RpcError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: refused")
