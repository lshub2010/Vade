"""A real IK handshake where the responder does NOT know the peer's key up front.

This is blocking item A from the C2 review: `make_crypto` used to build
`CryptoSession(mode="ik", role="responder")` without `remote_pub`, which
protocol-core rejects at construction time, and the whitelist stored only
fingerprints. Result: no stranger process could ever connect.
"""

from __future__ import annotations

import pytest

from vade.errors import VadeError
from vade.crypto_gateway.session import CandidateKeySession, CryptoSession, GatewayState, fingerprint_of
from vade.transport.framing import FrameType


def _proto():
    vade_protocol = pytest.importorskip("vade_protocol")
    return vade_protocol


class Allow:
    def __init__(self, *fingerprints: str) -> None:
        self._allowed = set(fingerprints)

    def allows(self, fingerprint: str) -> bool:
        return fingerprint in self._allowed


def _module_side(identity, core_pub: bytes) -> CryptoSession:
    return CryptoSession(
        identity,
        mode="ik",
        role="initiator",
        connection="module_initiated",
        remote_pub=core_pub,
    )


def test_responder_picks_the_right_key_out_of_several_candidates():
    proto = _proto()
    core_id = proto.FfiIdentity.generate()
    wrong = proto.FfiIdentity.generate()
    module_id = proto.FfiIdentity.generate()
    module_pub = bytes.fromhex(module_id.public_key_hex())

    responder = CandidateKeySession(
        core_id,
        [bytes.fromhex(wrong.public_key_hex()), module_pub],
        whitelist=Allow(fingerprint_of(module_pub)),
    )
    initiator = _module_side(module_id, bytes.fromhex(core_id.public_key_hex()))

    msg1 = initiator.start()
    assert msg1
    msg2 = responder.read_handshake(msg1)
    assert msg2, "the second candidate must produce msg2"
    initiator.read_handshake(msg2)
    initiator.establish()
    responder.establish()

    assert responder.attempted_keys == 2
    assert responder.state is GatewayState.ESTABLISHED
    assert responder.remote_fingerprint == fingerprint_of(module_pub)

    wire = initiator.seal(FrameType.RPC, b'{"jsonrpc":"2.0","id":1,"method":"module/register"}')
    header, plaintext = responder.open(wire)
    assert header.counter == 1
    assert b"module/register" in plaintext


def test_unlisted_key_gets_no_msg2_and_the_handshake_fails():
    proto = _proto()
    core_id = proto.FfiIdentity.generate()
    listed = proto.FfiIdentity.generate()
    stranger = proto.FfiIdentity.generate()
    stranger_pub = bytes.fromhex(stranger.public_key_hex())

    responder = CandidateKeySession(
        core_id,
        [bytes.fromhex(listed.public_key_hex())],
        whitelist=Allow(fingerprint_of(bytes.fromhex(listed.public_key_hex()))),
    )
    initiator = _module_side(stranger, bytes.fromhex(core_id.public_key_hex()))
    msg1 = initiator.start()

    with pytest.raises(VadeError) as excinfo:
        responder.read_handshake(msg1)
    assert excinfo.value.code in {"PERMISSION_DENIED", "INVALID_ARGUMENT"}
    assert responder.state is GatewayState.CLOSED


def test_empty_key_pool_fails_closed():
    proto = _proto()
    core_id = proto.FfiIdentity.generate()
    with pytest.raises(VadeError) as excinfo:
        CandidateKeySession(core_id, [])
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.fatal is True


def test_build_module_crypto_completes_a_real_handshake(tmp_path):
    """The factory wired into `python -m vade` must actually work."""
    from vade.app import App
    from vade.config import load_config
    from vade.identity.store import load_or_create
    from vade.__main__ import build_module_crypto

    proto = _proto()
    config = load_config(cli={"core.data_dir": str(tmp_path)})
    app = App(config)
    app.identity = load_or_create(tmp_path)
    from vade.identity.whitelist import Whitelist

    app.whitelist = Whitelist(tmp_path / "identity" / "whitelist.json")

    module_identity = proto.FfiIdentity.generate()
    module_pub = bytes.fromhex(module_identity.public_key_hex())
    app.whitelist.add("com.example.fs", "fs-01", fingerprint_of(module_pub), public_key=module_pub)

    responder = build_module_crypto(app)("conn-1")
    initiator = _module_side(module_identity, bytes.fromhex(app.identity.public_key_hex))

    msg1 = initiator.start()
    msg2 = responder.read_handshake(msg1)
    assert msg2
    initiator.read_handshake(msg2)
    initiator.establish()
    responder.establish()

    assert responder.remote_fingerprint == fingerprint_of(module_pub)
    reply = initiator.seal(FrameType.RPC, b'{"jsonrpc":"2.0","id":7,"method":"module/heartbeat"}')
    header, plaintext = responder.open(reply)
    assert header.type is FrameType.RPC
    assert b"module/heartbeat" in plaintext
