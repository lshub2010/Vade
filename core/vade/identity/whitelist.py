"""Trusted key whitelist: `(module_id, instance_id) -> fingerprint + public key`.

Two rules that are easy to get wrong and expensive to debug:

* Persist 0600 on every write (atomic replace), otherwise `pairing_accepted`
  survives only until the process restarts and "delete the fingerprint" in the
  config page has nothing to delete.
* `revoke(fp)` closes the connections that are USING that key right now. A
  revoke that only affects the next handshake leaves a live attacker connected.

Why the raw public key is stored as well as the fingerprint: IK needs the peer's
static key *before* the first message (`FfiHandshake` refuses IK without
`remote_pub` — there is no XX fallback), and the responder has one connection
and N stored keys. `candidate_keys()` is that pool; the gateway tries each one
on msg1 and the whitelist still decides in `allows()` after a successful read.
A fingerprint alone cannot reconstruct a public key.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..errors import VadeError

__all__ = ["Whitelist", "Entry", "WHITELIST_FILENAME"]

WHITELIST_FILENAME = "whitelist.json"


@dataclass(frozen=True, slots=True)
class Entry:
    module_id: str
    instance_id: str
    fingerprint: str
    added_at: float
    #: Raw X25519 public key, lowercase hex. `None` only for records written
    #: before this field existed (they can be revoked, not used for IK).
    public_key: str | None = None


def _fp8(public_key: bytes) -> str:
    import hashlib

    return hashlib.sha256(bytes(public_key)).hexdigest()[:16]


class Whitelist:
    def __init__(self, path: Path | str, *, on_revoke: Callable[[str], None] | None = None) -> None:
        self._path = Path(path)
        self._on_revoke = on_revoke
        self._entries: dict[tuple[str, str], Entry] = {}
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for item in data.get("entries", []):
            entry = Entry(
                module_id=item["module_id"],
                instance_id=item["instance_id"],
                fingerprint=item["fingerprint"],
                added_at=float(item.get("added_at", 0)),
                public_key=item.get("public_key"),
            )
            self._entries[(entry.module_id, entry.instance_id)] = entry
        try:
            os.chmod(self._path, 0o600)
        except OSError:  # pragma: no cover
            pass

    def _flush(self) -> None:
        payload = {
            "version": 1,
            "entries": [
                {
                    "module_id": e.module_id,
                    "instance_id": e.instance_id,
                    "fingerprint": e.fingerprint,
                    "public_key": e.public_key,
                    "added_at": e.added_at,
                }
                for e in sorted(self._entries.values(), key=lambda x: (x.module_id, x.instance_id))
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        # Create 0600 from the first byte: write_text() would leave a window
        # where the umask (usually 0644) applies.
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        finally:
            try:
                os.chmod(tmp, 0o600)
            except OSError:  # pragma: no cover
                pass
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------ queries
    def allows(self, fingerprint: str) -> bool:
        return any(e.fingerprint == fingerprint for e in self._entries.values())

    def fingerprint_for(self, module_id: str, instance_id: str | None = None) -> str | None:
        """`instance_id=None` -> the module's fingerprint when all instances agree."""
        if instance_id is not None:
            entry = self._entries.get((module_id, instance_id))
            return entry.fingerprint if entry else None
        fingerprints = {e.fingerprint for e in self._entries.values() if e.module_id == module_id}
        return fingerprints.pop() if len(fingerprints) == 1 else None

    def public_key_for(self, fingerprint: str) -> bytes | None:
        for entry in self._entries.values():
            if entry.fingerprint == fingerprint and entry.public_key:
                return bytes.fromhex(entry.public_key)
        return None

    def candidate_keys(self) -> tuple[bytes, ...]:
        """Every usable module public key, de-duplicated, stable order."""
        keys: list[bytes] = []
        seen: set[str] = set()
        for entry in sorted(self._entries.values(), key=lambda x: (x.module_id, x.instance_id)):
            if not entry.public_key or entry.public_key in seen:
                continue
            seen.add(entry.public_key)
            keys.append(bytes.fromhex(entry.public_key))
        return tuple(keys)

    def entries(self) -> tuple[Entry, ...]:
        return tuple(sorted(self._entries.values(), key=lambda e: (e.module_id, e.instance_id)))

    # ------------------------------------------------------------------ writes
    def add(self, module_id: str, instance_id: str, fingerprint: str, *, public_key: bytes | None = None) -> Entry:
        """Pairing success path. A DIFFERENT key for an existing module is refused.

        `public_key` is required for a NEW IK-capable entry; when supplied the
        fingerprint must be `SHA-256(pub)[:16]` (a caller that passes a
        mismatched pair would otherwise store a key the peer cannot prove).
        """
        if public_key is not None:
            raw = bytes(public_key)
            if len(raw) != 32:
                raise VadeError("INVALID_ARGUMENT", f"module public key must be 32 bytes, got {len(raw)}")
            if _fp8(raw) != fingerprint:
                raise VadeError(
                    "INVALID_ARGUMENT",
                    f"fingerprint {fingerprint} does not match the supplied public key ({_fp8(raw)})",
                )
        existing = self._entries.get((module_id, instance_id))
        if existing and existing.fingerprint != fingerprint:
            raise VadeError(
                "PERMISSION_DENIED",
                f"{module_id}#{instance_id} is already paired to another key; revoke it first",
            )
        if existing and public_key is None and existing.public_key is None:
            raise VadeError(
                "INVALID_ARGUMENT",
                f"{module_id}#{instance_id} has no stored public key; re-pair it with the raw key",
            )
        entry = Entry(
            module_id=module_id,
            instance_id=instance_id,
            fingerprint=fingerprint,
            added_at=time.time(),
            public_key=public_key.hex() if public_key is not None else (existing.public_key if existing else None),
        )
        self._entries[(module_id, instance_id)] = entry
        self._flush()
        return entry

    def revoke(self, fingerprint: str) -> tuple[str, ...]:
        """Remove every entry with this key and close the live connections."""
        removed = [key for key, entry in self._entries.items() if entry.fingerprint == fingerprint]
        for key in removed:
            del self._entries[key]
        if removed:
            self._flush()
        if self._on_revoke is not None:
            self._on_revoke(fingerprint)
        return tuple(f"{m}#{i}" for m, i in removed)
