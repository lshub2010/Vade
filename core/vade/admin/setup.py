"""First-run setup: the loopback path, the setup token, and the QR gate.

Ordering rules that matter (31-C5a §T4 ①):

* `admin.json` exists ⇔ initialised. There is no second "initialized" flag.
* non-loopback `GET /admin/setup` without an unlock cookie must not contain
  `bind_url`, `fp8` or any `acs_` string — the gate is the unlock, not the UI.
* write `admin.json` first (tmp + `os.replace`), then unlink the token. The
  reverse order produces "token gone, no password" and a reset is the only way
  out.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..errors import VadeError
from . import setup_token
from .auth import PasswordPolicyError, hash_password, validate_password

__all__ = ["SetupService", "InitializeSnapshot", "TICKET_ALPHABET", "TICKET_LEN", "TICKET_TTL_S"]

TICKET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TICKET_LEN = 26
TICKET_TTL_S = 600

#: One process-wide lock for every **first-run commit**, whoever the caller is:
#: `POST /admin/setup` (`initialize`) and the bind path (`initialize_bound` /
#: `begin_initialize_bound`) both create `admin.json`, and the existence of that
#: file is the *only* "initialised" flag this plane has. Held across
#: [re-check `initialized()` → write `admin.json` → unlink `setup.token`] and
#: never across the Argon2 digest, so the two entry points queue for the file
#: write instead of interleaving it (review P1-5). `admin/bind.py` consults it
#: through `setup_commit_in_flight()`.
_SETUP_COMMIT_LOCK = threading.RLock()


def setup_commit_lock() -> threading.RLock:
    """The shared mutex object (bind holds it through `begin_initialize_bound`)."""
    return _SETUP_COMMIT_LOCK


def setup_commit_in_flight() -> bool:
    """`True` while *another* thread is inside a first-run critical section.

    Used by the bind reservation to answer `BIND_SETUP_LOCKED` (423, retriable)
    instead of blocking a worker thread behind a browser that is finishing
    `admin.json`. A same-thread holder would see `True` from `RLock.acquire`
    re-entering, which is why this is only ever called from outside the lock.
    """
    if _SETUP_COMMIT_LOCK.acquire(blocking=False):
        _SETUP_COMMIT_LOCK.release()
        return False
    return True


@dataclass(frozen=True, slots=True)
class InitializeSnapshot:
    """The pre-bind state of `admin.json` and `setup.token`, for rollback.

    `setup_required=true` means `admin.json` did not exist; the token file did
    (generated at start, or pinned through `VADE_SETUP_TOKEN`). Both are captured
    so `rollback_initialize()` can return the plane to exactly what the bind
    request found — otherwise a failed enrolment leaves "initialised, no device,
    token gone", which only `vade admin reset` can repair.
    """

    admin_doc: bytes | None
    setup_token: str | None


@dataclass(slots=True)
class SetupService:
    data_dir: Path
    admin_host: str
    fp8: str
    wall: Callable[[], float] = time.time
    unlock_ttl_s: int = 600
    _tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Ticket state is read-modify-write (`peek` → `consume`); without the lock
    #: two concurrent `POST /admin/bind` calls can both observe an unconsumed
    #: ticket. See `admin/bind.py` for the enclosing bind transaction.
    _lock: Any = field(default_factory=threading.RLock)
    #: `(stat key, stored hash)` — see `password_hash()`.
    _hash_cache: tuple[tuple[int, int, int], str | None] | None = None
    #: `(stat key, version)` — see `password_version()`.
    _version_cache: tuple[tuple[int, int, int], int] | None = None

    # ------------------------------------------------------------------ state
    @property
    def admin_doc_path(self) -> Path:
        return Path(self.data_dir) / "admin" / "admin.json"

    def initialized(self) -> bool:
        return self.admin_doc_path.exists()

    def load_admin(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.admin_doc_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None

    def password_hash(self) -> str | None:
        """The stored credential, cached until `admin.json` itself changes.

        Every login, step-up and bind reads this, and each read used to parse
        the whole document; the parse is pure overhead next to the digest. The
        cache key is the file's identity (`st_mtime_ns`, `st_size`, `st_ino`) —
        every writer in this module replaces the file via `os.replace`, so a new
        inode (or a new mtime) is exactly "the credential may have changed".
        A missing file is not cached: `admin.json` appearing is what makes the
        plane initialised, and that transition must be visible immediately.
        """
        try:
            stat = self.admin_doc_path.stat()
        except OSError:
            self._hash_cache = None
            return None
        key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if self._hash_cache is not None and self._hash_cache[0] == key:
            return self._hash_cache[1]
        doc = self.load_admin()
        value = doc.get("password") if doc else None
        self._hash_cache = (key, value)
        return value

    def _write_admin(self, doc: Mapping[str, Any]) -> None:
        """tmp + `os.replace`, mode 0600. The only writer of `admin.json`."""
        self._atomic_write(
            self.admin_doc_path,
            json.dumps(dict(doc), ensure_ascii=False, indent=2).encode("utf-8"),
        )
        # Belt and braces: the new inode already misses the cache key, but a
        # credential change must never be served from a stale read even if a
        # filesystem reports a coarse `st_mtime_ns`.
        self._hash_cache = None
        self._version_cache = None

    def password_version(self) -> int:
        """A monotonic-enough number that changes with every password change.

        `SessionStore` stamps refresh rows with it and compares on every
        refresh; deriving it from `admin.json` (instead of a process counter)
        means a restart does not replay version 0, and a password reset
        performed by `vade admin reset-password` while the core was stopped
        still invalidates the persisted refresh rows (review P2-20).

        Millisecond resolution, cached by the file's identity like
        `password_hash()`: the only caller sits on every authenticated request.
        """
        try:
            stat = self.admin_doc_path.stat()
        except OSError:
            self._version_cache = None
            return 0
        key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if self._version_cache is not None and self._version_cache[0] == key:
            return self._version_cache[1]
        doc = self.load_admin()
        changed = (doc or {}).get("password_changed_at")
        version = 0
        if changed is not None:
            try:
                version = int(float(changed) * 1000)
            except (TypeError, ValueError):
                version = 0
        self._version_cache = (key, version)
        return version

    @staticmethod
    def _atomic_write(path: Path, raw: bytes) -> None:
        """tmp + `os.replace`, mode 0600 — for the state files this service owns."""
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    def _read_admin_bytes(self) -> bytes | None:
        """Raw `admin.json` bytes, or `None` when the plane is uninitialised."""
        try:
            return self.admin_doc_path.read_bytes()
        except (FileNotFoundError, OSError):
            return None

    def rehash_password(self, password: str, stored: str | None = None) -> bool:
        """Upgrade a verified credential in place (scrypt → Argon2id, or weak params).

        Called only after `verify_password` returned True for `password`. The
        `stored` comparison closes the window where another request already
        changed the password between verify and write: never overwrite a hash
        the caller did not just prove knowledge of.
        """
        from .auth import hash_password, password_needs_rehash

        current = stored if stored is not None else self.password_hash()
        if not password_needs_rehash(current):
            return False
        doc = self.load_admin()
        if doc is None or doc.get("password") != current:
            return False
        doc["password"] = hash_password(password)
        doc["password_rehashed_at"] = self.wall()
        self._write_admin(doc)
        return True

    def replace_password_hash(
        self,
        new_hash: str,
        *,
        expected_current: str | None,
        params: Mapping[str, Any] | None = None,
        rehashed_from: str | None = None,
    ) -> bool:
        """Offline swap of a stored credential (tools/migrate_password_hashes.py).

        Compare-and-swap on the hash read a moment earlier: a login that
        re-hashed in between makes this return False instead of clobbering the
        newer value. `new_hash` must already be at today's parameters — the
        guard below keeps a caller from writing back a weaker or foreign
        string (an empty value would otherwise silently lock the admin out).

        `params` is written verbatim as `password_params` so the cost settings
        are queryable without parsing the PHC string.
        """
        from .auth import password_needs_rehash

        if password_needs_rehash(new_hash):
            raise ValueError("refusing to store a hash that is not current Argon2id")
        doc = self.load_admin()
        if doc is None or doc.get("password") != expected_current:
            return False
        doc["password"] = new_hash
        doc["password_rehashed_at"] = self.wall()
        if params is not None:
            doc["password_params"] = dict(params)
        if rehashed_from is not None:
            doc["password_rehashed_from"] = rehashed_from
        self._write_admin(doc)
        return True

    def state(self, *, loopback: bool, unlocked: bool) -> dict[str, Any]:
        """The payload of `GET /admin/setup`. Gate first, QR second."""
        if not self.initialized():
            if loopback:
                ticket = self.create_ticket()
                return {
                    "setup_required": True,
                    "requires_token": False,
                    "bind_url": ticket["bind_url"],
                    "fp8": self.fp8,
                    "expires_at": ticket["expires_at"],
                }
            if not unlocked:
                # Nothing else may appear here: no bind_url, no fp8, no acs_.
                return {"setup_required": True, "requires_token": True}
            ticket = self.create_ticket()
            return {
                "setup_required": True,
                "requires_token": False,
                "bind_url": ticket["bind_url"],
                "fp8": self.fp8,
                "expires_at": ticket["expires_at"],
            }
        return {"setup_required": False}

    # ----------------------------------------------------------------- ticket
    def create_ticket(self, ttl: int = TICKET_TTL_S) -> dict[str, Any]:
        ticket = "".join(secrets.choice(TICKET_ALPHABET) for _ in range(TICKET_LEN))
        expires = self.wall() + ttl
        with self._lock:
            self._tickets[ticket] = {"expires_at": expires, "consumed": False}
        return {
            "ticket": ticket, "expires_at": int(expires),
            "bind_url": f"vade://bind?h={self.admin_host}&fp={self.fp8}&t={ticket}&v=1",
        }

    def peek_ticket(self, ticket: str) -> dict[str, Any] | None:
        """Read-only lookup for `GET /admin/bind/info` and step ④a."""
        with self._lock:
            entry = self._tickets.get(ticket)
            if entry is None or entry["consumed"] or entry["expires_at"] <= self.wall():
                return None
            return entry

    def consume_ticket(self, ticket: str) -> bool:
        """Atomic single-use claim: exactly one caller per ticket gets `True`."""
        with self._lock:
            entry = self.peek_ticket(ticket)
            if entry is None:
                return False
            entry["consumed"] = True
            return True

    def release_ticket(self, ticket: str) -> None:
        """Roll back a reservation after a later step of the same transaction failed.

        Only `admin/bind.py` calls this, after its commit failed. It is atomic
        on its own lock, so no other request can observe a ticket that is half
        released: from `consume_ticket` to here the ticket simply reads as used.
        """
        with self._lock:
            entry = self._tickets.get(ticket)
            if entry is not None:
                entry["consumed"] = False

    # ----------------------------------------------------------------- unlock
    def unlock(self, provided: str) -> dict[str, Any]:
        expected = setup_token.read_token(self.data_dir)
        if not setup_token.compare(expected, provided):
            raise VadeError("SETUP_UNAUTHORIZED", "setup token is missing or wrong")
        ticket = self.create_ticket()
        return {
            "bind_url": ticket["bind_url"],
            "fp8": self.fp8,
            "expires_at": int(self.wall() + self.unlock_ttl_s),
        }

    # ------------------------------------------------------------- initialize
    def initialize(
        self,
        *,
        password: str,
        setup_token_value: str | None = None,
        loopback: bool,
        ip: str | None,
        pubkey: str | None = None,
        alg: str | None = None,
        label: str = "first device",
        device_store: Any = None,
    ) -> dict[str, Any]:
        """Create `admin.json` from the browser path (loopback or setup token).

        The expensive part — the password digest — runs *before* the shared
        commit lock, and the [re-check → `admin.json` → token unlink] trio is
        `_commit_initial()`, which the bind path uses too: two writers meant the
        loser's password silently stopped working (review P1-5).
        """
        if self.initialized():
            raise VadeError("NOT_FOUND", "already initialised")
        if not loopback:
            # Cheap pre-check so an unauthenticated remote caller never makes
            # this process run an Argon2 digest; the authoritative re-check is
            # the `initialized()` inside the commit section below.
            expected = setup_token.read_token(self.data_dir)
            if not setup_token.compare(expected, setup_token_value):
                raise VadeError("SETUP_UNAUTHORIZED", "setup token is missing or wrong")
        try:
            validate_password(password)
        except PasswordPolicyError as exc:
            raise VadeError("INVALID_ARGUMENT", str(exc)) from None
        # Hash outside the lock: 64 MiB of Argon2 must not serialise the two
        # entry points (a bind would queue behind it for no reason).
        doc = self._initial_doc(password, ip)
        self._commit_initial(doc, code="NOT_FOUND", message="already initialised")
        device = None
        if pubkey and device_store is not None:
            from .auth import normalize_pubkey

            resolved_alg, raw = normalize_pubkey(pubkey, alg)
            import base64

            device = device_store.add(
                pubkey_b64=base64.b64encode(raw).decode(), alg=resolved_alg,
                label=label, ip=ip,
            )
        return {"initialized": True, "device": device.public() if device else None}

    def _commit_initial(
        self, doc: Mapping[str, Any], *, code: str, message: str
    ) -> InitializeSnapshot:
        """Re-check, write `admin.json`, unlink `setup.token` — one critical section.

        `admin.json` existing *is* "initialised", so the check and both writes
        have to be atomic across **both** entry points. `bind.py` used to guard
        bind↔bind with `_SETUP_COMMITS` while the browser path held nothing, so
        a phone and a browser finishing first-run together each wrote a document
        and one password silently died with the overwritten file (review P1-5).

        Returns the snapshot `rollback_initialize()` needs; the `admin.json` half
        is always `None` because the plane was uninitialised inside the lock.
        """
        with _SETUP_COMMIT_LOCK:
            if self.initialized():
                raise VadeError(code, message)
            snapshot = InitializeSnapshot(
                admin_doc=self._read_admin_bytes(),
                setup_token=setup_token.read_token(self.data_dir),
            )
            self._write_admin(doc)
            # Only now: a crash between the two leaves a token that is ignored
            # because admin.json exists, which is recoverable without `reset`.
            setup_token.consume(self.data_dir)
            return snapshot

    def _initial_doc(self, password: str, ip: str | None) -> dict[str, Any]:
        return {
            "version": 1,
            "password": hash_password(password),
            "created_at": self.wall(),
            "password_changed_at": self.wall(),
            "created_from_ip": ip,
        }

    def initialize_bound(self, *, password: str, ip: str | None) -> None:
        """First-run setup completed by a verified bind ticket (§7.12.3 ③).

        The bind ticket **is** the credential here: the envelope was decrypted,
        the challenge signature was verified and the ticket was reserved under
        the bind lock before this is called. Re-checking `setup.token` would
        demand a secret a phone scanning a QR never has — and the caller is
        never loopback, so `initialize()` would refuse it by design.

        Same crash ordering as `initialize()`: `admin.json` first, token second.
        Shares `_commit_initial()` with it, so the bind commit and a browser
        `POST /admin/setup` cannot both create the document (review P1-5).
        """
        # One body for both: the bind path wants the rollback snapshot, callers
        # that only need "make it initialised" do not.
        self.begin_initialize_bound(password=password, ip=ip)

    def begin_initialize_bound(self, *, password: str, ip: str | None) -> InitializeSnapshot:
        """`initialize_bound` plus the snapshot that can undo it.

        Called by `admin/bind.py` inside its bind transaction: if the device
        cannot be enrolled afterwards, `rollback_initialize()` puts the plane
        back to "setup required" and the caller keeps the ticket, so the same QR
        can be retried instead of needing `vade admin reset`.

        The snapshot is taken **inside** the commit section: a snapshot read
        before the lock could describe a plane another writer has since
        initialised, and the rollback would then delete *their* document.

        Losing the race is `BIND_SETUP_LOCKED` (423, retriable) — the same code
        the reservation uses — not `BIND_UNAUTHORIZED`: the ticket is still
        valid, and the App must retry instead of discarding the QR.
        """
        try:
            validate_password(password)
        except PasswordPolicyError as exc:
            raise VadeError("INVALID_ARGUMENT", str(exc)) from None
        return self._commit_initial(
            self._initial_doc(password, ip),
            code="BIND_SETUP_LOCKED",
            message="setup is in progress in another session",
        )

    def rollback_initialize(self, snapshot: InitializeSnapshot) -> None:
        """Undo `begin_initialize_bound`, in the crash-safe order.

        The token is restored *first*: a crash between the two steps then leaves
        "initialised + token present" (the token is ignored while `admin.json`
        exists, and `ensure_setup_token()` unlinks it on the next start). The
        reverse order would leave "no password, no token", which is the state
        `initialize()` exists to avoid.

        Runs under the same commit lock as the forward direction: an unlink that
        slipped between another writer's check and its `admin.json` write would
        delete a document this rollback does not own.
        """
        with _SETUP_COMMIT_LOCK:
            if snapshot.setup_token:
                self._atomic_write(
                    setup_token.token_path(self.data_dir), snapshot.setup_token.encode("ascii"),
                )
            path = self.admin_doc_path
            if snapshot.admin_doc is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                self._atomic_write(path, snapshot.admin_doc)

    def change_password(self, old: str, new: str) -> None:
        from .auth import verify_password

        doc = self.load_admin()
        if doc is None:
            raise VadeError("AUTH_FAILED", "authentication failed")
        if not verify_password(old, doc.get("password")):
            raise VadeError("AUTH_FAILED", "authentication failed")
        try:
            validate_password(new)
        except PasswordPolicyError as exc:
            raise VadeError("INVALID_ARGUMENT", str(exc)) from None
        doc["password"] = hash_password(new)
        doc["password_changed_at"] = self.wall()
        self._write_admin(doc)
