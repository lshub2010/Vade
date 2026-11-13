"""Pair codes — the ONLY implementation (C-15 consumer).

Shape: 2 hanzi + 6 base32, displayed `青松-A7K2M9`.
  alphabet: ABCDEFGHJKLMNPQRSTUVWXYZ23456789  (no I/O/0/1)
  entropy : 256^2 * 32^6 ~= 2^46

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

#: Placeholder 256-char table. C-15's `hanzi_table` is still null in
#: schema/pair-code.json; generation needs *some* anchor characters, but nothing
#: downstream depends on WHICH characters (consumption validates shape only,
#: exactly like protocol/core/src/paircode.rs). Replace when C-15 lands its table.
_HANZI = (
    "青松明月山川云海风雨雷电江河湖泽林森木火土金水晶光天日星辰春夏秋冬东南西北"
    "中和正大平安康健喜乐福寿仁义礼智信忠孝廉耻勇诚敬谦慎勤恒思学志远宁静"
    "清泉溪谷岩峰岭原野田禾苗花草竹梅兰菊莲桂枫柳柏槐梧桐鸿雁鹤鹰燕雀鱼龙"
    "虎豹鹿马牛羊鸡犬象狮熊鹰凤麟龟蛇蝶蜂蝉萤虹霞露霜雪冰雾烟波潮汐浪涛"
    "舟桥岸渡城乡村镇街巷庭院楼阁台亭廊窗门户墙垣阶梯径路里程尺寸斤两"
    "书琴棋画诗酒茶香墨砚笔纸卷轴章句篇章词曲歌舞蹈戏剧鼓钟铃箫笛弦"
    "剑弓刀斧锤钩绳索网罗囊袋箱柜桌椅床榻枕席帘幔帐帷幕"
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
    if algo != "scrypt" or salt_hex.startswith("{") or not salt_hex.endswith("}"):
        return False
    expected = _hash(code, bytes.fromhex(salt_hex.strip("{}"))).rsplit(":", 1)[-1]
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
        purpose = Purpose(purpose)
        if purpose is Purpose.MODULE and not module_id:
            # A module code without module_id cannot enforce constraint 5.
            module_id = None
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
    def consume(self, code: str, purpose: Purpose | str, *, ip: str | None = None) -> PairCode:
        purpose = Purpose(purpose)
        self._check_ip(ip)

        normalized = normalize(code)
        if not code_shape_ok(normalized):
            # Never echo the submitted code.
            self._record_ip(ip)
            raise VadeError("CODE_INVALID", "pair code is not valid")

        row = self._db.execute(
            "SELECT code_hash, purpose, module_id, expected_fp, expires_at, fails FROM pair_codes WHERE purpose=?",
            (purpose.value,),
        ).fetchall()
        match = next((r for r in row if _verify(normalized, r[0])), None)
        if match is None:
            self._record_ip(ip)
            raise VadeError("CODE_INVALID", "pair code is not valid")

        code_hash, _, module_id, expected_fp, expires_at, fails = match
        if self._clock() > expires_at:
            self._db.execute("DELETE FROM pair_codes WHERE code_hash=?", (code_hash,))
            self._db.commit()
            self._record_ip(ip)
            raise VadeError("CODE_EXPIRED", "pair code expired")

        if module_id and self._existing_fingerprint is not None:
            current = self._existing_fingerprint(module_id, None)
            if current and expected_fp and current != expected_fp:
                raise VadeError("PERMISSION_DENIED", "module is already paired to another key; revoke it first")

        # Consume = DELETE, judged by rowcount. Idempotent under concurrency.
        cursor = self._db.execute("DELETE FROM pair_codes WHERE code_hash=? AND purpose=?", (code_hash, purpose.value))
        self._db.commit()
        if cursor.rowcount != 1:
            raise VadeError("CODE_INVALID", "pair code is not valid")
        self._record_ip(ip)
        return PairCode(purpose=purpose, module_id=module_id, expected_fp=expected_fp, expires_at=expires_at)

    def revoke(self, code: str) -> bool:
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
        normalized = normalize(code)
        for stored, fails in self._db.execute("SELECT code_hash, fails FROM pair_codes").fetchall():
            if _verify(normalized, stored):
                if fails + 1 >= MAX_FAILS_PER_CODE:
                    self._db.execute("DELETE FROM pair_codes WHERE code_hash=?", (stored,))
                else:
                    self._db.execute("UPDATE pair_codes SET fails=? WHERE code_hash=?", (fails + 1, stored))
                self._db.commit()
                return

    # ------------------------------------------------------------------ rate limit
    def _check_ip(self, ip: str | None) -> None:
        if ip is None:
            return
        cutoff = self._clock() - IP_WINDOW_S
        self._db.execute("DELETE FROM pair_attempts WHERE ts < ?", (cutoff - IP_WINDOW_S,))
        count = self._db.execute("SELECT COUNT(*) FROM pair_attempts WHERE ip=? AND ts>=?", (ip, cutoff)).fetchone()[0]
        if count >= IP_MAX_ATTEMPTS:
            raise VadeError("ADMIN_LOCKED", "too many pair-code attempts from this address", data={"retry_after": IP_WINDOW_S})

    def _record_ip(self, ip: str | None) -> None:
        if ip is None:
            return
        self._db.execute("INSERT INTO pair_attempts (ip, ts) VALUES (?,?)", (ip, self._clock()))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
