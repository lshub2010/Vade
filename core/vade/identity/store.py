"""Core static X25519 identity (`data/core.key`).

* 32-byte raw seed, hex-encoded, file mode 0600, directory 0700.
* The private object has no readable repr and never enters a log line.
* Fingerprint for humans / QR: fp8 = SHA-256(pub)[:16] lowercase hex.
  (The full 64-hex value is kept for audit; `fp8` is what `--print-fingerprint`
  prints and what the setup page shows. TLS SPKI is a DIFFERENT value and is
  printed by `--print-tls-fingerprint`.)
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from ..errors import VadeError

__all__ = ["Identity", "load_or_create", "KEY_FILENAME"]

KEY_FILENAME = "core.key"

# Windows fds default to text mode and would translate every LF byte into CRLF,
# corrupting binary payloads. POSIX has no O_BINARY; OR-ing 0 is a no-op there.
_O_BINARY = getattr(os, "O_BINARY", 0)


class Identity:
    """Opaque wrapper; only the safe accessors are public."""

    __slots__ = ("_ffi", "_public_hex", "_fingerprint", "_secret_hex")

    def __init__(self, ffi: Any, secret_hex: str) -> None:
        self._ffi = ffi
        self._secret_hex = secret_hex
        self._public_hex = str(ffi.public_key_hex())
        self._fingerprint = hashlib.sha256(bytes.fromhex(self._public_hex)).hexdigest()

    def __repr__(self) -> str:  # never leak the seed
        return f"<Identity fp8={self.fp8} secret=[redacted]>"

    __str__ = __repr__

    @property
    def ffi(self) -> Any:
        return self._ffi

    @property
    def public_key_hex(self) -> str:
        return self._public_hex

    @property
    def public_key(self) -> bytes:
        return bytes.fromhex(self._public_hex)

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def fp8(self) -> str:
        return self._fingerprint[:16]

    def secret_hex(self) -> str:
        """Only caller: `save()`. Kept explicit so nothing serialises it by accident."""
        return self._secret_hex


def _protect(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - non-POSIX
        pass


def load_or_create(data_dir: Path | str, *, protocol_module: Any | None = None) -> Identity:
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover - non-POSIX
        pass

    key_path = directory / KEY_FILENAME
    if key_path.exists():
        raw = key_path.read_text(encoding="utf-8").strip()
        if len(raw) != 64:
            raise VadeError("INTERNAL", f"{key_path} must contain 64 hex chars (32-byte X25519 seed)")
        proto = protocol_module or _load_protocol()
        try:
            ffi = proto.FfiIdentity.from_secret_hex(raw)
        except Exception as exc:  # FfiError
            raise VadeError("INTERNAL", f"{key_path} is not a valid X25519 seed: {type(exc).__name__}") from None
        _protect(key_path)
        return Identity(ffi, raw)

    proto = protocol_module or _load_protocol()
    ffi = proto.FfiIdentity.generate()
    secret = str(ffi.to_secret_hex())
    # Create 0600 from the first byte; write_text() would briefly apply the
    # umask (usually 0644) to a private key.
    fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | _O_BINARY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(secret + "\n")
    _protect(key_path)
    return Identity(ffi, secret)


def _load_protocol() -> Any:
    try:
        import vade_protocol  # type: ignore
    except ImportError:
        raise VadeError(
            "INTERNAL",
            "protocol core python binding is not importable (missing libvade_protocol.so)",
        ) from None
    return vade_protocol
