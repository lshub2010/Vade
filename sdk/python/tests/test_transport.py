"""Transport-level invariants that do not need a live core."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from vade_sdk.transport import HEADER_LEN, TransportError, _frame, _peek, fingerprint_of, load_or_create_key


def test_key_file_is_created_0600_and_never_overwritten(tmp_path):
    path = tmp_path / "data" / "module.key"
    seed, public = load_or_create_key(path)
    assert len(seed) == 64 and len(public) == 32
    # POSIX mode bits are a no-op on Windows (chmod does nothing), so the 0600 claim
    # is only checkable on POSIX. Same guard, same wording as
    # core/tests/admin/test_passwords.py and core/tests/test_skill/test_install.py —
    # Linux still validates; nothing else in this test is relaxed.
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, oct(mode)

    again, again_public = load_or_create_key(path)
    assert again == seed
    assert again_public == public
    assert fingerprint_of(public) == fingerprint_of(again_public)


def test_a_corrupt_key_file_is_an_error_not_a_regeneration(tmp_path):
    path = tmp_path / "module.key"
    path.write_text("not-a-key\n", encoding="utf-8")
    with pytest.raises(TransportError, match="64 hex"):
        load_or_create_key(path)
    assert path.read_text(encoding="utf-8") == "not-a-key\n"


def test_header_peek_accepts_a_handshake_frame():
    wire = _frame(0x01, b"hello")
    header = _peek(wire)
    assert header["type"] == 0x01
    assert header["payload_len"] == 5
    assert len(wire) == HEADER_LEN + 5


@pytest.mark.parametrize("version", [0, 2, 255])
def test_wrong_version_is_rejected_before_any_decryption(version):
    wire = bytearray(_frame(0x02, b"x"))
    wire[0] = version
    with pytest.raises(TransportError) as excinfo:
        _peek(bytes(wire))
    assert excinfo.value.code == "PROTOCOL_MISMATCH"
    assert excinfo.value.fatal is True


def test_reserved_bits_and_unknown_types_are_fatal():
    wire = bytearray(_frame(0x02, b"x"))
    wire[2] = 1
    with pytest.raises(TransportError, match="reserved"):
        _peek(bytes(wire))
    with pytest.raises(TransportError, match="unknown frame type"):
        _peek(bytes(_frame(0x09, b"x")))


def test_encrypted_frame_with_counter_zero_is_rejected():
    header = bytearray(_frame(0x02, b"x"))
    header[12:20] = (0).to_bytes(8, "big")
    with pytest.raises(TransportError, match="counter 0"):
        _peek(bytes(header))
