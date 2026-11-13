"""Identity: X25519 static key, TLS material, whitelist, pair codes."""

from __future__ import annotations

from typing import Any

from .pairing import PairingStore, Purpose, code_shape_ok, normalize
from .store import Identity, load_or_create
from .whitelist import Entry, Whitelist

__all__ = [
    "Identity",
    "load_or_create",
    "Whitelist",
    "Entry",
    "PairingStore",
    "Purpose",
    "normalize",
    "code_shape_ok",
    "install",
]


def install(app: Any) -> None:
    """Idempotent: create identity + whitelist + pairing store on first call."""
    if getattr(app, "identity", None) is not None:
        return
    from pathlib import Path

    data_dir = Path(str(app.config.get("core.data_dir", "./data")))
    app.identity = load_or_create(data_dir)
    app.whitelist = Whitelist(
        data_dir / "identity" / "whitelist.json",
        on_revoke=lambda fp: app.registry.revoke_fingerprint(fp) if getattr(app, "registry", None) else None,
    )
    app.pairing = PairingStore(
        data_dir / "pairing.sqlite3",
        existing_fingerprint=lambda module_id, instance_id: app.whitelist.fingerprint_for(module_id, instance_id or "*"),
    )
