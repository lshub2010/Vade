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
from .registry.table import Registry
from .slots import install_slots

__all__ = ["App", "create_app", "install_components", "INFRA", "REQUIRED"]

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
            existing_fingerprint=lambda module_id, instance_id: self.whitelist.fingerprint_for(module_id, "*")
            if self.whitelist
            else None,
        )

        install_slots(self)
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
    def _close_connection(self, connection_id: str) -> None:
        app_connections = getattr(self.transport, "CONNECTIONS", None)
        if app_connections and connection_id in app_connections:
            app_connections[connection_id].close(1001, "closed by core")

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
            identity = load_or_create(data_dir)
        else:
            identity = self.identity
        return identity.fp8


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
