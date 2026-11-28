"""Windows text-mode fd regression: every raw write must survive intact.

`os.open()` without `O_BINARY` puts the descriptor in text mode on Windows and
the CRT then translates every LF byte into CRLF. The 32 pepper bytes are random
(~12% chance of at least one LF), so the file came back 33+ bytes and
`_load_pepper` fail-closed with "must be 32 bytes" on roughly every sixth
start. Measured before the fix: 51 corrupt files in 500 generations.

The decisive tests here build payloads that are GUARANTEED to contain LF bytes,
so they were red on Windows before the fix and are green after it. On Linux the
fd was never in text mode, so the same tests are green before and after — they
pin the contract, they do not discriminate platforms there.
"""

from __future__ import annotations

import json
import os
import pathlib

from vade.admin.auth import SessionStore
from vade.admin.identity import load_or_create as _admin_load_or_create
from vade.admin.setup import SetupService
from vade.admin.setup_token import _write as _setup_token_write
from vade.admin.setup_token import read_token, token_path
from vade.identity.pairing import _load_pepper
from vade.identity.whitelist import Whitelist, fp8_hex
from vade.skill.loader import _atomic_write as _skill_atomic_write

#: 32 LF bytes — the smallest payload that is guaranteed to be corrupted by a
#: text-mode fd (pre-fix Windows wrote it back as 64 bytes).
PAYLOAD_32LF = b"\n" * 32

#: 32 bytes with exactly three LF bytes — pre-fix Windows inflated this to 35
#: bytes, which the strict 32-byte length check must reject.
PAYLOAD_3LF = b"A" * 10 + b"\n" + b"B" * 10 + b"\n" + b"C" * 8 + b"\n" + b"D"

#: Mixed CRLF/LF content: pre-fix Windows rewrote every LF (including the one
#: inside each CRLF) into CRLF, growing 24 LFs into 24 extra bytes.
PAYLOAD_CRLF_MIX = b"\r\n" * 8 + b"\n" * 16


# ---------------------------------------------------------------- decisive
def test_pepper_payload_of_only_line_feeds_roundtrips_byte_exact(tmp_path, monkeypatch):
    """Regression: pepper bytes must survive the fd untouched (Windows O_BINARY).

    This exact payload (b"\\n" * 32) was corrupted by text-mode translation
    before the fix: 500 random generations produced 51 oversize files. Patches
    token_bytes to force 32 LF bytes through the REAL production write path,
    making that failure deterministic instead of probabilistic.
    """
    import vade.identity.pairing as pairing_mod

    monkeypatch.setattr(pairing_mod.secrets, "token_bytes", lambda n: PAYLOAD_32LF)
    path = tmp_path / "pairing.pepper"
    raw = _load_pepper(path)
    assert len(raw) == 32
    assert path.read_bytes() == raw == PAYLOAD_32LF  # file on disk is byte-exact


def test_pepper_with_three_line_feeds_is_not_inflated(tmp_path):
    path = tmp_path / "pairing.pepper"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = __import__("os").open(
        path,
        __import__("os").O_WRONLY | __import__("os").O_CREAT | __import__("os").O_EXCL
        | getattr(__import__("os"), "O_BINARY", 0),
        0o600,
    )
    try:
        __import__("os").write(fd, PAYLOAD_3LF)
    finally:
        __import__("os").close(fd)
    # The strict length check must accept a 32-byte payload containing LFs.
    assert _load_pepper(path) == PAYLOAD_3LF
    assert path.read_bytes() == PAYLOAD_3LF  # still exactly 32 bytes, not 35


def test_pepper_with_mixed_crlf_survives_text_mode_fds(tmp_path):
    path = tmp_path / "pairing.pepper"
    path.parent.mkdir(parents=True, exist_ok=True)
    os = __import__("os")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        os.write(fd, PAYLOAD_CRLF_MIX)
    finally:
        os.close(fd)
    assert _load_pepper(path) == PAYLOAD_CRLF_MIX


# ---------------------------------------------------------------- short write
def test_pepper_write_completes_a_short_os_write(tmp_path, monkeypatch):
    """A mocked short `os.write` must not silently truncate the pepper."""
    os = __import__("os")
    real_write = os.write
    written_log: list[int] = []

    def short_write(fd, data):
        n = real_write(fd, memoryview(data)[: max(1, len(data) // 2)])
        written_log.append(n)
        return n

    monkeypatch.setattr("vade.identity.pairing.os.write", short_write)
    path = tmp_path / "pairing.pepper"
    raw = _load_pepper(path)
    monkeypatch.setattr("vade.identity.pairing.os.write", real_write)
    assert written_log and written_log[0] < 32  # the mock really short-wrote
    assert len(raw) == 32
    assert path.read_bytes() == raw


# ---------------------------------------------------------------- per module
def test_session_store_persist_roundtrips_raw(tmp_path):
    """sessions.json is written with raw os.write + indent=2 JSON, so its
    structural LF bytes were CRLF-widened by a text-mode fd on Windows."""
    path = tmp_path / "sessions.json"
    store = SessionStore(persist_path=path)
    store.issue(mode="device", device_id="dev1")
    raw = path.read_bytes()
    assert b"\n" in raw  # indent=2 guarantees structural LFs in the doc
    assert b"\r\n" not in raw  # none of them may be widened
    reloaded = SessionStore(persist_path=path)  # must parse on restart
    assert reloaded.password_version == 0


def test_setup_token_write_roundtrips_raw(tmp_path):
    path = token_path(tmp_path)
    token = PAYLOAD_3LF.decode("ascii")
    _setup_token_write(path, token)
    assert path.read_bytes() == PAYLOAD_3LF  # exactly 32 bytes, LFs untouched
    assert read_token(tmp_path) == token


def test_setup_state_atomic_write_roundtrips_raw(tmp_path):
    target = tmp_path / "state.json"
    SetupService._atomic_write(target, PAYLOAD_3LF)
    assert target.read_bytes() == PAYLOAD_3LF


def test_core_identity_key_is_stored_byte_exact(tmp_path):
    """The Ed25519 seed is random: it must not be translated by the fd."""
    identity = _admin_load_or_create(tmp_path)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(files) == 1
    raw = files[0].read_bytes()
    # Raw serialization = the 32-byte seed; the file must be byte-exact.
    assert raw == identity._key.private_bytes_raw()
    assert len(raw) == 32


def test_core_key_store_roundtrips_raw(tmp_path):
    """core.key is written through fdopen("w") — byte-exact including the
    trailing LF (a text-mode fd widened it to CRLF before the fix)."""
    from vade.identity.store import KEY_FILENAME, load_or_create

    identity = load_or_create(tmp_path)
    raw = (tmp_path / KEY_FILENAME).read_bytes()
    assert raw == (identity.secret_hex() + "\n").encode("ascii")  # 65 bytes
    assert len(raw) == 65


def test_whitelist_flush_roundtrips_raw(tmp_path):
    """whitelist.json is written through a text wrapper — pin the LF contract."""
    pub = b"\n" * 32  # a valid 32-byte key whose fingerprint is deterministic
    path = tmp_path / "whitelist.json"
    wl = Whitelist(path)
    wl.add("mod", "inst-1", fp8_hex(pub), public_key=pub)
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    doc = json.loads(raw.decode("utf-8"))
    assert doc["entries"][0]["public_key"] == pub.hex()
    reloaded = Whitelist(path)
    assert reloaded.allows(fp8_hex(pub))


def test_skill_store_atomic_write_roundtrips_raw(tmp_path):
    target = tmp_path / "sub" / "contract.json"
    _skill_atomic_write(target, PAYLOAD_3LF)
    assert target.read_bytes() == PAYLOAD_3LF


def test_skill_store_write_completes_a_short_fdopen_write(tmp_path, monkeypatch):
    """loader._atomic_write uses fdopen("wb"); short TextIO writes are retried
    by the io layer, but a mocked raw `os.write` short write must still leave
    every byte on disk."""
    os = __import__("os")
    real_write = os.write

    def half_write(fd, data):
        return real_write(fd, memoryview(data)[: max(1, len(data) // 2)])

    monkeypatch.setattr("vade.skill.loader.os.write", half_write)
    target = tmp_path / "contract.json"
    _skill_atomic_write(target, PAYLOAD_32LF)
    assert target.read_bytes() == PAYLOAD_32LF


def test_no_undecorated_write_open_flags_remain():
    """Guard: every raw os.open write site in the fixed modules passes O_BINARY.

    catalog.py's O_RDONLY directory fd is deliberately exempt (read-only, no
    translation applies) and admin/locks.py is owned by another fix.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "core" / "vade"
    pattern = re.compile(r"os\.open\(([^)]*)\)")
    offenders: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if py.name == "locks.py":
            continue
        for match in pattern.finditer(py.read_text(encoding="utf-8")):
            flags = match.group(1)
            if "O_RDONLY" in flags:
                continue
            if "_O_BINARY" not in flags:
                offenders.append(f"{py.relative_to(root.parent.parent)}:{flags.strip()}")
    assert offenders == []
