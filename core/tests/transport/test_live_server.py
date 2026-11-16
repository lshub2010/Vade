"""Real listener smoke: uvicorn on an ephemeral port, real TCP + real WS.

These tests exist because the review found the integration surface missing:
`/healthz`, `/ui/session` and the WS upgrade had only ever been exercised
through hand-built ASGI scopes, and `tests/fixtures/fake_module.py` did not
exist. Everything below goes over a socket.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from vade.app import App, install_components, make_rpc_handler
from vade.config import load_config
from vade.identity.pairing import Purpose
from vade.identity.store import load_or_create
from vade.llm.mock import MockLLMProvider
from vade.transport.server import CoreApp
from vade.__main__ import build_module_crypto

ROOT = Path(__file__).resolve().parents[3]
GOOD_MANIFEST = ROOT / "tests" / "fixtures" / "manifests" / "good.json"
BAD_MANIFEST = ROOT / "tests" / "fixtures" / "manifests" / "bad-tool-name.json"
MODULE_ID = "com.example.fs"
INSTANCE_ID = "fs-01"


def _load_fake_module():
    path = ROOT / "tests" / "fixtures" / "fake_module.py"
    spec = importlib.util.spec_from_file_location("c2_fake_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _teardown(app: App) -> None:
    async def run() -> None:
        session = getattr(app, "session", None)
        if session is not None:
            await session.close()  # detach sinks, stop queue workers, close sqlite
        await app.shutdown()

    asyncio.run(run())


@pytest.fixture()
def live_core(tmp_path):
    import uvicorn

    config = load_config(cli={"core.data_dir": str(tmp_path), "core.listen": "127.0.0.1:0"})
    app = App(config)
    asyncio.run(app.startup())  # identity, whitelist, registry, pairing, transport facade
    # Same second call `__main__._serve` makes: without it `app.session_handlers`
    # does not exist and every `ui/*` method is NOT_FOUND on a real socket.
    installed = install_components(app, strict=False)
    assert {"session", "router"} <= installed, installed

    core_app = CoreApp(
        config=config,
        identity=app.identity,
        registry=app.registry,
        pairing=app.pairing,
        whitelist=app.whitelist,
        make_crypto=build_module_crypto(app),
        on_rpc=make_rpc_handler(app),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            core_app,
            host="127.0.0.1",
            port=0,
            log_level="error",
            lifespan="off",
            # Production disables uvicorn's own XFF rewrite (`_serve` does the
            # same): otherwise 127.0.0.1 is "trusted" and every forged header
            # replaces scope["client"], defeating the 5/5min pair-code budget.
            proxy_headers=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    app._test_core_app = core_app  # type: ignore[attr-defined]
    try:
        yield app, f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/ws", tmp_path
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        _teardown(app)


def _hex_pub(identity) -> bytes:
    return bytes.fromhex(identity.public_key_hex)


def _fp(pub: bytes) -> str:
    from vade.crypto_gateway.session import fingerprint_of

    return fingerprint_of(pub)


def test_healthz_is_live_and_admin_is_absent(live_core):
    _app, http_url, _ws_url, _tmp = live_core
    response = httpx.get(f"{http_url}/healthz", timeout=5)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["version"]
    assert "policy_mode" in body
    # No config, no fingerprints, no module names on an unauthenticated route.
    assert set(body) == {"ok", "version", "policy_mode"}

    assert httpx.get(f"{http_url}/admin", timeout=5).status_code == 404
    assert httpx.get(f"{http_url}/admin/nodes", timeout=5).status_code == 404


def test_pair_code_flow_and_xff_is_not_trusted(live_core):
    app, http_url, _ws_url, _tmp = live_core
    code = app.pairing.create(Purpose.BROWSER)
    response = httpx.post(f"{http_url}/ui/session", json={"pair_code": code, "client_name": "test"}, timeout=5)
    assert response.status_code == 200, response.text
    assert response.json()["device_id"].startswith("ui-")
    set_cookie = response.headers.get("set-cookie", "")
    assert "__Host-ac_ui=" in set_cookie
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie and "SameSite=Strict" in set_cookie

    # The same code cannot be consumed twice.
    second = httpx.post(f"{http_url}/ui/session", json={"pair_code": code}, timeout=5)
    assert second.status_code == 400
    assert second.json()["code"] == "CODE_INVALID"

    # A forged X-Forwarded-For must NOT buy a fresh budget. The reused code
    # above already spent one of the five attempts for this socket; four more
    # failures exhaust it, whatever header each request carries.
    for index in range(4):
        attempt = httpx.post(
            f"{http_url}/ui/session",
            json={"pair_code": f"青松-AAAA{chr(ord('A') + index)}A"},
            headers={"X-Forwarded-For": f"203.0.113.{index}"},
            timeout=5,
        )
        assert attempt.status_code == 400, attempt.text
    locked = httpx.post(
        f"{http_url}/ui/session",
        json={"pair_code": "青松-AAAAAZ"},
        headers={"X-Forwarded-For": "198.51.100.9"},
        timeout=5,
    )
    assert locked.status_code == 429, locked.text
    assert locked.json()["code"] == "ADMIN_LOCKED"
    assert locked.headers.get("retry-after") == "300"


def test_fake_module_registers_over_real_ws(live_core):
    app, _http_url, ws_url, tmp_path = live_core
    fake = _load_fake_module()

    module_identity_path = tmp_path / "module.key"
    module_identity = load_or_create(module_identity_path)
    module_pub = _hex_pub(module_identity)
    app.whitelist.add(MODULE_ID, INSTANCE_ID, _fp(module_pub), public_key=module_pub)

    with fake.FakeModule(ws_url, _hex_pub(app.identity), identity_path=module_identity_path) as client:
        reply = client.register(json.loads(GOOD_MANIFEST.read_text(encoding="utf-8")))

    assert "error" not in reply, reply
    result = reply["result"]
    assert result["accepted"] is True
    assert result["session_id"] == f"{MODULE_ID}#{INSTANCE_ID}"
    assert "read_file" in result["tool_names"]

    record = app.registry.get(MODULE_ID, INSTANCE_ID)
    assert record is not None
    assert record.fingerprint == _fp(module_pub)
    assert record.connection_id
    # The snapshot C5a would render must work for a live node.
    [node] = app.registry_api.snapshot_for_admin()
    assert node["tool_count"] == len(result["tool_names"])


def test_bad_manifest_is_rejected_over_the_wire_with_a_pointer(live_core):
    app, _http_url, ws_url, tmp_path = live_core
    fake = _load_fake_module()
    module_identity_path = tmp_path / "module.key"
    module_identity = load_or_create(module_identity_path)
    module_pub = _hex_pub(module_identity)
    app.whitelist.add(MODULE_ID, INSTANCE_ID, _fp(module_pub), public_key=module_pub)

    with fake.FakeModule(ws_url, _hex_pub(app.identity), identity_path=module_identity_path) as client:
        reply = client.register(json.loads(BAD_MANIFEST.read_text(encoding="utf-8")))

    assert reply["error"]["code"] == "MANIFEST_INVALID"
    assert "/capabilities/tools/0/name" in reply["error"]["message"]
    assert app.registry.get(MODULE_ID, INSTANCE_ID) is None


def test_browser_session_ws_and_revoke_closes_only_that_device(live_core):
    from websockets.sync.client import connect

    app, http_url, ws_url, _tmp = live_core
    codes = [app.pairing.create(Purpose.BROWSER) for _ in range(2)]
    tokens: list[str] = []
    devices: list[str] = []
    for code in codes:
        response = httpx.post(f"{http_url}/ui/session", json={"pair_code": code}, timeout=5)
        assert response.status_code == 200, response.text
        devices.append(response.json()["device_id"])
        cookie = response.headers["set-cookie"]
        tokens.append(cookie.split("__Host-ac_ui=", 1)[1].split(";", 1)[0])

    first = connect(ws_url, additional_headers={"Cookie": f"__Host-ac_ui={tokens[0]}"}, open_timeout=5)
    second = connect(ws_url, additional_headers={"Cookie": f"__Host-ac_ui={tokens[1]}"}, open_timeout=5)
    try:
        # browser_session == plain JSON, no Noise frame header.
        first.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "module/heartbeat"}))
        reply = json.loads(first.recv(timeout=5))
        assert reply["id"] == 1

        # Revoke device 0: its socket closes, device 1 must survive.
        core_app = app._test_core_app  # type: ignore[attr-defined]
        ids = core_app.revoke_session(devices[0])
        assert ids, "revoke must report the closed connection"
        with pytest.raises(Exception):
            first.recv(timeout=3)
        second.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "module/heartbeat"}))
        assert json.loads(second.recv(timeout=5))["id"] == 2
    finally:
        first.close()
        second.close()


def test_a_real_cookie_socket_carries_the_device_identity(live_core):
    """`POST /ui/session` → `/ws`: the live connection knows which device it is.

    RFC-0004 §6.4 compares `file_id` ownership against `connection.device_id`;
    the server used to build `Connection` without one, so the value never
    existed on a real socket even though the RPC layer read it.
    """
    from websockets.sync.client import connect

    app, http_url, ws_url, _tmp = live_core
    code = app.pairing.create(Purpose.BROWSER)
    response = httpx.post(f"{http_url}/ui/session", json={"pair_code": code}, timeout=5)
    assert response.status_code == 200, response.text
    device_id = response.json()["device_id"]
    token = response.headers["set-cookie"].split("__Host-ac_ui=", 1)[1].split(";", 1)[0]

    core_app = app._test_core_app  # type: ignore[attr-defined]
    ws = connect(ws_url, additional_headers={"Cookie": f"__Host-ac_ui={token}"}, open_timeout=5)
    try:
        deadline = time.time() + 5
        connection = None
        while time.time() < deadline:
            connection = next(
                (c for c in core_app._connections.values()
                 if getattr(c, "ui_token", None) == token),
                None,
            )
            if connection is not None:
                break
            time.sleep(0.02)
        assert connection is not None, "the browser socket must be registered"
        assert connection.device_id == device_id
    finally:
        ws.close()


def _recv_until(ws, msg_id, timeout: float = 8.0):
    """Read frames until the JSON-RPC reply with `msg_id`; collect events seen."""
    events: list[dict] = []
    while True:
        message = json.loads(ws.recv(timeout=timeout))
        if message.get("id") == msg_id:
            return message, events
        if message.get("method") == "event":
            events.append(message)


def _recv_event(ws, predicate, timeout: float = 8.0) -> dict:
    while True:
        message = json.loads(ws.recv(timeout=timeout))
        if message.get("method") == "event" and predicate(message["params"]):
            return message


def _await_event(ws, predicate, already=(), timeout: float = 8.0) -> dict:
    """Look in the frames already consumed, then keep reading the socket."""
    for message in already:
        if message.get("method") == "event" and predicate(message["params"]):
            return message
    return _recv_event(ws, predicate, timeout)


def test_ui_attach_submit_detach_over_a_real_browser_socket(live_core):
    """`ui/*` must work on a real WS, not only through a hand-built handler."""
    from websockets.sync.client import connect

    app, http_url, ws_url, _tmp = live_core
    assert "ui/submit" in app.session_handlers
    assert app.session_rpc is not None
    # No model is needed: with no module online the loop answers honestly and
    # the turn still completes, which is what proves the queue ran it.
    app.loop.llm = MockLLMProvider([])

    code = app.pairing.create(Purpose.BROWSER)
    response = httpx.post(f"{http_url}/ui/session", json={"pair_code": code}, timeout=5)
    assert response.status_code == 200, response.text
    token = response.headers["set-cookie"].split("__Host-ac_ui=", 1)[1].split(";", 1)[0]

    ws = connect(ws_url, additional_headers={"Cookie": f"__Host-ac_ui={token}"}, open_timeout=5)
    try:
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ui/attach",
                            "params": {"session_id": None}}))
        reply, events = _recv_until(ws, 1)
        assert "error" not in reply, reply
        result = reply["result"]
        session_id = result["session_id"]
        assert result["from_seq"] >= 0
        assert result["snapshot"]["session"]["id"] == session_id
        assert result["snapshot"]["queue"]["pending"] == []
        # The attach itself is an event on the same bus.
        attached = _await_event(ws, lambda p: p["type"] == "session.attached", events)
        assert attached["params"]["payload"]["snapshot"] is True

        ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ui/submit", "params": {
            "session_id": session_id, "client_msg_id": "m1",
            "content": [{"type": "text", "text": "hello over the wire"}]}}))
        reply, events = _recv_until(ws, 2)
        assert "error" not in reply, reply
        turn_id = reply["result"]["turn_id"]
        assert reply["result"]["queue_pos"] == 1
        queued = _await_event(
            ws, lambda p: p["type"] == "turn.queued" and p["payload"]["turn_id"] == turn_id, events
        )
        assert queued["params"]["payload"]["state"] == "queued"

        finished = _await_event(
            ws, lambda p: p["type"] == "turn.completed" and p["payload"]["turn_id"] == turn_id, events
        )
        assert finished["params"]["payload"]["state"] == "completed"
        persisted = [e.type for e in app.session_store.read_since(session_id, 0)]
        assert "turn.completed" in persisted and "turn.queued" in persisted

        ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ui/detach",
                            "params": {"session_id": session_id}}))
        reply, _ = _recv_until(ws, 3)
        assert reply["result"] == {"ok": True}
        # The second detach is an error the UI can render, not a silent success.
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ui/detach",
                            "params": {"session_id": session_id}}))
        reply, _ = _recv_until(ws, 4)
        assert reply["error"]["code"] == "NOT_FOUND"
    finally:
        ws.close()
