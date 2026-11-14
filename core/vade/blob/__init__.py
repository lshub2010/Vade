"""Attachment upload channel (RFC-0004 §4, front half).

`install(app)` follows the component contract: core discovers this package by
scan and calls it, then the data plane mounts `app.blob` on `/ui/blob*`.
Disabled (`attach.enabled=false`) means the routes do not exist at all (404),
not "exist but refuse" — a disabled feature should not be an oracle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .auth import CSRF_COOKIE, CSRF_HEADER, UI_COOKIE, Authenticator, Principal
from .http import BEGIN_BODY_MAX, PART_BODY_SLACK, ROUTES, BlobApp, build_from_config
from .service import BlobService
from .settings import AttachSettings

__all__ = [
    "install",
    "BlobApp",
    "BlobService",
    "AttachSettings",
    "Authenticator",
    "Principal",
    "ROUTES",
    "BEGIN_BODY_MAX",
    "PART_BODY_SLACK",
    "UI_COOKIE",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "build_from_config",
]


def _audit_sink(app: Any):
    """Lazily resolve the audit adapter: component install order is alphabetical,
    and `audit` happens to come first today — don't bake that in."""

    def sink(event: Any) -> None:
        adapter = getattr(app, "audit", None)
        record = getattr(adapter, "record", None)
        if callable(record):
            record(event)

    return sink


def install(app: Any) -> None:
    if getattr(app, "blob", None) is not None:
        return
    if getattr(app, "config", None) is None:
        return
    settings = AttachSettings.from_config(app.config)
    if not settings.enabled:
        app.blob = None
        return

    data_dir = Path(str(app.config.get("core.data_dir", "./data")))
    service = BlobService(data_dir, settings, audit=_audit_sink(app))
    origins = tuple(app.config.get("server.ui_origins", []) or [])
    blob = BlobApp(service, origins=origins)
    # Startup reconciliation (§4.7): files without rows go to `blobs/orphan/`,
    # rows without files become `missing` so GET can answer 410 instead of 404.
    try:
        service.reconcile()
    except Exception:  # pragma: no cover - a broken store must not abort startup
        pass
    app.blob = blob
