"""WS + Noise transport for a module process.

Layout: one `Client` per connection (one reader task, one writer lock, one
heartbeat task), wrapped by `serve()` which owns the reconnect loop.

Deliberate choices:
  * The Noise state machine is the C-01 binding (`vade_protocol`), not a second
    implementation. A hand-written Noise IK would be a second oracle for a
    frozen byte format, and every mismatch would look like "wrong pair code".
  * A single `asyncio.Lock` around `seal_frame` + `ws.send`: `TransportState`
    is not concurrency-safe, and interleaved seals show up as random frame loss.
  * `payload_len`/counter are validated by the binding's `open_frame`; this
    module only peeks the 20-byte header to separate heartbeat/close/rekey from
    RPC before handing the frame over.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import ssl
import struct
import sys
import time
from pathlib import Path
from typing import Any, Mapping

__all__ = ["serve", "Client", "TransportError", "load_or_create_key", "module_public_key"]

HEADER = struct.Struct(">BBBBIIQ")
HEADER_LEN = 20
MAX_PLAINTEXT = 65519

FT_HANDSHAKE = 0x01
FT_RPC = 0x02
FT_EVENT = 0x03
FT_HEARTBEAT = 0x04
FT_CLOSE = 0x05
FT_REKEY = 0x06

PROTOCOL = "vade/v1"
HEARTBEAT_S = 10.0
BACKOFF_MIN = 0.5
BACKOFF_MAX = 30.0


class TransportError(Exception):
    def __init__(self, code: str, message: str, *, fatal: bool = False) -> None:
        self.code = code
        self.message = message
        self.fatal = fatal
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------- key material
def _x25519_public(seed: bytes) -> bytes:
    """Derive the raw X25519 public key; binding first, `cryptography` second."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        key = X25519PrivateKey.from_private_bytes(seed)
        return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except ImportError:
        pass
    try:
        import vade_protocol  # type: ignore

        return bytes.fromhex(vade_protocol.FfiIdentity.from_secret_hex(seed.hex()).public_key_hex())
    except ImportError:
        raise TransportError(
            "INTERNAL",
            "cannot derive the module public key: install `cryptography` or the vade_protocol binding",
        ) from None


def module_public_key(seed_hex: str) -> bytes:
    return _x25519_public(bytes.fromhex(seed_hex))


def load_or_create_key(path: Path | str | None = None) -> tuple[str, bytes]:
    """`MODULE_KEY_PATH` (default `data/module.key`), 0600, NEVER overwritten.

    Overwriting the key changes the fingerprint and core's whitelist will
    reject the module — the failure looks like "pairing broke overnight", so
    the reuse rule is enforced here, not left to the caller.
    """
    target = Path(path or os.environ.get("MODULE_KEY_PATH") or "data/module.key")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    if target.exists():
        raw = target.read_text(encoding="utf-8").strip()
        if len(raw) != 64:
            raise TransportError("INTERNAL", f"{target} must contain 64 hex chars (32-byte X25519 seed)")
        seed = bytes.fromhex(raw)
        try:
            os.chmod(target, 0o600)
        except OSError:  # pragma: no cover
            pass
        return raw.lower(), _x25519_public(seed)

    seed = os.urandom(32).hex()
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(seed + "\n")
    return seed, _x25519_public(bytes.fromhex(seed))


def fingerprint_of(public_key: bytes) -> str:
    return hashlib.sha256(bytes(public_key)).hexdigest()[:16]


# ---------------------------------------------------------------- core pin
def _pin_path(app) -> Path:
    data_dir = Path(os.environ.get("VADE_DATA_DIR") or "data")
    return data_dir / "core_pin.json"


def load_core_public_key(app) -> bytes | None:
    env = os.environ.get("CORE_PUBLIC_KEY")
    if env:
        raw = bytes.fromhex(env.strip())
        if len(raw) != 32:
            raise TransportError("INVALID_ARGUMENT", "CORE_PUBLIC_KEY must be 32 bytes (64 hex chars)")
        return raw
    path = _pin_path(app)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("core_public_key")
    return bytes.fromhex(value) if value else None


def save_core_pin(app, public_key: bytes, *, tls_fingerprint: str | None = None) -> None:
    path = _pin_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "core_public_key": bytes(public_key).hex(),
        "fingerprint": fingerprint_of(public_key),
        "tls_fingerprint": tls_fingerprint,
        "pinned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")


# ---------------------------------------------------------------- TLS
def _tls_context(url: str, app) -> tuple[ssl.SSLContext | None, str | None]:
    if not url.startswith("wss://"):
        return None, None
    pin = os.environ.get("CORE_FINGERPRINT")
    if pin:
        # Pinned self-signed cert: verification is the pin, not the CA chain.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context, pin.lower()
    return ssl.create_default_context(), None


def _check_tls_pin(app, ws, expected: str | None) -> None:
    if expected is None:
        # No pin to verify. We do NOT invent one here: `core_pin.json` is
        # written when the Noise identity is known (save_core_pin), otherwise a
        # "pin" of an empty key would look like a trusted record.
        return
    try:
        ssl_object = ws.transport.get_extra_info("ssl_object")
    except AttributeError:  # pragma: no cover - plain ws
        return
    if ssl_object is None:
        return
    der = ssl_object.getpeercert(binary_form=True)
    actual = hashlib.sha256(der).hexdigest()[:16]
    if actual != expected:
        raise TransportError(
            "PERMISSION_DENIED",
            f"core TLS certificate fingerprint {actual} does not match the pin {expected}",
            fatal=True,
        )


# ---------------------------------------------------------------- Noise
class NoiseChannel:
    """Initiator-side IK handshake plus transport seal/open."""

    def __init__(self, seed_hex: str, core_public_key: bytes) -> None:
        try:
            import vade_protocol  # type: ignore
        except ImportError:
            raise TransportError(
                "INTERNAL",
                "the vade_protocol binding is not importable; connecting needs "
                "protocol/core (cargo build --features uniffi) or the published wheel",
            ) from None
        self._proto = vade_protocol
        self._identity = vade_protocol.FfiIdentity.from_secret_hex(seed_hex)
        try:
            self._handshake = vade_protocol.FfiHandshake(
                mode="ik",
                role="initiator",
                connection="module_initiated",
                local=self._identity,
                remote_pub=bytes(core_public_key),
                psk=None,
            )
        except Exception as exc:
            raise TransportError("PERMISSION_DENIED", f"cannot start IK handshake: {exc}", fatal=True) from None
        self._transport = None
        self._remote_public: bytes | None = None

    def start(self) -> bytes:
        return _frame(FT_HANDSHAKE, bytes(self._handshake.write(b"")))

    def read_handshake(self, wire: bytes) -> bytes | None:
        header = _peek(wire)
        if header["type"] != FT_HANDSHAKE:
            raise TransportError("PROTOCOL_MISMATCH", f"expected handshake frame, got 0x{header['type']:02x}", fatal=True)
        if len(wire) != HEADER_LEN + header["payload_len"]:
            raise TransportError("INVALID_ARGUMENT", "handshake frame length mismatch", fatal=True)
        try:
            self._handshake.read(wire[HEADER_LEN:])
            remote = self._handshake.remote_static()
            if remote is not None:
                self._remote_public = bytes(remote)
            if not self._handshake.is_finished():
                return _frame(FT_HANDSHAKE, bytes(self._handshake.write(b"")))
            return None
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError("PERMISSION_DENIED", f"handshake failed: {exc}", fatal=True) from None

    def establish(self) -> None:
        try:
            self._transport = self._handshake.into_transport()
        except Exception as exc:
            raise TransportError("PERMISSION_DENIED", f"cannot enter transport mode: {exc}", fatal=True) from None

    @property
    def remote_public(self) -> bytes | None:
        return self._remote_public

    def seal(self, frame_type: int, plaintext: bytes) -> bytes:
        if self._transport is None:
            raise TransportError("INVALID_ARGUMENT", "transport not established")
        if len(plaintext) > MAX_PLAINTEXT:
            raise TransportError("PAYLOAD_TOO_LARGE", f"payload {len(plaintext)} > {MAX_PLAINTEXT}", fatal=True)
        return bytes(self._transport.seal_frame(int(frame_type), plaintext))

    def open(self, wire: bytes) -> bytes:
        if self._transport is None:
            raise TransportError("INVALID_ARGUMENT", "transport not established")
        return bytes(self._transport.open_frame(bytes(wire)))


def _frame(frame_type: int, payload: bytes) -> bytes:
    return HEADER.pack(1, frame_type, 0, 0, len(payload), 0, 0) + bytes(payload)


def _peek(wire: bytes) -> dict[str, Any]:
    if len(wire) < HEADER_LEN:
        raise TransportError("INVALID_ARGUMENT", f"need {HEADER_LEN} header bytes, got {len(wire)}", fatal=True)
    version, ftype, flags, rsv, payload_len, generation, counter = HEADER.unpack(wire[:HEADER_LEN])
    if version != 1:
        raise TransportError("PROTOCOL_MISMATCH", f"frame version {version} != 1", fatal=True)
    if flags or rsv:
        raise TransportError("PROTOCOL_MISMATCH", "reserved header bits must be zero", fatal=True)
    if payload_len > (65535 if ftype == FT_HANDSHAKE else MAX_PLAINTEXT):
        raise TransportError("PAYLOAD_TOO_LARGE", f"payload_len {payload_len} over the frame ceiling", fatal=True)
    if ftype not in (FT_HANDSHAKE, FT_RPC, FT_EVENT, FT_HEARTBEAT, FT_CLOSE, FT_REKEY):
        raise TransportError("PROTOCOL_MISMATCH", f"unknown frame type 0x{ftype:02x}", fatal=True)
    if ftype != FT_HANDSHAKE and counter == 0:
        raise TransportError("PROTOCOL_MISMATCH", "encrypted frame with counter 0", fatal=True)
    return {"type": ftype, "payload_len": payload_len, "generation": generation, "counter": counter}


# ---------------------------------------------------------------- client
class Client:
    def __init__(self, app, url: str, ws, noise: NoiseChannel) -> None:
        self.app = app
        self.url = url
        self._ws = ws
        self._noise = noise
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._seq = 0
        self._send_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._reader: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ setup
    @classmethod
    async def connect(cls, app, url: str, seed_hex: str, core_public_key: bytes) -> "Client":
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            raise TransportError("INTERNAL", "`websockets` is not installed; `pip install vade-sdk[core]`") from None

        context, pin = _tls_context(url, app)
        try:
            ws = await ws_connect(url, ssl=context, max_size=None, ping_interval=20, ping_timeout=20, close_timeout=5)
        except Exception as exc:
            raise TransportError("UNAVAILABLE", f"cannot connect to {url}: {type(exc).__name__}: {exc}") from None
        _check_tls_pin(app, ws, pin)

        noise = NoiseChannel(seed_hex, core_public_key)
        client = cls(app, url, ws, noise)
        try:
            await client._do_handshake()
        except Exception:
            await ws.close()
            raise
        client._reader = asyncio.create_task(client._read_loop(), name="vade-sdk-reader")
        client._heartbeat = asyncio.create_task(client._heartbeat_loop(), name="vade-sdk-heartbeat")
        app.log.info("connected", url=url, core_key=fingerprint_of(core_public_key))
        return client

    async def _do_handshake(self) -> None:
        await self._raw_send(self._noise.start())
        while True:
            wire = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            if isinstance(wire, str):  # a JSON peer is a protocol violation
                raise TransportError("PROTOCOL_MISMATCH", "core sent a text frame during the Noise handshake", fatal=True)
            reply = self._noise.read_handshake(bytes(wire))
            if reply is not None:
                await self._raw_send(reply)
            if self._noise._handshake.is_finished():  # type: ignore[attr-defined]
                break
        self._noise.establish()
        expected = os.environ.get("CORE_FINGERPRINT")
        remote = self._noise.remote_public
        if remote is not None and expected and fingerprint_of(remote) != expected.lower():
            raise TransportError(
                "PERMISSION_DENIED",
                "core static key fingerprint does not match CORE_FINGERPRINT",
                fatal=True,
            )

    # ------------------------------------------------------------------ io
    async def _raw_send(self, wire: bytes) -> None:
        await self._ws.send(wire)

    async def _send_json(self, frame_type: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        async with self._send_lock:
            await self._ws.send(self._noise.seal(frame_type, data))

    async def request(self, method: str, params: Any = None, *, timeout: float = 30.0) -> Any:
        self._seq += 1
        request_id = f"m{self._seq}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._send_json(FT_RPC, message)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise TransportError("TIMEOUT", f"{method} timed out after {timeout}s") from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send_json(FT_RPC, message)

    async def _reply(self, request_id: Any, result: Any) -> None:
        await self._send_json(FT_RPC, {"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _reply_error(self, request_id: Any, code: str, message: str) -> None:
        from .errors import ERROR_CODES

        await self._send_json(
            FT_RPC,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message[:300], "data": {"retriable": ERROR_CODES.get(code, False)}},
            },
        )

    async def _read_loop(self) -> None:
        reason: BaseException | None = None
        try:
            while not self._closed.is_set():
                wire = await self._ws.recv()
                if isinstance(wire, str):
                    raise TransportError("PROTOCOL_MISMATCH", "core sent a text frame on the module channel", fatal=True)
                header = _peek(bytes(wire))
                if header["type"] == FT_HEARTBEAT:
                    continue
                if header["type"] == FT_REKEY:
                    self._noise.open(bytes(wire))  # advances the receive generation
                    continue
                if header["type"] == FT_CLOSE:
                    try:
                        body = json.loads(self._noise.open(bytes(wire)) or b"{}")
                    except Exception:
                        body = {}
                    raise TransportError(str(body.get("code", "UNAVAILABLE")), str(body.get("message", "core closed")), fatal=True)
                if header["type"] == FT_HANDSHAKE:
                    raise TransportError("PROTOCOL_MISMATCH", "handshake frame after the handshake completed", fatal=True)
                plaintext = self._noise.open(bytes(wire))
                try:
                    message = json.loads(plaintext.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise TransportError("PROTOCOL_MISMATCH", "core sent malformed JSON", fatal=True) from None
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except TransportError as exc:
            reason = exc
        except Exception as exc:  # websockets.ConnectionClosed and friends
            reason = TransportError("UNAVAILABLE", f"connection closed: {type(exc).__name__}: {exc}")
        finally:
            self._fail_pending(reason or TransportError("UNAVAILABLE", "connection closed"))
            self._closed.set()
            self.app.log.warning("disconnected", code=getattr(reason, "code", "UNAVAILABLE"), msg=str(reason or ""))

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            request_id = message.get("id")
            future = self._pending.pop(str(request_id), None) if request_id is not None else None
            if future is None or future.done():
                self.app.log.debug("response for an unknown id", code="NOT_FOUND")
                return
            error = message.get("error")
            if isinstance(error, Mapping):
                future.set_exception(
                    TransportError(str(error.get("code", "INTERNAL")), str(error.get("message", ""))[:300])
                )
            else:
                future.set_result(message.get("result"))
            return
        task = asyncio.create_task(self._handle_request(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_request(self, message: Mapping[str, Any]) -> None:
        method = str(message.get("method"))
        params = message.get("params") or {}
        request_id = message.get("id")
        if method == "tools/call":
            call_id = str(params.get("call_id") or "")
            tool = str(params.get("tool") or "")
            args = params.get("args") or {}
            context = params.get("ctx") if isinstance(params.get("ctx"), Mapping) else {}
            envelope = await self.app.call_tool(tool, args, call_id=call_id, context=context)
            envelope.pop("truncated", None)
            if request_id is not None:
                await self._reply(request_id, envelope)
            return
        if method == "tools/cancel":
            call_id = str(params.get("call_id") or "")
            cancelled = self.app.cancel(call_id)
            if request_id is not None:
                await self._reply(request_id, {"cancelled": bool(cancelled)})
            return
        if method == "module/drain":
            grace_ms = int(params.get("grace_ms") or 0)
            self.app.begin_drain(grace_ms)
            if request_id is not None:
                await self._reply(request_id, {"accepted": True})
            asyncio.create_task(self._drain_and_stop(grace_ms))
            return
        if method == "prompts/get":
            if request_id is not None:
                await self._reply_error(request_id, "NOT_FOUND", f"module {self.app.module_id} declares no prompts")
            return
        if method == "events/deliver":
            topic = str(params.get("topic") or "")
            payload = params.get("payload")
            try:
                result = await self.app.dispatch_event(topic, payload)
            except Exception as exc:  # a subscriber bug must not kill the socket
                code = str(getattr(exc, "code", "INTERNAL"))
                detail = str(getattr(exc, "message", str(exc)))[:300]
                if request_id is not None:
                    await self._reply_error(request_id, code, detail)
                return
            if request_id is not None:
                await self._reply(request_id, result if isinstance(result, Mapping) else {"ok": True})
            return
        # Private RPCs (memory/read_session, memory/admin_*, ...) are not tools
        # and never appear in the manifest. They are dispatched only when the
        # module explicitly registered a handler; otherwise this stays a clear
        # NOT_FOUND rather than a silent success.
        try:
            result = await self.app.dispatch_rpc(method, params)
        except Exception as exc:
            code = str(getattr(exc, "code", "NOT_FOUND"))
            detail = str(getattr(exc, "message", str(exc)))[:300]
            if request_id is not None:
                await self._reply_error(request_id, code, detail)
            return
        if request_id is not None:
            await self._reply(request_id, result if isinstance(result, Mapping) else {"result": result})

    async def _drain_and_stop(self, grace_ms: int) -> None:
        deadline = time.monotonic() + max(0, grace_ms) / 1000.0
        while self.app.inflight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.app.log.info("drain complete", code=None, inflight=self.app.inflight)
        self.app.request_stop(0)

    async def _heartbeat_loop(self) -> None:
        failures = 0
        while not self._closed.is_set():
            await asyncio.sleep(HEARTBEAT_S)
            if self._closed.is_set():
                return
            try:
                await self.request(
                    "module/heartbeat",
                    {"load": round(self.app.load(), 3), "inflight": self.app.inflight},
                    timeout=5.0,
                )
                failures = 0
            except Exception as exc:
                failures += 1
                self.app.log.warning(
                    "heartbeat failed", code=getattr(exc, "code", "UNAVAILABLE"), attempts=failures
                )
                if failures >= 3:
                    # Three unanswered beats mean the socket is a zombie; drop it
                    # so the reconnect loop can redo register on a fresh one.
                    await self.close()
                    return

    # ------------------------------------------------------------------ teardown
    def _fail_pending(self, exc: BaseException) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)

    async def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for task in (self._reader, self._heartbeat):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass

    async def wait_closed(self) -> None:
        await self._closed.wait()


# ---------------------------------------------------------------- serve loop
async def serve(app, url: str, *, pair_code: str | None = None) -> int:
    """Connect, register, serve until drain; reconnect forever on failure."""
    if pair_code:
        app.log.error(
            "pairing is not available in vade-sdk 0.1",
            code="UNSUPPORTED",
            msg="core only accepts IK responders today; whitelist the module key, then run --core without --pair-code",
        )
        return 2
    if not (url.startswith("ws://") or url.startswith("wss://")):
        app.log.error("--core must be a ws:// or wss:// URL", code="INVALID_ARGUMENT")
        return 2

    key_path = os.environ.get("MODULE_KEY_PATH") or "data/module.key"
    seed_hex, public_key = load_or_create_key(key_path)
    app.log.info(
        "module identity loaded",
        code=None,
        fingerprint=fingerprint_of(public_key),
        key_path=key_path,
    )

    core_public_key = load_core_public_key(app)
    if core_public_key is None:
        app.log.error(
            "core public key unknown",
            code="PERMISSION_DENIED",
            msg="set CORE_PUBLIC_KEY (hex) or write data/core_pin.json; IK cannot start without it",
        )
        return 2

    app._stopped = asyncio.Event()
    backoff = BACKOFF_MIN
    attempts = 0
    while not app.draining:
        attempts += 1
        try:
            client = await Client.connect(app, url, seed_hex, core_public_key)
        except TransportError as exc:
            if attempts == 1 or attempts % 10 == 0:
                app.log.error("connect failed", code=exc.code, msg=exc.message, attempts=attempts)
            if app._stopped is not None and app._stopped.is_set():
                break
            delay = min(BACKOFF_MAX, backoff) * random.uniform(0.8, 1.2)
            try:
                await asyncio.wait_for(app._stopped.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue

        try:
            result = await client.request("module/register", app.manifest(), timeout=15.0)
            app.log.info(
                "registered",
                code=None,
                session_id=(result or {}).get("session_id"),
                tools=len(app.tool_names),
                fingerprint=fingerprint_of(public_key),
            )
            app._transport = client
            backoff = BACKOFF_MIN
            attempts = 0
            drain_task = asyncio.create_task(client.wait_closed())
            stop_task = asyncio.create_task(app._stopped.wait())
            done, _ = await asyncio.wait({drain_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in (drain_task, stop_task):
                if task not in done:
                    task.cancel()
            await client.close()
        except TransportError as exc:
            app.log.warning("registration failed", code=exc.code, msg=exc.message, attempts=attempts)
            await client.close()
        finally:
            app._transport = None

        if app.draining or (app._stopped is not None and app._stopped.is_set()):
            break
        delay = min(BACKOFF_MAX, backoff) * random.uniform(0.8, 1.2)
        app.log.info("reconnecting", code=None, delay_s=round(delay, 2))
        try:
            await asyncio.wait_for(app._stopped.wait(), timeout=delay)
            break
        except asyncio.TimeoutError:
            pass
        backoff = min(BACKOFF_MAX, backoff * 2)

    app.log.info("stopped", code=None)
    return app._exit_code
