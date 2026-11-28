"""Pair codes — the ONLY implementation (C-15 consumer).

Shape: 2 hanzi + 6 base32, displayed `青松-A7K2M9`.
  alphabet: ABCDEFGHJKLMNPQRSTUVWXYZ23456789  (no I/O/0/1)
  entropy : 256^2 * 32^6 ~= 2^46  (the table is 256 unique chars)

Five constraints that make this safe (21-C2 T1):
  1. `purpose ∈ {module, browser}` and consumption must match.
  2. Failure counter per code (5 -> code is destroyed); per-IP 5 / 5 min on a
     budget SHARED with the admin setup-token endpoints, plus a global lock
     (20 failures / 5 min -> every source refused for 15 min). A missing
     source address is refused, never exempted.
  3. Entropy as above (the anchor hanzi exist so a human reads it right).
  4. Consume = DELETE ... WHERE code_hash AND purpose, judged by rowcount.
     "mark used" can be consumed twice under concurrency; rowcount cannot.
  5. If module_id is supplied and that module already has a DIFFERENT
     fingerprint, refuse: otherwise one code silently displaces a trusted node.

Lookup is O(1), not O(N) scrypt (K6). A plain `scrypt(normalize(code))` hash is
salted, so the only way to find a row was to verify the submitted code against
*every* row — one 16 MiB KDF per stored code per attempt, which is a CPU/memory
amplifier for an unauthenticated endpoint. `create()` therefore also stores
`code_index = HMAC-SHA256(pepper, normalize(code))` under a UNIQUE index, and
`consume` reaches the row directly. scrypt still runs exactly once per attempt
as the second check: the index is a lookup key, never the verifier (a pepper
leak must not turn the table into a password database).

The pepper lives next to the database in `data/pairing.pepper`, mode 0600, and
is cached in-process. Losing it does not weaken anything kept: codes live at
most `TTL_DEFAULT` seconds, and a restart that finds no pepper file mints a new
one, which makes every pre-restart row unmatchable (they are purged on create/
consume as expired anyway).

Storage note (honest deviation): the spec says `argon2id(normalize(code))`.
`argon2-cffi` is not installed in this environment, so this uses
`hashlib.scrypt(n=2**14, r=8, p=1)` with a per-code salt — still memory-hard
and salted, still never storing plaintext. Switching to argon2id later only
changes `_hash()`; the stored record carries `algo` so old rows stay readable.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from ..errors import VadeError

__all__ = [
    "normalize",
    "PairingStore",
    "PairCode",
    "Purpose",
    "BASE32",
    "code_shape_ok",
    "PEPPER_FILENAME",
    "MAX_ACTIVE_CODES",
]

# Windows fds default to text mode and would translate every LF byte into CRLF,
# corrupting the 32 random pepper bytes (random bytes contain LFs ~12% of the
# time; the translated file then fails the strict 32-byte length check).
# POSIX has no O_BINARY; OR-ing 0 is a no-op there.
_O_BINARY = getattr(os, "O_BINARY", 0)


def _write_all(fd: int, data: bytes) -> None:
    """`os.write()` until every byte is out; a short write must not truncate."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - regular files never do this
            raise OSError(f"write on fd {fd} made no progress, {len(view)} byte(s) left")
        view = view[written:]

BASE32 = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SEPARATORS = {"-", " ", "\u3000", "\t", "\u00b7", "\u30fb"}
CODE_LEN = 8
HANZI_PER_CODE = 2
TTL_DEFAULT = 180
MAX_FAILS_PER_CODE = 5

#: Per-source-IP budget, shared by every endpoint that spends a guess on a
#: secret (`/ui/session` and the admin setup token endpoints): 5 per 5 min.
IP_WINDOW_S = 300
IP_MAX_ATTEMPTS = 5
#: Distributed guessing: 20 failures in the same window lock every source for
#: 15 min. Without this, N addresses each get their own 5-attempt budget.
GLOBAL_WINDOW_S = 300
GLOBAL_MAX_FAILURES = 20
GLOBAL_LOCK_S = 900

#: Outstanding (unexpired) codes. `create()` is admin-only, so this is a
#: housekeeping bound, not the anti-guessing control; it stops an abandoned
#: invite loop or a stuck script from growing the table without limit.
MAX_ACTIVE_CODES = 64

#: 32 bytes of key material, created 0600 on first use.
PEPPER_FILENAME = "pairing.pepper"
PEPPER_BYTES = 32

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


def _load_pepper(path: Path) -> bytes:
    """Read `data/pairing.pepper`, creating it 0600 if it is not there yet.

    O_EXCL, not "write a temp then replace": two processes starting at once
    must agree on ONE pepper. The loser of the race re-reads the winner's file;
    generating over it would silently split the table into two key spaces.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        except FileExistsError:
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise VadeError("INTERNAL", f"pairing pepper {path.name} is unreadable: {exc}") from None
        else:
            try:
                _write_all(fd, secrets.token_bytes(PEPPER_BYTES))
            finally:
                os.close(fd)
            try:
                os.chmod(path, 0o600)
            except OSError:  # pragma: no cover - best effort
                pass
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise VadeError("INTERNAL", f"pairing pepper {path.name} is unreadable: {exc}") from None
    except OSError as exc:  # pragma: no cover - permissions/disk
        raise VadeError("INTERNAL", f"pairing pepper {path.name} is unreadable: {exc}") from None
    if len(raw) != PEPPER_BYTES:
        # Fail closed: a truncated pepper would make the index unstable and
        # every lookup a miss, which looks exactly like "wrong code" to users.
        raise VadeError("INTERNAL", f"pairing pepper {path.name} must be {PEPPER_BYTES} bytes")
    try:
        os.chmod(path, 0o600)  # repair a widened mode on every start
    except OSError:  # pragma: no cover
        pass
    return raw


class PairingStore:
    def __init__(
        self,
        path: Path | str,
        *,
        existing_fingerprint: Callable[[str, str | None], str | None] | None = None,
        clock: Callable[[], float] = time.time,
        pepper_path: Path | str | None = None,
        max_active_codes: int = MAX_ACTIVE_CODES,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._existing_fingerprint = existing_fingerprint
        self._max_active_codes = int(max_active_codes)
        #: Read once per process: the KDF-free index must be stable across a
        #: restart, and re-reading a 32-byte file per attempt buys nothing.
        self._pepper = _load_pepper(Path(pepper_path) if pepper_path is not None else self._default_pepper_path())
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        # One connection, two threads in production (uvicorn's loop calls
        # /ui/session while admin code runs elsewhere). Without a lock the
        # implicit transactions interleave and committed attempt rows go
        # missing, which silently disables the 5/5min budget.
        self._lock = threading.RLock()
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS pair_codes (
                   code_hash TEXT PRIMARY KEY,
                   code_index TEXT,
                   purpose    TEXT NOT NULL,
                   module_id  TEXT,
                   expected_fp TEXT,
                   expires_at REAL NOT NULL,
                   fails      INTEGER NOT NULL DEFAULT 0
               )"""
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(pair_codes)")}
        if "code_index" not in columns:
            self._db.execute("ALTER TABLE pair_codes ADD COLUMN code_index TEXT")
            # A row written before the index existed cannot be found any more
            # (the pepper did not exist then). They are short-lived invites
            # from a previous process; deleting them is the only honest move.
            self._db.execute("DELETE FROM pair_codes")
        # UNIQUE: one plaintext can only ever own one row. Multiple NULLs are
        # allowed by SQLite, so the migration above cannot collide.
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pair_codes_index ON pair_codes(code_index)")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS pair_attempts (
                   ip TEXT NOT NULL,
                   ts REAL NOT NULL,
                   scope TEXT NOT NULL DEFAULT 'pair'
               )"""
        )
        attempt_columns = {row[1] for row in self._db.execute("PRAGMA table_info(pair_attempts)")}
        if "scope" not in attempt_columns:
            self._db.execute("ALTER TABLE pair_attempts ADD COLUMN scope TEXT NOT NULL DEFAULT 'pair'")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_attempts ON pair_attempts(ip, ts)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_attempts_ts ON pair_attempts(ts)")
        self._db.commit()

    def _default_pepper_path(self) -> Path:
        # Same directory as `pairing.sqlite3` (= `data/`): one backup unit,
        # one mode-0700 parent, no second path to configure.
        return self._path.parent / PEPPER_FILENAME

    def _index(self, normalized: str) -> str:
        """HMAC-SHA256(pepper, normalized code) — the O(1) lookup key."""
        return hmac.new(self._pepper, normalized.encode("utf-8"), hashlib.sha256).hexdigest()

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
        now = self._clock()
        self._purge_expired(now)
        active = int(self._db.execute("SELECT COUNT(*) FROM pair_codes").fetchone()[0])
        if active >= self._max_active_codes:
            raise VadeError(
                "BUDGET_EXCEEDED",
                f"{active} pair codes are outstanding (limit {self._max_active_codes}); "
                "wait for one to expire or revoke an invite first",
            )
        plaintext = self._generate()
        normalized = normalize(plaintext)
        stored = _hash(normalized, secrets.token_bytes(16))
        self._db.execute(
            "INSERT INTO pair_codes (code_hash, code_index, purpose, module_id, expected_fp, expires_at, fails) "
            "VALUES (?,?,?,?,?,?,0)",
            (stored, self._index(normalized), purpose.value, module_id, expected_fp, now + ttl),
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

        match = self._lookup(normalized)
        if match is None:
            self._purge_expired(self._clock())
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

        now = self._clock()
        if now > expires_at:
            self._db.execute("DELETE FROM pair_codes WHERE code_hash=?", (code_hash,))
            self._db.commit()
            self._purge_expired(now)
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
        self._purge_expired(now)
        # A success is NOT an attempt: charging it against the per-IP budget
        # locks out an admin who pairs several modules in a row.
        return PairCode(purpose=purpose, module_id=module_id, expected_fp=expected_fp, expires_at=expires_at)

    def revoke(self, code: str) -> bool:
        with self._lock:
            return self._revoke(code)

    def _revoke(self, code: str) -> bool:
        normalized = normalize(code)
        if not code_shape_ok(normalized):
            return False
        row = self._lookup(normalized, verify=False)
        if row is None or not _verify(normalized, row[0]):
            return False
        cursor = self._db.execute("DELETE FROM pair_codes WHERE code_hash=?", (row[0],))
        self._db.commit()
        return cursor.rowcount == 1

    def note_failure(self, code: str) -> None:
        """Increment the per-code failure counter; 5 strikes destroys the code."""
        with self._lock:
            normalized = normalize(code)
            if not code_shape_ok(normalized):
                return
            row = self._lookup(normalized, verify=False)
            if row is not None and _verify(normalized, row[0]):
                self._note_failure_row(row[0])

    def _lookup(self, normalized: str, *, verify: bool = True) -> tuple | None:
        """One indexed read; scrypt only confirms the row we already found."""
        row = self._db.execute(
            "SELECT code_hash, purpose, module_id, expected_fp, expires_at, fails "
            "FROM pair_codes WHERE code_index=?",
            (self._index(normalized),),
        ).fetchone()
        if row is None:
            return None
        if verify and not _verify(normalized, row[0]):
            # Index hit but hash mismatch: a torn write, a hand-edited row or a
            # pepper/table mismatch. Treat as unknown; never as a valid code.
            return None
        return row

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

    def _purge_expired(self, now: float) -> int:
        """Drop rows whose TTL has passed. Called from create/consume."""
        cursor = self._db.execute("DELETE FROM pair_codes WHERE expires_at < ?", (now,))
        if cursor.rowcount:
            self._db.commit()
        return int(cursor.rowcount)

    # ------------------------------------------------------------------ rate limit
    def _global_locked_until(self, now: float) -> float:
        """15-minute global lock once 20 failures land in the 5-minute window.

        Derived from the table, not a process flag: a restart must not hand an
        attacker a fresh global budget, and the lock expires exactly
        `GLOBAL_LOCK_S` after the last counted failure.
        """
        row = self._db.execute(
            "SELECT COUNT(*), MAX(ts) FROM pair_attempts WHERE ts >= ?",
            (now - GLOBAL_WINDOW_S,),
        ).fetchone()
        count, newest = int(row[0] or 0), row[1]
        if count >= GLOBAL_MAX_FAILURES and newest is not None:
            return float(newest) + GLOBAL_LOCK_S
        return 0.0

    def _check_ip(self, ip: str | None) -> None:
        """Refuse before any work when the source address is unknown.

        `ip is None` used to mean "no rate limit": an ASGI scope without a
        client address (or a stripped `X-Forwarded-For` under trust) turned the
        budget off. Fail closed instead — a caller that cannot say where it
        came from has no business guessing a pair code.
        """
        if ip is None or not str(ip).strip():
            raise VadeError("PERMISSION_DENIED", "pair-code attempts require a client address")
        now = self._clock()
        locked_until = self._global_locked_until(now)
        if locked_until > now:
            raise VadeError(
                "ADMIN_LOCKED",
                "too many pair-code attempts from this host; try again later",
                data={"retry_after": max(1, int(locked_until - now))},
            )
        cutoff = now - IP_WINDOW_S
        count = int(
            self._db.execute(
                "SELECT COUNT(*) FROM pair_attempts WHERE ip=? AND ts>=?", (str(ip), cutoff)
            ).fetchone()[0]
        )
        if count >= IP_MAX_ATTEMPTS:
            raise VadeError(
                "ADMIN_LOCKED",
                "too many pair-code attempts from this address",
                data={"retry_after": IP_WINDOW_S},
            )

    def _record_ip(self, ip: str | None, scope: str = "pair") -> None:
        if ip is None or not str(ip).strip():
            # `_check_ip` fails closed before this point; never write an
            # attempt that cannot be attributed (it would also never be read).
            return
        now = self._clock()
        self._db.execute("INSERT INTO pair_attempts (ip, ts, scope) VALUES (?,?,?)", (str(ip), now, scope))
        # Attempts older than the longest window/lock serve no decision.
        self._db.execute("DELETE FROM pair_attempts WHERE ts < ?", (now - max(IP_WINDOW_S, GLOBAL_LOCK_S),))
        self._db.commit()

    # ------------------------------------------------------- shared setup budget
    def begin_setup_attempt(self, ip: str | None) -> None:
        """Charge one admin setup-token guess against the SAME budget.

        `/ui/session` and the setup endpoints both spend a guess on a secret;
        giving each its own counter would double every attacker's budget. The
        admin plane holds this store (`AdminContext.pairing`), so its
        `POST /admin/setup` and `POST /admin/setup/unlock` handlers call this
        before validating the token — one table, one 5/5min per-IP cap and one
        20-failure/15min global lock for all of them.
        """
        with self._lock:
            self._check_ip(ip)
            self._record_ip(ip, scope="setup")

    def close(self) -> None:
        with self._lock:
            self._db.close()
