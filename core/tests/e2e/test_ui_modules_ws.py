"""Real socket: register a module over Noise, then ask `ui/modules` as a browser.

The unit tests call the service directly, so they cannot catch the failure mode
this file exists for: a method that is declared in `rpc.json` and implemented in
`service.py` but never reaches a socket (the handler table is a separate object,
and a missing entry answers `-32601`). Everything here goes over TCP + WS:
`module/register` on a Noise peer, `/ui/session` for the browser cookie, then
`ui/modules` on that cookie's socket.
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
from vade.transport.server import CoreApp
from vade.__main__ import build_module_crypto

ROOT = Path(__file__).resolve().parents[3]
MODULE_ID = "com.example.download"
INSTANCE_ID = "dl-1"
PAGE_ROW_KEYS = {"id", "title", "icon", "entry", "version", "requires", "refresh_s"}
MODULE_ROW_KEYS = {"module_id", "instance_id", "display_name", "version", "online", "pages"}

MANIFEST = {
    "module_id": MODULE_ID,
    "instance_id": INSTANCE_ID,
    "display_name": "下载管理",
    "version": "0.3.1",
    "protocol": "vade/v1",
    "capabilities": {
        "tools": [
            {
                "name": "refresh",
                "description": "Refresh the download queue",
                "input_schema": {"type": "object", "properties": {}},
                "risk": "low",
                "idempotent": True,
                "timeout_ms": 10000,
            }
        ]
    },
    "limits": {"max_concurrency": 1, "max_payload": 65519},
    "ui": {
        "pages": [
            {"id": "queue", "title": "下载队列", "version": 1,
             "schema": {"type": "column", "children": []}},
            {"id": "settings", "title": "下载设置", "version": 2, "requires": ["session"],
             "icon": "settings", "entry": "panel", "refresh_s": 5,
             "schema": {"type": "column", "children": []}},
        ]
    },
}


def _load_fake_module():
    path = ROOT / "tests" / "fixtures" / "fake_module.py"
    spec = importlib.util.spec_from_file_location("c2_fake_module_modules", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _hex_pub(identity) -> bytes:
    return bytes.fromhex(identity.public_key_hex)


def _fp(pub: bytes) -> str:
    from vade.crypto_gateway.session import fingerprint_of

    return fingerprint_of(pub)


def _teardown(app: App) -> None:
    async def run() -> None:
        session = getattr(app, "session", None)
        if session is not None:
            await session.close()
        await app.shutdown()

    asyncio.run(run())


@pytest.fixture()
def live_core(tmp_path):
    import uvicorn

    config = load_config(cli={"core.data_dir": str(tmp_path), "core.listen": "127.0.0.1:0"})
    app = App(config)
    asyncio.run(app.startup())
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


def _recv_reply(ws, msg_id: int, timeout: float = 8.0) -> dict:
    while True:
        message = json.loads(ws.recv(timeout=timeout))
        if message.get("id") == msg_id:
            return message


def test_ui_modules_over_a_real_browser_socket(live_core):
    from websockets.sync.client import connect

    app, http_url, ws_url, tmp_path = live_core
    fake = _load_fake_module()

    # 1. A module registers over the real Noise channel (the same path M-group
    #    modules use), and stays connected so `online` is true.
    identity_path = tmp_path / "module.key"
    identity = load_or_create(identity_path)
    pub = _hex_pub(identity)
    app.whitelist.add(MODULE_ID, INSTANCE_ID, _fp(pub), public_key=pub)

    # 2. A browser session: this socket carries `ui-<hex>` and nothing else.
    code = app.pairing.create(Purpose.BROWSER)
    response = httpx.post(f"{http_url}/ui/session", json={"pair_code": code}, timeout=5)
    assert response.status_code == 200, response.text
    device_id = response.json()["device_id"]
    assert device_id.startswith("ui-")
    token = response.headers["set-cookie"].split("__Host-ac_ui=", 1)[1].split(";", 1)[0]

    with fake.FakeModule(ws_url, _hex_pub(app.identity), identity_path=identity_path) as client:
        reply = client.register(json.loads(json.dumps(MANIFEST)))
        assert "error" not in reply, reply
        assert reply["result"]["accepted"] is True

        ws = connect(ws_url, additional_headers={"Cookie": f"__Host-ac_ui={token}"}, open_timeout=5)
        try:
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ui/modules", "params": {}}))
            answer = _recv_reply(ws, 1)
            assert "error" not in answer, answer
            result = answer["result"]

            assert result["revision"] >= 1, "registration bumps the registry revision"
            [row] = result["modules"]
            assert set(row) == MODULE_ROW_KEYS
            assert row["module_id"] == MODULE_ID
            assert row["instance_id"] == INSTANCE_ID
            assert row["display_name"] == "下载管理"
            assert row["version"] == "0.3.1"
            assert row["online"] is True
            assert [item["id"] for item in row["pages"]] == ["queue", "settings"]
            assert set(row["pages"][0]) == PAGE_ROW_KEYS
            assert row["pages"][1]["requires"] == ["session"]
            assert row["pages"][1]["refresh_s"] == 5
            assert row["pages"][1]["entry"] == "panel"

            # 3. A second pull is a no-op: same revision, same list (§5).
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ui/modules",
                                "params": {"unknown_future_key": True}}))
            again = _recv_reply(ws, 2)
            assert again["result"] == result

            # 4. No page body ever crossed: the client would render `schema` as
            #    a node tree, and `ui/page.get` stays the only body channel.
            wire = json.dumps(result, ensure_ascii=False)
            for forbidden in ('"schema"', '"children"', '"$defs"'):
                assert forbidden not in wire
        finally:
            ws.close()

    # 5. The method is on the live dispatch table, not just in the schema.
    assert "ui/modules" in app.session_handlers
