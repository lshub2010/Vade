"""Framing limits, replay window, and a real Noise handshake via protocol-core."""

from __future__ import annotations

import struct

import pytest

from vade.crypto_gateway.session import CryptoSession, GatewayState, fingerprint_of
from vade.errors import VadeError
from vade.transport.framing import (
    HEADER_LEN,
    MAX_FRAME_PLAINTEXT,
    FrameHeader,
    FrameType,
    ReplayWindow,
    decode_handshake,
    encode_handshake,
    open_frame as open_wire,
    split_stream,
)


def header_bytes(*, version=1, typ=0x02, flags=0, rsv=0, payload_len=10, generation=1, counter=1):
    return struct.pack(">BBBBIIQ", version, typ, flags, rsv, payload_len, generation, counter)


class CountingTransport:
    """Records AEAD calls so we can prove a rejected frame never decrypts."""

    def __init__(self, plaintext=b"ok"):
        self.calls = 0
        self._plaintext = plaintext

    def open_frame(self, wire):
        self.calls += 1
        return self._plaintext

    def seal_frame(self, frame_type, plaintext):
        self.calls += 1
        return header_bytes(payload_len=len(plaintext)) + plaintext

    def send_generation(self):
        return 1

    def recv_generation(self):
        return 1

    def begin_rekey(self):
        return b""


def test_wrong_version_is_rejected_before_any_decryption():
    transport = CountingTransport()
    wire = header_bytes(version=2) + b"x" * 10 + b"\x00" * 16
    with pytest.raises(VadeError) as excinfo:
        open_wire(transport, wire, replay=ReplayWindow())
    assert excinfo.value.code == "PROTOCOL_MISMATCH"
    assert excinfo.value.fatal is True
    assert transport.calls == 0


def test_payload_above_65519_is_rejected_before_allocation():
    transport = CountingTransport()
    with pytest.raises(VadeError) as excinfo:
        FrameHeader.peek(header_bytes(payload_len=MAX_FRAME_PLAINTEXT + 1))
    assert excinfo.value.code == "PAYLOAD_TOO_LARGE"
    assert transport.calls == 0


def test_exactly_65519_is_accepted():
    header = FrameHeader.peek(header_bytes(payload_len=MAX_FRAME_PLAINTEXT))
    assert header.payload_len == MAX_FRAME_PLAINTEXT
    assert header.wire_len == HEADER_LEN + MAX_FRAME_PLAINTEXT + 16


def test_reserved_bits_and_unknown_type_are_fatal():
    with pytest.raises(VadeError):
        FrameHeader.peek(header_bytes(flags=1))
    with pytest.raises(VadeError):
        FrameHeader.peek(header_bytes(typ=0x7F))


def test_declared_length_mismatch_is_rejected():
    transport = CountingTransport()
    wire = header_bytes(payload_len=10) + b"x" * 4
    with pytest.raises(VadeError) as excinfo:
        open_wire(transport, wire, replay=ReplayWindow())
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert transport.calls == 0


def test_replay_is_rejected_and_never_decrypts_twice():
    transport = CountingTransport()
    replay = ReplayWindow()
    wire = header_bytes(counter=1) + b"x" * 10 + b"\x00" * 16
    open_wire(transport, wire, replay=replay)
    with pytest.raises(VadeError) as excinfo:
        open_wire(transport, wire, replay=replay)
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.fatal is True
    assert transport.calls == 1  # the replayed frame never reached AEAD


def test_generation_rollback_and_jump_are_fatal():
    replay = ReplayWindow()
    replay.accept(FrameHeader.peek(header_bytes(generation=1, counter=5)))
    with pytest.raises(VadeError) as rollback:
        replay.accept(FrameHeader.peek(header_bytes(generation=0, counter=9)))
    assert rollback.value.code == "PERMISSION_DENIED"
    with pytest.raises(VadeError) as jump:
        replay.accept(FrameHeader.peek(header_bytes(generation=3, counter=1)))
    assert jump.value.code == "PROTOCOL_MISMATCH"


def test_rekey_generation_restarts_counter():
    replay = ReplayWindow()
    replay.accept(FrameHeader.peek(header_bytes(generation=1, counter=7)))
    replay.accept(FrameHeader.peek(header_bytes(generation=2, counter=1)))
    assert (replay.generation, replay.counter) == (2, 1)


def test_handshake_frame_wrapping():
    wire = encode_handshake(b"noise-msg")
    assert decode_handshake(wire) == b"noise-msg"
    with pytest.raises(VadeError):
        decode_handshake(header_bytes(typ=0x02, payload_len=3) + b"abc" + b"\x00" * 16)


def test_split_stream_uses_payload_len_only():
    frame = header_bytes(typ=0x04, payload_len=0, counter=2) + b"\x00" * 16
    buffer = bytearray(frame + frame)
    assert split_stream(buffer) == [frame, frame]
    assert len(buffer) == 0


# ---------------------------------------------------------------- real handshake
pytestmark_rust = pytest.mark.rust


def _proto():
    import vade_protocol

    return vade_protocol


@pytest.mark.rust
def test_real_ik_handshake_and_replay_through_gateway():
    proto = _proto()
    core_id = proto.FfiIdentity.generate()
    mod_id = proto.FfiIdentity.generate()
    mod_pub = bytes.fromhex(mod_id.public_key_hex())

    class Allow:
        def __init__(self, fp):
            self.fp = fp

        def allows(self, fingerprint):
            return fingerprint == self.fp

    responder = CryptoSession(
        core_id,
        mode="ik",
        role="responder",
        connection="module_initiated",
        remote_pub=mod_pub,
        whitelist=Allow(fingerprint_of(mod_pub)),
    )
    initiator = CryptoSession(
        mod_id,
        mode="ik",
        role="initiator",
        connection="module_initiated",
        remote_pub=bytes.fromhex(core_id.public_key_hex()),
    )

    msg1 = initiator.start()
    assert msg1
    msg2 = responder.read_handshake(msg1)
    assert msg2
    initiator.read_handshake(msg2)
    initiator.establish()
    responder.establish()

    assert responder.state is GatewayState.ESTABLISHED
    assert responder.remote_fingerprint == fingerprint_of(mod_pub)

    wire = initiator.seal(FrameType.RPC, b'{"jsonrpc":"2.0","id":1,"method":"module/heartbeat"}')
    header, plaintext = responder.open(wire)
    assert header.counter == 1
    assert b"module/heartbeat" in plaintext

    with pytest.raises(VadeError) as excinfo:
        responder.open(wire)  # replay
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert responder.state is GatewayState.CLOSED


@pytest.mark.rust
def test_unlisted_static_key_is_rejected():
    proto = _proto()
    core_id = proto.FfiIdentity.generate()
    stranger = proto.FfiIdentity.generate()

    class Deny:
        def allows(self, fingerprint):
            return False

    responder = CryptoSession(
        core_id,
        mode="ik",
        role="responder",
        connection="module_initiated",
        remote_pub=bytes.fromhex(stranger.public_key_hex()),
        whitelist=Deny(),
    )
    initiator = CryptoSession(
        stranger,
        mode="ik",
        role="initiator",
        connection="module_initiated",
        remote_pub=bytes.fromhex(core_id.public_key_hex()),
    )
    msg1 = initiator.start()
    with pytest.raises(VadeError) as excinfo:
        responder.read_handshake(msg1)
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.fatal is True


@pytest.mark.rust
def test_ik_without_remote_pub_does_not_fall_back_to_xx():
    proto = _proto()
    with pytest.raises(VadeError) as excinfo:
        CryptoSession(proto.FfiIdentity.generate(), mode="ik", role="initiator")
    assert excinfo.value.code == "INVALID_ARGUMENT"
