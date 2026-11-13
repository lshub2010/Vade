"""Production assembly: module_caller -> router -> tools/call round trip.

The C3 review found three dead ends that unit tests could not see:

* `app.module_caller` had no assignment in the whole repo, so `install()` built
  `router = None` and every turn answered "no capability";
* `Connection` dropped every method-less message on the floor, so a
  `tools/call` reply had nowhere to land;
* `make_rpc_handler` only knew `module/*`, so `ui/*` was NOT_FOUND on a socket.

These tests go through `App.startup()` + `install_components()` (the same two
calls `__main__._serve` makes) and drive a real `Connection` with a fake module
on the other end. No mocks at the router or request layer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

from vade.app import App, install_components, make_rpc_handler
from vade.config import load_config
from vade.errors import VadeError
from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from vade.registry.manifest import validate_manifest
from vade.transport.connection import Connection, RpcError
from tests.session.helpers import make_manifest, wait_until


class FakeWs:
    """Duck-typed websocket: inbound queue for the core reader, outbox for us."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.outbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed_with: int | None = None

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))
        self.outbox.put_nowait(bytes(data))

    async def receive(self) -> bytes | None:
        return await self._inbox.get()

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code
        self._inbox.put_nowait(None)

    async def deliver(self, message: dict[str, Any]) -> None:
        """What a module writes into the socket (plain JSON: browser_session)."""
        self._inbox.put_nowait(json.dumps(message).encode("utf-8"))


class FakeConnection:
    def __init__(self, connection_id: str = "ui-1") -> None:
        self.connection_id = connection_id
        self.device_id = f"dev-{connection_id}"
        self.auth_mode = "device_key"
        self.events: list[dict[str, Any]] = []

    async def send_event(self, frame: dict[str, Any]) -> None:
        self.events.append(dict(frame["params"]))


class ModuleSide:
    """Answers tools/call like a real module and records what it saw."""

    def __init__(self, ws: FakeWs, connection: Connection, result: Any = None, error: dict | None = None) -> None:
        self.ws = ws
        self.connection = connection
        self.result = result if result is not None else {"ok": True, "result": {"content": "hello"}}
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        while True:
            frame = await self.ws.outbox.get()
            if frame is None:
                return
            message = json.loads(frame)
            if message.get("method") == "tools/call":
                self.calls.append(dict(message["params"]))
                if self.error is not None:
                    await self.ws.deliver({"jsonrpc": "2.0", "id": message["id"], "error": self.error})
                else:
                    await self.ws.deliver({"jsonrpc": "2.0", "id": message["id"], "result": self.result})
            elif message.get("method") == "tools/cancel":
                self.cancelled.append(message["params"]["call_id"])

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


@pytest_asyncio.fixture()
async def wired(tmp_path):
    """App assembled exactly like `_serve`: startup -> install_components."""
    config = load_config(cli={"core.data_dir": str(tmp_path)})
    app = App(config)
    await app.startup()
    install_components(app, strict=False)
    yield app
    await app.session.close()
    await app.shutdown()


def _new_manifest(**kwargs: Any):
    return validate_manifest(make_manifest(**kwargs))


@pytest.mark.asyncio
async def test_startup_wires_a_real_module_caller_and_router(wired):
    app = wired
    assert app.module_caller is not None, "app.module_caller must be assigned in production"
    assert app.router is not None, "C3 install() must build a Router, not None"
    assert "ui/submit" in app.session_handlers
    assert app.session_rpc is not None


@pytest.mark.asyncio
async def test_full_turn_goes_through_a_real_connection_not_a_stub(wired):
    app = wired
    ws = FakeWs()
    module_conn = Connection(ws, None, connection_id="c-fs", browser_session=True)
    run_task = asyncio.create_task(module_conn.run())
    assert await wait_until(lambda: module_conn.state.value == "established", 1)
    app.transport["connections"]["c-fs"] = module_conn
    app.registry.register(_new_manifest(), fingerprint="fp-module", connection_id="c-fs")

    module = ModuleSide(ws, module_conn)
    module.start()
    app.loop.llm = MockLLMProvider([
        [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_file",
                                        arguments='{"path":"a.txt"}'),))],
        [Delta(text="The file says hello.")],
    ])

    ui = FakeConnection()
    handler = make_rpc_handler(app)
    attached = await handler(ui, {"jsonrpc": "2.0", "id": 1, "method": "ui/attach",
                                  "params": {"session_id": None}})
    assert not isinstance(attached, RpcError), attached
    session_id = attached["session_id"]
    reply = await handler(ui, {"jsonrpc": "2.0", "id": 2, "method": "ui/submit",
                               "params": {"session_id": session_id, "client_msg_id": "m1",
                                          "content": [{"type": "text", "text": "read a.txt"}]}})
    assert not isinstance(reply, RpcError), reply
    await app.session.wait_idle(session_id)
    await asyncio.sleep(0.05)

    # The router really sent tools/call over the wire, correlated the reply by
    # JSON-RPC id, and the loop consumed the module's result.
    assert len(module.calls) == 1, module.calls
    params = module.calls[0]
    assert params["tool"] == "read_file"
    assert params["args"] == {"path": "a.txt"}
    assert params["ctx"] == {"session_id": session_id}
    assert params["deadline_ms"] >= 100

    events = app.session_store.read_since(session_id, 0)
    types = [e.type for e in events]
    assert "turn.tool_result" in types and "turn.completed" in types
    tool_result = next(e for e in events if e.type == "turn.tool_result")
    assert tool_result.payload["ok"] is True
    assert "hello" in tool_result.payload["summary"]
    completed = next(e for e in events if e.type == "turn.completed")
    assert completed.payload["state"] == "completed"
    roles = [m.role for m in app.session_store.load(session_id).messages]
    assert roles == ["user", "assistant", "tool", "assistant"]

    await module.stop()
    await module_conn.close()
    run_task.cancel()


@pytest.mark.asyncio
async def test_module_jsonrpc_error_is_mapped_to_a_tool_error(wired):
    app = wired
    ws = FakeWs()
    module_conn = Connection(ws, None, connection_id="c-fs", browser_session=True)
    run_task = asyncio.create_task(module_conn.run())
    assert await wait_until(lambda: module_conn.state.value == "established", 1)
    app.transport["connections"]["c-fs"] = module_conn
    app.registry.register(_new_manifest(), fingerprint="fp-module", connection_id="c-fs")
    module = ModuleSide(ws, module_conn, error={"code": "PERMISSION_DENIED", "message": "no access"})
    module.start()
    app.loop.llm = MockLLMProvider([
        [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="fs__read_file",
                                        arguments='{"path":"secret"}'),))],
        [Delta(text="denied")],
    ])

    ui = FakeConnection()
    handler = make_rpc_handler(app)
    session_id = (await handler(ui, {"method": "ui/attach", "params": {"session_id": None}}))["session_id"]
    await handler(ui, {"method": "ui/submit", "params": {
        "session_id": session_id, "client_msg_id": "m1",
        "content": [{"type": "text", "text": "read secret"}]}})
    await app.session.wait_idle(session_id)
    await asyncio.sleep(0.05)

    tool_result = next(e for e in app.session_store.read_since(session_id, 0)
                       if e.type == "turn.tool_result")
    assert tool_result.payload["ok"] is False
    assert tool_result.payload["code"] == "PERMISSION_DENIED"
    assert "no access" in tool_result.payload["summary"]

    await module.stop()
    await module_conn.close()
    run_task.cancel()


@pytest.mark.asyncio
async def test_pending_request_fails_fast_when_the_socket_dies(wired):
    ws = FakeWs()
    connection = Connection(ws, None, connection_id="c-x", browser_session=True)
    run_task = asyncio.create_task(connection.run())
    assert await wait_until(lambda: connection.state.value == "established", 1)

    waiter = asyncio.create_task(connection.request("tools/call", {"call_id": "c1"}, timeout_s=5))
    await asyncio.sleep(0.02)
    await connection.close()
    with pytest.raises(VadeError) as exc:
        await asyncio.wait_for(waiter, 1)
    assert exc.value.code == "UNAVAILABLE"
    run_task.cancel()


@pytest.mark.asyncio
async def test_ui_methods_reach_session_rpc_not_not_found(wired):
    app = wired
    handler = make_rpc_handler(app)
    ui = FakeConnection()
    calls = [
        ("ui/attach", {"session_id": None}),
        ("ui/submit", {"session_id": "missing", "client_msg_id": "x",
                       "content": [{"type": "text", "text": "x"}]}),
        ("ui/interrupt", {"session_id": "missing", "turn_id": "t"}),
        ("ui/queue.cancel", {"session_id": "missing", "turn_id": "t"}),
        ("ui/detach", {"session_id": "missing"}),
        ("approval/decide", {"request_id": "r", "decision": "approve"}),
    ]
    for method, params in calls:
        result = await handler(ui, {"jsonrpc": "2.0", "id": 9, "method": method, "params": params})
        if isinstance(result, RpcError):
            assert "unknown method" not in result.message, f"{method} is not routed: {result.message}"
