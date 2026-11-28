"""Windows text-mode fd regression for the SDK transport's key/pin writers.

`module.key` and `core_pin.json` are written through `os.open` + `fdopen`.
Without `O_BINARY` on the descriptor, Windows translated the trailing LF of the
key file into CRLF (65 -> 66 bytes) and every indented JSON line of the pin
file grew by one byte, so the 64-hex read-back check failed after a restart.
"""

from __future__ import annotations

import json

from vade_sdk.transport import load_or_create_key, save_core_pin


def test_module_key_file_roundtrips_raw(tmp_path, monkeypatch):
    target = tmp_path / "module.key"
    monkeypatch.setenv("MODULE_KEY_PATH", str(target))
    seed, _public = load_or_create_key(target)
    raw = target.read_bytes()
    assert raw == (seed + "\n").encode("ascii")  # byte-exact, LF not widened
    assert len(raw) == 65


def test_widened_legacy_key_file_still_loads_same_seed(tmp_path, monkeypatch):
    """A file widened by a pre-fix text-mode write (66 B, CRLF tail) still loads
    the SAME key: the read path strips whitespace, so the corruption was latent
    here — and the never-overwrite rule must not regenerate the file either."""
    target = tmp_path / "module.key"
    target.write_bytes(b"a" * 64 + b"\r\n")  # what pre-fix Windows wrote
    monkeypatch.setenv("MODULE_KEY_PATH", str(target))
    seed, _public = load_or_create_key(target)
    assert seed == "a" * 64  # same key is recovered
    assert target.read_bytes() == b"a" * 64 + b"\r\n"  # never regenerated


def test_core_pin_write_roundtrips_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("VADE_DATA_DIR", str(tmp_path))
    save_core_pin(object(), b"\n" * 32)
    path = tmp_path / "core_pin.json"
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    doc = json.loads(raw.decode("utf-8"))
    assert doc["core_public_key"] == "0a" * 32
    assert doc["fingerprint"] == json.loads(raw.decode("utf-8"))["fingerprint"]
