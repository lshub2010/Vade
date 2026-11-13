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
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from enum import Enum
from typing import Any, Awaitable, Callable

from ..errors import ERROR_CODES, VadeError
from ..logging import get_logger
from .framing import decode_handshake, encode_handshake, FrameType

__all__ = ["Connection", "ConnectionState", "LoopLagMonitor", "RpcError", "MAX_QUEUE_DEFAULT"]

log = get_logger("vade.transport.connection")

MAX_QUEUE_DEFAULT = 256
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_TRY_AGAIN = 1013


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
    ) -> None:
        self.ws = ws
        self.crypto = crypto
        self.connection_id = connection_id
        self.state = ConnectionState.NEW
        self.browser_session = browser_session  # plain JSON, no Noise, e2e=false
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
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name=f"hb-{self.connection_id}")
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
        if result is not None and message.get("id") is not None:
            if isinstance(result, RpcError):
                await self.send_json({"jsonrpc": "2.0", "id": message["id"], "error": result.to_dict()})
            else:
                await self.send_json({"jsonrpc": "2.0", "id": message["id"], "result": result})

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
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.browser_session:
            await self._enqueue(payload)
            return
        assert self.crypto is not None
        frame = self.crypto.seal(FrameType.RPC, payload)
        await self._enqueue(frame)

    async def send_event(self, obj: Any) -> None:
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.browser_session:
            await self._enqueue(payload)
        else:
            assert self.crypto is not None
            await self._enqueue(self.crypto.seal(FrameType.EVENT, payload))

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
            if time.monotonic() - self._last_rx > 2 * self._heartbeat_interval:
                await self.close(CLOSE_GOING_AWAY, "heartbeat timeout")
                return
            if self.browser_session:
                continue
            assert self.crypto is not None
            try:
                await self._enqueue(self.crypto.seal(FrameType.HEARTBEAT, b""))
            except VadeError:
                return

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
        for task in (self._heartbeat_task, self._writer_task):
            if task is not None:
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


def _silence_unretrieved(future: asyncio.Future[Any]) -> None:
    """Mark the exception retrieved without consuming it for the real awaiter.

    `close()` and `wait_for` can both abandon a pending reply; without this the
    loop logs "Future exception was never retrieved" for a normal disconnect.
    """
    if not future.cancelled():
        future.exception()
