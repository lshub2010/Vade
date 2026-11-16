"""`Connection` I/O contract: replies, frame-cap answers, heartbeat, rekey.

Every case drives the real `Connection.run()` loop with a fake websocket and a
fake crypto session — the loop, the reply path, the heartbeat timer and the
rekey accounting are the production ones. AEAD itself (counter monotonicity,
replay, rekey generations) is covered against real `vade_protocol` in
`test_framing.py` / `test_handshake_wiring.py`; here the crypto object only has
to be honest about *when* it is asked to seal and rekey, which is exactly what
these behaviours are about.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from vade.errors import VadeError
from vade.transport.connection import (
    CLOSE_GOING_AWAY,
    CLOSE_REHANDSHAKE,
    MAX_REPLY_BYTES,
    NO_REPLY,
    Connection,
    ConnectionState,
)
from vade.transport.framing import MAX_FRAME_PLAINTEXT, FrameType


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.close_codes: list[int] = []
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def receive(self) -> bytes | None:
        return await self._inbox.get()

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)
        self._inbox.put_nowait(None)

    def feed(self, data: bytes) -> None:
        self._inbox.put_nowait(data)

    @property
    def closed_with(self) -> int | None:
        return self.close_codes[-1] if self.close_codes else None

    def replies(self) -> list[dict]:
        return [json.loads(frame.split(b":", 2)[2].decode()) for frame in self.sent]


class FakeCrypto:
    """Records seal order; `maybe_rekey` returns the frame the real one would."""

    def __init__(self) -> None:
        self.sealed: list[tuple[FrameType, bytes]] = []
        self.rekeys = 0
        self.fail_rekey: VadeError | None = None
        self._generation = 1
        self._counter = 0

    # handshake surface ------------------------------------------------
    def start(self) -> None:
        return None

    def is_finished(self) -> bool:
        return True

    def read_handshake(self, message: bytes) -> None:  # pragma: no cover - never used
        return None

    def establish(self) -> None:
        return None

    # record surface ---------------------------------------------------
    def seal(self, frame_type: FrameType, plaintext: bytes) -> bytes:
        self._counter += 1
        self.sealed.append((frame_type, bytes(plaintext)))
        return b"S%d/%d:%d:" % (self._generation, self._counter, int(frame_type)) + bytes(plaintext)

    def open(self, wire: bytes):  # inbound frames in these tests are marked `I:`
        assert wire.startswith(b"I:"), wire
        return SimpleNamespace(type=FrameType.RPC), wire[2:]

    def maybe_rekey(self) -> bytes:
        if self.fail_rekey is not None:
            raise self.fail_rekey
        # `Transport::begin_rekey` seals the 0x06 frame with the OLD key and
        # only then advances the generation; model that ordering here.
        self.rekeys += 1
        body = self._generation.to_bytes(4, "big")
        self.sealed.append((FrameType.REKEY, body))
        self._generation += 1
        self._counter = 0
        return b"S%d/0:%d:" % (self._generation, int(FrameType.REKEY)) + body


def inbound(payload: dict) -> bytes:
    return b"I:" + json.dumps(payload).encode()


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def start(ws: FakeWs, crypto=None, **kwargs) -> tuple[Connection, asyncio.Task]:
    kwargs.setdefault("heartbeat_interval_s", 60.0)  # keep ticks out of the way
    connection = Connection(ws, crypto, connection_id="c1", **kwargs)
    task = asyncio.create_task(connection.run())
    await wait_until(lambda: connection.state is ConnectionState.ESTABLISHED)
    return connection, task


async def stop(connection: Connection, task: asyncio.Task) -> None:
    await connection.close()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: B014 - the loop is already down
        pass


# --------------------------------------------------------------- null replies
@pytest.mark.asyncio
async def test_a_null_result_is_replied_to():
    """`result: null` is a result. Silence here means 'no answer,' not 'null'."""
    ws = FakeWs()
    crypto = FakeCrypto()

    async def handler(connection, message):
        assert message["method"] == "module/ack"
        return None

    connection, task = await start(ws, crypto, on_rpc=handler, browser_session=False)
    try:
        ws.feed(inbound({"jsonrpc": "2.0", "id": 7, "method": "module/ack"}))
        await wait_until(lambda: len(ws.sent) == 1)
        assert ws.replies() == [{"jsonrpc": "2.0", "id": 7, "result": None}]
        assert crypto.sealed[-1][0] is FrameType.RPC
        assert connection.state is ConnectionState.ESTABLISHED
    finally:
        await stop(connection, task)


@pytest.mark.asyncio
async def test_no_reply_suppresses_the_envelope_and_notifications_stay_silent():
    ws = FakeWs()
    crypto = FakeCrypto()

    async def handler(connection, message):
        if message["method"] == "module/silent":
            return NO_REPLY
        return {"ok": True}  # a notification must not turn this into a reply

    connection, task = await start(ws, crypto, on_rpc=handler)
    try:
        ws.feed(inbound({"jsonrpc": "2.0", "id": 1, "method": "module/silent"}))
        ws.feed(inbound({"jsonrpc": "2.0", "method": "module/notify", "params": {}}))
        # A later, ordinary request proves the loop got past both messages.
        ws.feed(inbound({"jsonrpc": "2.0", "id": 2, "method": "module/notify"}))
        await wait_until(lambda: len(ws.sent) == 1)
        assert ws.replies() == [{"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}]
    finally:
        await stop(connection, task)


# ------------------------------------------------------------- oversized result
@pytest.mark.asyncio
async def test_an_oversized_result_is_answered_with_payload_too_large():
    ws = FakeWs()
    crypto = FakeCrypto()
    big = {"data": "x" * (MAX_REPLY_BYTES + 1000)}

    async def handler(connection, message):
        if message["method"] == "module/big":
            return big
        return {"ok": True}

    connection, task = await start(ws, crypto, on_rpc=handler)
    try:
        ws.feed(inbound({"jsonrpc": "2.0", "id": 11, "method": "module/big"}))
        await wait_until(lambda: len(ws.sent) == 1)
        envelope = ws.replies()[0]
        assert envelope["id"] == 11
        assert "result" not in envelope
        assert envelope["error"]["code"] == "PAYLOAD_TOO_LARGE"
        data = envelope["error"]["data"]
        assert data["field"] == "result"
        assert data["limit"] == MAX_REPLY_BYTES == MAX_FRAME_PLAINTEXT == 65519
        assert data["actual"] > MAX_REPLY_BYTES
        assert data["unit"] == "bytes"

        # Nothing over the cap ever reached the AEAD, and the socket is alive.
        assert all(len(plain) <= MAX_FRAME_PLAINTEXT for _, plain in crypto.sealed)
        assert ws.closed_with is None
        assert connection.state is ConnectionState.ESTABLISHED

        # The connection keeps serving: the next request gets a normal reply.
        ws.feed(inbound({"jsonrpc": "2.0", "id": 12, "method": "module/ok"}))
        await wait_until(lambda: len(ws.sent) == 2)
        assert ws.replies()[1] == {"jsonrpc": "2.0", "id": 12, "result": {"ok": True}}
    finally:
        await stop(connection, task)


# -------------------------------------------------------------------- heartbeat
@pytest.mark.asyncio
async def test_a_browser_socket_is_not_closed_for_silence():
    """The page sends nothing on an idle socket; that is not a dead peer."""
    ws = FakeWs()
    connection, task = await start(ws, None, browser_session=True, heartbeat_interval_s=0.02)
    try:
        await asyncio.sleep(0.2)  # 10× the interval, 5× the old 2×interval rule
        assert ws.close_codes == []
        assert connection.state is ConnectionState.ESTABLISHED
    finally:
        await stop(connection, task)


@pytest.mark.asyncio
async def test_a_noise_socket_is_still_closed_for_silence():
    """The idle rule keeps applying where the peer is supposed to ping."""
    ws = FakeWs()
    connection, task = await start(ws, FakeCrypto(), heartbeat_interval_s=0.02)
    try:
        await wait_until(lambda: ws.closed_with is not None, timeout=2.0)
        assert ws.closed_with == CLOSE_GOING_AWAY
        assert connection.state is ConnectionState.CLOSED
    finally:
        await stop(connection, task)


@pytest.mark.asyncio
async def test_a_dead_heartbeat_loop_closes_instead_of_leaving_a_zombie():
    """The 24 h deadline lives there; a silent death would never re-handshake."""
    ws = FakeWs()

    async def boom() -> None:
        raise RuntimeError("heartbeat blew up")

    connection = Connection(ws, None, connection_id="c1", browser_session=True,
                            heartbeat_interval_s=0.02)
    connection._heartbeat_loop = boom  # type: ignore[assignment]
    task = asyncio.create_task(connection.run())
    try:
        await wait_until(lambda: ws.closed_with is not None)
        assert ws.closed_with == CLOSE_GOING_AWAY
        assert connection.state is ConnectionState.CLOSED
    finally:
        await stop(connection, task)


# ------------------------------------------------------------------------ rekey
@pytest.mark.asyncio
async def test_rekey_trips_on_the_frame_threshold_and_is_sealed_last():
    ws = FakeWs()
    crypto = FakeCrypto()
    connection, task = await start(ws, crypto, rekey_frames=3)
    try:
        for index in range(3):
            await connection.send_event({"n": index})
        assert crypto.rekeys == 1
        assert connection.rekeys == 1
        assert connection.sealed_frames == 0  # counter reset by the rekey
        assert crypto.sealed[-1][0] is FrameType.REKEY
        assert crypto.sealed[-2][1] == b'{"n":2}'  # the 0x06 frame follows the data

        # The counter starts over: two more frames must not trip it again.
        await connection.send_event({"n": 3})
        await connection.send_event({"n": 4})
        assert crypto.rekeys == 1
        assert connection.sealed_frames == 2
    finally:
        await stop(connection, task)


@pytest.mark.asyncio
async def test_rekey_trips_on_the_byte_threshold():
    ws = FakeWs()
    crypto = FakeCrypto()
    connection, task = await start(ws, crypto, rekey_bytes=64)
    try:
        await connection.send_event({"payload": "y" * 100})
        assert crypto.rekeys == 1
        assert connection.sealed_bytes == 0
    finally:
        await stop(connection, task)


@pytest.mark.asyncio
async def test_a_failed_rekey_closes_instead_of_reusing_the_key():
    """FR-3.6: no silent fallback to the old key."""
    ws = FakeWs()
    crypto = FakeCrypto()
    crypto.fail_rekey = VadeError("UNAVAILABLE", "session is closed")
    connection, task = await start(ws, crypto, rekey_frames=1)
    try:
        await connection.send_event({"n": 0})
        await wait_until(lambda: ws.closed_with is not None)
        assert connection.state is ConnectionState.CLOSED
    finally:
        await stop(connection, task)


@pytest.mark.asyncio
async def test_the_seal_and_the_queue_put_are_one_critical_section():
    """Sealing outside the lock reorders frames and the peer drops the socket.

    The delay stands in for any real suspension point between `seal()` and the
    queue put (an offloaded seal, a full queue, a slow transport): the frame
    that was sealed first must also be the frame that is queued first.
    """
    ws = FakeWs()
    crypto = FakeCrypto()
    connection, task = await start(ws, crypto)
    queued: list[bytes] = []
    calls = 0

    async def slow_enqueue(wire: bytes) -> None:
        nonlocal calls
        index = calls
        calls += 1
        await asyncio.sleep(0.05 if index == 0 else 0.0)
        queued.append(wire)

    connection._enqueue = slow_enqueue  # type: ignore[assignment]
    try:
        await asyncio.gather(
            connection.send_event({"n": 0}),
            connection.send_event({"n": 1}),
        )
        sealed_order = [json.loads(plain)["n"] for _, plain in crypto.sealed]
        queued_order = [json.loads(frame.split(b":", 2)[2])["n"] for frame in queued]
        assert sealed_order == [0, 1]
        assert queued_order == sealed_order
    finally:
        await stop(connection, task)


# ------------------------------------------------------------ 24 h key lifetime
@pytest.mark.asyncio
async def test_a_24h_old_key_is_closed_with_1012_for_a_new_handshake():
    """Rekey does not refresh forward secrecy; 24 h means handshake again."""
    ws = FakeWs()
    crypto = FakeCrypto()
    connection, task = await start(ws, crypto, rekey_interval_s=0.0, heartbeat_interval_s=0.02)
    try:
        await wait_until(lambda: ws.closed_with is not None)
        assert ws.closed_with == CLOSE_REHANDSHAKE == 1012
        assert crypto.rekeys == 0  # 24 h is NOT a rekey: same ephemeral key
        assert connection.state is ConnectionState.CLOSED
    finally:
        await stop(connection, task)


@pytest.mark.asyncio
async def test_the_key_lifetime_rule_does_not_touch_the_browser_channel():
    """No Noise on that path: TLS owns the key, and 1012 would loop reconnects."""
    ws = FakeWs()
    connection, task = await start(
        ws, None, browser_session=True, rekey_interval_s=0.0, heartbeat_interval_s=0.02
    )
    try:
        await asyncio.sleep(0.15)
        assert ws.close_codes == []
        assert connection.state is ConnectionState.ESTABLISHED
    finally:
        await stop(connection, task)
