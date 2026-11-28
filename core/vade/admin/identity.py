"""Core identity key: the fingerprint users compare with the QR code.

This is **not** the TLS certificate fingerprint. TLS rotates; the pin must not
break when it does (31-C5a §T4 ⑥, §七). `fp8 = SHA-256(core_pub)[:16]`.

The private key never leaves `data/admin/core_identity.key` (0600) and is never
returned by an API, a log line or an audit row.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

__all__ = ["CoreIdentity", "load_or_create", "HELLO_PREFIX"]

HELLO_PREFIX = "vade-bind-hello-v1"

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


@dataclass(frozen=True, slots=True)
class CoreIdentity:
    fp8: str
    public_raw: bytes
    _key: Any

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def hello_signature(self, *, kex_pub: str, challenge: str, ticket: str, admin_host: str) -> str:
        """`Ed25519(core_identity, "vade-bind-hello-v1\n" ‖ kex_pub ‖ challenge ‖ SHA256(ticket) ‖ admin_host)`."""
        ticket_hash = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
        message = "\n".join([HELLO_PREFIX, kex_pub, challenge, ticket_hash, admin_host]).encode("utf-8")
        return base64.b64encode(self.sign(message)).decode("ascii")

    def public_b64(self) -> str:
        return base64.b64encode(self.public_raw).decode("ascii")


def load_or_create(data_dir: Path | str) -> CoreIdentity:
    directory = Path(data_dir) / "admin"
    path = directory / "core_identity.key"
    if path.exists():
        raw = path.read_bytes()
        key = Ed25519PrivateKey.from_private_bytes(raw[:32])
        public = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return CoreIdentity(fp8=_fp8(public), public_raw=public, _key=key)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # O_EXCL + 0600: another process must not win the race and leave us with a
    # key we do not actually own.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    try:
        _write_all(fd, raw)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return CoreIdentity(fp8=_fp8(public), public_raw=public, _key=key)


def _fp8(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]
