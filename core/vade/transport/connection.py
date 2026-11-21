"""One WS connection: handshake -> transport, single reader, single writer.

Why single reader (not just single writer): `TransportState` is not concurrency
safe. Two coroutines calling `open()` interleave nonces and the symptom is
"random frame loss" — extremely hard to diagnose. Rust enforces `&mut`; Python
enforces it with one reader task draining one bounded queue.

Why single writer + Queue: concurrent `ws.send` interleaves frames.

Backpressure: 256 queued frames per connection (config), then close 1013.
Frames are sealed inline: a seal is < 2 ms. A loop-lag monitor flips this to
`asyncio.to_thread` if p95 exceeds 5 ms (see `LoopLagMonitor`).

Core->module requests: `request()` allocates an id, parks a Future in
`_pending`, and the read loop hands a method-less message to that table
(`_resolve_response`). Without this table every `tools/call` reply was
dropped on the floor — C2's dispatcher returned `None` for responses and no
one was waiting.

Replies: `None` is a legal JSON result (`result: null`), so "do not answer"
is a separate sentinel (`NO_REPLY`). A handler returning `None` for a request
that carried an `id` still gets a reply — otherwise an RPC whose answer is
`null` (a module acknowledging a command) leaves the peer waiting for its
timeout. Notifications (no `id`) never get a reply, whatever the handler
returns.

Sized replies: v1 has no fragmentation (rpc.json:365), so a reply that does
not fit one frame cannot go on the wire at all. The encoded reply is measured
before sealing and an oversized one is answered with `PAYLOAD_TOO_LARGE`;
raising there instead would close a connection over a module bug.

Rekey (FR-3.6 / FR-3.12, D56): the sender counts sealed bytes and frames and
rekeys at 1 GiB or 2^20 frames; 24 h of key lifetime is closed with 1012 so
the peer performs a *new handshake*, because rekey does not refresh forward
secrecy. Every seal runs under `_seal_lock` — `seal()` (which advances the
send counter) and the queue put must not be split, or the peer sees a counter
gap and drops the connection.

Credentials: `auth_mode` / `device_key` / `read_only` are set by whoever
verified the credential (the WS upgrade for a cookie, the handshake hook for a
static key) and are never taken from the peer. `auth_mode=None` means "nothing
verified yet" and authority checks must fail closed on it. `read_only` is the
session row's flag, so `READ_ONLY_SESSION` and the socket agree by
construction, and the CSRF verdict for a cookie socket is cached in `csrf_ok`
because the transport is the only layer that sees the row's digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections import deque
from enum import Enum
from typing import Any, Awaitable, Callable

from ..errors import ERROR_CODES, VadeError
from ..logging import get_logger
from .framing import MAX_FRAME_PLAINTEXT, decode_handshake, encode_handshake, FrameType

__all__ = [
    "Connection",
    "ConnectionState",
    "LoopLagMonitor",
    "NO_REPLY",
    "RpcError",
    "CLOSE_REHANDSHAKE",
    "MAX_QUEUE_DEFAULT",
    "MAX_REPLY_BYTES",
]

log = get_logger("vade.transport.connection")

MAX_QUEUE_DEFAULT = 256
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_REHANDSHAKE = 1012  # service restart: come back and handshake again
CLOSE_TRY_AGAIN = 1013

#: A reply must fit one frame: 65519 bytes of plaintext (Noise MAXMSGLEN minus
#: the 16-byte tag, C-01 §7.5). v1 has no fragmentation, so there is nothing
#: to fall back to — see `_send_result`.
MAX_REPLY_BYTES = MAX_FRAME_PLAINTEXT

#: "This request gets no reply". `None` cannot mean that any more: it is a
#: legal JSON result and JSON-RPC clients wait for the envelope either way.
NO_REPLY = object()

#: Sender-side rekey thresholds (D56 / FR-3.6). 24 h is not here on purpose:
#: `rekey` keeps the same ephemeral key pair, so it cannot refresh forward
#: secrecy. That threshold closes with 1012 and demands a new handshake.
REKEY_BYTES_DEFAULT = 1 << 30       # 1 GiB per direction
REKEY_FRAMES_DEFAULT = 1 << 20      # 2^20 frames per direction
REKEY_INTERVAL_DEFAULT = 24 * 3600  # key lifetime, then re-handshake



class RpcError:
    """A JSON-RPC error reply the dispatcher should send instead of a result.

    Manifest rejections must be reported to the module (the CLI prints the
    pointer), NOT turned into a close: a module with a bad manifest is allowed
    to fix it and retry on the same connection.
    """

    __slots__ = ("code", "message", "data")

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


class ConnectionState(str, Enum):
    NEW = "new"
    HANDSHAKING = "handshaking"
    ESTABLISHED = "established"
    DRAINING = "draining"
    CLOSED = "closed"


class LoopLagMonitor:
    """p95 event-loop lag; > 5 ms means seal/open should move off-loop."""

    def __init__(self, window: int = 128) -> None:
        self._samples: deque[float] = deque(maxlen=window)

    def record(self, seconds: float) -> None:
        self._samples.append(seconds)

    @property
    def p95(self) -> float:
        if not self._samples:
            return 0.0
        ordered = sorted(self._samples)
        return ordered[max(0, int(len(ordered) * 0.95) - 1)]

    @property
    def should_offload(self) -> bool:
        return self.p95 > 0.005


class Connection:
    """Duck-typed websocket: anything with `send(bytes)`, `close(code)` and an
    async iterator of inbound `bytes` works (real ASGI adapter in server.py,
    fake in tests)."""

    def __init__(
        self,
        ws: Any,
        crypto: Any | None,
        *,
        connection_id: str,
        max_queue: int = MAX_QUEUE_DEFAULT,
        heartbeat_interval_s: float = 10.0,
        handshake_timeout_s: float = 5.0,
        on_rpc: Callable[["Connection", dict[str, Any]], Awaitable[Any]] | None = None,
        on_close: Callable[["Connection"], None] | None = None,
        browser_session: bool = False,
        device_id: str | None = None,
        auth_mode: str | None = None,
        device_key: str | None = None,
        read_only: bool = False,
        csrf_hash: str | None = None,
        on_established: Callable[["Connection"], None] | None = None,
        rekey_interval_s: float = REKEY_INTERVAL_DEFAULT,
        rekey_bytes: int = REKEY_BYTES_DEFAULT,
        rekey_frames: int = REKEY_FRAMES_DEFAULT,
    ) -> None:
        self.ws = ws
        self.crypto = crypto
        self.connection_id = connection_id
        self.state = ConnectionState.NEW
        self.browser_session = browser_session  # plain JSON, no Noise, e2e=false
        #: The authenticated device this socket belongs to (RFC-0004 §6.4).
        #: `None` is the fail-closed default: a submit carrying a `file_id`
        #: cannot be attributed, so it is refused. It is set by the transport
        #: from a credential it verified itself — never from a client claim.
        self.device_id = device_id
        #: WHICH credential this socket proved: `browser_session` (the
        #: `__Host-ac_ui` cookie), `device_key` (a Noise static key) or
        #: `module_key` (a Noise key that registered as a module). `None` means
        #: "no credential has been verified yet" and every authority question
        #: must fail closed against it — it must never read as a permissive
        #: default. `approval/decide` is the caller that depends on this: it
        #: used to `getattr(connection, "auth_mode", "browser_session")`, and
        #: because the attribute was never set, every legitimate approver was
        #: answered `APPROVER_INVALID` (the dead end this field closes).
        self.auth_mode = auth_mode
        #: The verified static-key fingerprint behind a native handshake. Not a
        #: client claim either: `CryptoSession` computes it from the key that
        #: completed the IK exchange, and `None` for cookie sessions.
        self.device_key = device_key
        #: Explicit read-only session (AMEND-22 #14): the transport copies
        #: `ui_sessions['read_only']` here at upgrade, so `READ_ONLY_SESSION`
        #: is decided by the same row that authenticated the socket.
        self.read_only = bool(read_only)
        #: `sha256(__Host-ac_csrf)` of the session row, never the token. The
        #: write gate compares against it and caches success in `csrf_ok`.
        self.csrf_hash = csrf_hash or None
        #: Cached CSRF verdict for this socket ("验 CSRF，缓存到 Connection"):
        #: a browser connection pays the check once, not per frame.
        self.csrf_ok = False

        self._on_established = on_established
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_queue)
        self._heartbeat_interval = heartbeat_interval_s
        self._handshake_timeout = handshake_timeout_s
        self._on_rpc = on_rpc
        self._on_close = on_close
        self._writer_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_close: tuple[int, str] | None = None
        self._last_rx = time.monotonic()
        self._closed = False
        #: Core->module request correlation: id -> Future of the peer's `result`.
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._request_seq = 0
        #: Rekey accounting (FR-3.6). Counters are per send direction and reset
        #: on every rekey that this side starts; `_established_at` is when the
        #: key this socket runs on came into force.
        self._seal_lock = asyncio.Lock()
        self._rekey_interval = rekey_interval_s
        self._rekey_bytes = rekey_bytes
        self._rekey_frames = rekey_frames
        self._sealed_bytes = 0
        self._sealed_frames = 0
        self._rekeys = 0
        self._established_at: float | None = None

    # ------------------------------------------------------------------ credentials
    @property
    def csrf_required(self) -> bool:
        """Only cookie credentials carry ambient authority, so only they need CSRF.

        A native peer proved possession of a static key on this socket; there is
        no browser that could be tricked into attaching it, so a token would be
        ceremony, not a defence. That asymmetry is the same one `blob/auth.py`
        draws between Bearer and cookie principals.
        """
        return self.browser_session

    def accepts_csrf(self, token: Any) -> bool:
        """Check a double-submit CSRF token and cache the verdict on this socket.

        `csrf_hash` is `sha256(__Host-ac_csrf)` copied from the session row at
        upgrade; the raw token is never stored. A success is sticky
        (`csrf_ok`): the socket has already proved it belongs to a page that
        can read the paired cookie, and re-proving that per frame would only
        add a hash to every write. A failure is NOT cached — a client that
        fixes its token may retry.
        """
        if self.csrf_ok:
            return True
        hash_ = self.csrf_hash
        if not hash_ or not isinstance(token, str) or not token:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if hmac.compare_digest(digest, hash_):
            self.csrf_ok = True
            return True
        return False

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._pending_close is not None:
            # Governance closed this connection before it started running.
            code, reason = self._pending_close
            self._pending_close = None
            await self.close(code, reason)
            return
        self._writer_task = asyncio.create_task(self._writer_loop(), name=f"writer-{self.connection_id}")
        try:
            if self.browser_session:
                self.state = ConnectionState.ESTABLISHED
            else:
                await self._handshake_phase()
            # Key lifetime starts when the key does: the 24 h re-handshake
            # deadline is measured from here, not from TCP accept.
            self._established_at = time.monotonic()
            if self._on_established is not None:
                try:
                    self._on_established(self)
                except Exception:
                    # Identity binding is best-effort by contract: a resolver
                    # bug must not kill a socket that already authenticated.
                    # The connection simply keeps `device_id=None` (fail-closed)
                    # instead of becoming an unattributable *and* broken one.
                    log.warning("on_established hook failed", extra={
                        "code": None, "connection_id": self.connection_id})
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name=f"hb-{self.connection_id}")
            self._heartbeat_task.add_done_callback(self._on_heartbeat_done)
            await self._read_loop()
        except asyncio.TimeoutError:
            await self.close(CLOSE_GOING_AWAY, "handshake timeout")
        except VadeError as exc:
            log.warning("connection error", extra={"code": exc.code, "connection_id": self.connection_id, "msg": exc.message})
            await self.close(CLOSE_GOING_AWAY if exc.fatal else CLOSE_NORMAL, exc.code)
        except asyncio.CancelledError:
            raise
        finally:
            await self.close(CLOSE_NORMAL, "eof")

    async def _handshake_phase(self) -> None:
        self.state = ConnectionState.HANDSHAKING
        assert self.crypto is not None
        first = self.crypto.start()
        if first:
            await self._enqueue(encode_handshake(first))
        deadline = time.monotonic() + self._handshake_timeout
        while not self.crypto.is_finished():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            message = await asyncio.wait_for(self._next_bytes(), remaining)
            if message is None:
                raise VadeError("INVALID_ARGUMENT", "connection closed during handshake")
            reply = self.crypto.read_handshake(decode_handshake(message))
            if reply:
                await self._enqueue(encode_handshake(reply))
        self.crypto.establish()
        self.state = ConnectionState.ESTABLISHED

    # ------------------------------------------------------------------ io
    async def _read_loop(self) -> None:
        while not self._closed:
            wire = await self._next_bytes()
            if wire is None:
                return
            self._last_rx = time.monotonic()
            if self.browser_session:
                await self._dispatch(wire)
                continue
            assert self.crypto is not None
            header, plaintext = self.crypto.open(wire)
            if header.type is FrameType.HEARTBEAT:
                continue
            if header.type is FrameType.REKEY:
                continue  # transport-internal; input stream stays in order
            if header.type is FrameType.CLOSE:
                await self.close(CLOSE_NORMAL, "peer close")
                return
            await self._dispatch(plaintext)

    async def _dispatch(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self.close(CLOSE_NORMAL, "malformed json")
            return
        if message.get("method") is None:
            # A response to a core->module request (tools/call). Route it by id
            # to the waiting Future; the RPC handler must NOT see it.
            self._resolve_response(message)
            return
        if self._on_rpc is None:
            return
        result = await self._on_rpc(self, message)
        reply_id = message.get("id")
        if reply_id is None:
            # A notification: no envelope exists for it, sentinel or not.
            return
        if result is NO_REPLY:
            return
        if isinstance(result, RpcError):
            await self.send_json({"jsonrpc": "2.0", "id": reply_id, "error": result.to_dict()})
            return
        # `None` is a result too (`result: null`): a module acknowledging a
        # command answers without a payload, and the caller is still waiting.
        await self._send_result(reply_id, result)

    async def _send_result(self, reply_id: Any, result: Any) -> None:
        """Reply with `result`, or with `PAYLOAD_TOO_LARGE` if it cannot fit.

        Measured on the *encoded envelope*, not on `result`: the frame carries
        the whole JSON line, and a result that only fits without its envelope
        does not fit at all. The error keeps the id, so the peer stops waiting
        and the socket stays up — a module bug must not cost the connection.
        """
        reply = {"jsonrpc": "2.0", "id": reply_id, "result": result}
        encoded = _encode_json(reply)
        if len(encoded) > MAX_REPLY_BYTES:
            error = RpcError(
                "PAYLOAD_TOO_LARGE",
                f"result encodes to {len(encoded)} bytes, over the {MAX_REPLY_BYTES}-byte frame cap",
                {"field": "result", "limit": MAX_REPLY_BYTES, "actual": len(encoded), "unit": "bytes"},
            )
            log.warning("reply over the frame cap", extra={
                "code": "PAYLOAD_TOO_LARGE", "connection_id": self.connection_id,
                "msg": f"id={reply_id!r} size={len(encoded)}"})
            await self.send_json({"jsonrpc": "2.0", "id": reply_id, "error": error.to_dict()})
            return
        await self._write(FrameType.RPC, encoded)

    # ------------------------------------------------------------------ requests
    async def request(self, method: str, params: Any = None, *, timeout_s: float | None = None) -> Any:
        """Send one request and await the peer's `result` (or raise VadeError).

        The id is allocated here, never by the caller: two modules answering
        the same id is exactly the kind of bug that only shows up under load.
        """
        if self._closed:
            raise VadeError("UNAVAILABLE", "connection is closed")
        loop = asyncio.get_running_loop()
        self._request_seq += 1
        request_id = self._request_seq
        future: asyncio.Future[Any] = loop.create_future()
        # Consume the exception if nobody awaits the future (timeout path).
        future.add_done_callback(_silence_unretrieved)
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self.send_json(message)
            if timeout_s is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        """Fire-and-forget notification (tools/cancel): no id, no reply."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self.send_json(message)

    def _resolve_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if request_id is None:
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            log.debug("response for an unknown request id", extra={"code": "NOT_FOUND",
                                                                   "connection_id": self.connection_id})
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "INTERNAL")
            if code not in ERROR_CODES:
                # C-04: a module may only return declared codes.
                code = "INTERNAL"
            future.set_exception(VadeError(code, str(error.get("message", ""))[:300]))
            return
        future.set_result(message.get("result"))

    async def send_json(self, obj: Any) -> None:
        await self._write(FrameType.RPC, _encode_json(obj))

    async def send_event(self, obj: Any) -> None:
        await self._write(FrameType.EVENT, _encode_json(obj))

    async def _write(self, frame_type: FrameType, payload: bytes) -> None:
        """One frame out: seal and enqueue are a single critical section.

        Splitting them is a real bug, not a theoretical one: `seal()` advances
        the send counter, so if two coroutines seal A then B and enqueue B then
        A, the peer's replay window sees counter 2 before 1 and drops the
        connection as a protocol violation. The browser channel has no counters
        but uses the same path so there is one send implementation.
        """
        if self.browser_session:
            await self._enqueue(payload)
            return
        assert self.crypto is not None
        async with self._seal_lock:
            wire = self.crypto.seal(frame_type, payload)
            self._sealed_bytes += len(payload)
            self._sealed_frames += 1
            await self._enqueue(wire)
            await self._rekey_if_due_locked()

    # ------------------------------------------------------------------ rekey
    def _rekey_due(self) -> bool:
        return self._sealed_bytes >= self._rekey_bytes or self._sealed_frames >= self._rekey_frames

    async def _rekey_if_due_locked(self) -> None:
        """Called with `_seal_lock` held: no frame may be sealed across a rekey.

        `begin_rekey()` seals the `0x06` frame with the *old* key and only then
        resets the send counter to 0, so doing it between two seals would make
        the next frame start at generation+1/counter 1 with a gap in the old
        generation. In here, the ordering the peer sees is exactly the ordering
        we sealed.
        """
        if not self._rekey_due():
            return
        if self.state is not ConnectionState.ESTABLISHED:
            return
        assert self.crypto is not None
        try:
            frame = self.crypto.maybe_rekey()
        except VadeError as exc:
            # FR-3.6: a failed rekey means the session is not usable; do not
            # keep sending under a key we could not rotate.
            log.warning("rekey failed", extra={
                "code": exc.code, "connection_id": self.connection_id, "msg": exc.message})
            await self.close(CLOSE_GOING_AWAY, f"rekey failed: {exc.code}")
            return
        if frame:
            await self._enqueue(frame)
        self._sealed_bytes = 0
        self._sealed_frames = 0
        self._rekeys += 1
        log.debug("rekeyed", extra={"code": None, "connection_id": self.connection_id,
                                    "msg": f"rekeys={self._rekeys}"})

    @property
    def sealed_bytes(self) -> int:
        return self._sealed_bytes

    @property
    def sealed_frames(self) -> int:
        return self._sealed_frames

    @property
    def rekeys(self) -> int:
        return self._rekeys

    async def _enqueue(self, wire: bytes) -> None:
        try:
            self._queue.put_nowait(wire)
        except asyncio.QueueFull:
            # Never buffer without bound: a slow peer must not eat core memory.
            await self.close(CLOSE_TRY_AGAIN, "send queue full")

    async def _writer_loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            try:
                await self.ws.send(item)
            except Exception:
                return

    async def _next_bytes(self) -> bytes | None:
        message = await self.ws.receive()
        return message

    # ------------------------------------------------------------------ heartbeat
    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._heartbeat_interval)
            if self._closed:
                return
            if not self.browser_session and self._key_expired():
                # FR-3.12: rekey keeps the same ephemeral key, so it cannot
                # refresh forward secrecy. 24 h is answered with 1012 + a new
                # handshake, not with a rekey.
                await self.close(CLOSE_REHANDSHAKE, "key lifetime exceeded; re-handshake required")
                return
            if self.browser_session:
                # A browser socket has no protocol heartbeat: the page sends
                # nothing on an idle connection, so "no bytes for 2×interval"
                # is the normal state, not a dead peer. Liveness there is the
                # TCP/TLS stack and the browser's own WS ping.
                continue
            if time.monotonic() - self._last_rx > 2 * self._heartbeat_interval:
                await self.close(CLOSE_GOING_AWAY, "heartbeat timeout")
                return
            assert self.crypto is not None
            try:
                # Same seal path as RPC: heartbeats consume counter values too,
                # so they must not be sealed outside `_seal_lock`.
                await self._write(FrameType.HEARTBEAT, b"")
            except VadeError:
                return

    def _key_expired(self) -> bool:
        if self._established_at is None:
            return False
        return time.monotonic() - self._established_at >= self._rekey_interval

    def _on_heartbeat_done(self, task: asyncio.Task[None]) -> None:
        """A heartbeat loop that dies must not leave a zombie socket behind.

        This loop is the only place the 24 h re-handshake deadline is checked,
        so a silent task death means a key that is never rotated and a peer
        that is never declared gone. Closing is honest; pretending the socket
        is healthy is not.
        """
        if task.cancelled() or self._closed:
            return
        exc = task.exception()
        if exc is None:
            return
        log.warning("heartbeat loop died", extra={
            "code": None, "connection_id": self.connection_id, "msg": repr(exc)})
        asyncio.ensure_future(self.close(CLOSE_GOING_AWAY, "heartbeat loop failed"))

    # ------------------------------------------------------------------ close
    async def drain(self) -> None:
        if self.state is ConnectionState.ESTABLISHED:
            self.state = ConnectionState.DRAINING

    def request_close(self, code: int = CLOSE_GOING_AWAY, reason: str = "closed by core") -> None:
        """Sync, thread-safe close for governance hooks (`Registry` runs on the
        admin thread and cannot await).

        `Connection.close` is a coroutine; calling it without awaiting only
        creates a coroutine object and leaves the socket open (the bug this
        method replaces). Here the close is scheduled on the loop that owns the
        connection, so the writer task is cancelled and the WS is really shut.
        """
        if self._closed:
            return
        loop = self._loop
        if loop is None:
            # Not running yet (or never started): remember it so run() exits.
            self._pending_close = (code, reason)
            return
        try:
            loop.call_soon_threadsafe(self._spawn_close, code, reason)
        except RuntimeError:  # pragma: no cover - loop already closed
            self._closed = True
            self.state = ConnectionState.CLOSED

    def _spawn_close(self, code: int, reason: str) -> None:
        asyncio.ensure_future(self.close(code, reason))

    async def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        self.state = ConnectionState.CLOSED
        self._fail_pending(VadeError("UNAVAILABLE", "connection closed before the reply arrived"))
        # `close()` is reached from the heartbeat task too (timeout, 1012 key
        # lifetime): cancelling the task we are running in would cut the socket
        # shutdown at its next await, leaving `ws.close` and `_on_close` undone.
        current = asyncio.current_task()
        for task in (self._heartbeat_task, self._writer_task):
            if task is not None and task is not current:
                task.cancel()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        try:
            await self.ws.close(code)
        except Exception:
            pass
        if self._on_close is not None:
            self._on_close(self)

    def _fail_pending(self, exc: BaseException) -> None:
        """No caller may hang on a socket that is already gone."""
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)


def _encode_json(obj: Any) -> bytes:
    """The one encoding used for the wire — byte counts depend on it.

    `separators` matters: an encoder that emits spaces reports a size that is
    not the size of what gets sealed, and the frame cap is measured in bytes.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _silence_unretrieved(future: asyncio.Future[Any]) -> None:
    """Mark the exception retrieved without consuming it for the real awaiter.

    `close()` and `wait_for` can both abandon a pending reply; without this the
    loop logs "Future exception was never retrieved" for a normal disconnect.
    """
    if not future.cancelled():
        future.exception()
