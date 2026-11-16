"""Pair-code constraints: purpose, per-code burn, constraint 5, IP budget.

Every test here targets a path the C2 review found dead or wrong:
`note_failure` had no caller, constraint 5 was unreachable because `App`
injected `fingerprint_for(module_id, "*")`, and a successful consume was
charged against the per-IP budget.
"""

from __future__ import annotations

import pytest

from vade.errors import VadeError
from vade.identity.pairing import (
    _HANZI,
    MAX_FAILS_PER_CODE,
    PairingStore,
    Purpose,
    code_shape_ok,
    normalize,
)


@pytest.fixture()
def store(tmp_path):
    pairing = PairingStore(tmp_path / "pairing.sqlite3")
    try:
        yield pairing
    finally:
        pairing.close()


def test_hanzi_table_is_256_unique():
    assert len(_HANZI) == 256
    assert len(set(_HANZI)) == 256


def test_generated_code_has_the_documented_shape(store):
    code = store.create(Purpose.BROWSER)
    assert code_shape_ok(normalize(code))
    hanzi, _, body = code.partition("-")
    assert len(hanzi) == 2 and all(ch in _HANZI for ch in hanzi)
    assert len(body) == 6


def test_module_code_requires_module_id(store):
    with pytest.raises(VadeError) as excinfo:
        store.create(Purpose.MODULE)
    assert excinfo.value.code == "INVALID_ARGUMENT"


def test_four_spellings_consume_once_then_fail(store):
    code = store.create(Purpose.BROWSER)
    hanzi, _, body = code.partition("-")
    variants = [
        code,
        f"{hanzi}{body.lower()}",
        f"{hanzi} {body}",
        f"{normalize(hanzi)}-{''.join(chr(ord(c) + 0xFEE0) if c.isascii() else c for c in body)}",
    ]
    assert store.consume(variants[0], Purpose.BROWSER, ip="127.0.0.1").purpose is Purpose.BROWSER
    for variant in variants:
        with pytest.raises(VadeError) as excinfo:
            store.consume(variant, Purpose.BROWSER, ip="198.51.100.1")
        assert excinfo.value.code == "CODE_INVALID"


def test_purpose_mismatch_never_consumes_and_burns_the_code(store):
    code = store.create(Purpose.BROWSER)
    for _ in range(MAX_FAILS_PER_CODE):
        with pytest.raises(VadeError) as excinfo:
            store.consume(code, Purpose.MODULE, ip="127.0.0.1", fingerprint="0" * 16)
        assert excinfo.value.code == "CODE_INVALID"
    # Fifth miss destroyed the row: even the right purpose cannot use it now.
    with pytest.raises(VadeError):
        store.consume(code, Purpose.BROWSER, ip="127.0.0.1")


def test_note_failure_is_wired_and_destroys_after_five(store):
    code = store.create(Purpose.BROWSER)
    for _ in range(MAX_FAILS_PER_CODE):
        store.note_failure(code)
    with pytest.raises(VadeError) as excinfo:
        store.consume(code, Purpose.BROWSER, ip="127.0.0.1")
    assert excinfo.value.code == "CODE_INVALID"


def test_constraint_5_refuses_a_different_fingerprint(tmp_path):
    existing = "a" * 16
    pairing = PairingStore(tmp_path / "pairing.sqlite3", existing_fingerprint=lambda module_id: existing)
    try:
        code = pairing.create(Purpose.MODULE, module_id="com.example.fs")
        with pytest.raises(VadeError) as excinfo:
            pairing.consume(code, Purpose.MODULE, ip="127.0.0.1", fingerprint="b" * 16)
        assert excinfo.value.code == "PERMISSION_DENIED"
        # A refused claim must not burn the code: the legitimate owner can retry.
        assert pairing.consume(code, Purpose.MODULE, ip="127.0.0.1", fingerprint=existing).module_id == "com.example.fs"
    finally:
        pairing.close()


def test_constraint_5_refuses_when_the_claiming_key_is_unknown(tmp_path):
    pairing = PairingStore(tmp_path / "pairing.sqlite3", existing_fingerprint=lambda module_id: "a" * 16)
    try:
        code = pairing.create(Purpose.MODULE, module_id="com.example.fs")
        with pytest.raises(VadeError) as excinfo:
            pairing.consume(code, Purpose.MODULE, ip="127.0.0.1")
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert "must be supplied" in excinfo.value.message
    finally:
        pairing.close()


def test_constraint_5_allows_the_expected_fingerprint_on_first_pairing(tmp_path):
    pairing = PairingStore(tmp_path / "pairing.sqlite3", existing_fingerprint=lambda module_id: None)
    try:
        code = pairing.create(Purpose.MODULE, module_id="com.example.fs", expected_fp="c" * 16)
        record = pairing.consume(code, Purpose.MODULE, ip="127.0.0.1", fingerprint="c" * 16)
        assert record.expected_fp == "c" * 16
    finally:
        pairing.close()


def test_successful_consumes_do_not_count_toward_the_ip_budget(tmp_path):
    pairing = PairingStore(tmp_path / "pairing.sqlite3")
    try:
        for _ in range(6):
            code = pairing.create(Purpose.BROWSER)
            assert pairing.consume(code, Purpose.BROWSER, ip="127.0.0.1")
    finally:
        pairing.close()


def test_five_failures_from_one_ip_lock_it_out(store):
    for index in range(5):
        with pytest.raises(VadeError) as excinfo:
            store.consume(f"青松-AAAA{chr(ord('A') + index)}A", Purpose.BROWSER, ip="203.0.113.7")
        assert excinfo.value.code == "CODE_INVALID"
    with pytest.raises(VadeError) as excinfo:
        store.consume("青松-AAAAAZ", Purpose.BROWSER, ip="203.0.113.7")
    assert excinfo.value.code == "ADMIN_LOCKED"
    assert excinfo.value.data == {"retry_after": 300}
