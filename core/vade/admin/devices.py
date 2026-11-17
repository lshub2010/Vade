"""`devices.json`: the only list of keys that may sign admin challenges.

The core never generates a device key and never signs one (`31 §七`). A device
entry is a public key plus metadata; `device_id = SHA-256(pubkey)[:16]`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import VadeError
from .auth import verify_signature

__all__ = ["Device", "DeviceStore", "MAX_DEVICES"]

MAX_DEVICES = 5


@dataclass(slots=True)
class Device:
    device_id: str
    label: str
    alg: str
    pubkey: str                  # base64, never returned by the list endpoint
    fp8: str
    hardware_backed: bool
    added_at: float
    added_from_ip: str | None
    last_used_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "label": self.label,
            "alg": self.alg,
            "fp8": self.fp8,
            "hardware_backed": bool(self.hardware_backed),
            "added_at": self.added_at,
            "added_from_ip": self.added_from_ip,
            "last_used_at": self.last_used_at,
        }


class DeviceStore:
    def __init__(self, path: Path | str, *, max_devices: int = MAX_DEVICES,
                 wall: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.max_devices = max_devices
        self._wall = wall
        self._lock = threading.RLock()
        self._devices: dict[str, Device] | None = None
        self._revoked: set[str] | None = None
        self._version = 0

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict[str, Device]:
        if self._devices is not None:
            return self._devices
        with self._lock:
            if self._devices is not None:
                return self._devices
            items: dict[str, Device] = {}
            doc: dict[str, Any] = {}
            if self.path.exists():
                try:
                    doc = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raise VadeError("INTERNAL", "devices.json is unreadable") from None
                for entry in doc.get("devices", []):
                    try:
                        device = Device(**entry)
                    except TypeError:
                        continue
                    items[device.device_id] = device
            self._devices = items
            self._revoked = {str(item) for item in doc.get("revoked", []) if item}
            self._version = int(doc.get("version", 0)) if isinstance(doc, dict) else 0
            return items

    def _save(self) -> None:
        assert self._devices is not None
        assert self._revoked is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._version += 1
        doc = {
            "version": self._version,
            "devices": [asdict(d) for d in self._devices.values()],
            # Revoked keys stay on disk (§7.12.3 ④d): popping the entry alone
            # would let the same key re-bind with a fresh ticket.
            "revoked": sorted(self._revoked),
        }
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # ------------------------------------------------------------------ queries
    @property
    def version(self) -> int:
        self._load()
        return self._version

    def all(self) -> list[Device]:
        return list(self._load().values())

    def public_list(self) -> list[dict[str, Any]]:
        return [d.public() for d in self.all()]

    def get(self, device_id: str) -> Device | None:
        return self._load().get(device_id)

    def __len__(self) -> int:
        return len(self._load())

    def is_active(self, device_id: str) -> bool:
        return device_id in self._load()

    def is_revoked(self, pubkey: bytes) -> bool:
        """True iff this exact key was revoked before (survives restarts).

        `POST /admin/bind` refuses it with the generic 401 (§7.12.3 ④d);
        `add()` refuses it too so the step-up API cannot re-enrol it either.
        """
        self._load()
        assert self._revoked is not None
        return hashlib.sha256(pubkey).hexdigest()[:16] in self._revoked

    # ------------------------------------------------------------------ mutation
    def add(self, *, pubkey_b64: str, alg: str, label: str, ip: str | None,
            hardware_backed: bool = False) -> Device:
        with self._lock:
            devices = self._load()
            if len(devices) >= self.max_devices:
                raise VadeError("BIND_DEVICE_LIMIT", f"at most {self.max_devices} devices per account")
            raw = base64.b64decode(pubkey_b64 + "=" * (-len(pubkey_b64) % 4))
            device_id = hashlib.sha256(raw).hexdigest()[:16]
            assert self._revoked is not None
            if device_id in self._revoked:
                raise VadeError("INVALID_ARGUMENT", "this public key has been revoked")
            if device_id in devices:
                raise VadeError("INVALID_ARGUMENT", "this public key is already registered")
            device = Device(
                device_id=device_id, label=(label or "device")[:64], alg=alg.lower(),
                pubkey=base64.b64encode(raw).decode("ascii"),
                fp8=hashlib.sha256(raw).hexdigest()[:16],
                hardware_backed=bool(hardware_backed), added_at=self._wall(),
                added_from_ip=ip,
            )
            devices[device_id] = device
            self._save()
            return device

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            devices = self._load()
            if device_id not in devices:
                return False
            devices.pop(device_id)
            assert self._revoked is not None
            self._revoked.add(device_id)
            self._save()
            return True

    def mark_used(self, device_id: str) -> None:
        with self._lock:
            device = self._load().get(device_id)
            if device is None:
                return
            device.last_used_at = self._wall()
            self._save()

    # ------------------------------------------------------------------ signing
    def verify(self, device_id: str, message: bytes, signature_b64: str) -> bool:
        device = self.get(device_id)
        if device is None:
            return False
        try:
            signature = base64.b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
        except Exception:  # noqa: BLE001 - malformed base64 is just a bad signature
            return False
        raw = base64.b64decode(device.pubkey)
        return verify_signature(device.alg, raw, message, signature)
