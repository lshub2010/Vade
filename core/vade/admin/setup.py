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
        doc = self.load_admin()
        return doc.get("password") if doc else None

    def _write_admin(self, doc: Mapping[str, Any]) -> None:
        """tmp + `os.replace`, mode 0600. The only writer of `admin.json`."""
        self._atomic_write(
            self.admin_doc_path,
            json.dumps(dict(doc), ensure_ascii=False, indent=2).encode("utf-8"),
        )

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

        Only `admin/bind.py` calls this, while it holds its own lock, so no
        other request can observe the ticket as consumable in between.
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
        if self.initialized():
            raise VadeError("NOT_FOUND", "already initialised")
        if not loopback:
            expected = setup_token.read_token(self.data_dir)
            if not setup_token.compare(expected, setup_token_value):
                raise VadeError("SETUP_UNAUTHORIZED", "setup token is missing or wrong")
        try:
            validate_password(password)
        except PasswordPolicyError as exc:
            raise VadeError("INVALID_ARGUMENT", str(exc)) from None
        doc = self._initial_doc(password, ip)
        self._write_admin(doc)
        # Only now: a crash between the two leaves a token that is ignored
        # because admin.json exists, which is recoverable without `reset`.
        setup_token.consume(self.data_dir)
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
        """
        if self.initialized():
            raise VadeError("BIND_UNAUTHORIZED", "admin plane is already initialised")
        try:
            validate_password(password)
        except PasswordPolicyError as exc:
            raise VadeError("INVALID_ARGUMENT", str(exc)) from None
        self._write_admin(self._initial_doc(password, ip))
        setup_token.consume(self.data_dir)

    def begin_initialize_bound(self, *, password: str, ip: str | None) -> InitializeSnapshot:
        """`initialize_bound` plus the snapshot that can undo it.

        Called by `admin/bind.py` inside its bind transaction: if the device
        cannot be enrolled afterwards, `rollback_initialize()` puts the plane
        back to "setup required" and the caller keeps the ticket, so the same QR
        can be retried instead of needing `vade admin reset`.
        """
        snapshot = InitializeSnapshot(
            admin_doc=self._read_admin_bytes(),
            setup_token=setup_token.read_token(self.data_dir),
        )
        self.initialize_bound(password=password, ip=ip)
        return snapshot

    def rollback_initialize(self, snapshot: InitializeSnapshot) -> None:
        """Undo `begin_initialize_bound`, in the crash-safe order.

        The token is restored *first*: a crash between the two steps then leaves
        "initialised + token present" (the token is ignored while `admin.json`
        exists, and `ensure_setup_token()` unlinks it on the next start). The
        reverse order would leave "no password, no token", which is the state
        `initialize()` exists to avoid.
        """
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
