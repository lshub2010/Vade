"""Fingerprint naming split (D78) + whitelist record self-check.

Three values that are easy to confuse, and each confusion is a real bug:

* `fp8_hex(pub)` — `SHA-256(X25519 pub)[:16]`, the identity fingerprint humans
  compare (`--print-fingerprint`, QR `fp`, setup page).
* `spki_fingerprint_hex(cert)` — `SHA-256(SPKI)[:16]`, the TLS pin (`tls=`,
  D78). Must NOT degrade to the certificate DER when `cryptography` is absent.
* Rust `Identity::fingerprint_hex()` — BLAKE2s, the whitelist / pair-code PSK
  value. Its uniffi name is frozen; `psk_fingerprint_hex()` is the honest alias.

Whitelist records that carry a public key must prove their fingerprint:
`sha256(pub)[:16] == fingerprint`. A bad pair is dropped on load and refused by
`add()`. Legacy records without a public key are loaded untouched.
"""

from __future__ import annotations

import builtins
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from vade.errors import VadeError
from vade.identity.tls import (
    cert_fingerprint_hex,
    ensure_certificates,
    spki_fingerprint_hex,
)
from vade.identity.whitelist import Whitelist, fp8_hex

PUB = bytes(range(32))
FP = fp8_hex(PUB)


def _generated_cert(tmp_path: Path) -> Path:
    cert, _key = ensure_certificates(None, None, tmp_path)
    return Path(cert)


def _write(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


# --------------------------------------------------------------------- SPKI


def test_spki_fingerprint_is_sha256_of_the_public_key_info_not_the_der(tmp_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert_path = _generated_cert(tmp_path)
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    spki = certificate.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    expected = hashlib.sha256(spki).hexdigest()[:16]

    value = spki_fingerprint_hex(cert_path)
    assert value == expected
    assert len(value) == 16
    assert value == value.lower()
    int(value, 16)  # valid hex

    # The DER digest is a different value: if these ever match, someone
    # "implemented" SPKI by hashing the whole certificate.
    assert value != cert_fingerprint_hex(cert_path)[:16]


def test_missing_cryptography_is_unsupported_not_a_der_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cryptography  # noqa: F401  (installed here; the fake import below hides it)

    cert_path = _generated_cert(tmp_path)
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError("cryptography is not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(VadeError) as excinfo:
        spki_fingerprint_hex(cert_path)
    assert excinfo.value.code == "UNSUPPORTED"
    assert "cryptography" in excinfo.value.message
    # Explicitly not the DER value hiding behind a fallback.
    assert "DER" in excinfo.value.message


# ---------------------------------------------------------------- whitelist


def test_fp8_hex_is_sha256_first_16_and_keeps_the_private_alias() -> None:
    from vade.identity.whitelist import _fp8

    assert FP == hashlib.sha256(PUB).hexdigest()[:16]
    assert _fp8 is fp8_hex
    assert _fp8(PUB) == FP


def test_load_keeps_consistent_entry(tmp_path: Path) -> None:
    path = tmp_path / "whitelist.json"
    _write(
        path,
        [
            {
                "module_id": "com.example.fs",
                "instance_id": "fs-01",
                "fingerprint": FP,
                "public_key": PUB.hex(),
                "added_at": 1,
            }
        ],
    )
    whitelist = Whitelist(path)
    assert whitelist.allows(FP)
    assert whitelist.fingerprint_for("com.example.fs", "fs-01") == FP
    assert whitelist.public_key_for(FP) == PUB
    assert whitelist.candidate_keys() == (PUB,)


def test_load_drops_entry_whose_key_does_not_prove_the_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "whitelist.json"
    bad_fp = "00" * 8
    _write(
        path,
        [
            {
                "module_id": "com.example.good",
                "instance_id": "g-01",
                "fingerprint": FP,
                "public_key": PUB.hex(),
                "added_at": 1,
            },
            {
                "module_id": "com.example.bad",
                "instance_id": "b-01",
                "fingerprint": bad_fp,
                "public_key": PUB.hex(),  # proves FP, not bad_fp
                "added_at": 2,
            },
        ],
    )
    whitelist = Whitelist(path)
    assert [f"{e.module_id}#{e.instance_id}" for e in whitelist.entries()] == ["com.example.good#g-01"]
    assert not whitelist.allows(bad_fp)
    assert whitelist.public_key_for(bad_fp) is None
    assert whitelist.device_for_fingerprint(bad_fp) is None


def test_load_drops_malformed_or_non_32_byte_keys(tmp_path: Path) -> None:
    path = tmp_path / "whitelist.json"
    short = b"\x01" * 31
    _write(
        path,
        [
            {
                "module_id": "m",
                "instance_id": "not-hex",
                "fingerprint": "aa" * 8,
                "public_key": "zz" * 32,
                "added_at": 1,
            },
            {
                "module_id": "m",
                "instance_id": "short",
                "fingerprint": fp8_hex(short),
                "public_key": short.hex(),
                "added_at": 2,
            },
        ],
    )
    whitelist = Whitelist(path)
    assert whitelist.entries() == ()
    assert whitelist.candidate_keys() == ()


def test_load_keeps_legacy_entry_without_public_key(tmp_path: Path) -> None:
    path = tmp_path / "whitelist.json"
    legacy_fp = "ab" * 8
    _write(
        path,
        [
            {
                "module_id": "com.example.legacy",
                "instance_id": "old-01",
                "fingerprint": legacy_fp,
                "added_at": 1,
            }
        ],
    )
    whitelist = Whitelist(path)
    assert whitelist.allows(legacy_fp)
    assert whitelist.fingerprint_for("com.example.legacy", "old-01") == legacy_fp
    assert whitelist.public_key_for(legacy_fp) is None
    assert whitelist.candidate_keys() == ()
    assert whitelist.revoke(legacy_fp) == ("com.example.legacy#old-01",)


def test_add_refuses_a_key_that_does_not_match_the_fingerprint(tmp_path: Path) -> None:
    whitelist = Whitelist(tmp_path / "whitelist.json")
    with pytest.raises(VadeError) as excinfo:
        whitelist.add("com.example.fs", "fs-01", "00" * 8, public_key=PUB)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert "does not match" in excinfo.value.message
    assert whitelist.entries() == ()


def test_add_accepts_and_persists_a_consistent_pair(tmp_path: Path) -> None:
    path = tmp_path / "whitelist.json"
    Whitelist(path).add("com.example.fs", "fs-01", FP, public_key=PUB)
    reloaded = Whitelist(path)
    entry = reloaded.entries()[0]
    assert entry.fingerprint == FP
    assert entry.public_key == PUB.hex()
    assert reloaded.public_key_for(FP) == PUB
