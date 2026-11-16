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
import inspect
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
        #: `router.ModuleCaller` protocol implementation, assigned by `startup()`
        #: (C2) so C3's `install()` can build the Router instead of None.
        self.module_caller: Any = None
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
            paired_fingerprint=self._paired_fingerprint,
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
        # The core->module RPC client is a transport capability (it needs the
        # live connection table), so it is assembled here, before
        # `install_components` builds C3's Router. Without it the router is None
        # and every turn answers "no capability" — the gap the review found.
        from .router.caller import ConnectionCaller

        self.module_caller = ConnectionCaller(self._connections)
        self.started = True

    async def shutdown(self) -> None:
        if self.pairing is not None:
            self.pairing.close()
        # RFC-0004: the blob store holds a sqlite handle *and* a GC task; close
        # both with the app so a restart does not depend on process exit. The
        # async closer is preferred because it awaits the cancelled sweeper
        # instead of leaving a pending task behind (`BlobApp` exposes both:
        # `aclose` awaits, `close` is the sync fallback).
        blob = getattr(self, "blob", None)
        if blob is not None:
            closer = getattr(blob, "aclose", None) or getattr(blob, "close", None)
            if callable(closer):
                result = closer()
                if inspect.isawaitable(result):
                    await result
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

    def _paired_fingerprint(self, module_id: str, instance_id: str) -> str | None:
        """Registry trust input: the key paired to this exact node, if any."""
        return self.whitelist.fingerprint_for(module_id, instance_id) if self.whitelist is not None else None

    def _revoke_connection(self, fingerprint: str, removed_ids: tuple[str, ...] = ()) -> None:
        if self.registry is not None:
            self.registry.revoke_fingerprint(fingerprint)
        # RFC-0016 §5.6-17 / Q4 (RFC default, NOT 拍板): unbinding a device
        # kills its minted blob tokens in the same stroke — without this the
        # tokens of an unbound device stay valid until their TTL ceiling
        # (900 s), and a revoked pairing could keep uploading. `removed_ids`
        # are the deleted `module#instance` names, i.e. exactly the
        # `device_id` values tokens were minted against (§5.1-1).
        blob = getattr(self, "blob", None)
        service = getattr(blob, "service", None) if blob is not None else None
        if service is not None:
            for device_id in removed_ids:
                service.revoke_device(device_id)

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

        # Three identities must agree: the key that finished the handshake, the
        # pairing record that key belongs to, and the manifest claiming a slot.
        # `device_id` is set by the transport from the verified key, never from
        # anything the peer sent (`transport/server.py::_bind_device_identity`).
        # None therefore means "no pairing" and this must fail closed — a
        # paired module A declaring itself as B would otherwise inherit B's
        # tools and approval surface (review D1).
        device_id = getattr(connection, "device_id", None)
        if not device_id:
            raise VadeError(
                "PERMISSION_DENIED",
                "module/register requires a paired module identity on this connection",
                fatal=True,
            )
        claimed = f"{manifest.module_id}#{manifest.instance_id}"
        if str(device_id) != claimed:
            raise VadeError(
                "PERMISSION_DENIED",
                f"connection is paired as {device_id!r}, manifest claims {claimed!r}",
                fatal=True,
            )

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
            # A response is not a request: `Connection` routes it to its own
            # pending-request table before this handler is ever called.
            return None
        if method == "module/register":
            return app.register_module(connection, params)
        if method == "module/capabilities.update":
            return app.update_module_capabilities(connection, params)
        if method == "module/heartbeat":
            return {}
        # C3 publishes `app.session_handlers` (ui/attach, ui/submit, ...). One
        # lookup here is the whole wire; otherwise ui/* is NOT_FOUND on a real
        # socket and T6 only exists in unit tests.
        session_handlers = getattr(app, "session_handlers", None) or {}
        if method in session_handlers:
            try:
                return await session_handlers[method](connection, params)
            except VadeError as exc:
                # RFC-0016 (found by the e2e ws test): a provider-published
                # handler that raises VadeError must degrade to the C-04 error
                # envelope, exactly the way `SessionRPC.dispatch` converts its
                # own (`session/rpc.py:61-74`, `data` rides along). Letting it
                # propagate closes the socket (`connection.py:170` catches it
                # one frame up) instead of answering the client — a refused
                # mint (cookie session / no identity) would look like a
                # dropped connection and the client would retry forever.
                return RpcError(exc.code, exc.message, dict(exc.data) if exc.data else None)
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
