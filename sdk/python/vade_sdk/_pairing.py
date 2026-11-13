"""Pair-code normalisation and PSK derivation (C-15), internal to the SDK.

Frozen rules (frames.md §6):

    normalize: NFKC -> drop '-', spaces (incl U+3000), tab, '·', '・' ->
               per-character uppercase  => exactly 8 chars
    PSK = HKDF-SHA256(ikm = utf8(normalize(code)) || BLAKE2s256(core_pub),
                      salt = "vade/pair/v1", info = "", L = 32)

The SDK must use the SAME normalisation as core; a second implementation is how
"looks identical, handshake fails" bugs happen. `protocol/v1/kat.json` carries
four equivalent spellings whose PSK must be byte-equal, and the tests use it as
the judge (no hand-written vectors).

Status: derivation is implemented and KAT-verified, but the pairing handshake
path is not usable against core yet — core's listener builds an IK responder
only, and XXpsk3 needs the PSK *after* msg2 while `FfiHandshake` takes it at
construction. `--pair-code` therefore fails closed with UNSUPPORTED instead of
pretending to pair. See sdk/PYTHON_SDK.md §5.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from typing import Iterable

__all__ = ["normalize", "derive_psk", "BASE32", "SEPARATORS", "code_shape_ok"]

BASE32 = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SEPARATORS = {"-", " ", "\u3000", "\t", "\u00b7", "\u30fb"}
CODE_LEN = 8
HANZI_PER_CODE = 2
SALT = b"vade/pair/v1"


def normalize(code: str) -> str:
    """NFKC, drop separators, uppercase per character (locale independent)."""
    text = unicodedata.normalize("NFKC", str(code))
    text = "".join(ch for ch in text if ch not in SEPARATORS)
    return "".join(ch.upper() for ch in text)


def code_shape_ok(normalized: str) -> bool:
    return len(normalized) == CODE_LEN and all(ch in BASE32 for ch in normalized[HANZI_PER_CODE:])


def derive_psk(code: str, core_public_key: bytes) -> bytes:
    """HKDF-SHA256 over the normalised code and the core static public key."""
    normalized = normalize(code)
    if not code_shape_ok(normalized):
        raise ValueError("pair code must be 2 hanzi + 6 base32 characters after normalisation")
    ikm = normalized.encode("utf-8") + hashlib.blake2s(bytes(core_public_key)).digest()
    prk = hmac.new(SALT, ikm, hashlib.sha256).digest()
    return hmac.new(prk, b"\x01", hashlib.sha256).digest()  # HKDF-Expand, L=32, info=""
