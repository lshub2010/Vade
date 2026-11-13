"""Process lifecycle and component assembly.

Component contract (all batches): a package exposes `def install(app) -> None`
in its **`__init__.py`**. Discovery is by module scan, never a hardcoded name
list: a fixed tuple silently skips late batches (C3b's `skill`, C3d's
`events`) and `except ImportError: continue` hides it.

Missing `session`/`policy`/`audit` in strict mode aborts startup (fail-closed).
`strict` is the inverse of `[dev].allow_missing_components`.

The four slots C3 owns are created by `slots.install_slots(app)`.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

from . import logging as vade_logging
from .config import Config, load_config
from .errors import VadeError
from .identity.pairing import PairingStore
from .identity.store import Identity, load_or_create
from .identity.whitelist import Whitelist
from .registry.api import RegistryAPI
from .registry.manifest import ManifestError, validate_manifest
from .registry.table import Registry
from .slots import install_slots
from .transport.connection import RpcError

__all__ = ["App", "create_app", "install_components", "make_rpc_handler", "INFRA", "REQUIRED"]

INFRA = {"transport", "crypto_gateway", "registry", "identity"}
REQUIRED = {"session", "policy", "audit"}

log = vade_logging.get_logger("vade.app")


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.identity: Identity | None = None
        self.whitelist: Whitelist | None = None
        self.pairing: PairingStore | None = None
        self.registry: Registry | None = None
        self.registry_api: RegistryAPI | None = None
        self.transport: Any = None
        self.crypto_gateway: Any = None
        self.heartbeat_interval_s = float(config.get("core.heartbeat_interval_s", 10))
        self.prompt_hooks: list[Any] = []
        self.tool_failure_hooks: list[Any] = []
        self.on_session_end: list[Any] = []
        self.approval_port: Any = None
        self.started = False

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        cfg = self.config
        vade_logging.setup_logging(str(cfg.get("core.log_level", "info")))
        data_dir = Path(str(cfg.get("core.data_dir", "./data")))

        self.identity = load_or_create(data_dir)
        log.info(
            "core identity loaded",
            extra={"code": None, "msg": f"fingerprint={self.identity.fp8}"},
        )
        self.registry = Registry(
            heartbeat_interval_s=self.heartbeat_interval_s,
            close_connection=self._close_connection,
        )
        self.registry_api = RegistryAPI(self.registry)
        self.whitelist = Whitelist(data_dir / "identity" / "whitelist.json", on_revoke=self._revoke_connection)
        self.pairing = PairingStore(
            data_dir / "pairing.sqlite3",
            existing_fingerprint=self._existing_module_fingerprint,
        )

        install_slots(self)
        # Import first, then let the package replace the attribute with its
        # facade. `install()` used to early-return because `app.transport` was
        # already the module object, so the facade dict (and its connection
        # table) never existed.
        self.transport = importlib.import_module("vade.transport")
        if hasattr(self.transport, "install"):
            self.transport.install(self)
        self.crypto_gateway = importlib.import_module("vade.crypto_gateway")
        if hasattr(self.crypto_gateway, "install"):
            self.crypto_gateway.install(self)
        self.started = True

    async def shutdown(self) -> None:
        if self.pairing is not None:
            self.pairing.close()
        self.started = False

    # ------------------------------------------------------------------ hooks
    def _connections(self) -> dict[str, Any]:
        """Live connection table, whichever shape `transport` currently has."""
        transport = self.transport
        if isinstance(transport, dict):
            return transport.get("connections") or {}
        return getattr(transport, "CONNECTIONS", None) or {}

    def _close_connection(self, connection_id: str) -> None:
        """Called by `Registry` from a sync (admin) thread.

        `Connection.close` is async, so the old direct call only built a
        coroutine and closed nothing. `request_close` schedules the real close
        on the connection's own loop.
        """
        connection = self._connections().get(connection_id)
        if connection is not None:
            connection.request_close(1001, "closed by core")

    def _existing_module_fingerprint(self, module_id: str) -> str | None:
        """Constraint 5 input: the key already paired to this module, if any."""
        return self.whitelist.fingerprint_for(module_id) if self.whitelist is not None else None

    def _revoke_connection(self, fingerprint: str) -> None:
        if self.registry is not None:
            self.registry.revoke_fingerprint(fingerprint)

    def print_fingerprint(self) -> str:
        """ONLY the core identity fp8 (`SHA-256(core_pub)[:16]`).

        The TLS SPKI fingerprint is a different value and a different command;
        mixing them makes the QR code comparison fail for the user.
        """
        if self.identity is None:
            data_dir = Path(str(self.config.get("core.data_dir", "./data")))
            if not (data_dir / "core.key").exists():
                # A read-only query must not create key material as a side
                # effect; `load_or_create` on an existing file only loads.
                raise VadeError("NOT_FOUND", "core identity does not exist yet; start the core once to create data/core.key")
            self.identity = load_or_create(data_dir)
        return self.identity.fp8

    # ------------------------------------------------------------------ module RPC
    def _peer_fingerprint(self, connection: Any) -> str:
        crypto = getattr(connection, "crypto", None)
        fingerprint = getattr(crypto, "remote_fingerprint", None) if crypto is not None else None
        if not fingerprint:
            raise VadeError("PERMISSION_DENIED", "module RPC requires an authenticated Noise session", fatal=True)
        return str(fingerprint)

    def register_module(self, connection: Any, params: Any) -> Any:
        """`module/register`: validate first, then place the node in the table.

        Rejections come back as a JSON-RPC error on the same connection (the
        module may fix the manifest and retry); only an unauthenticated caller
        is fatal.
        """
        if self.registry is None:
            raise VadeError("INTERNAL", "registry is not available")
        try:
            manifest = validate_manifest(params if params is not None else {})
        except ManifestError as exc:
            return RpcError("MANIFEST_INVALID", exc.message)
        except VadeError as exc:
            return RpcError(exc.code, exc.message)

        kind = "device" if manifest.role == "ui" else "module"
        auth_mode = "module_key"
        try:
            registration = self.registry.register(
                manifest,
                fingerprint=self._peer_fingerprint(connection),
                connection_id=connection.connection_id,
                kind=kind,
                auth_mode=auth_mode,
            )
        except VadeError as exc:
            return RpcError(exc.code, exc.message)
        return {
            "accepted": registration.accepted,
            "session_id": registration.session_id,
            "tool_names": list(registration.tool_names),
        }

    def update_module_capabilities(self, connection: Any, params: Any) -> Any:
        if self.registry is None:
            raise VadeError("INTERNAL", "registry is not available")
        fingerprint = self._peer_fingerprint(connection)
        try:
            manifest = validate_manifest(params if params is not None else {})
            epoch = self.registry.update_capabilities(manifest, fingerprint=fingerprint)
        except ManifestError as exc:
            return RpcError("MANIFEST_INVALID", exc.message)
        except VadeError as exc:
            return RpcError(exc.code, exc.message)
        return {"accepted": True, "cap_epoch": epoch, "tool_names": list(manifest.tool_names)}


def make_rpc_handler(app: App) -> Any:
    """JSON-RPC dispatcher for the data plane (C2 owns the wire, not the tools)."""

    async def handler(connection: Any, message: dict[str, Any]) -> Any:
        method = message.get("method")
        params = message.get("params")
        if method is None:
            return None  # a response or notification from the peer
        if method == "module/register":
            return app.register_module(connection, params)
        if method == "module/capabilities.update":
            return app.update_module_capabilities(connection, params)
        if method == "module/heartbeat":
            return {}
        return RpcError("NOT_FOUND", f"unknown method {method!r}")

    return handler


def install_components(app: App, strict: bool) -> set[str]:
    """Import every `vade.*` subpackage that exposes `install(app)`."""
    present: set[str] = set()
    package_dir = Path(__file__).parent
    for module_info in sorted(pkgutil.iter_modules([str(package_dir)]), key=lambda m: m.name):
        name = module_info.name
        if name.startswith("_") or name in INFRA:
            continue
        module = importlib.import_module(f"vade.{name}")
        install = getattr(module, "install", None)
        if callable(install):
            install(app)
            present.add(name)

    missing = REQUIRED - present
    if missing and strict:
        raise SystemExit(f"missing core components: {sorted(missing)}")
    if missing:
        log.warning("running WITHOUT %s — dev mode only", extra={"code": None, "msg": str(sorted(missing))})
    return present


def create_app(config: Config | None = None, *, config_path: str | None = None, cli: dict[str, Any] | None = None) -> App:
    cfg = config or load_config(toml_path=config_path, cli=cli)
    return App(cfg)
