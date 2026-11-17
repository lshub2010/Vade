"""Trusted key whitelist: `(module_id, instance_id) -> fingerprint + public key`.

Three rules that are easy to get wrong and expensive to debug:

* Persist 0600 on every write (atomic replace), otherwise `pairing_accepted`
  survives only until the process restarts and "delete the fingerprint" in the
  config page has nothing to delete.
* `revoke(fp)` closes the connections that are USING that key right now. A
  revoke that only affects the next handshake leaves a live attacker connected.
* A record that carries `public_key` must PROVE its fingerprint:
  `sha256(pub)[:16] == fingerprint`. On load such a record is dropped
  (fail closed) and `add()` refuses the write. Otherwise `allows()` would
  trust a name that `candidate_keys()` can never authenticate. Records without
  a public key (written before that field existed) are loaded as-is: they can
  still be revoked and listed, they just cannot be used for IK.

Why the raw public key is stored as well as the fingerprint: IK needs the peer's
static key *before* the first message (`FfiHandshake` refuses IK without
`remote_pub` — there is no XX fallback), and the responder has one connection
and N stored keys. `candidate_keys()` is that pool; the gateway tries each one
on msg1 and the whitelist still decides in `allows()` after a successful read.
A fingerprint alone cannot reconstruct a public key.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..errors import VadeError

__all__ = ["Whitelist", "Entry", "WHITELIST_FILENAME", "fp8_hex"]

WHITELIST_FILENAME = "whitelist.json"
PUBLIC_KEY_LEN = 32


@dataclass(frozen=True, slots=True)
class Entry:
    module_id: str
    instance_id: str
    fingerprint: str
    added_at: float
    #: Raw X25519 public key, lowercase hex. `None` only for records written
    #: before this field existed (they can be revoked, not used for IK).
    public_key: str | None = None


def fp8_hex(public_key: bytes) -> str:
    """`SHA-256(public_key)[:16]`, lowercase hex — the `fp8` humans compare.

    This is the *identity* fingerprint (QR `fp`, setup page, `--print-fingerprint`).
    It is NOT a TLS pin: the TLS pin is `SHA-256(SPKI)[:16]` over a certificate,
    see `vade.identity.tls.spki_fingerprint_hex`. Two different values (D78).

    It is also not the Rust `psk_fingerprint_hex()` (BLAKE2s-256, used as the
    pair-code PSK's `core_pub_fp`): different hash, different value. Never
    compare one against the other.
    """
    return hashlib.sha256(bytes(public_key)).hexdigest()[:16]


#: Backwards-compatible private alias; new code should import `fp8_hex`.
_fp8 = fp8_hex


def _key_proves_fingerprint(public_key: bytes, fingerprint: str) -> bool:
    """`len == 32` and `sha256(pub)[:16] == fingerprint` (exact, lowercase)."""
    return len(public_key) == PUBLIC_KEY_LEN and fp8_hex(public_key) == fingerprint


def _stored_key_is_consistent(public_key: str, fingerprint: str) -> bool:
    try:
        raw = bytes.fromhex(public_key)
    except (TypeError, ValueError):
        return False
    return _key_proves_fingerprint(raw, fingerprint)


class Whitelist:
    def __init__(
        self,
        path: Path | str,
        *,
        on_revoke: Callable[[str, tuple[str, ...]], None] | None = None,
    ) -> None:
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
            public_key = item.get("public_key")
            fingerprint = item["fingerprint"]
            if public_key is not None and not _stored_key_is_consistent(public_key, fingerprint):
                # Fail closed: a stored key that cannot prove the stored
                # fingerprint would let `allows()` trust a key that no
                # handshake can ever authenticate. Never rewrite the pair —
                # re-pairing is the only way back in.
                continue
            entry = Entry(
                module_id=item["module_id"],
                instance_id=item["instance_id"],
                fingerprint=fingerprint,
                added_at=float(item.get("added_at", 0)),
                public_key=public_key,
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

    def devices_for_fingerprint(self, fingerprint: str) -> frozenset[str]:
        """Every `module#instance` this key is paired to (possibly empty).

        One static key may be paired to several instances — a pooled module
        runs N workers that share one key. Whoever decides *whether* a claimed
        slot belongs to this key must ask this set: any single-name answer is
        a guess that rejects the other instances.
        """
        return frozenset(
            f"{e.module_id}#{e.instance_id}"
            for e in self._entries.values()
            if e.fingerprint == fingerprint
        )

    def device_for_fingerprint(self, fingerprint: str) -> str | None:
        """A single paired device name, or `None` when unpaired OR ambiguous.

        Kept for `CoreApp._bind_device_identity`, which can only store one
        name on `connection.device_id`. A key paired to several instances
        returns `None` instead of the lexicographically smallest name: that
        name is a claim that the socket belongs to *that* pairing, and it
        locked the connection out of every other instance (P1-3, a pooled
        worker whose `module/register` was refused as `PERMISSION_DENIED`).
        Registration re-derives the exact name from
        `devices_for_fingerprint()` once the manifest says which slot it
        claims. `None` means "no pairing or no single answer" — callers must
        fail closed rather than invent an identity.
        """
        names = self.devices_for_fingerprint(fingerprint)
        return next(iter(names)) if len(names) == 1 else None

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
            if len(raw) != PUBLIC_KEY_LEN:
                raise VadeError("INVALID_ARGUMENT", f"module public key must be 32 bytes, got {len(raw)}")
            if not _key_proves_fingerprint(raw, fingerprint):
                raise VadeError(
                    "INVALID_ARGUMENT",
                    f"fingerprint {fingerprint} does not match the supplied public key ({fp8_hex(raw)})",
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
        """Remove every entry with this key and close the live connections.

        The callback receives `(fingerprint, removed_ids)` — RFC-0016 §5.6-17
        (Q4 default): unbinding a device must also kill its minted blob
        tokens, and only the callback knows who consumes that (the App wires
        `BlobService.revoke_device`). `removed_ids` are the deleted
        `module_id#instance_id` names, the same tuple this method returns, so
        callers and the callback agree on one spelling of the device identity.
        """
        removed = [key for key, entry in self._entries.items() if entry.fingerprint == fingerprint]
        for key in removed:
            del self._entries[key]
        if removed:
            self._flush()
        removed_ids = tuple(f"{m}#{i}" for m, i in removed)
        if self._on_revoke is not None:
            self._on_revoke(fingerprint, removed_ids)
        return removed_ids
