r"""Blob identifiers and on-disk paths (RFC-0004 §4.1, §8.4).

Two rules, both security-relevant:

* **Accept by regex only.** `^(?:blb|upl)_[0-9a-z]{26}\Z` — the two prefixes of
  RFC-0004 §4.1 (`blb_` blob, `upl_` upload). The RFC prints the shape as
  `^bl[ub]_…`, which spells `blu_`; the prose prefixes win, because those are
  what the generator and every response field carry. `\Z`, not `$`: `$` also
  matches *before* a trailing newline, and an id with a newline in it must not
  reach a filename. Anything else is 404, not "bad format": a caller that can
  distinguish "malformed" from "does not exist" has an oracle. Validation
  happens before any table lookup.
* **Paths come from the sha256, never from the caller.** The only string that
  reaches the filesystem is a validated 64-char lowercase hex digest, split
  two levels deep. `upload_id` only ever names a *temporary* file inside
  `blobs/tmp/` and is itself regex-checked first. `name` is display-only and
  never touches a path.

Generated ids are 26-char Crockford base32 (48-bit ms + 80 random bits), so
they sort by creation time and are ~130 bits unguessable. The regex is wider
than the generator on purpose: it is a *shape* check, not a canonicaliser.
"""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path

from ..errors import VadeError

__all__ = [
    "ID_RE",
    "SHA256_RE",
    "BLOB_PREFIX",
    "UPLOAD_PREFIX",
    "new_blob_id",
    "new_upload_id",
    "is_id",
    "require_upload_id",
    "require_file_id",
    "normalize_sha256",
    "blob_relpath",
    "blob_path",
    "tmp_path",
    "thumb_path",
    "orphan_dir",
    "sanitize_name",
]

ID_RE = re.compile(r"^(?:blb|upl)_[0-9a-z]{26}\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
BLOB_PREFIX = "blb_"
UPLOAD_PREFIX = "upl_"

#: Crockford base32, lowercased (no i/l/o/u). 26 chars * 5 bits = 130 bits.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_SEQ_LOCK_BITS = 80


def _encode(value: int, length: int = 26) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _new(prefix: str) -> str:
    millis = int(time.time() * 1000)
    entropy = int.from_bytes(secrets.token_bytes(10), "big")  # 80 bits
    return prefix + _encode((millis << _SEQ_LOCK_BITS) | entropy)


def new_blob_id() -> str:
    return _new(BLOB_PREFIX)


def new_upload_id() -> str:
    return _new(UPLOAD_PREFIX)


def is_id(value: object) -> bool:
    return isinstance(value, str) and ID_RE.match(value) is not None


def _require(value: object, prefix: str, field: str) -> str:
    # One code for "wrong shape" and "does not exist": no format oracle.
    if not is_id(value) or not str(value).startswith(prefix):
        raise VadeError("NOT_FOUND", f"{field} is not a known identifier", data={"field": field})
    return str(value)


def require_upload_id(value: object) -> str:
    return _require(value, UPLOAD_PREFIX, "upload_id")


def require_file_id(value: object) -> str:
    return _require(value, BLOB_PREFIX, "file_id")


def normalize_sha256(value: object) -> str:
    """Accept 64 hex (any case), return lowercase. Everything else is 400.

    Case is normalised; whitespace is *not*. A digest that arrives with a
    trailing newline is a client bug (or a smuggling attempt), and silently
    repairing it would make two spellings of one digest interchangeable.
    """
    if isinstance(value, str):
        lowered = value.lower()
        if SHA256_RE.match(lowered):
            return lowered
    raise VadeError(
        "INVALID_ARGUMENT",
        "sha256 must be 64 lowercase hex characters",
        data={"field": "sha256"},
    )


def blob_relpath(sha256: str) -> str:
    """`ab/cd/<sha>`; callers must have validated `sha256` already."""
    if not isinstance(sha256, str) or not SHA256_RE.match(sha256):
        raise VadeError("INTERNAL", "blob path requested for an unvalidated digest")
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}"


def blob_path(root: Path | str, sha256: str) -> Path:
    """Final, content-addressed path. Refuses anything outside `root`."""
    base = Path(root).resolve()
    candidate = base / blob_relpath(sha256)
    # Defense in depth: `sha256` is regex-validated upstream, so this can only
    # fire if the store root itself is a symlink farm. Resolving the parent keeps
    # the check cheap for the (still absent) final component.
    parent = candidate.parent.resolve()
    if not parent.is_relative_to(base) or candidate.name != sha256:
        raise VadeError("INTERNAL", "refusing to build a blob path outside the store")
    return candidate


def tmp_path(root: Path | str, upload_id: str) -> Path:
    if not is_id(upload_id) or not upload_id.startswith(UPLOAD_PREFIX):
        raise VadeError("INTERNAL", "temporary path requested for an invalid upload id")
    return Path(root).resolve() / "tmp" / f"{upload_id}.part"


def thumb_path(root: Path | str, sha256: str) -> Path:
    base = Path(root).resolve()
    candidate = base / blob_relpath(sha256)
    return candidate.with_name(f"{sha256}.thumb")


def orphan_dir(root: Path | str) -> Path:
    return Path(root).resolve() / "orphan"


def sanitize_name(value: object, *, default: str = "attachment", limit: int = 255) -> str:
    """Display name only: no CR/LF, no control characters, <=255 chars.

    This value reaches `Content-Disposition` and the database. It never reaches
    a path (§8.4), but header injection is still header injection.
    """
    if not isinstance(value, str):
        return default
    cleaned = "".join(ch for ch in value if ch.isprintable() and ch not in "\r\n")
    cleaned = cleaned.replace("\\", "_").replace("/", "_").strip()
    if not cleaned:
        return default
    return cleaned[:limit]
