"""RFC-0004 §6.4: the device identity of a real connection.

`transport/server.py` built `Connection` without `device_id`, so on a live
socket every `ui/submit` carrying a `file_id` answered
`PERMISSION_DENIED/device_unknown` — even for the device that had just uploaded
the file. The fail-closed answer was right; the missing identity was not.

Two rules are pinned here:

* the browser identity is the one `/ui/session` minted and stored server-side.
  The cookie is only a lookup key: an unknown or revoked key grants nothing,
  and nothing is ever read from a query parameter or header;
* the native identity is the pairing behind the static key the IK handshake
  proved. No pairing ⇒ `device_id is None`, never a value the peer supplied.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import pytest

from vade.blob.ids import new_blob_id
from vade.blob.service import BlobService
from vade.blob.settings import AttachSettings
from vade.blob.store import BlobRecord
from vade.config import load_config
from vade.crypto_gateway.session import CandidateKeySession, CryptoSession, fingerprint_of
from vade.identity.pairing import PairingStore, Purpose
from vade.identity.whitelist import Whitelist
from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore
from vade.transport.connection import ConnectionState
from vade.transport.framing import FrameHeader, FrameType, decode_handshake, encode_handshake
from vade.transport.server import CoreApp

SESSION_ID = "s-device"


# --------------------------------------------------------------------- harness
class _Socket:
    """Minimal ASGI peer: collects outbound messages, serves inbound ones."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(self, message: dict[str, Any]) -> None:
        self.frames.append(message)

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()


class _PassthroughCrypto:
    """Test double for the gateway: one handshake message, then plain frames.

    Only used to reach states a real handshake cannot reach by design (an
    unpaired fingerprint is refused *inside* the gateway, before msg2). The
    real state machine is exercised with a real Noise peer in
    `test_a_noise_socket_...` below.
    """

    def __init__(self, fingerprint: str | None = None) -> None:
        self.remote_static = None
        self.remote_fingerprint = fingerprint
        self._finished = False

    def start(self) -> None:
        return None

    def read_handshake(self, message: bytes) -> None:
        self._finished = True
        return None

    def is_finished(self) -> bool:
        return self._finished

    def establish(self) -> None:
        return None

    def seal(self, frame_type: FrameType, plaintext: bytes) -> bytes:
        return bytes(plaintext)

    def open(self, wire: bytes) -> tuple[FrameHeader, bytes]:
        header = FrameHeader(
            version=1, type=FrameType.RPC, flags=0, reserved=0,
            payload_len=len(wire), generation=1, counter=0,
        )
        return header, bytes(wire)


def _config(tmp_path) -> Any:
    return load_config(cli={"core.data_dir": str(tmp_path)})


async def _wait(predicate, timeout: float = 2.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _outbound(sock: _Socket) -> list[bytes]:
    return [frame["bytes"] for frame in sock.frames
            if frame["type"] == "websocket.send" and frame.get("bytes") is not None]


async def _open(app: CoreApp, conns: dict[str, Any], *, cookie: str | None = None):
    sock = _Socket()
    headers = [(b"cookie", f"__Host-ac_ui={cookie}".encode())] if cookie else []
    scope = {"type": "websocket", "path": "/ws", "headers": headers}
    task = asyncio.create_task(app(scope, sock.receive, sock.send))
    connection = await _wait(lambda: next(iter(conns.values()), None), what="the connection")
    return sock, task, connection


async def _shutdown(sock: _Socket, task: asyncio.Task[Any]) -> None:
    sock.inbox.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(task, timeout=2)


async def _login(app: CoreApp, code: str) -> tuple[str, str]:
    """Drive a real `POST /ui/session`; return `(device_id, token)`."""
    sock = _Socket()
    body = json.dumps({"pair_code": code, "client_name": "pytest"}).encode()
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    await app({"type": "http", "method": "POST", "path": "/ui/session", "headers": []}, receive, sock.send)
    start, payload = sock.frames[0], sock.frames[1]
    assert start["status"] == 200, payload["body"]
    cookie = next(
        value.decode() for key, value in start["headers"]
        if key.lower() == b"set-cookie" and b"__Host-ac_ui=" in value
    )
    token = cookie.split("__Host-ac_ui=", 1)[1].split(";", 1)[0]
    return json.loads(payload["body"])["device_id"], token


async def _reply(sock: _Socket, message_id: int, timeout: float = 2.0) -> dict[str, Any]:
    def find() -> dict[str, Any] | None:
        for raw in _outbound(sock):
            try:
                message = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # a Noise frame, not a browser reply
            if isinstance(message, dict) and message.get("id") == message_id:
                return message
        return None

    return await _wait(find, timeout, f"reply {message_id}")


async def _handshake(sock: _Socket, initiator: CryptoSession) -> None:
    first = initiator.start()
    assert first
    sock.inbox.put_nowait({"type": "websocket.receive", "bytes": encode_handshake(first)})

    def msg2() -> bytes | None:
        for raw in _outbound(sock):
            try:
                return decode_handshake(raw)
            except Exception:
                continue
        return None

    reply = initiator.read_handshake(await _wait(msg2, what="msg2"))
    if reply:
        sock.inbox.put_nowait({"type": "websocket.receive", "bytes": encode_handshake(reply)})
    initiator.establish()


async def _echo(turn, content) -> TurnOutcome:
    return TurnOutcome(assistant_text="ok", stopped_reason="stop")


def _session_log(tmp_path, service: BlobService) -> SessionLog:
    log = SessionLog(
        SQLiteStore(tmp_path / "sessions.db"),
        replay=ReplayBuffer(maxlen=100),
        blobs=service,
    )
    log.set_executor(_echo)
    log.create_session(session_id=SESSION_ID)
    return log


def _blob_owned_by(service: BlobService, device_id: str) -> BlobRecord:
    now = time.time()
    record = BlobRecord(
        file_id=new_blob_id(),
        device_id=device_id,
        sha256=hashlib.sha256(device_id.encode()).hexdigest(),
        size=180_000,
        mime="image/jpeg",
        name="IMG_0042.jpg",
        kind="image",
        width=800,
        height=600,
        pinned=False,
        expires_at=now + 3600,
        created_at=now,
        scan="clean",
        state="ok",
    )
    return service.store.add_blob(record)


def _image_part(record: BlobRecord) -> dict[str, Any]:
    return {
        "type": "image",
        "file_id": record.file_id,
        "mime": record.mime,
        "name": record.name,
        "size": record.size,
        "width": record.width,
        "height": record.height,
        "sha256": record.sha256,
    }


def _submit(sock: _Socket, message_id: int, part: dict[str, Any]) -> None:
    frame = {
        "jsonrpc": "2.0", "id": message_id, "method": "ui/submit",
        "params": {"session_id": SESSION_ID, "client_msg_id": f"m{message_id}",
                   "content": [{"type": "text", "text": "看看这个"}, part]},
    }
    sock.inbox.put_nowait({"type": "websocket.receive", "bytes": json.dumps(frame).encode()})


async def _drain(log: SessionLog) -> None:
    await log.wait_idle(SESSION_ID)
    await log.close()


# ------------------------------------------------------------------ browser path
@pytest.mark.asyncio
async def test_the_cookie_socket_is_bound_to_the_device_id_ui_session_returned(tmp_path):
    service = BlobService(tmp_path / "blob", AttachSettings(read_scope="core"))
    log = _session_log(tmp_path, service)
    rpc = SessionRPC(log)

    async def on_rpc(connection, message):
        return await rpc.dispatch(connection, str(message.get("method")), message.get("params"))

    conns: dict[str, Any] = {}
    pairing = PairingStore(tmp_path / "pairing.sqlite3")
    app = CoreApp(config=_config(tmp_path), pairing=pairing, on_rpc=on_rpc, connections=conns)
    device_id, token = await _login(app, pairing.create(Purpose.BROWSER))
    assert device_id.startswith("ui-")

    sock, task, connection = await _open(app, conns, cookie=token)
    try:
        assert connection.browser_session is True
        assert connection.device_id == device_id, "the cookie must resolve to the session row"

        # A file this device uploaded must be submittable — the check that the
        # bug made impossible (`device_unknown` for its own file).
        record = _blob_owned_by(service, device_id)
        _submit(sock, 1, _image_part(record))
        reply = await _reply(sock, 1)
        assert "error" not in reply, reply
        assert reply["result"]["queue_pos"] == 1
    finally:
        await _drain(log)
        await _shutdown(sock, task)


@pytest.mark.asyncio
async def test_a_revoked_cookie_grants_no_identity_and_its_file_submit_is_denied(tmp_path):
    service = BlobService(tmp_path / "blob", AttachSettings(read_scope="core"))
    log = _session_log(tmp_path, service)
    rpc = SessionRPC(log)

    async def on_rpc(connection, message):
        return await rpc.dispatch(connection, str(message.get("method")), message.get("params"))

    conns: dict[str, Any] = {}
    pairing = PairingStore(tmp_path / "pairing.sqlite3")
    # A native-capable app: a revoked cookie must fall back to "anonymous",
    # not to "accepted as a browser session".
    app = CoreApp(
        config=_config(tmp_path), pairing=pairing, on_rpc=on_rpc, connections=conns,
        make_crypto=lambda connection_id: _PassthroughCrypto(),
    )
    device_id, token = await _login(app, pairing.create(Purpose.BROWSER))
    assert app.revoke_session(device_id) == ()

    sock, task, connection = await _open(app, conns, cookie=token)
    try:
        sock.inbox.put_nowait({"type": "websocket.receive", "bytes": encode_handshake(b"stub")})
        await _wait(lambda: connection.state is ConnectionState.ESTABLISHED, what="the handshake")
        assert connection.browser_session is False
        assert connection.device_id is None

        record = _blob_owned_by(service, device_id)
        _submit(sock, 2, _image_part(record))
        reply = await _reply(sock, 2)
        assert reply["error"]["code"] == "PERMISSION_DENIED"
        assert reply["error"]["data"] == {
            "field": "content[1].file_id", "reason": "device_unknown",
        }
        loaded = log.store.load(SESSION_ID)
        assert loaded is not None and loaded.turns == ()
    finally:
        await _drain(log)
        await _shutdown(sock, task)


# ------------------------------------------------------------------- noise path
@pytest.mark.asyncio
async def test_a_noise_socket_is_bound_to_the_device_that_owns_the_verified_key(tmp_path):
    proto = pytest.importorskip("vade_protocol")
    core_identity = proto.FfiIdentity.generate()
    module_identity = proto.FfiIdentity.generate()
    module_pub = bytes.fromhex(module_identity.public_key_hex())

    whitelist = Whitelist(tmp_path / "identity" / "whitelist.json")
    whitelist.add("com.example.fs", "fs-01", fingerprint_of(module_pub), public_key=module_pub)

    def make_crypto(connection_id: str) -> CandidateKeySession:
        return CandidateKeySession(
            core_identity, whitelist.candidate_keys(), whitelist=whitelist,
        )

    conns: dict[str, Any] = {}
    app = CoreApp(config=_config(tmp_path), whitelist=whitelist,
                  make_crypto=make_crypto, connections=conns)
    sock, task, connection = await _open(app, conns)
    try:
        initiator = CryptoSession(
            module_identity,
            mode="ik",
            role="initiator",
            connection="module_initiated",
            remote_pub=bytes.fromhex(core_identity.public_key_hex()),
        )
        await _handshake(sock, initiator)
        assert connection.state is ConnectionState.ESTABLISHED
        assert connection.browser_session is False
        # The identity is the pairing behind the key the IK handshake proved.
        assert connection.device_id == "com.example.fs#fs-01"
    finally:
        await _shutdown(sock, task)


@pytest.mark.asyncio
async def test_an_unpaired_fingerprint_stays_anonymous_and_fail_closed(tmp_path):
    """No pairing ⇒ no identity ⇒ a `file_id` submit is still refused."""
    service = BlobService(tmp_path / "blob", AttachSettings(read_scope="core"))
    log = _session_log(tmp_path, service)
    rpc = SessionRPC(log)

    async def on_rpc(connection, message):
        return await rpc.dispatch(connection, str(message.get("method")), message.get("params"))

    stranger = bytes(range(32))
    whitelist = Whitelist(tmp_path / "identity" / "whitelist.json")
    whitelist.add("com.example.other", "o-1", fingerprint_of(stranger), public_key=stranger)
    crypto = _PassthroughCrypto(fingerprint="0" * 16)  # verified, but nobody's pairing

    conns: dict[str, Any] = {}
    app = CoreApp(config=_config(tmp_path), whitelist=whitelist, on_rpc=on_rpc,
                  connections=conns, make_crypto=lambda connection_id: crypto)
    sock, task, connection = await _open(app, conns)
    try:
        sock.inbox.put_nowait({"type": "websocket.receive", "bytes": encode_handshake(b"stub")})
        await _wait(lambda: connection.state is ConnectionState.ESTABLISHED, what="the handshake")
        assert connection.device_id is None

        record = _blob_owned_by(service, "com.example.fs#fs-01")
        _submit(sock, 3, _image_part(record))
        reply = await _reply(sock, 3)
        assert reply["error"]["code"] == "PERMISSION_DENIED"
        assert reply["error"]["data"]["reason"] == "device_unknown"
    finally:
        await _drain(log)
        await _shutdown(sock, task)


# -------------------------------------------------------------------- resolver
def test_one_key_paired_twice_resolves_to_one_canonical_name(tmp_path):
    """A stable answer across restarts; `None` for a key nobody paired."""
    key = bytes(range(32))
    fingerprint = fingerprint_of(key)
    whitelist = Whitelist(tmp_path / "identity" / "whitelist.json")
    whitelist.add("com.example.zeta", "z-1", fingerprint, public_key=key)
    assert whitelist.device_for_fingerprint(fingerprint) == "com.example.zeta#z-1"

    whitelist.add("com.example.alpha", "a-9", fingerprint, public_key=key)
    assert whitelist.device_for_fingerprint(fingerprint) == "com.example.alpha#a-9"
    assert whitelist.device_for_fingerprint(fingerprint_of(bytes(range(32, 64)))) is None
