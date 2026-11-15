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
        # RFC-0016 §5.6-17: the on_revoke contract is (fingerprint,
        # removed_ids). Delegate to the App hook so unbind closes live
        # connections AND purges the device's blob tokens — one wiring, not
        # two that can drift.
        on_revoke=lambda fp, removed: app._revoke_connection(fp, removed),
    )
    app.pairing = PairingStore(
        data_dir / "pairing.sqlite3",
        existing_fingerprint=lambda module_id, instance_id: app.whitelist.fingerprint_for(module_id, instance_id or "*"),
    )
