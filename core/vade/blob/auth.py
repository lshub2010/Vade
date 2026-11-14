"""Upload credentials: device bearer tokens + the browser cookie's four gates.

Two principal kinds (RFC-0004 §4.5, §8.1):

* **device** — `Authorization: Bearer <token>`. Tokens are minted from an
  already Noise-authenticated ws session (`ui/blob.token` is the next slice;
  `BlobService.issue_token` is the seam), live 600 s by default, are stored
  only as `sha256(token)` and stay valid across a core restart.
* **cookie** — `__Host-ac_ui`, the same cookie `/ui/session` issues. Writes
  additionally require **all four** gates of §8.2. Losing any one is
  `403 ORIGIN_MISMATCH`; an unknown token is `401 AUTH_FAILED`.

`READ_ONLY_SESSION` is returned only for a session/devices that is *explicitly*
read-only (D100 / AMEND-22 R8): seeing a cookie no longer implies read-only.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping, MutableMapping

from ..errors import VadeError

__all__ = [
    "Principal",
    "Authenticator",
    "hash_token",
    "new_token",
    "cookie_value",
    "UI_COOKIE",
    "CSRF_COOKIE",
    "CSRF_HEADER",
]

UI_COOKIE = "__Host-ac_ui"
CSRF_COOKIE = "__Host-ac_csrf"
CSRF_HEADER = "x-vade-csrf"


@dataclass(frozen=True, slots=True)
class Principal:
    device_id: str
    kind: str  # "device" | "cookie"
    read_only: bool = False
    max_bytes: int | None = None

    def require_write(self) -> None:
        if self.read_only:
            raise VadeError(
                "READ_ONLY_SESSION",
                "this session is explicitly read-only; uploads are refused",
                data={"field": "session"},
            )


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cookie_value(header: str, name: str) -> str | None:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _bearer(headers: Mapping[str, str]) -> str | None:
    raw = headers.get("authorization", "")
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


class Authenticator:
    """Turns headers into a `Principal`. Never touches the filesystem."""

    def __init__(
        self,
        *,
        tokens: Any,
        sessions: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None,
        origins: tuple[str, ...] = (),
        wall: Callable[[], float],
    ) -> None:
        self._tokens = tokens
        self._sessions = sessions
        self._origins = tuple(origins)
        self._wall = wall

    # ------------------------------------------------------------------ helpers
    @property
    def origins(self) -> tuple[str, ...]:
        return self._origins

    def _session_table(self) -> Mapping[str, MutableMapping[str, Any]]:
        table = self._sessions
        if callable(table):
            table = table()
        return table or {}

    def has_cookie(self, headers: Mapping[str, str]) -> bool:
        return cookie_value(headers.get("cookie", ""), UI_COOKIE) is not None

    # -------------------------------------------------------------------- gates
    def _cookie_gates(self, headers: Mapping[str, str]) -> tuple[str, MutableMapping[str, Any]]:
        """§8.2 gates 1-3, in that order, before any session lookup."""
        origin = headers.get("origin")
        if not origin or origin not in self._origins:
            raise VadeError(
                "ORIGIN_MISMATCH",
                "cookie credentials require an Origin on the configured allowlist",
                data={"reason": "origin"},
            )
        if headers.get("sec-fetch-site") != "same-origin":
            # Missing counts as "not a browser" — and a non-browser context
            # never gets to use an ambient cookie credential.
            raise VadeError(
                "ORIGIN_MISMATCH",
                "cookie credentials require Sec-Fetch-Site: same-origin",
                data={"reason": "fetch_site"},
            )

        token = cookie_value(headers.get("cookie", ""), UI_COOKIE)
        if not token:
            raise VadeError("AUTH_FAILED", "authentication failed")
        session = self._session_table().get(token)
        if session is None or session.get("revoked"):
            raise VadeError("AUTH_FAILED", "authentication failed")

        header = headers.get(CSRF_HEADER, "")
        cookie = cookie_value(headers.get("cookie", ""), CSRF_COOKIE)
        if not header or not cookie or not hmac.compare_digest(header, cookie):
            raise VadeError(
                "ORIGIN_MISMATCH",
                "missing or mis-matched double-submit CSRF token",
                data={"reason": "csrf"},
            )
        stored = str(session.get("csrf_hash") or "")
        digest = hashlib.sha256(header.encode("utf-8")).hexdigest()
        if not stored or not hmac.compare_digest(digest, stored):
            raise VadeError(
                "ORIGIN_MISMATCH",
                "CSRF token is not bound to this session",
                data={"reason": "csrf_binding"},
            )
        return token, session

    # ------------------------------------------------------------------ public
    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        bearer = _bearer(headers)
        if bearer is not None:
            record = self._tokens.get_token(hash_token(bearer), self._wall())
            if record is None:
                # No oracle: expired, revoked and never-issued are identical.
                raise VadeError("AUTH_FAILED", "authentication failed")
            return Principal(
                device_id=record.device_id,
                kind="device",
                read_only=record.read_only,
                max_bytes=record.max_bytes,
            )

        if self.has_cookie(headers):
            _token, session = self._cookie_gates(headers)
            return Principal(
                device_id=str(session.get("device_id") or "ui-unknown"),
                kind="cookie",
                read_only=bool(session.get("read_only")),
            )

        raise VadeError("AUTH_FAILED", "authentication failed")
