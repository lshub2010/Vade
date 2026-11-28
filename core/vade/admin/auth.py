"""Passwords, challenges, device signatures, sessions, origin/Host gates.

The non-obvious rules, all from 31-C5a §T4:

* One error for every login failure (`AUTH_FAILED`); a distinguishable
  "wrong password" vs "unknown device" is an oracle.
* P-256 signatures with `s > n/2` are rejected even though they verify: the
  same challenge would otherwise have two valid signatures and one-time
  semantics would be gone.
* A `browser` session is read-only by construction, not by UI politeness.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils as asym_utils

from ..errors import VadeError
from ..logging import get_logger

__all__ = [
    "hash_password", "verify_password", "password_needs_rehash", "validate_password",
    "verify_password_async", "run_argon2", "ARGON2_SLOTS",
    "PasswordPolicyError", "ChallengeStore", "SessionStore", "Session", "verify_signature",
    "normalize_pubkey", "is_loopback", "host_allowed", "origin_allowed",
    "ADMIN_ORIGIN_SCHEME", "ARGON2_TIME_COST", "ARGON2_MEMORY_KIB", "SWEEP_EVERY",
]

ADMIN_ORIGIN_SCHEME = "https"

_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

# Windows fds default to text mode and would translate every LF byte into CRLF,
# corrupting binary payloads. POSIX has no O_BINARY; OR-ing 0 is a no-op there.
_O_BINARY = getattr(os, "O_BINARY", 0)


def _write_all(fd: int, data: bytes) -> None:
    """`os.write()` until every byte is out; a short write must not truncate."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - regular files never do this
            raise OSError(f"write on fd {fd} made no progress, {len(view)} byte(s) left")
        view = view[written:]


# --------------------------------------------------------------------- password
class PasswordPolicyError(ValueError):
    pass


#: First ~40 of the usual top-10k list, plus shape checks. The point is to stop
#: `passw0rd12345`, not to impose symbol soup (31 §T4 ②).
_WEAK = {
    "password", "password1", "password123", "passw0rd", "passw0rd123",
    "123456789012", "1234567890123", "qwertyuiop12", "qwerty123456",
    "letmein12345", "iloveyou1234", "adminadmin12", "administrator",
    "welcome123456", "changeme1234", "abc123456789", "monkey123456",
    "dragon123456", "football1234", "baseball1234", "sunshine1234",
    "princess1234", "trustno12345", "starwars1234", "whatever1234",
    "111111111111", "000000000000", "aaaaaaaaaaaa", "qazwsxedcrfv",
}


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < 12:
        raise PasswordPolicyError("password must be at least 12 characters")
    if len(password) > 256:
        raise PasswordPolicyError("password is too long")
    lowered = password.lower()
    if lowered in _WEAK or any(lowered == w + str(i) for i, w in enumerate(_WEAK)):
        raise PasswordPolicyError("password is in the common-password list")
    if len(set(password)) <= 3:
        raise PasswordPolicyError("password has too few distinct characters")


#: Argon2id parameters from 31-C5a §T4 ② / design.md: m=64 MiB, t=3, p=1.
#: 64 MiB is 2**16 KiB; `memory_cost` is in KiB, not bytes.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_KIB = 64 * 1024
ARGON2_PARALLELISM = 1
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16

_hasher = PasswordHasher(
    time_cost=ARGON2_TIME_COST, memory_cost=ARGON2_MEMORY_KIB,
    parallelism=ARGON2_PARALLELISM, hash_len=ARGON2_HASH_LEN,
    salt_len=ARGON2_SALT_LEN, type=Type.ID,
)


def hash_password(password: str) -> str:
    """Argon2id PHC string, `m=64 MiB, t=3, p=1` (31-C5a §T4 ②).

    Parameters travel inside the hash, so raising them later is a re-hash on the
    next successful login (`password_needs_rehash`), never a migration window.
    """
    return _hasher.hash(password)


def password_needs_rehash(stored: str | None) -> bool:
    """True for legacy `scrypt$…` material and for Argon2id below today's params."""
    if not stored:
        return False
    if not stored.startswith("$argon2"):
        return True
    try:
        return _hasher.check_needs_rehash(stored)
    except (InvalidHashError, ValueError):
        return True


def _verify_legacy_scrypt(password: str, stored: str) -> bool:
    """`scrypt$n$r$p$salt$digest` — the pre-Argon2id format, kept verify-only.

    Existing `admin.json` files must keep working; they are upgraded in place by
    `SetupService.rehash_password()` after the next successful login.
    """
    try:
        algo, n_s, r_s, p_s, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64 + "=" * (-len(salt_b64) % 4))
        expected = base64.b64decode(digest_b64 + "=" * (-len(digest_b64) % 4))
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n_s), r=int(r_s),
                                p=int(p_s), dklen=len(expected))
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


def verify_password(password: str, stored: str | None) -> bool:
    """Argon2id, with legacy scrypt accepted so old hashes are not orphaned."""
    if not stored:
        return False
    if stored.startswith("$argon2"):
        try:
            return _hasher.verify(stored, password)
        except (VerificationError, InvalidHashError, ValueError):
            return False
    return _verify_legacy_scrypt(password, stored)


# ------------------------------------------------------- argon2 off the loop
#: How many Argon2 calls may run at once. This is a *memory* ceiling, not
#: politeness: each digest holds 64 MiB, so an unbounded fan-out of four logins
#: is 256 MiB of transient RSS on a box that may have 5 GiB total. Two keeps a
#: login at ~2x its single-digest latency and the peak at 128 MiB.
ARGON2_SLOTS = 2

#: `asyncio.Semaphore` binds itself to the first loop that has to *wait* on it,
#: so one module-level instance raises `bound to a different event loop` under
#: the second loop (pytest gives each test a fresh one). Keyed by loop, created
#: on demand, weakly held so a finished loop is not kept alive.
_SLOTS: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _argon2_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _SLOTS.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(ARGON2_SLOTS)
        _SLOTS[loop] = semaphore
    return semaphore


async def run_argon2(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a CPU-bound Argon2 call in a worker thread, `ARGON2_SLOTS` at a time.

    m=64 MiB / t=3 is tens of milliseconds of pure CPU (hundreds on a small
    box). On the event loop that stalls every websocket, every SSE frame and
    every other admin request for the duration — including the requests of the
    same login burst. `to_thread` moves the digest off the loop; the semaphore
    keeps the memory ceiling. Every Argon2 call site goes through here.
    """
    async with _argon2_slots():
        return await asyncio.to_thread(fn, *args, **kwargs)


async def verify_password_async(password: str, stored: str | None) -> bool:
    """`verify_password` without blocking the event loop (see `run_argon2`)."""
    return await run_argon2(verify_password, password, stored)


# -------------------------------------------------------------------- challenges
@dataclass(slots=True)
class _Challenge:
    value: bytes
    expires_at: float
    ip_net: str
    devices_version: int
    used: bool = False


class ChallengeStore:
    def __init__(self, *, ttl_s: float = 60.0, wall: Callable[[], float] = time.time) -> None:
        self.ttl_s = ttl_s
        self._wall = wall
        self._items: dict[str, _Challenge] = {}

    def issue(self, ip: str | None, devices_version: int) -> dict[str, Any]:
        challenge = secrets.token_bytes(32)
        challenge_id = secrets.token_urlsafe(18)
        now = self._wall()
        self._items[challenge_id] = _Challenge(
            value=challenge, expires_at=now + self.ttl_s,
            ip_net=self._net(ip), devices_version=devices_version,
        )
        self._prune(now)
        return {
            "challenge_id": challenge_id,
            "challenge": base64.b64encode(challenge).decode("ascii"),
            "expires_at": int(now + self.ttl_s),
        }

    def consume(self, challenge_id: str, ip: str | None, devices_version: int) -> bytes:
        """One-time: the entry is burned even when the bind checks fail."""
        item = self._items.get(challenge_id)
        if item is None:
            raise VadeError("AUTH_FAILED", "authentication failed")
        if item.used or item.expires_at <= self._wall():
            self._items.pop(challenge_id, None)
            raise VadeError("AUTH_FAILED", "authentication failed")
        item.used = True
        self._items.pop(challenge_id, None)
        if item.ip_net != self._net(ip) or item.devices_version != devices_version:
            # Same /24 is deliberate: a phone switching from Wi-Fi to LTE must
            # not lose the challenge, a different subnet must not reuse it.
            raise VadeError("AUTH_FAILED", "authentication failed")
        return item.value

    @staticmethod
    def _net(ip: str | None) -> str:
        if not ip:
            return ""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return ""
        if addr.version == 4:
            return str(ipaddress.ip_network(f"{ip}/24", strict=False))
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._items.items() if v.expires_at <= now]:
            self._items.pop(key, None)


# ---------------------------------------------------------------------- sessions
@dataclass(slots=True)
class Session:
    token: str
    mode: str                     # "device" | "browser"
    device_id: str | None
    created_at: float
    expires_at: float
    password_version: int
    ip: str | None = None
    csrf: str = ""
    #: sha256 of the refresh token minted together with this access row. Only
    #: the digest is held (and persisted), so a memory dump or a backup of
    #: `sessions.json` cannot be replayed as a refresh credential.
    refresh_hash: str = ""
    #: The refresh row's jti; put on the blacklist by `logout()`.
    refresh_jti: str = ""
    refresh_expires_at: float = 0.0


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: How many `guard()` calls pass between session-table sweeps. Mirrors
#: `transport.server`'s `UiSessions.prune`: without it, an access row is only
#: forgotten when the exact token is presented (`resolve`) and a refresh row
#: only when it is rotated (`refresh`) — so every issued-and-abandoned token,
#: and every rotated refresh chain, stays in the dict for the life of the
#: process (refresh TTLs are 30 days, i.e. effectively forever).
SWEEP_EVERY = 64

#: `data/admin/sessions.json` format version.
SESSIONS_FILE_VERSION = 1


class SessionStore:
    """Access tokens in memory, refresh tokens on disk.

    Access rows are 15-minute bearers: losing them to a restart costs one
    `/admin/api/token/refresh`, which the client is designed to do. Refresh rows
    are 30-day credentials stored in a device keychain; the whole point of that
    design is that `core` may restart and the App stays signed in, so they are
    written to `sessions.json` (0600, tmp+`os.replace`).

    What is persisted, per design §7.12.6: **sha256(refresh)** only — never the
    token — plus `jti`, `device_id`, `mode`, `password_version` and
    `expires_at`. The password version follows `admin.json`'s
    `password_changed_at` (see `set_password_version_source`), so a password
    change made by the running core *or* by `vade admin reset-password` while it
    was stopped invalidates every persisted refresh.
    """

    def __init__(
        self,
        *,
        access_ttl_s: float = 900.0,
        refresh_ttl_s: float = 30 * 86_400.0,
        wall: Callable[[], float] = time.time,
        sweep_every: int = SWEEP_EVERY,
        persist_path: Path | str | None = None,
    ) -> None:
        self.access_ttl_s = access_ttl_s
        self.refresh_ttl_s = refresh_ttl_s
        self._wall = wall
        self.sweep_every = int(sweep_every)
        self._guard_calls = 0
        self._sessions: dict[str, Session] = {}
        # Keyed by sha256(refresh token), never by the token itself.
        self._refresh: dict[str, dict[str, Any]] = {}
        #: jti → expiry. A logged-out refresh row is deleted outright; the
        #: blacklist is the second lock, and it is persisted so a restart does
        #: not resurrect a jti whose row is still in flight/rotated.
        self._blacklist: dict[str, float] = {}
        self._password_version = 0
        self._password_version_source: Callable[[], int] | None = None
        self._device_check: Callable[[str], bool] | None = None
        self.persist_path = Path(persist_path) if persist_path is not None else None
        self._load_persisted()

    def set_device_checker(self, check: Callable[[str], bool]) -> None:
        self._device_check = check

    def set_password_version_source(self, source: Callable[[], int] | None) -> None:
        """Tie the session version to `admin.json`, not to process memory.

        `SetupService.password_version()` derives it from
        `password_changed_at`, so both `POST /admin/api/password` and an offline
        `vade admin reset-password` invalidate persisted refresh rows on the
        next read — without this, restarting after a CLI password reset replayed
        the old version (0) and every 30-day refresh still worked (review
        P2-20).
        """
        self._password_version_source = source

    @property
    def password_version(self) -> int:
        source = self._password_version_source
        if source is not None:
            try:
                return int(source())
            except Exception:  # noqa: BLE001 - a broken source must not 500 a request
                pass
        return self._password_version

    # ------------------------------------------------------------- persistence
    def _load_persisted(self) -> None:
        path = self.persist_path
        if path is None:
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(doc, dict):
            return
        try:
            self._password_version = int(doc.get("password_version", 0) or 0)
        except (TypeError, ValueError):
            self._password_version = 0
        now = self._wall()
        for row in doc.get("refresh") or []:
            if not isinstance(row, dict):
                continue
            try:
                digest = str(row["token_hash"])
                jti = str(row["jti"])
                expires_at = float(row["expires_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if not digest or not jti or expires_at <= now:
                continue
            self._refresh[digest] = {
                "device_id": row.get("device_id"),
                "mode": str(row.get("mode") or "device"),
                "expires_at": expires_at,
                "password_version": int(row.get("password_version") or 0),
                "jti": jti,
            }
        for row in doc.get("blacklist") or []:
            if not isinstance(row, dict):
                continue
            try:
                jti = str(row["jti"])
                expires_at = float(row["expires_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if jti and expires_at > now:
                self._blacklist[jti] = expires_at

    def _persist(self) -> None:
        path = self.persist_path
        if path is None:
            return
        doc = {
            "version": SESSIONS_FILE_VERSION,
            "password_version": int(self.password_version),
            "refresh": [
                {
                    "token_hash": digest, "jti": entry["jti"],
                    "device_id": entry.get("device_id"), "mode": entry.get("mode"),
                    "password_version": int(entry.get("password_version") or 0),
                    "expires_at": float(entry["expires_at"]),
                }
                for digest, entry in sorted(self._refresh.items())
            ],
            "blacklist": [
                {"jti": jti, "expires_at": expires_at}
                for jti, expires_at in sorted(self._blacklist.items())
            ],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o600)
            try:
                _write_all(fd, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            # A login that already proved the password must not turn into a 500
            # because the disk is full; the access token it just minted is
            # still valid, only the refresh is lost.
            get_logger("vade.admin").error(
                "sessions.json write failed: %s", exc,
                extra={"code": None, "msg": "session_persist_failed"},
            )

    # ---------------------------------------------------------------- mutation
    def issue(self, *, mode: str, device_id: str | None = None, ip: str | None = None) -> dict[str, Any]:
        now = self._wall()
        token = "acc_" + secrets.token_urlsafe(32)
        refresh = "ref_" + secrets.token_urlsafe(32)
        jti = secrets.token_urlsafe(12)
        csrf = secrets.token_urlsafe(24)
        if device_id:
            # §7.12.6: one active refresh per device, new login kicks the old.
            # Dropping the old *access* rows too means a stolen bearer does not
            # outlive the login that replaced it by 15 minutes.
            self._kick_device(device_id)
        session = Session(
            token=token, mode=mode, device_id=device_id, created_at=now,
            expires_at=now + self.access_ttl_s, password_version=self.password_version,
            ip=ip, csrf=csrf,
            refresh_hash=_token_hash(refresh), refresh_jti=jti,
            refresh_expires_at=now + self.refresh_ttl_s,
        )
        self._sessions[token] = session
        self._refresh[session.refresh_hash] = {
            "device_id": device_id, "mode": mode, "expires_at": session.refresh_expires_at,
            "password_version": session.password_version, "jti": jti,
        }
        self._persist()
        return {
            "access_token": token, "refresh_token": refresh, "refresh_jti": jti,
            "expires_in": int(self.access_ttl_s), "mode": mode, "csrf_token": csrf,
        }

    def _kick_device(self, device_id: str) -> None:
        for digest in [d for d, e in self._refresh.items() if e.get("device_id") == device_id]:
            entry = self._refresh.pop(digest, None)
            if entry:
                self._blacklist_jti(entry)
        for token in [t for t, s in self._sessions.items() if s.device_id == device_id]:
            self._sessions.pop(token, None)

    def _blacklist_jti(self, entry: Mapping[str, Any]) -> None:
        jti = str(entry.get("jti") or "")
        if not jti:
            return
        expires_at = float(entry.get("expires_at") or self._wall() + self.refresh_ttl_s)
        if expires_at > self._wall():
            self._blacklist[jti] = expires_at

    def _is_blacklisted(self, jti: str) -> bool:
        expires_at = self._blacklist.get(jti)
        if expires_at is None:
            return False
        if expires_at <= self._wall():
            self._blacklist.pop(jti, None)
            return False
        return True

    # ----------------------------------------------------------------- queries
    def resolve(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= self._wall() or session.password_version != self.password_version:
            self._sessions.pop(token, None)
            return None
        if session.device_id and self._device_check is not None and not self._device_check(session.device_id):
            # "Revocation is immediate" cannot wait for the 15-minute expiry.
            self._sessions.pop(token, None)
            return None
        return session

    def refresh(self, refresh_token: str, *, device_id: str | None) -> dict[str, Any] | None:
        digest = _token_hash(refresh_token)
        entry = self._refresh.get(digest)
        if entry is None:
            return None
        if (entry["expires_at"] <= self._wall()
                or entry["password_version"] != self.password_version
                or self._is_blacklisted(str(entry.get("jti") or ""))):
            self._refresh.pop(digest, None)
            self._persist()
            return None
        if device_id and entry["device_id"] != device_id:
            return None
        if entry["device_id"] and self._device_check is not None and not self._device_check(entry["device_id"]):
            self._refresh.pop(digest, None)
            self._persist()
            return None
        self._refresh.pop(digest, None)
        return self.issue(mode=entry["mode"], device_id=entry["device_id"])

    def peek_refresh(self, refresh_token: str) -> dict[str, Any] | None:
        """Metadata needed to verify the refresh signature *before* rotation."""
        entry = self._refresh.get(_token_hash(refresh_token))
        if entry is None or entry["expires_at"] <= self._wall():
            return None
        if entry["password_version"] != self.password_version:
            return None
        if self._is_blacklisted(str(entry.get("jti") or "")):
            return None
        return dict(entry)

    def drop_refresh(self, refresh_token: str) -> None:
        digest = _token_hash(refresh_token)
        entry = self._refresh.pop(digest, None)
        if entry is not None:
            self._blacklist_jti(entry)
            self._persist()

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)

    def logout(self, session: Session, *, refresh_token: str | None = None) -> None:
        """§7.12.6: delete the refresh row and blacklist its jti.

        Before this existed, logout only popped the 15-minute access row; the
        30-day refresh stayed valid, so a copy taken before logout kept the
        device signed in (review P1-6).
        """
        if session.refresh_hash:
            entry = self._refresh.pop(session.refresh_hash, None)
            if entry is not None:
                self._blacklist_jti(entry)
        if refresh_token:
            self.drop_refresh(refresh_token)
        elif session.refresh_jti:
            self._blacklist[session.refresh_jti] = (
                session.refresh_expires_at or self._wall() + self.refresh_ttl_s
            )
        self._sessions.pop(session.token, None)
        self._persist()

    def revoke_device(self, device_id: str) -> int:
        dead = [t for t, s in self._sessions.items() if s.device_id == device_id]
        for token in dead:
            self._sessions.pop(token, None)
        changed = bool(dead)
        for digest in [d for d, e in self._refresh.items() if e["device_id"] == device_id]:
            entry = self._refresh.pop(digest, None)
            if entry:
                self._blacklist_jti(entry)
                changed = True
        if changed:
            self._persist()
        return len(dead)

    def revoke_all(self) -> int:
        count = len(self._sessions)
        for entry in self._refresh.values():
            self._blacklist_jti(entry)
        self._sessions.clear()
        self._refresh.clear()
        self._persist()
        return count

    def bump_password_version(self) -> None:
        """Invalidates every session by version, not by waiting out the TTL."""
        if self._password_version_source is None:
            # With a live source the version already moved (admin.json was
            # rewritten before this call); incrementing a shadow counter would
            # desynchronise the two.
            self._password_version += 1
        for entry in self._refresh.values():
            self._blacklist_jti(entry)
        self._sessions.clear()
        self._refresh.clear()
        self._persist()

    def active_count(self) -> int:
        return len(self._sessions)

    # ------------------------------------------------------------------ sweep
    def prune(self, now: float | None = None) -> dict[str, int]:
        """Drop every expired access and refresh row; return the counts.

        Same contract as `transport.server.UiSessions.prune`: it never touches a
        live row, so it is safe to call between requests with no lock. Rows
        minted under a previous password version are dead by definition
        (`bump_password_version` clears the table, but a version bump from
        another path must not leave orphans behind).
        """
        at = self._wall() if now is None else float(now)
        version = self.password_version
        dead_access = [
            token for token, session in self._sessions.items()
            if session.expires_at <= at or session.password_version != version
        ]
        for token in dead_access:
            self._sessions.pop(token, None)
        dead_refresh = [
            key for key, entry in self._refresh.items()
            if entry["expires_at"] <= at or entry["password_version"] != version
        ]
        for key in dead_refresh:
            self._refresh.pop(key, None)
        dead_blacklist = [jti for jti, expires_at in self._blacklist.items() if expires_at <= at]
        for jti in dead_blacklist:
            self._blacklist.pop(jti, None)
        if dead_refresh or dead_blacklist:
            self._persist()
        return {"access": len(dead_access), "refresh": len(dead_refresh)}

    def sweep_if_due(self) -> dict[str, int]:
        """`prune()` every `sweep_every` calls; `guard()` is the only caller.

        A request already pays for a dict lookup, so the counter rides along
        with it instead of a background task (this module owns no loop). `0`
        disables the sweep, which the tests use to pin the un-swept behaviour.
        """
        self._guard_calls += 1
        if self.sweep_every <= 0 or self._guard_calls % self.sweep_every:
            return {"access": 0, "refresh": 0}
        return self.prune()


# --------------------------------------------------------------------- signature
def verify_signature(alg: str, pubkey: bytes, message: bytes, signature: bytes) -> bool:
    alg = str(alg).lower()
    try:
        if alg == "ed25519":
            key = ed25519.Ed25519PublicKey.from_public_bytes(pubkey)
            key.verify(signature, message)
            return True
        if alg in {"p256", "es256", "ecdsa-p256"}:
            key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pubkey)
            der = signature
            if len(signature) == 64:  # raw r||s from a WebCrypto/Keychain signer
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                der = asym_utils.encode_dss_signature(r, s)
            else:
                r, s = asym_utils.decode_dss_signature(signature)
            if s > _P256_N // 2:
                # Malleable twin of a valid signature: refuse, or the
                # challenge is not really one-time (31 §T4 ③).
                return False
            key.verify(der, message, ec.ECDSA(hashes.SHA256()))
            return True
    except (InvalidSignature, ValueError, TypeError):
        return False
    return False


def normalize_pubkey(text: str, alg: str | None = None) -> tuple[str, bytes]:
    """Accept base64 raw, PEM or an OpenSSH one-liner; return (alg, raw bytes)."""
    raw = (text or "").strip()
    if not raw:
        raise VadeError("INVALID_ARGUMENT", "public key is empty")
    if raw.startswith("ssh-ed25519") or raw.startswith("-----BEGIN"):
        try:
            from cryptography.hazmat.primitives import serialization

            key = serialization.load_ssh_public_key(raw.encode()) if raw.startswith("ssh-") else serialization.load_pem_public_key(raw.encode())
        except Exception as exc:  # noqa: BLE001 - surfaced as INVALID_ARGUMENT
            raise VadeError("INVALID_ARGUMENT", f"cannot parse public key: {type(exc).__name__}") from None
        if isinstance(key, ed25519.Ed25519PublicKey):
            return "ed25519", key.public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
            )
        if isinstance(key, ec.EllipticCurvePublicKey):
            from cryptography.hazmat.primitives import serialization

            return "p256", key.public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            )
        raise VadeError("INVALID_ARGUMENT", "unsupported key type")
    try:
        blob = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise VadeError("INVALID_ARGUMENT", "public key is not valid base64") from None
    detected = alg
    if detected is None:
        detected = "p256" if len(blob) == 65 and blob[:1] == b"\x04" else "ed25519"
    detected = detected.lower()
    if detected == "ed25519" and len(blob) != 32:
        raise VadeError("INVALID_ARGUMENT", "ed25519 public key must be 32 raw bytes")
    if detected in {"p256", "es256"} and not (len(blob) == 65 and blob[:1] == b"\x04"):
        raise VadeError("INVALID_ARGUMENT", "p256 public key must be a 65-byte uncompressed point")
    return ("p256" if detected in {"p256", "es256"} else detected), blob


# ------------------------------------------------------------------- host/origin
def is_loopback(host: str | None, client: str | None) -> bool:
    if not client:
        return False
    try:
        client_ip = ipaddress.ip_address(client)
    except ValueError:
        return False
    if not client_ip.is_loopback:
        return False
    if not host:
        return False
    hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    hostname = hostname.strip("[]")
    if hostname in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def host_allowed(host: str | None, allowed: set[str] | frozenset[str]) -> bool:
    return bool(host) and host in allowed


def origin_allowed(origin: str | None, admin_host: str) -> bool:
    if not origin:
        return True
    return origin in {f"{ADMIN_ORIGIN_SCHEME}://{admin_host}", f"https://{admin_host}"}
