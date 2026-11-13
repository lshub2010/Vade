"""Trusted key whitelist: `(module_id, instance_id) -> fingerprint`.

Two rules that are easy to get wrong and expensive to debug:

* Persist 0600 on every write (atomic replace), otherwise `pairing_accepted`
  survives only until the process restarts and "delete the fingerprint" in the
  config page has nothing to delete.
* `revoke(fp)` closes the connections that are USING that key right now. A
  revoke that only affects the next handshake leaves a live attacker connected.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..errors import VadeError

__all__ = ["Whitelist", "Entry", "WHITELIST_FILENAME"]

WHITELIST_FILENAME = "whitelist.json"


@dataclass(frozen=True, slots=True)
class Entry:
    module_id: str
    instance_id: str
    fingerprint: str
    added_at: float


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
                {"module_id": e.module_id, "instance_id": e.instance_id, "fingerprint": e.fingerprint, "added_at": e.added_at}
                for e in sorted(self._entries.values(), key=lambda x: (x.module_id, x.instance_id))
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------ queries
    def allows(self, fingerprint: str) -> bool:
        return any(e.fingerprint == fingerprint for e in self._entries.values())

    def fingerprint_for(self, module_id: str, instance_id: str) -> str | None:
        entry = self._entries.get((module_id, instance_id))
        return entry.fingerprint if entry else None

    def entries(self) -> tuple[Entry, ...]:
        return tuple(sorted(self._entries.values(), key=lambda e: (e.module_id, e.instance_id)))

    # ------------------------------------------------------------------ writes
    def add(self, module_id: str, instance_id: str, fingerprint: str) -> Entry:
        """Pairing success path. A DIFFERENT key for an existing module is refused."""
        existing = self._entries.get((module_id, instance_id))
        if existing and existing.fingerprint != fingerprint:
            raise VadeError(
                "PERMISSION_DENIED",
                f"{module_id}#{instance_id} is already paired to another key; revoke it first",
            )
        entry = Entry(module_id=module_id, instance_id=instance_id, fingerprint=fingerprint, added_at=time.time())
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
