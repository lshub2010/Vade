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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..errors import VadeError
from . import setup_token
from .auth import PasswordPolicyError, hash_password, validate_password

__all__ = ["SetupService", "TICKET_ALPHABET", "TICKET_LEN", "TICKET_TTL_S"]

TICKET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TICKET_LEN = 26
TICKET_TTL_S = 600


@dataclass(slots=True)
class SetupService:
    data_dir: Path
    admin_host: str
    fp8: str
    wall: Callable[[], float] = time.time
    unlock_ttl_s: int = 600
    _tickets: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        path = self.admin_doc_path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(dict(doc), ensure_ascii=False, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
        os.chmod(path, 0o600)

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
        self._tickets[ticket] = {"expires_at": expires, "consumed": False}
        return {
            "ticket": ticket, "expires_at": int(expires),
            "bind_url": f"vade://bind?h={self.admin_host}&fp={self.fp8}&t={ticket}&v=1",
        }

    def peek_ticket(self, ticket: str) -> dict[str, Any] | None:
        entry = self._tickets.get(ticket)
        if entry is None or entry["consumed"] or entry["expires_at"] <= self.wall():
            return None
        return entry

    def consume_ticket(self, ticket: str) -> bool:
        entry = self.peek_ticket(ticket)
        if entry is None:
            return False
        entry["consumed"] = True
        return True

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
        doc = {
            "version": 1,
            "password": hash_password(password),
            "created_at": self.wall(),
            "password_changed_at": self.wall(),
            "created_from_ip": ip,
        }
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
