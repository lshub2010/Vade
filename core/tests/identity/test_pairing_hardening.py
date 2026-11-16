"""K6 hardening: indexed lookup, expiry/cap housekeeping, fail-closed budget.

The behaviours pinned here are the ones a reviewer cannot see by reading a
single happy-path test:

* a lookup must NOT run scrypt over the whole table (CPU/memory amplifier on an
  unauthenticated endpoint);
* the pepper file is 0600 and survives a restart (index stability);
* expired rows are removed by create/consume, and unbounded outstanding codes
  are refused;
* an unknown source address is refused rather than exempted;
* `/ui/session` and the admin setup-token endpoints spend ONE per-IP budget and
  ONE global lock, not one each.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from vade.errors import VadeError
from vade.identity import pairing as pairing_module
from vade.identity.pairing import (
    GLOBAL_LOCK_S,
    MAX_ACTIVE_CODES,
    MAX_FAILS_PER_CODE,
    PEPPER_BYTES,
    PEPPER_FILENAME,
    PairingStore,
    Purpose,
)


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def store(tmp_path):
    pairing = PairingStore(tmp_path / "pairing.sqlite3")
    try:
        yield pairing
    finally:
        pairing.close()


def _valid_unknown(store: PairingStore) -> str:
    """A shape-valid code that was never issued (uses the real generator)."""
    return store._generate()


# ------------------------------------------------------------------ O(1) lookup
def test_unknown_code_costs_zero_scrypt_and_known_code_costs_one(tmp_path, monkeypatch):
    calls: list[str] = []
    real = pairing_module._verify

    def counted(code: str, stored: str) -> bool:
        calls.append(stored)
        return real(code, stored)

    monkeypatch.setattr(pairing_module, "_verify", counted)
    store = PairingStore(tmp_path / "pairing.sqlite3")
    try:
        codes = [store.create(Purpose.BROWSER) for _ in range(8)]
        assert len(calls) == 0, "create() must not verify anything"

        with pytest.raises(VadeError) as miss:
            store.consume(_valid_unknown(store), Purpose.BROWSER, ip="127.0.0.1")
        assert miss.value.code == "CODE_INVALID"
        assert calls == [], "a miss must be an index miss, not 8 scrypt runs"

        assert store.consume(codes[3], Purpose.BROWSER, ip="127.0.0.1").purpose is Purpose.BROWSER
        assert len(calls) == 1, "the hit gets exactly one scrypt confirmation"
    finally:
        store.close()


def test_consume_works_after_a_restart_with_the_same_pepper(tmp_path):
    path = tmp_path / "pairing.sqlite3"
    first = PairingStore(path)
    try:
        code = first.create(Purpose.BROWSER)
    finally:
        first.close()
    second = PairingStore(path)
    try:
        assert second.consume(code, Purpose.BROWSER, ip="127.0.0.1").purpose is Purpose.BROWSER
    finally:
        second.close()


# ---------------------------------------------------------------------- pepper
def test_pepper_file_is_created_0600_with_32_bytes(tmp_path):
    store = PairingStore(tmp_path / "pairing.sqlite3")
    try:
        pepper = tmp_path / PEPPER_FILENAME
        assert pepper.exists()
        assert len(pepper.read_bytes()) == PEPPER_BYTES
        assert os.stat(pepper).st_mode & 0o777 == 0o600
        # No plaintext code, and no unhashed code, is ever stored.
        rows = store._db.execute("SELECT code_index, code_hash FROM pair_codes").fetchall()
        assert rows == []
        code = store.create(Purpose.BROWSER)
        index, digest = store._db.execute("SELECT code_index, code_hash FROM pair_codes").fetchone()
        assert len(index) == 64 and index != digest
        assert code not in index and code not in digest
    finally:
        store.close()


def test_a_widened_pepper_mode_is_repaired_on_the_next_start(tmp_path):
    path = tmp_path / "pairing.sqlite3"
    store = PairingStore(path)
    store.close()
    pepper = tmp_path / PEPPER_FILENAME
    os.chmod(pepper, 0o644)
    again = PairingStore(path)
    try:
        assert os.stat(pepper).st_mode & 0o777 == 0o600
    finally:
        again.close()


# ------------------------------------------------------------------- migration
def test_legacy_table_without_code_index_is_migrated_and_unmatchable_rows_dropped(tmp_path):
    path = tmp_path / "pairing.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        """CREATE TABLE pair_codes (
               code_hash TEXT PRIMARY KEY,
               purpose    TEXT NOT NULL,
               module_id  TEXT,
               expected_fp TEXT,
               expires_at REAL NOT NULL,
               fails      INTEGER NOT NULL DEFAULT 0
           )"""
    )
    legacy.execute(
        "INSERT INTO pair_codes (code_hash, purpose, expires_at, fails) VALUES (?,?,?,0)",
        ("scrypt:{00}:deadbeef", "browser", 10**9),
    )
    legacy.commit()
    legacy.close()

    store = PairingStore(path)
    try:
        columns = {row[1] for row in store._db.execute("PRAGMA table_info(pair_codes)")}
        assert "code_index" in columns
        assert store._db.execute("SELECT COUNT(*) FROM pair_codes").fetchone()[0] == 0, (
            "a pre-index row can never be found again; keeping it only makes lookups lie"
        )
        code = store.create(Purpose.BROWSER)
        assert store.consume(code, Purpose.BROWSER, ip="127.0.0.1").purpose is Purpose.BROWSER
    finally:
        store.close()


# ---------------------------------------------------------- expiry and the cap
def test_create_purges_expired_rows(tmp_path):
    clock = _Clock()
    store = PairingStore(tmp_path / "pairing.sqlite3", clock=clock)
    try:
        store.create(Purpose.BROWSER, ttl=10)
        clock.advance(11)
        store.create(Purpose.BROWSER, ttl=10)
        rows = store._db.execute("SELECT COUNT(*) FROM pair_codes").fetchone()[0]
        assert rows == 1, "the expired invite must not linger"
    finally:
        store.close()


def test_consume_purges_expired_rows_but_still_answers_code_expired_for_the_submitted_one(tmp_path):
    clock = _Clock()
    store = PairingStore(tmp_path / "pairing.sqlite3", clock=clock)
    try:
        submitted = store.create(Purpose.BROWSER, ttl=10)
        stale = store.create(Purpose.BROWSER, ttl=10)
        clock.advance(11)
        with pytest.raises(VadeError) as excinfo:
            store.consume(submitted, Purpose.BROWSER, ip="127.0.0.1")
        # The submitted row gets the precise answer; the rest is housekeeping.
        assert excinfo.value.code == "CODE_EXPIRED"
        assert store._db.execute("SELECT COUNT(*) FROM pair_codes").fetchone()[0] == 0
        _ = stale
    finally:
        store.close()


def test_outstanding_codes_are_capped_and_revoke_frees_a_slot(tmp_path):
    store = PairingStore(tmp_path / "pairing.sqlite3", max_active_codes=3)
    try:
        codes = [store.create(Purpose.BROWSER) for _ in range(3)]
        with pytest.raises(VadeError) as excinfo:
            store.create(Purpose.BROWSER)
        assert excinfo.value.code == "BUDGET_EXCEEDED"
        assert store.revoke(codes[0]) is True
        store.create(Purpose.BROWSER)
        assert store._db.execute("SELECT COUNT(*) FROM pair_codes").fetchone()[0] == 3
    finally:
        store.close()


def test_default_cap_is_the_exported_constant():
    assert MAX_ACTIVE_CODES >= 16
    assert MAX_FAILS_PER_CODE == 5


# ---------------------------------------------------------------- fail closed
def test_missing_source_address_is_refused_and_does_not_spend_the_code(store):
    code = store.create(Purpose.BROWSER)
    for missing in (None, "", "   "):
        with pytest.raises(VadeError) as excinfo:
            store.consume(code, Purpose.BROWSER, ip=missing)
        assert excinfo.value.code == "PERMISSION_DENIED"
    # Nothing was consumed and no unattributable attempt was recorded.
    assert store._db.execute("SELECT COUNT(*) FROM pair_attempts").fetchone()[0] == 0
    assert store.consume(code, Purpose.BROWSER, ip="127.0.0.1").purpose is Purpose.BROWSER


def test_missing_source_address_is_refused_for_setup_attempts_too(store):
    with pytest.raises(VadeError) as excinfo:
        store.begin_setup_attempt(None)
    assert excinfo.value.code == "PERMISSION_DENIED"


# ------------------------------------------------------------- shared budgets
def test_setup_and_pair_code_attempts_share_the_per_ip_budget(store):
    ip = "198.51.100.7"
    for _ in range(4):
        store.begin_setup_attempt(ip)
    # The fifth guess — on the OTHER endpoint — is spent from the same bucket.
    with pytest.raises(VadeError) as excinfo:
        store.consume(_valid_unknown(store), Purpose.BROWSER, ip=ip)
    assert excinfo.value.code == "CODE_INVALID"
    with pytest.raises(VadeError) as locked:
        store.begin_setup_attempt(ip)
    assert locked.value.code == "ADMIN_LOCKED"
    assert locked.value.data == {"retry_after": 300}
    # A second address has its own budget; sharing does not mean one address.
    store.begin_setup_attempt("198.51.100.8")


def test_twenty_failures_anywhere_lock_every_source_for_fifteen_minutes(tmp_path):
    clock = _Clock()
    store = PairingStore(tmp_path / "pairing.sqlite3", clock=clock)
    try:
        for index in range(20):
            with pytest.raises(VadeError):
                store.consume(_valid_unknown(store), Purpose.BROWSER, ip=f"203.0.113.{index}")
        with pytest.raises(VadeError) as excinfo:
            store.begin_setup_attempt("192.0.2.99")
        assert excinfo.value.code == "ADMIN_LOCKED"
        assert 1 <= excinfo.value.data["retry_after"] <= GLOBAL_LOCK_S

        # Refused attempts must not extend the lock.
        for _ in range(3):
            with pytest.raises(VadeError):
                store.begin_setup_attempt("192.0.2.99")
        clock.advance(GLOBAL_LOCK_S + 1)
        store.begin_setup_attempt("192.0.2.99")
    finally:
        store.close()
