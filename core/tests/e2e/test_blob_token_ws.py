"""Real socket: a native Noise device mints `ui/blob.token`, uploads over the
six HTTP routes, submits the file_id, and a brand-new connection sees it.

The unit tests drive `BlobTokenRPC` by hand, so they cannot catch the failure
mode this file exists for: the handler never reaching a socket (the provider
not drained into `app.session_handlers` answers NOT_FOUND on a real wire), or
the minted token not being accepted by the mounted `/ui/blob*` sub-app.
Everything here goes over TCP + WS + HTTP against a real uvicorn listener
(RFC-0016 §10 A2 ws leg + A12 protocol repro).

A12's full acceptance (a real desktop drag-and-drop, screenshots) is the
client batch's job; this file is the protocol-layer reproduction the RFC
prescribes: submit, then `ui/attach` from a FRESH connection sees the part.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from vade.app import App, install_components, make_rpc_handler
from vade.config import load_config
from vade.identity.store import load_or_create
from vade.llm.mock import MockLLMProvider
from vade.transport.server import CoreApp
from vade.__main__ import build_module_crypto

ROOT = Path(__file__).resolve().parents[3]
MODULE_ID = "com.example.uploader"
INSTANCE_ID = "up-1"
DEVICE = f"{MODULE_ID}#{INSTANCE_ID}"

# --- 墙钟阈值：只当**死锁保险**，不当性能断言 ------------------------------
#
# 第一版写的是 5 s，结果在 12 路并发抢机器的全量跑里，整份套件 18 分 19 秒，
# 这条用例的「铸 token → 上传」腿直接 blow（单跑 8.0–12.0 s 稳定绿）。
# 5 s 那会儿不是在测「快不快」，是在测「机器闲不闲」——那不是它该管的事。
#
# 现在：阈值给足（30 s），超时要**说出来是哪一段、过了多久**，因为裸的
# `httpx.ReadTimeout` 对下一个读日志的人等于零信息。
# 需要收紧时用环境变量，不要改默认值：`VADE_E2E_HTTP_TIMEOUT=5 pytest ...`。
HTTP_TIMEOUT = float(os.environ.get("VADE_E2E_HTTP_TIMEOUT", "30"))
WS_TIMEOUT = float(os.environ.get("VADE_E2E_WS_TIMEOUT", "30"))
JOIN_TIMEOUT = float(os.environ.get("VADE_E2E_JOIN_TIMEOUT", "30"))


def _timeout_diagnosis(fn):
    """把裸超时换成「哪一层、过了多久、下一步查什么」的断言。

    没有这层，负载下的红与真背压下的红在日志里长得一模一样——今晚已经因此
    差点把这条用例当成「新端点坏了」去查一遍后端。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except httpx.TimeoutException as exc:
            elapsed = time.monotonic() - started
            raise AssertionError(
                f"{fn.__name__}: HTTP/WS 超时（已过 {elapsed:.1f}s；"
                f"HTTP 阈值 {HTTP_TIMEOUT}s、WS 阈值 {WS_TIMEOUT}s、join {JOIN_TIMEOUT}s）。"
                "分辨办法：单跑本文件应 8–12s 绿 ⇒ 是机器负载；"
                "单跑仍红 ⇒ 是真背压（端点持锁读 body / provider 未排空），别调大阈值。"
            ) from exc

    return wrapper


#: 2600 ASCII bytes → three 1024-byte parts on the default part size.
BODY = b"vade-blob-token-e2e-" * 130
DIGEST = hashlib.sha256(BODY).hexdigest()


def _load_fake_module():
    path = ROOT / "tests" / "fixtures" / "fake_module.py"
    spec = importlib.util.spec_from_file_location("c2_fake_module_blobtoken", path)
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

    config = load_config(cli={
        "core.data_dir": str(tmp_path), "core.listen": "127.0.0.1:0",
        # The cookie four-gate needs an allowed Origin to answer at all
        # (auth.py:118-123) — a real deployment sets server.ui_origins. The
        # origin host must also be a listen host (`_check_origins`), and the
        # listener here binds 127.0.0.1 on an ephemeral port.
        "server.ui_origins": ["https://127.0.0.1"],
    })
    app = App(config)
    asyncio.run(app.startup())
    installed = install_components(app, strict=False)
    assert {"session", "router"} <= installed, installed
    assert app.blob is not None, "attach defaults to enabled; the data plane must be mounted"

    core_app = CoreApp(
        config=config,
        identity=app.identity,
        registry=app.registry,
        pairing=app.pairing,
        whitelist=app.whitelist,
        make_crypto=build_module_crypto(app),
        on_rpc=make_rpc_handler(app),
        blob=app.blob,
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
        thread.join(timeout=JOIN_TIMEOUT)
        _teardown(app)


def _recv_reply(ws, msg_id: int, timeout: float = WS_TIMEOUT) -> dict:
    while True:
        message = json.loads(ws.recv(timeout=timeout))
        if message.get("id") == msg_id:
            return message


@_timeout_diagnosis
def test_native_device_mints_uploads_submits_and_a_fresh_connection_sees_it(live_core):
    from websockets.sync.client import connect

    app, http_url, ws_url, tmp_path = live_core
    fake = _load_fake_module()

    # 1. Pair the uploader (Noise static key -> whitelist) and connect.
    identity_path = tmp_path / "uploader.key"
    identity = load_or_create(identity_path)
    pub = _hex_pub(identity)
    app.whitelist.add(MODULE_ID, INSTANCE_ID, _fp(pub), public_key=pub)

    with fake.FakeModule(ws_url, _hex_pub(app.identity), identity_path=identity_path) as native:
        # 2. Mint over the already-proven device channel (RFC-0016 §4).
        reply = native.call("ui/blob.token", {})
        assert "error" not in reply, reply
        token = reply["result"]["token"]
        expires_at = reply["result"]["expires_at"]
        assert isinstance(expires_at, int) and expires_at > time.time() * 1000
        assert set(reply["result"]["endpoints"]) == {
            "begin", "part", "complete", "abort", "resume", "fetch",
        }
        assert len(token) == 43

        # 3. The six HTTP routes accept the minted bearer.
        auth = {"Authorization": f"Bearer {token}"}
        begun = httpx.post(
            f"{http_url}/ui/blob",
            headers={**auth, "Content-Type": "application/json"},
            json={"kind": "file", "sha256": DIGEST, "size": len(BODY),
                  "mime": "text/plain", "name": "up.txt"},
            timeout=HTTP_TIMEOUT,
        )
        assert begun.status_code in (200, 201), begun.text
        upload_id = begun.json()["upload_id"]
        part_size = int(begun.json()["part_size"])
        for offset in range(0, len(BODY), part_size):
            part = httpx.put(
                f"{http_url}/ui/blob/upload/{upload_id}",
                params={"offset": offset},
                headers={**auth, "Content-Type": "application/octet-stream"},
                content=BODY[offset:offset + part_size],
                timeout=HTTP_TIMEOUT,
            )
            assert part.status_code == 200, part.text
        done = httpx.post(
            f"{http_url}/ui/blob/upload/{upload_id}/complete",
            headers={**auth, "Content-Type": "application/json"},
            json={"sha256": DIGEST, "size": len(BODY)},
            timeout=HTTP_TIMEOUT,
        )
        assert done.status_code in (200, 201), done.text
        file_id = done.json()["file_id"]

        # The token is replayable (拍板 Q3): fetch through it again.
        fetched = httpx.get(f"{http_url}/ui/blob/{file_id}", headers=auth, timeout=HTTP_TIMEOUT)
        assert fetched.status_code == 200 and fetched.content == BODY

        # 4. The SAME connection submits the file_id (A3: the binding that was
        #    broken before this RFC — the cookie path could never do this).
        app.loop.llm = MockLLMProvider([])
        attached = native.call("ui/attach", {"session_id": None})
        assert "error" not in attached, attached
        session_id = attached["result"]["session_id"]
        submitted = native.call("ui/submit", {
            "session_id": session_id,
            "client_msg_id": "m-1",
            "content": [{
                "type": "file", "file_id": file_id,
                "mime": "text/plain", "name": "up.txt", "size": len(BODY),
            }],
        })
        assert "error" not in submitted, submitted

    # 5. A brand-new connection (browser cookie session, different identity)
    #    attaches to the same session and sees the attachment part (A12).
    from vade.identity.pairing import Purpose

    code = app.pairing.create(Purpose.BROWSER)
    login = httpx.post(f"{http_url}/ui/session", json={"pair_code": code}, timeout=HTTP_TIMEOUT)
    assert login.status_code == 200, login.text
    set_cookies = login.headers.get_list("set-cookie")
    ui_cookie = next(c for c in set_cookies if c.startswith("__Host-ac_ui="))
    csrf_cookie = next(c for c in set_cookies if c.startswith("__Host-ac_csrf="))
    ui_value = ui_cookie.split("=", 1)[1].split(";", 1)[0]
    csrf_value = csrf_cookie.split("=", 1)[1].split(";", 1)[0]

    # 5a. A cookie session must NOT be able to mint (拍板 Q1, over the wire).
    with connect(
        ws_url, additional_headers={"Cookie": f"__Host-ac_ui={ui_value}"}, open_timeout=HTTP_TIMEOUT
    ) as ws:
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ui/blob.token", "params": {}}))
        denied = _recv_reply(ws, 1)
        assert denied["error"]["code"] == "PERMISSION_DENIED", denied
        assert denied["error"]["data"]["reason"] == "cookie_session"

        ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ui/attach",
                            "params": {"session_id": session_id}}))
        again = _recv_reply(ws, 2)
        assert "error" not in again, again
        parts = again["result"]["snapshot"]["messages"][-1]["parts"]
        file_parts = [p for p in parts if p.get("file_id") == file_id]
        assert file_parts, f"the fresh attach must carry the uploaded part: {parts}"

        # 5b. The cookie read of the blob still renders (read_scope="core"
        # keeps cookie semantics; only token reads are device-scoped — Q7).
        # All four cookie gates of §8.2 ride along, as a real browser would.
        got = httpx.get(
            f"{http_url}/ui/blob/{file_id}",
            headers={
                "Cookie": f"__Host-ac_ui={ui_value}; __Host-ac_csrf={csrf_value}",
                "Origin": "https://127.0.0.1",
                "Sec-Fetch-Site": "same-origin",
                "x-vade-csrf": csrf_value,
            },
            timeout=HTTP_TIMEOUT,
        )
        assert got.status_code == 200, got.text
        assert got.content == BODY

    # 6. The blobs row is pinned by the submit with the right sha256 (A12).
    from vade.blob.auth import hash_token  # noqa: F401  (import guard: vade.blob importable)

    row = app.blob.service.store.get_blob(file_id)
    assert row is not None and row.pinned is True
    assert row.sha256 == DIGEST and row.device_id == DEVICE
