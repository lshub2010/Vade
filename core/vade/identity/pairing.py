"""Pair codes — the ONLY implementation (C-15 consumer).

Shape: 2 hanzi + 6 base32, displayed `青松-A7K2M9`.
  alphabet: ABCDEFGHJKLMNPQRSTUVWXYZ23456789  (no I/O/0/1)
  entropy : 256^2 * 32^6 ~= 2^46  (the table is 256 unique chars)

Five constraints that make this safe (21-C2 T1):
  1. `purpose ∈ {module, browser}` and consumption must match.
  2. Failure counter per code (5 -> code is destroyed); per-IP 5 / 5 min.
  3. Entropy as above (the anchor hanzi exist so a human reads it right).
  4. Consume = DELETE ... WHERE code_hash AND purpose, judged by rowcount.
     "mark used" can be consumed twice under concurrency; rowcount cannot.
  5. If module_id is supplied and that module already has a DIFFERENT
     fingerprint, refuse: otherwise one code silently displaces a trusted node.

Storage note (honest deviation): the spec says `argon2id(normalize(code))`.
`argon2-cffi` is not installed in this environment, so this uses
`hashlib.scrypt(n=2**14, r=8, p=1)` with a per-code salt — still memory-hard
and salted, still never storing plaintext. Switching to argon2id later only
changes `_hash()`; the stored record carries `algo` so old rows stay readable.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from ..errors import VadeError

__all__ = ["normalize", "PairingStore", "PairCode", "Purpose", "BASE32", "code_shape_ok"]

BASE32 = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SEPARATORS = {"-", " ", "\u3000", "\t", "\u00b7", "\u30fb"}
CODE_LEN = 8
HANZI_PER_CODE = 2
TTL_DEFAULT = 180
MAX_FAILS_PER_CODE = 5
IP_WINDOW_S = 300
IP_MAX_ATTEMPTS = 5

#: Placeholder 256-char table, 256 UNIQUE characters (asserted by
#: `core/tests/identity/test_pairing.py::test_hanzi_table_is_256_unique`).
#: C-15's `hanzi_table` is still null in schema/pair-code.json; generation
#: needs *some* anchor characters, but nothing downstream depends on WHICH
#: characters (consumption validates shape only, exactly like
#: protocol/core/src/paircode.rs). Replace when C-15 lands its table.
_HANZI = (
    "青松明月山川云海风雨雷电江河湖泽林森木火土金水晶光天日星辰春夏秋"
    "冬东南西北中和正大平安康健喜乐福寿仁义礼智信忠孝廉耻勇诚敬谦慎勤"
    "恒思学志远宁静清泉溪谷岩峰岭原野田禾苗花草竹梅兰菊莲桂枫柳柏槐梧"
    "桐鸿雁鹤鹰燕雀鱼龙虎豹鹿马牛羊鸡犬象狮熊凤麟龟蛇蝶蜂蝉萤虹霞露霜"
    "雪冰雾烟波潮汐浪涛舟桥岸渡城乡村镇街巷庭院楼阁台亭廊窗门户墙垣阶"
    "梯径路里程尺寸斤两书琴棋画诗酒茶香墨砚笔纸卷轴章句篇词曲歌舞蹈戏"
    "剧鼓钟铃箫笛弦剑弓刀斧锤钩绳索网罗囊袋箱柜桌椅床榻枕席帘幔帐帷幕"
    "峦崖壁峡涧源潭渊泊汉淮汛洲渚滩礁屿岛堤坝梁道衢市井郊畴阡陌稼稻麦"
)


class Purpose(str, Enum):
    MODULE = "module"
    BROWSER = "browser"


@dataclass(frozen=True, slots=True)
class PairCode:
    purpose: Purpose
    module_id: str | None
    expected_fp: str | None
    expires_at: float


def normalize(code: str) -> str:
    """NFKC -> drop separators -> per-char uppercase (locale independent)."""
    text = unicodedata.normalize("NFKC", code)
    text = "".join(ch for ch in text if ch not in SEPARATORS)
    return "".join(ch.upper() for ch in text)


def code_shape_ok(code: str) -> bool:
    return len(code) == CODE_LEN and all(ch in BASE32 for ch in code[HANZI_PER_CODE:])


def _hash(code: str, salt: bytes) -> str:
    digest = hashlib.scrypt(code.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt:{" + salt.hex() + "}:" + digest.hex()


def _verify(code: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split(":")
    except ValueError:
        return False
    # Stored format: `scrypt:{<salt hex>}:<digest hex>`; the algo prefix keeps
    # old rows readable if the KDF changes.
    if algo != "scrypt" or not (salt_hex.startswith("{") and salt_hex.endswith("}")):
        return False
    try:
        salt = bytes.fromhex(salt_hex[1:-1])
        expected = _hash(code, salt).rsplit(":", 1)[-1]
    except ValueError:
        return False
    return hmac.compare_digest(expected, digest_hex)


class PairingStore:
    def __init__(
        self,
        path: Path | str,
        *,
        existing_fingerprint: Callable[[str, str | None], str | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._existing_fingerprint = existing_fingerprint
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        # One connection, two threads in production (uvicorn's loop calls
        # /ui/session while admin code runs elsewhere). Without a lock the
        # implicit transactions interleave and committed attempt rows go
        # missing, which silently disables the 5/5min budget.
        self._lock = threading.RLock()
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS pair_codes (
                   code_hash TEXT PRIMARY KEY,
                   purpose    TEXT NOT NULL,
                   module_id  TEXT,
                   expected_fp TEXT,
                   expires_at REAL NOT NULL,
                   fails      INTEGER NOT NULL DEFAULT 0
               )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS pair_attempts (
                   ip TEXT NOT NULL,
                   ts REAL NOT NULL
               )"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_attempts ON pair_attempts(ip, ts)")
        self._db.commit()

    # ------------------------------------------------------------------ create
    def create(
        self,
        purpose: Purpose | str,
        module_id: str | None = None,
        expected_fp: str | None = None,
        ttl: int = TTL_DEFAULT,
    ) -> str:
        with self._lock:
            return self._create(purpose, module_id, expected_fp, ttl)

    def _create(
        self,
        purpose: Purpose | str,
        module_id: str | None = None,
        expected_fp: str | None = None,
        ttl: int = TTL_DEFAULT,
    ) -> str:
        purpose = Purpose(purpose)
        if purpose is Purpose.MODULE and not module_id:
            # Without module_id constraint 5 cannot be enforced, and a code that
            # can be replayed against any module is exactly the displacement
            # hole the constraint exists for.
            raise VadeError("INVALID_ARGUMENT", "purpose=module requires module_id (constraint 5)")
        plaintext = self._generate()
        stored = _hash(normalize(plaintext), secrets.token_bytes(16))
        self._db.execute(
            "INSERT INTO pair_codes (code_hash, purpose, module_id, expected_fp, expires_at, fails) VALUES (?,?,?,?,?,0)",
            (stored, purpose.value, module_id, expected_fp, self._clock() + ttl),
        )
        self._db.commit()
        return plaintext

    @staticmethod
    def _generate() -> str:
        anchor = "".join(secrets.choice(_HANZI) for _ in range(HANZI_PER_CODE))
        body = "".join(secrets.choice(BASE32) for _ in range(CODE_LEN - HANZI_PER_CODE))
        return f"{anchor}-{body}"

    # ------------------------------------------------------------------ consume
    def consume(
        self,
        code: str,
        purpose: Purpose | str,
        *,
        ip: str | None = None,
        fingerprint: str | None = None,
    ) -> PairCode:
        """Consume a code for `purpose`.

        `fingerprint` is the key that is *actually* claiming the module (known
        only after the handshake, e.g. from `remote_static`). Constraint 5
        compares it with `existing_fingerprint(module_id)`: a code must not
        silently displace an already-paired module, and if the module IS paired
        the caller has to prove the new key matches — an absent fingerprint is
        refused rather than trusted.
        """
        purpose = Purpose(purpose)
        with self._lock:
            return self._consume_locked(code, purpose, ip, fingerprint)

    def _consume_locked(self, code: str, purpose: Purpose, ip: str | None, fingerprint: str | None) -> PairCode:
        self._check_ip(ip)
        normalized = normalize(code)
        if not code_shape_ok(normalized):
            # Never echo the submitted code.
            self._record_ip(ip)
            raise VadeError("CODE_INVALID", "pair code is not valid")

        row = self._db.execute(
            "SELECT code_hash, purpose, module_id, expected_fp, expires_at, fails FROM pair_codes"
        ).fetchall()
        match = next((r for r in row if _verify(normalized, r[0])), None)
        if match is None:
            self._record_ip(ip)
            raise VadeError("CODE_INVALID", "pair code is not valid")

        code_hash, stored_purpose, module_id, expected_fp, expires_at, _fails = match
        if stored_purpose != purpose.value:
            # The code exists but was issued for the other flow. Count it and
            # answer exactly like an unknown code: revealing "this is a browser
            # code" would hand an attacker a type oracle.
            self._note_failure_row(code_hash)
            self._record_ip(ip)
            raise VadeError("CODE_INVALID", "pair code is not valid")

        if self._clock() > expires_at:
            self._db.execute("DELETE FROM pair_codes WHERE code_hash=?", (code_hash,))
            self._db.commit()
            self._record_ip(ip)
            raise VadeError("CODE_EXPIRED", "pair code expired")

        if module_id and self._existing_fingerprint is not None:
            current = self._existing_fingerprint(module_id)
            if current:
                candidate = fingerprint or expected_fp
                if candidate is None:
                    raise VadeError(
                        "PERMISSION_DENIED",
                        f"{module_id} is already paired; the claiming key must be supplied before the code is consumed",
                    )
                if current != candidate:
                    raise VadeError("PERMISSION_DENIED", "module is already paired to another key; revoke it first")

        # Consume = DELETE, judged by rowcount. Idempotent under concurrency.
        cursor = self._db.execute("DELETE FROM pair_codes WHERE code_hash=? AND purpose=?", (code_hash, purpose.value))
        self._db.commit()
        if cursor.rowcount != 1:
            self._record_ip(ip)
            raise VadeError("CODE_INVALID", "pair code is not valid")
        # A success is NOT an attempt: charging it against the per-IP budget
        # locks out an admin who pairs several modules in a row.
        return PairCode(purpose=purpose, module_id=module_id, expected_fp=expected_fp, expires_at=expires_at)

    def revoke(self, code: str) -> bool:
        with self._lock:
            return self._revoke(code)

    def _revoke(self, code: str) -> bool:
        normalized = normalize(code)
        rows = self._db.execute("SELECT code_hash FROM pair_codes").fetchall()
        for (stored,) in rows:
            if _verify(normalized, stored):
                cursor = self._db.execute("DELETE FROM pair_codes WHERE code_hash=?", (stored,))
                self._db.commit()
                return cursor.rowcount == 1
        return False

    def note_failure(self, code: str) -> None:
        """Increment the per-code failure counter; 5 strikes destroys the code."""
        with self._lock:
            normalized = normalize(code)
            for (stored,) in self._db.execute("SELECT code_hash FROM pair_codes").fetchall():
                if _verify(normalized, stored):
                    self._note_failure_row(stored)
                    return

    def _note_failure_row(self, code_hash: str) -> None:
        """Count one failed attempt against a row we already matched."""
        row = self._db.execute("SELECT fails FROM pair_codes WHERE code_hash=?", (code_hash,)).fetchone()
        if row is None:
            return
        fails = int(row[0]) + 1
        if fails >= MAX_FAILS_PER_CODE:
            self._db.execute("DELETE FROM pair_codes WHERE code_hash=?", (code_hash,))
        else:
            self._db.execute("UPDATE pair_codes SET fails=? WHERE code_hash=?", (fails, code_hash))
        self._db.commit()

    # ------------------------------------------------------------------ rate limit
    def _check_ip(self, ip: str | None) -> None:
        if ip is None:
            return
        cutoff = self._clock() - IP_WINDOW_S
        self._db.execute("DELETE FROM pair_attempts WHERE ts < ?", (cutoff,))
        count = self._db.execute("SELECT COUNT(*) FROM pair_attempts WHERE ip=? AND ts>=?", (ip, cutoff)).fetchone()[0]
        if count >= IP_MAX_ATTEMPTS:
            raise VadeError("ADMIN_LOCKED", "too many pair-code attempts from this address", data={"retry_after": IP_WINDOW_S})

    def _record_ip(self, ip: str | None) -> None:
        if ip is None:
            return
        self._db.execute("INSERT INTO pair_attempts (ip, ts) VALUES (?,?)", (ip, self._clock()))
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
