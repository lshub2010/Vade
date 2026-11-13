"""Frame layer: header peek/validation, explicit replay window, seal/open.

C-01 §1.3 fixes the validation order, and order is the security property:

    1. read 20 bytes -> ver/type/flags/rsv legality
    2. payload_len <= min(limits.max_payload, 65519|65535)  (before allocating!)
    3. declared length == actual length (exact)
    4. (generation, counter) replay decision
    5. only now AEAD

The Rust core repeats steps 1-4 inside `Transport::open_frame`, but the Python
side keeps its own `ReplayWindow` because `counter` monotonicity is the ONLY
replay defence once nonce handling lives in `snow` (frames.md §7.1). Relying on
an opaque FFI call to enforce a security property we cannot observe would be
unverifiable; this one is testable.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol

from ..errors import VadeError

__all__ = [
    "HEADER_LEN",
    "MAX_FRAME_PLAINTEXT",
    "MAX_HANDSHAKE_MESSAGE",
    "FrameType",
    "FrameHeader",
    "ReplayWindow",
    "seal",
    "open_frame",
]

HEADER_LEN = 20
MAX_FRAME_PLAINTEXT = 65519  # Noise MAXMSGLEN 65535 - 16B Poly1305 tag (C-01 §7.5)
MAX_HANDSHAKE_MESSAGE = 65535
DEFAULT_MAX_PAYLOAD = 65519  # what a module may promise: min(manifest, physical ceiling)

_HEADER = struct.Struct(">BBBBIIQ")


class FrameType(IntEnum):
    HANDSHAKE = 0x01
    RPC = 0x02
    EVENT = 0x03
    HEARTBEAT = 0x04
    CLOSE = 0x05
    REKEY = 0x06

    @classmethod
    def parse(cls, value: int) -> "FrameType":
        try:
            return cls(value)
        except ValueError:
            # Frame layer has no forward compatibility: an unknown type is a
            # protocol violation, not something to ignore.
            raise VadeError("PROTOCOL_MISMATCH", f"unknown frame type 0x{value:02x}", fatal=True) from None

    @property
    def is_plaintext(self) -> bool:
        return self is FrameType.HANDSHAKE


@dataclass(frozen=True, slots=True)
class FrameHeader:
    version: int
    type: FrameType
    flags: int
    reserved: int
    payload_len: int
    generation: int
    counter: int

    @classmethod
    def peek(cls, buf: bytes | bytearray | memoryview, *, max_payload: int = DEFAULT_MAX_PAYLOAD) -> "FrameHeader":
        if len(buf) < HEADER_LEN:
            raise VadeError("INVALID_ARGUMENT", f"need {HEADER_LEN} header bytes, got {len(buf)}")
        version, typ, flags, rsv, payload_len, generation, counter = _HEADER.unpack(bytes(buf[:HEADER_LEN]))
        if version != 1:
            # Rejected BEFORE any decryption; the caller must not touch AEAD.
            raise VadeError("PROTOCOL_MISMATCH", f"frame version {version} != 1", fatal=True)
        if flags != 0 or rsv != 0:
            raise VadeError("PROTOCOL_MISMATCH", f"reserved bits must be zero (flags={flags}, rsv={rsv})", fatal=True)
        frame_type = FrameType.parse(typ)
        ceiling = MAX_HANDSHAKE_MESSAGE if frame_type.is_plaintext else MAX_FRAME_PLAINTEXT
        if payload_len > ceiling:
            raise VadeError(
                "PAYLOAD_TOO_LARGE",
                f"payload_len {payload_len} > ceiling {ceiling} for frame type 0x{typ:02x}",
                fatal=True,
            )
        if payload_len > max_payload:
            raise VadeError(
                "PAYLOAD_TOO_LARGE",
                f"payload_len {payload_len} > negotiated max_payload {max_payload}",
                fatal=True,
            )
        if not frame_type.is_plaintext and counter == 0:
            raise VadeError("PROTOCOL_MISMATCH", "encrypted frame with counter 0", fatal=True)
        return cls(version, frame_type, flags, rsv, payload_len, generation, counter)

    @property
    def wire_len(self) -> int:
        return HEADER_LEN + self.payload_len + (0 if self.type.is_plaintext else 16)


class _Transport(Protocol):
    def open_frame(self, wire: bytes) -> bytes: ...
    def seal_frame(self, frame_type: int, plaintext: bytes) -> bytes: ...
    def send_generation(self) -> int: ...
    def recv_generation(self) -> int: ...
    def begin_rekey(self) -> bytes: ...


class ReplayWindow:
    """Strict lexicographic (generation, counter) gate, per direction.

    No sliding window: WS is ordered and reliable, so anything out of order is
    either an attack or a bug. A rejected frame consumes no crypto state.
    """

    def __init__(self) -> None:
        self._generation = 1
        self._counter = 0

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def counter(self) -> int:
        return self._counter

    def accept(self, header: FrameHeader) -> None:
        gen, counter = header.generation, header.counter
        if gen < self._generation:
            raise VadeError(
                "PERMISSION_DENIED",
                f"generation rollback: got {gen}, expected >= {self._generation}",
                fatal=True,
            )
        if gen > self._generation + 1:
            raise VadeError(
                "PROTOCOL_MISMATCH",
                f"generation jump: got {gen}, expected <= {self._generation + 1}",
                fatal=True,
            )
        if gen == self._generation and counter <= self._counter:
            raise VadeError(
                "PERMISSION_DENIED",
                f"replay: counter {counter} <= last accepted {self._counter} at generation {gen}",
                fatal=True,
            )
        # gen == self._generation + 1: the peer rekeyed; counter restarts at 1.
        self._generation = gen
        self._counter = counter

    def reset(self, generation: int = 1) -> None:
        self._generation = generation
        self._counter = 0


def encode_handshake(message: bytes) -> bytes:
    """Wrap a Noise message in a `type=0x01` header (gen=0, counter=0)."""
    if len(message) > MAX_HANDSHAKE_MESSAGE:
        raise VadeError("PAYLOAD_TOO_LARGE", f"handshake message {len(message)} > {MAX_HANDSHAKE_MESSAGE}")
    return _HEADER.pack(1, int(FrameType.HANDSHAKE), 0, 0, len(message), 0, 0) + bytes(message)


def decode_handshake(wire: bytes) -> bytes:
    """Validate a `type=0x01` frame and return the Noise message body."""
    header = FrameHeader.peek(wire, max_payload=MAX_HANDSHAKE_MESSAGE)
    if header.type is not FrameType.HANDSHAKE:
        raise VadeError(
            "PROTOCOL_MISMATCH",
            f"expected a handshake frame (0x01) before the handshake completes, got 0x{int(header.type):02x}",
            fatal=True,
        )
    if header.generation != 0 or header.counter != 0:
        raise VadeError("PROTOCOL_MISMATCH", "handshake frame must use generation 0 / counter 0", fatal=True)
    if len(wire) != header.wire_len:
        raise VadeError("INVALID_ARGUMENT", f"handshake frame length {len(wire)} != declared {header.wire_len}")
    return wire[HEADER_LEN:]


def seal(transport: _Transport, frame_type: FrameType, plaintext: bytes) -> bytes:
    if frame_type.is_plaintext:
        raise VadeError("INVALID_ARGUMENT", "handshake frames are written via Handshake::write, not seal()")
    if len(plaintext) > MAX_FRAME_PLAINTEXT:
        raise VadeError("PAYLOAD_TOO_LARGE", f"plaintext {len(plaintext)} > {MAX_FRAME_PLAINTEXT}")
    return bytes(transport.seal_frame(int(frame_type), plaintext))


def open_frame(transport: _Transport, wire: bytes, *, replay: ReplayWindow, max_payload: int = DEFAULT_MAX_PAYLOAD) -> tuple[FrameHeader, bytes]:
    """Validate the header, run the replay check, then and only then decrypt."""
    header = FrameHeader.peek(wire, max_payload=max_payload)
    if len(wire) != header.wire_len:
        raise VadeError(
            "INVALID_ARGUMENT",
            f"declared wire length {header.wire_len} != actual {len(wire)}",
        )
    if header.type.is_plaintext:
        raise VadeError("INVALID_ARGUMENT", "handshake frames are not opened by the transport")
    replay.accept(header)
    return header, bytes(transport.open_frame(wire))


def split_stream(buffer: bytearray, *, max_payload: int = DEFAULT_MAX_PAYLOAD) -> list[bytes]:
    """Cut complete frames out of a byte stream (non-WS transports, tests).

    Uses `payload_len` as the only length prefix (C-01 §1.2).
    """
    frames: list[bytes] = []
    while len(buffer) >= HEADER_LEN:
        header = FrameHeader.peek(buffer, max_payload=max_payload)
        if len(buffer) < header.wire_len:
            break
        frames.append(bytes(buffer[: header.wire_len]))
        del buffer[: header.wire_len]
    return frames


def _unused(*_: Any) -> None:  # pragma: no cover
    pass
