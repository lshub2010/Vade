"""Pair-code normalisation and PSK derivation against C-02's vectors.

`protocol/v1/kat.json` carries four spellings of the same code (canonical,
full-width, lowercase, middle-dot) plus the expected PSK. Using the vectors as
the judge is the only way to prove the SDK and core agree — a hand-written
expectation would just repeat our own bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vade_sdk._pairing import derive_psk, normalize

REPO = Path(__file__).resolve().parents[3]
KAT = REPO / "protocol" / "v1" / "kat.json"


def _kat() -> dict:
    if not KAT.exists():
        pytest.skip("protocol/v1/kat.json is not present")
    return json.loads(KAT.read_text(encoding="utf-8"))


def test_normalize_matches_kat():
    for vector in _kat()["normalize"]:
        assert normalize(vector["input"]) == vector["expected"], vector["name"]


def test_all_four_spellings_derive_one_psk():
    vectors = _kat()["hkdf"]
    psks = {derive_psk(v["pair_code"], bytes.fromhex(v["core_pub"])).hex() for v in vectors}
    assert len(psks) == 1, psks
    assert psks.pop() == vectors[0]["psk"]


def test_bad_code_shape_is_rejected():
    with pytest.raises(ValueError):
        derive_psk("nope", b"\x00" * 32)
