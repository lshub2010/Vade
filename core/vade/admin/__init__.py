"""Admin plane component (C5a): context assembly + `install(app)`.

`install` never opens a listener and never touches the filesystem beyond the
core identity key — starting the admin server is an explicit `vade.admin.server.run`
call from C2's entry point / `core admin` CLI. That keeps tests and the data
plane from accidentally exposing `/admin`.
"""

from __future__ import annotations

from typing import Any

from ..logging import get_logger
from .app import (
    COOKIE_NAME, CONFIG_ITEMS, AdminConfig, AdminContext, create_admin_app,
    login_message, refresh_message,
)
from .auth import ChallengeStore, SessionStore, hash_password, verify_password
from .devices import DeviceStore
from .identity import CoreIdentity, load_or_create
from .setup import SetupService
from .setup_token import ensure_setup_token, mask
from .views import to_public_view

__all__ = [
    "install", "AdminConfig", "AdminContext", "create_admin_app", "COOKIE_NAME",
    "CONFIG_ITEMS", "DeviceStore", "SessionStore", "ChallengeStore", "SetupService",
    "CoreIdentity", "load_or_create", "ensure_setup_token", "mask",
    "hash_password", "verify_password", "to_public_view", "login_message", "refresh_message",
]

log = get_logger("vade.admin")


def install(app: Any) -> None:
    if getattr(app, "admin", None) is not None:
        return
    try:
        ctx = AdminContext.from_app(app)
    except Exception as exc:  # pragma: no cover - a broken data dir must not kill the core
        log.warning("admin plane not assembled: %s", type(exc).__name__,
                    extra={"code": None, "msg": "admin_install_failed"})
        return
    app.admin = ctx
    # The port C3's `approval/decide` handler resolves against, when policy is
    # installed after admin (component order is alphabetical: admin < policy).
    if getattr(app, "approvals", None) is not None and ctx.approvals is None:
        ctx.approvals = app.approvals
    if getattr(app, "audit_writer", None) is not None and ctx.audit is None:
        ctx.audit = app.audit_writer
