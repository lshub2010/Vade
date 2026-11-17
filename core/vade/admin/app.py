"""`/admin` + `/admin/setup` + `/admin/api/*` routes, auth gates, whitelist views.

One ASGI app, one listener (see `server.py`). Everything here assumes it is *not*
reachable from the data plane: `GET :8443/admin` must be 404 (C2 owns that).

Session model (31-C5a §T4 ⑤):

===========  ==========================================  ===================
mode         credential                                  permission
===========  ==========================================  ===================
``device``   ``Authorization: Bearer``                   full
``browser``  ``__Host-ac_admin`` cookie                  read-only (403 on
                                                         non-GET business ops)
===========  ==========================================  ===================

`browser` is not a lesser implementation, it is the honest answer: a browser has
no raw signing API, so it cannot supply the second factor, so it cannot write.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..errors import VadeError, http_status
from ..identity.pairing import Purpose
from . import bind as bind_service, setup_token
from .auth import (
    ChallengeStore, PasswordPolicyError, SessionStore, hash_password, is_loopback,
    normalize_pubkey, origin_allowed, validate_password, verify_password,
)
from .devices import DeviceStore
from .identity import CoreIdentity, load_or_create
from .setup import SetupService
from .skills import build_routes as skill_routes
from .stream import EventStream
from .views import to_public_view

__all__ = [
    "AdminConfig", "AdminContext", "create_admin_app", "login_message", "refresh_message",
    "COOKIE_NAME", "CSRF_HEADER",
]

COOKIE_NAME = "__Host-ac_admin"
SETUP_COOKIE = "__Host-ac_setup"
CSRF_HEADER = "x-csrf-token"
#: 31-C5a §T4 ⑤ exemption list, exactly: the only paths a browser cookie may
#: reach with a write method. `logout-all` and `password` are deliberately NOT
#: here — both are destructive and device-only. Adding a path to this set is
#: the one way to widen browser writes, and a test pins the set.
READONLY_EXEMPT = frozenset({
    "/admin/api/login", "/admin/api/login/challenge", "/admin/api/logout",
    "/admin/api/token/refresh", "/admin/api/health",
})
#: First-run setup and binding happen before any session exists; `guard()` is
#: never called there. Declared next to the exemption list so every path that
#: escapes the read-only rule lives in one place.
PREAUTH_PREFIXES = ("/admin/setup", "/admin/bind")

#: Every `/admin` request body is capped at 1 MiB. The largest legitimate body is
#: a bind envelope (a 65-byte key, a signature and a password: a few hundred
#: bytes); everything else is a handful of config keys. The cap exists because
#: the pre-auth routes (`/admin/setup`, `/admin/setup/unlock`, `/admin/bind`) are
#: reachable without a session, so an unbounded JSON parse there is free memory
#: for anyone who can reach the port. Enforced twice: `Content-Length` is refused
#: before a byte is read (`GuardedApp`), and `_json` stops a chunked/lying body
#: while streaming it.
MAX_BODY_BYTES = 1024 * 1024


def readonly_exempt(path: str) -> bool:
    """True iff a browser session may issue a non-GET request to `path`."""
    return path in READONLY_EXEMPT or path.startswith(PREAUTH_PREFIXES)

#: §7.9 whitelist. Anything not listed here is `UNKNOWN_CONFIG_KEY` on PUT and
#: absent from GET — the config page cannot become a config-file editor.
CONFIG_ITEMS: dict[str, dict[str, Any]] = {
    "evolve.enabled": {"default": True, "type": "bool", "mode": "hot", "danger": "off"},
    "evolve.daily_token_budget": {"default": 200_000, "type": "int", "mode": "hot",
                                  "min": 0, "max": 10_000_000, "danger": "zero"},
    "evolve.nudge_interval": {"default": 10, "type": "int", "mode": "hot",
                              "min": 1, "max": 100, "danger": "low"},
    "evolve.review_interval": {"default": 20, "type": "int", "mode": "hot", "min": 1, "max": 200},
    "memory.global_limit": {"default": 5000, "type": "int", "mode": "restart", "min": 100, "max": 100_000},
    "memory.staging_ttl_days": {"default": 14, "type": "int", "mode": "restart", "min": 1, "max": 90},
    "policy.approval_timeout_s": {"default": 60, "type": "int", "mode": "hot", "min": 10, "max": 600},
    "audit.retention_days": {"default": 30, "type": "int", "mode": "restart",
                             "min": 1, "max": 3650},
    "core.log_level": {"default": "info", "type": "enum", "mode": "hot",
                       "values": ["debug", "info", "warning", "error"]},
    "e2e.enabled": {"default": False, "type": "bool", "mode": "restart"},
}


# --------------------------------------------------------------------- payloads
def login_message(challenge_b64: str, admin_origin: str) -> bytes:
    """`"vade-admin-v2\\n" || challenge || "\\n" || admin_origin`.

    The prefix is domain separation; the origin stops a challenge minted for one
    host from being replayed against another.
    """
    return f"vade-admin-v2\n{challenge_b64}\n{admin_origin}".encode("utf-8")


def refresh_message(jti: str, device_id: str) -> bytes:
    return f"vade-admin-v2\nrefresh\n{jti}\n{device_id}".encode("utf-8")


@dataclass(slots=True)
class AdminConfig:
    data_dir: Path
    admin_host: str = "127.0.0.1:8444"
    allowed_hosts: frozenset[str] = frozenset({"127.0.0.1:8444", "localhost:8444"})
    access_ttl_s: int = 900
    refresh_ttl_s: int = 30 * 86_400
    challenge_ttl_s: int = 60
    unlock_ttl_s: int = 600
    version: str = "1.0.0"
    e2e_enabled: bool = False
    allow_remote_readonly: bool = True
    remote_writes_require_device: bool = True

    @classmethod
    def from_app(cls, app: Any) -> "AdminConfig":
        data_dir = Path(str(app.config.get("core.data_dir", "./data")))
        # `core.admin_host` is the only config leaf for this (there is no
        # `[admin]` section in config.schema.json). Reading `admin.admin_host`
        # always fell back to the default, so `--admin-bind` and a remote
        # `core.admin_host` never reached the Host allow-list and every remote
        # request was 403 ORIGIN_MISMATCH (review P1-4).
        admin_host = str(app.config.get("core.admin_host", "127.0.0.1:8444"))
        hosts = app.config.get("admin.allowed_hosts", None) or [admin_host, "127.0.0.1:8444", "localhost:8444"]
        return cls(
            data_dir=data_dir, admin_host=admin_host, allowed_hosts=frozenset(hosts),
            access_ttl_s=int(app.config.get("admin.access_ttl_s", 900)),
            refresh_ttl_s=int(app.config.get("admin.refresh_ttl_s", 30 * 86_400)),
            version=str(app.config.get("core.version", "1.0.0")),
            e2e_enabled=bool(app.config.get("e2e.enabled", False)),
        )


@dataclass
class AdminContext:
    config: AdminConfig
    identity: CoreIdentity
    setup: SetupService
    devices: DeviceStore
    sessions: SessionStore
    challenges: ChallengeStore
    registry: Any = None
    registry_api: Any = None
    audit: Any = None
    approvals: Any = None
    pairing: Any = None
    #: C3b's `SkillLoader` (`app.skill_loader`). Resolved again in `bind_ports`
    #: because `admin` installs BEFORE `skill` (alphabetical), so it does not
    #: exist yet when this context is built.
    skills: Any = None
    #: The trust table (`vade.identity.whitelist.Whitelist`). Revoking a node
    #: must unbind it HERE as well as in the registry, because the registry's
    #: `_revoked` set is process memory: whitelist.json is what survives a
    #: restart (otherwise the same key handshakes again).
    whitelist: Any = None
    stream: EventStream = field(default_factory=EventStream)
    sessions_provider: Callable[[], int] | None = None
    version: str = "1.0.0"
    e2e_enabled: bool = False
    allow_remote_readonly: bool = True
    #: The assembled `App`, kept only so `bind_ports()` can resolve components
    #: installed AFTER this one (alphabetical order: admin < audit < policy).
    _app: Any = None
    #: unlock cookies are process-local (10 min); they die with a restart, which
    #: is the right failure mode for a QR gate.
    _unlocks: dict[str, float] = field(default_factory=dict)
    _bind_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    _invites: dict[str, str] = field(default_factory=dict)
    _config_overrides: dict[str, Any] = field(default_factory=dict)
    _layout: dict[str, Any] = field(default_factory=lambda: {"rev": 0, "nodes": []})
    _limits: dict[str, list[float]] = field(default_factory=dict)
    _global_failures: list[float] = field(default_factory=list)
    _locked_until: float = 0.0
    _rate_lock: Any = field(default_factory=lambda: __import__("threading").RLock())

    # ------------------------------------------------------------- construction
    @classmethod
    def from_app(cls, app: Any) -> "AdminContext":
        config = AdminConfig.from_app(app)
        identity = load_or_create(config.data_dir)
        return cls(
            config=config,
            identity=identity,
            setup=SetupService(config.data_dir, config.admin_host, identity.fp8),
            devices=DeviceStore(config.data_dir / "admin" / "devices.json"),
            sessions=SessionStore(access_ttl_s=config.access_ttl_s, refresh_ttl_s=config.refresh_ttl_s),
            challenges=ChallengeStore(ttl_s=config.challenge_ttl_s),
            registry=getattr(app, "registry", None),
            registry_api=getattr(app, "registry_api", None),
            audit=getattr(app, "audit_writer", None),
            approvals=getattr(app, "approvals", None),
            pairing=getattr(app, "pairing", None),
            whitelist=getattr(app, "whitelist", None),
            skills=getattr(app, "skill_loader", None),
            version=config.version,
            e2e_enabled=config.e2e_enabled,
            allow_remote_readonly=config.allow_remote_readonly,
            _app=app,
        )

    def bind_ports(self, app: Any | None = None) -> None:
        """Resolve ports owned by components that install after `admin`.

        `install_components()` is alphabetical, so `audit_writer` and
        `approvals` do not exist when this context is built. Without a second
        pass `ctx.audit`/`ctx.approvals` stayed `None` forever: the audit page
        was always empty and `approval/decide` always answered 501 (review D2).
        Idempotent, and it never overwrites a port a test injected directly.
        """
        source = app if app is not None else self._app
        if source is None:
            return
        if self.registry is None:
            self.registry = getattr(source, "registry", None)
        if self.registry_api is None:
            self.registry_api = getattr(source, "registry_api", None)
        if self.audit is None:
            self.audit = getattr(source, "audit_writer", None)
        if self.approvals is None:
            self.approvals = getattr(source, "approvals", None)
        if self.pairing is None:
            self.pairing = getattr(source, "pairing", None)
        if self.skills is None:
            self.skills = getattr(source, "skill_loader", None)

    def __post_init__(self) -> None:
        self.sessions.set_device_checker(self.devices.is_active)
        self._load_overrides()
        self.bind_ports()

    # --------------------------------------------------------------- overrides
    @property
    def overrides_path(self) -> Path:
        return Path(self.config.data_dir) / "config.overrides.json"

    def _load_overrides(self) -> None:
        try:
            self._config_overrides = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            self._config_overrides = {}

    def _save_overrides(self) -> None:
        path = self.overrides_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._config_overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------- audit
    def write_audit(self, **event: Any) -> str | None:
        if self.audit is None:
            return None
        try:
            return self.audit.record(event)
        except Exception:
            return None


# ----------------------------------------------------------------------- helpers
def _client(request: Request) -> str | None:
    return request.client.host if request.client else None


def _loopback(request: Request) -> bool:
    return is_loopback(request.headers.get("host"), _client(request))


def _error(exc: VadeError, status: int | None = None) -> JSONResponse:
    return JSONResponse(
        {"code": exc.code, "message": exc.message},
        status_code=status if status is not None else http_status(exc.code),
    )


async def _json(request: Request, *, required: bool = True) -> dict[str, Any]:
    """Parse a JSON object body, refusing anything over `MAX_BODY_BYTES`.

    The `Content-Length` check happens before reading, and the streaming cap
    covers a chunked upload or a header that lies — a pre-auth route must not be
    able to make the core buffer an arbitrary amount of memory.
    """
    if _declared_too_large(request.headers.get("content-length")):
        raise _too_large()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _too_large()
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        if required:
            raise VadeError("INVALID_ARGUMENT", "request body must be a JSON object")
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise VadeError("INVALID_ARGUMENT", "request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise VadeError("INVALID_ARGUMENT", "request body must be a JSON object")
    return data


def _too_large() -> VadeError:
    return VadeError("PAYLOAD_TOO_LARGE", "request body exceeds 1 MiB")


def _declared_too_large(value: str | None) -> bool:
    """`Content-Length` above the cap — refuse before the body is read."""
    if value is None:
        return False
    try:
        return int(value) > MAX_BODY_BYTES
    except ValueError:
        # A malformed length never reaches here in practice (the server rejects
        # it); the streaming cap in `_json` is the real boundary either way.
        return False


def _node_ids(node_id: str) -> tuple[str, str]:
    if not node_id or "#" not in node_id:
        raise VadeError("NOT_FOUND", "unknown node")
    module_id, instance_id = node_id.split("#", 1)
    return module_id, instance_id


def _audit_id(event: Any) -> str | None:
    """`AuditWriter.record` returns the ULID directly; tolerate both shapes."""
    if isinstance(event, str):
        return event or None
    extra = event.get("extra") if isinstance(event, Mapping) else None
    return extra.get("audit_id") if isinstance(extra, Mapping) else None


class RateLimiter:
    """Log hygiene, not a security boundary: 256-bit tokens cannot be guessed."""

    def __init__(self, ctx: AdminContext) -> None:
        self.ctx = ctx

    def _bucket(self, key: str, window: float) -> list[float]:
        now = time.time()
        with self.ctx._rate_lock:
            items = [t for t in self.ctx._limits.get(key, []) if now - t < window]
            self.ctx._limits[key] = items
            return items

    def hit(self, key: str, window: float) -> int:
        items = self._bucket(key, window)
        items.append(time.time())
        return len(items)

    def locked(self) -> bool:
        return time.time() < self.ctx._locked_until

    def password_failure(self, ip: str | None) -> None:
        with self.ctx._rate_lock:
            now = time.time()
            self.ctx._global_failures = [t for t in self.ctx._global_failures if now - t < 300]
            self.ctx._global_failures.append(now)
            if len(self.ctx._global_failures) >= 20:
                self.ctx._locked_until = now + 900
                self.ctx.write_audit(
                    kind="admin_lockout", status="locked", code="ADMIN_LOCKED",
                    actor=None, extra={"reason": "20 failures in 5 minutes"},
                )

    def check_ip(self, ip: str | None) -> None:
        if self.locked():
            raise VadeError("ADMIN_LOCKED", "too many failed attempts; try again later")
        key = f"pw:{ip or 'unknown'}"
        if len(self._bucket(key, 300)) >= 5:
            raise VadeError("ADMIN_LOCKED", "too many failed attempts; try again in 5 minutes")


# ------------------------------------------------------------------------ routes
def create_admin_app(ctx: AdminContext, *, static_dist: Path | str | None = None) -> Starlette:
    limiter = RateLimiter(ctx)
    routes: list[Route] = []

    # ---------------------------------------------------------------- auth core
    def guard(request: Request, *, readonly_check: bool = True) -> Any:
        host = request.headers.get("host")
        if host not in ctx.config.allowed_hosts:
            ctx.write_audit(kind="admin.rejected", status="denied", code="ORIGIN_MISMATCH",
                            extra={"reason": "host not allowed", "host": host, "path": request.url.path})
            raise VadeError("ORIGIN_MISMATCH", "Host is not the configured admin host")
        origin = request.headers.get("origin")
        if origin and not origin_allowed(origin, ctx.config.admin_host):
            ctx.write_audit(kind="admin.rejected", status="denied", code="ORIGIN_MISMATCH",
                            extra={"reason": "origin mismatch", "origin": origin, "path": request.url.path})
            raise VadeError("ORIGIN_MISMATCH", "Origin does not match the admin plane")
        fetch_site = request.headers.get("sec-fetch-site")
        if not origin:
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                ctx.write_audit(kind="admin.rejected", status="denied", code="ORIGIN_MISMATCH",
                                extra={"reason": "cross-site fetch", "path": request.url.path})
                raise VadeError("ORIGIN_MISMATCH", "cross-site requests are refused")
            if not fetch_site and not request.headers.get("authorization"):
                # No Origin, no Sec-Fetch-Site and no bearer: a cookie-driven
                # request from an unknown context. curl for the API must use a
                # bearer token.
                raise VadeError("ORIGIN_MISMATCH", "missing Origin; use a bearer token")

        session = None
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            session = ctx.sessions.resolve(auth[7:].strip())
        elif request.cookies.get(COOKIE_NAME):
            session = ctx.sessions.resolve(request.cookies.get(COOKIE_NAME))
        if session is None:
            raise VadeError("AUTH_FAILED", "authentication failed")
        if readonly_check and session.mode == "browser" and request.method not in {"GET", "HEAD"}:
            if not readonly_exempt(request.url.path):
                ctx.write_audit(kind="admin.rejected", status="denied", code="READ_ONLY_SESSION",
                                actor=session.device_id or "browser", method=request.method,
                                extra={"path": request.url.path})
                raise VadeError("READ_ONLY_SESSION", "browser sessions are read-only; use the Vade App")
        return session

    def require_initialized() -> None:
        if not ctx.setup.initialized():
            raise VadeError("NOT_FOUND", "admin plane is not initialised")

    def actor_of(session: Any) -> str:
        return getattr(session, "device_id", None) or "loopback_admin"

    # -------------------------------------------------------------------- setup
    async def get_setup(request: Request) -> Response:
        if ctx.setup.initialized():
            return RedirectResponse("/login", status_code=302)
        loopback = _loopback(request)
        unlocked = False
        token = request.cookies.get(SETUP_COOKIE)
        if token:
            expiry = ctx._unlocks.get(token, 0.0)
            unlocked = expiry > time.time()
        try:
            body = ctx.setup.state(loopback=loopback, unlocked=unlocked)
        except VadeError as exc:
            return _error(exc)
        return JSONResponse(body)

    async def post_setup_unlock(request: Request) -> Response:
        if ctx.setup.initialized():
            raise VadeError("NOT_FOUND", "admin plane is already initialised")
        ip = _client(request)
        if not _loopback(request):
            # Share the pairing ledger so setup-token guessing and pair-code
            # guessing draw from the same per-IP and global budget.
            pairing = getattr(ctx, "pairing", None)
            if pairing is not None and hasattr(pairing, "begin_setup_attempt"):
                pairing.begin_setup_attempt(ip)
            else:
                if len(limiter._bucket(f"unlock:{ip}", 300)) >= 5:
                    raise VadeError("ADMIN_LOCKED", "too many unlock attempts")
                limiter.hit(f"unlock:{ip}", 300)
        body = await _json(request)
        provided = body.get("setup_token")
        payload = ctx.setup.unlock(provided if isinstance(provided, str) else "")
        token = secrets.token_urlsafe(32)
        ctx._unlocks[token] = time.time() + ctx.config.unlock_ttl_s
        ctx.write_audit(kind="setup.attempt", status="ok", code=None, actor=None,
                        extra={"source": "loopback" if _loopback(request) else "token", "ip": ip,
                               "action": "unlock"})
        response = JSONResponse(payload)
        # Only the setup routes ever see this cookie; it is not an admin session.
        response.set_cookie(
            SETUP_COOKIE, token, max_age=ctx.config.unlock_ttl_s, secure=True,
            httponly=True, samesite="strict", path="/",
        )
        return response

    async def post_setup(request: Request) -> Response:
        if ctx.setup.initialized():
            raise VadeError("NOT_FOUND", "admin plane is already initialised")
        body = await _json(request)
        loopback = _loopback(request)
        if not loopback:
            pairing = getattr(ctx, "pairing", None)
            if pairing is not None and hasattr(pairing, "begin_setup_attempt"):
                pairing.begin_setup_attempt(_client(request))
            else:
                limiter.hit(f"setup:{_client(request)}", 300)
        try:
            result = ctx.setup.initialize(
                password=str(body.get("password") or ""),
                setup_token_value=body.get("setup_token"),
                loopback=loopback, ip=_client(request),
                pubkey=body.get("pubkey"), alg=body.get("alg"),
                device_store=ctx.devices,
            )
        except VadeError as exc:
            ctx.write_audit(kind="setup.attempt", status="denied", code=exc.code, actor=None,
                            extra={"source": "loopback" if loopback else "token", "ip": _client(request)})
            raise
        ctx.write_audit(kind="setup.attempt", status="ok", code=None, actor=None,
                        extra={"source": "loopback" if loopback else "token", "ip": _client(request)})
        return JSONResponse({"ok": True, **result})

    # --------------------------------------------------------------------- bind
    async def bind_info(request: Request) -> Response:
        ticket = request.headers.get("x-bind-ticket") or ""
        entry = ctx.setup.peek_ticket(ticket)
        if entry is None:
            raise VadeError("BIND_UNAUTHORIZED", "bind ticket is unknown, expired or already used")
        session = bind_service.begin_hello(ctx, ticket, entry, ip=_client(request))
        kex_b64 = base64.b64encode(session["kex_public"]).decode()
        challenge_b64 = base64.b64encode(session["challenge"]).decode()
        hello_sig = ctx.identity.hello_signature(
            kex_pub=kex_b64, challenge=challenge_b64, ticket=ticket, admin_host=ctx.config.admin_host,
        )
        return JSONResponse({
            "core_pub": ctx.identity.public_b64(), "fp8": ctx.identity.fp8,
            "kex_pub": kex_b64, "challenge": challenge_b64,
            "setup_required": not ctx.setup.initialized(),
            "expires_at": int(entry["expires_at"]), "hello_sig": hello_sig,
        })

    async def bind(request: Request) -> Response:
        """§7.12.3 ③–⑤: verify the envelope, then consume the ticket.

        Everything that can reject the request happens in `bind.complete()`
        before the ticket is touched, so a failed attempt leaves the QR usable.
        The plaintext password only exists inside that call.
        """
        body = await _json(request)
        payload = bind_service.complete(ctx, body, ip=_client(request))
        return JSONResponse(payload)

    # --------------------------------------------------------------------- auth
    async def login_challenge(request: Request) -> Response:
        require_initialized()
        limiter.hit(f"challenge:{_client(request)}", 60)
        if len(limiter._bucket(f"challenge:{_client(request)}", 60)) > 30:
            raise VadeError("OVERLOADED", "too many challenge requests")
        await _json(request, required=False)
        issued = ctx.challenges.issue(_client(request), ctx.devices.version)
        issued["devices"] = ctx.devices.public_list()
        return JSONResponse(issued)

    async def login(request: Request) -> Response:
        require_initialized()
        ip = _client(request)
        limiter.check_ip(ip)
        body = await _json(request)
        challenge_id = str(body.get("challenge_id") or "")
        challenge_raw = ctx.challenges.consume(challenge_id, ip, ctx.devices.version)
        challenge_b64 = base64.b64encode(challenge_raw).decode()
        password = str(body.get("password") or "")
        stored = ctx.setup.password_hash()
        if not verify_password(password, stored):
            limiter.hit(f"pw:{ip}", 300)
            limiter.password_failure(ip)
            ctx.write_audit(kind="admin.login", status="denied", code="AUTH_FAILED", actor=None,
                            extra={"ip": ip, "reason": "password"}, )
            raise VadeError("AUTH_FAILED", "password or device is incorrect")
        # The password is proven, so legacy scrypt material (or Argon2id with
        # weaker parameters) can be replaced in place. No offline migration.
        if ctx.setup.rehash_password(password, stored):
            ctx.write_audit(kind="admin.password_rehashed", status="ok", actor=None,
                            extra={"ip": ip, "to": "argon2id"})

        device_id = body.get("device_id")
        signature = body.get("signature")
        origin = request.headers.get("origin") or f"https://{ctx.config.admin_host}"
        if device_id and signature:
            if not isinstance(device_id, str) or not isinstance(signature, str):
                raise VadeError("AUTH_FAILED", "password or device is incorrect")
            if not ctx.devices.verify(device_id, login_message(challenge_b64, origin), signature):
                limiter.hit(f"pw:{ip}", 300)
                limiter.password_failure(ip)
                ctx.write_audit(kind="admin.login", status="denied", code="AUTH_FAILED",
                                actor=device_id, extra={"ip": ip, "reason": "signature"})
                raise VadeError("AUTH_FAILED", "password or device is incorrect")
            ctx.devices.mark_used(device_id)
            tokens = ctx.sessions.issue(mode="device", device_id=device_id, ip=ip)
            ctx.write_audit(kind="admin.login", status="ok", code=None, actor=device_id,
                            extra={"ip": ip, "mode": "device"})
            return JSONResponse(tokens)

        loopback = _loopback(request)
        wants_cli = (request.headers.get("x-ac-client", "").lower() == "cli")
        if loopback and wants_cli:
            tokens = ctx.sessions.issue(mode="device", ip=ip)
            ctx.write_audit(kind="admin.login", status="ok", code=None, actor="loopback_admin",
                            extra={"ip": ip, "mode": "device-cli"})
            return JSONResponse(tokens)
        if not loopback and not ctx.allow_remote_readonly:
            raise VadeError("AUTH_FAILED", "password or device is incorrect")
        tokens = ctx.sessions.issue(mode="browser", ip=ip)
        ctx.write_audit(kind="admin.login", status="ok", code=None, actor="browser",
                        extra={"ip": ip, "mode": "browser", "loopback": loopback})
        # Bearer never goes into a cookie; a browser session never receives a
        # token in the body (XSS would read it out of the DOM/response).
        response = JSONResponse({"mode": "browser", "expires_in": ctx.config.access_ttl_s,
                                 "csrf_token": tokens["csrf_token"]})
        response.set_cookie(COOKIE_NAME, tokens["access_token"], max_age=ctx.config.access_ttl_s,
                            secure=True, httponly=True, samesite="strict", path="/")
        return response

    async def token_refresh(request: Request) -> Response:
        require_initialized()
        body = await _json(request)
        refresh_token = str(body.get("refresh_token") or "")
        entry = ctx.sessions.peek_refresh(refresh_token)
        if entry is None:
            raise VadeError("AUTH_FAILED", "refresh token is unknown or expired")
        device_id = entry.get("device_id")
        signature = body.get("signature")
        if not device_id or not signature:
            # Refreshing without the device key would make a stolen refresh
            # token enough to keep a session alive forever.
            ctx.sessions.drop_refresh(refresh_token)
            raise VadeError("AUTH_FAILED", "refresh requires a device signature")
        if not ctx.devices.verify(str(device_id), refresh_message(entry["jti"], str(device_id)), str(signature)):
            ctx.sessions.drop_refresh(refresh_token)
            raise VadeError("AUTH_FAILED", "refresh signature is invalid")
        tokens = ctx.sessions.refresh(refresh_token, device_id=str(device_id))
        if tokens is None:
            raise VadeError("AUTH_FAILED", "refresh token is unknown or expired")
        ctx.devices.mark_used(str(device_id))
        return JSONResponse(tokens)

    async def logout(request: Request) -> Response:
        session = guard(request, readonly_check=False)
        ctx.sessions.revoke(session.token)
        ctx.write_audit(kind="admin.logout", status="ok", actor=actor_of(session))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
        return response

    async def logout_all(request: Request) -> Response:
        # Destructive: one stolen browser cookie must not be able to kick every
        # enrolled device offline (a DoS, not an escalation). Device-only.
        session = guard(request)
        count = ctx.sessions.revoke_all()
        ctx.write_audit(kind="admin.logout_all", status="ok", actor=actor_of(session),
                        extra={"sessions": count})
        response = JSONResponse({"ok": True, "revoked": count})
        response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
        return response

    async def change_password(request: Request) -> Response:
        # Device-only: the design gives the browser no second factor, so a
        # password change must come from the App. `guard()` 403s a browser
        # session here, which also means the browser never receives tokens.
        session = guard(request)
        body = await _json(request)
        old = str(body.get("old_password") or "")
        new = str(body.get("new_password") or "")
        ctx.setup.change_password(old, new)
        # Keep the caller signed in, invalidate everyone else by version.
        mode, device_id = session.mode, session.device_id
        ctx.sessions.bump_password_version()
        tokens = ctx.sessions.issue(mode=mode, device_id=device_id, ip=_client(request))
        ctx.write_audit(kind="admin.password_changed", status="ok", actor=actor_of(session))
        payload: dict[str, Any] = {"ok": True, "expires_in": tokens["expires_in"], "mode": mode}
        response = JSONResponse(payload)
        if mode == "browser":
            # Unreachable through `guard()`, but the invariant is load-bearing:
            # a browser response body never carries a token (XSS can read a
            # body; HttpOnly keeps a cookie out of reach). Keep the new cookie
            # and drop the body tokens rather than trusting the guard forever.
            response.set_cookie(COOKIE_NAME, tokens["access_token"], max_age=ctx.config.access_ttl_s,
                                secure=True, httponly=True, samesite="strict", path="/")
            return response
        payload["access_token"] = tokens["access_token"]
        payload["refresh_token"] = tokens["refresh_token"]
        return JSONResponse(payload)

    # ------------------------------------------------------------------ devices
    async def list_devices(request: Request) -> Response:
        guard(request)
        return JSONResponse({"items": ctx.devices.public_list(), "max": ctx.devices.max_devices})

    async def add_device(request: Request) -> Response:
        session = guard(request)
        body = await _json(request)
        password = str(body.get("password") or "")
        # step-up: the only load-bearing check on this path. A logged-in
        # attacker with a stolen cookie must not be able to enrol a new key.
        if not verify_password(password, ctx.setup.password_hash()):
            raise VadeError("AUTH_FAILED", "password or device is incorrect")
        alg, raw = normalize_pubkey(str(body.get("pubkey") or ""), body.get("alg"))
        device = ctx.devices.add(
            pubkey_b64=base64.b64encode(raw).decode(), alg=alg,
            label=str(body.get("label") or "device")[:64], ip=_client(request),
            hardware_backed=bool(body.get("hardware_backed", False)),
        )
        ctx.write_audit(kind="device.added", status="ok", actor=actor_of(session),
                        extra={"device_id": device.device_id, "alg": alg, "label": device.label})
        return JSONResponse(device.public())

    async def revoke_device(request: Request) -> Response:
        session = guard(request)
        device_id = request.path_params["id"]
        body = await _json(request, required=False)
        if body.get("password") and not verify_password(str(body["password"]), ctx.setup.password_hash()):
            raise VadeError("AUTH_FAILED", "password or device is incorrect")
        if not ctx.devices.revoke(device_id):
            raise VadeError("NOT_FOUND", "unknown device")
        ctx.sessions.revoke_device(device_id)
        ctx.write_audit(kind="device.revoked", status="ok", actor=actor_of(session),
                        extra={"device_id": device_id})
        return JSONResponse({"ok": True})

    async def device_ticket(request: Request) -> Response:
        session = guard(request)
        body = await _json(request)
        if not verify_password(str(body.get("password") or ""), ctx.setup.password_hash()):
            raise VadeError("AUTH_FAILED", "password or device is incorrect")
        ticket = ctx.setup.create_ticket()
        ctx.write_audit(kind="device.ticket", status="ok", actor=actor_of(session),
                        extra={"expires_at": ticket["expires_at"]})
        return JSONResponse({
            "bind_ticket": ticket["ticket"], "bind_url": ticket["bind_url"],
            "expires_at": ticket["expires_at"],
        })

    # -------------------------------------------------------------------- nodes
    def _views() -> list[dict[str, Any]]:
        if ctx.registry is None:
            return []
        bindings = list(ctx.registry.tools().values())
        layout_nodes = {n["id"]: n for n in ctx._layout.get("nodes", []) if isinstance(n, dict) and "id" in n}
        views = []
        for record in ctx.registry.snapshot():
            names = sorted(
                b.spec.name for b in bindings
                if b.module_id == record.module_id and b.instance_id == record.instance_id
            )
            views.append(to_public_view(record, tool_names=names, layout=layout_nodes.get(record.node_id),
                                        revoked=record.health.value == "rejected"))
        return views

    def _find(node_id: str) -> Any:
        module_id, instance_id = _node_ids(node_id)
        if ctx.registry is None:
            raise VadeError("NOT_FOUND", "registry is not available")
        record = ctx.registry.get(module_id, instance_id)
        if record is None:
            raise VadeError("NOT_FOUND", "unknown node")
        return record

    def _impact(record: Any, *, changes: Mapping[str, Any]) -> dict[str, Any]:
        bindings = [b for b in (ctx.registry.tools().values() if ctx.registry else [])
                    if b.module_id == record.module_id and b.instance_id == record.instance_id]
        names = sorted(b.spec.name for b in bindings)
        affected = ctx.sessions_provider() if ctx.sessions_provider else 0
        return {"tool_count": len(names), "tool_names": names, "sessions_affected": affected,
                "changes": dict(changes)}

    async def list_nodes(request: Request) -> Response:
        guard(request)
        return JSONResponse({"items": _views()})

    async def get_node(request: Request) -> Response:
        guard(request)
        record = _find(request.path_params["id"])
        names = sorted(b.spec.name for b in ctx.registry.tools().values()
                       if b.module_id == record.module_id and b.instance_id == record.instance_id)
        layout = next((n for n in ctx._layout.get("nodes", [])
                       if isinstance(n, dict) and n.get("id") == record.node_id), None)
        return JSONResponse(to_public_view(record, tool_names=names, layout=layout,
                                           revoked=record.health.value == "rejected"))

    async def patch_node(request: Request) -> Response:
        session = guard(request)
        record = _find(request.path_params["id"])
        body = await _json(request)
        unknown = set(body) - {"enabled", "concurrency", "risk", "label"}
        if unknown:
            raise VadeError("INVALID_ARGUMENT", f"unknown fields: {sorted(unknown)}")
        changes: dict[str, Any] = {}
        if "enabled" in body:
            enabled = bool(body["enabled"])
            changes["enabled"] = enabled
        if "concurrency" in body:
            changes["concurrency"] = str(body["concurrency"])
        if "risk" in body:
            risk_map = body["risk"]
            if not isinstance(risk_map, dict):
                raise VadeError("INVALID_ARGUMENT", "risk must be an object of {tool: low|medium|high}")
            changes["risk"] = risk_map
        if "label" in body:
            changes["label"] = str(body["label"])[:64]

        impact = _impact(record, changes=changes)
        if "enabled" in changes:
            await ctx.registry_api.set_enabled(record.module_id, record.instance_id, changes["enabled"])
        if "concurrency" in changes:
            await ctx.registry_api.set_concurrency_override(record.module_id, changes["concurrency"])
        if "risk" in changes:
            for tool, risk in changes["risk"].items():
                # Raises RISK_DOWNGRADE_FORBIDDEN; a partial change is possible
                # only if the caller sends several tools, so fail on the first.
                await ctx.registry_api.set_risk_override(record.module_id, str(tool), str(risk))
        if "label" in changes:
            ctx._layout["nodes"] = [
                n for n in ctx._layout.get("nodes", []) if isinstance(n, dict) and n.get("id") != record.node_id
            ] + [{"id": record.node_id, "label": changes["label"]}]
        event = ctx.write_audit(
            kind="node.updated", status="ok", code=None, actor=actor_of(session),
            module_id=record.module_id, instance_id=record.instance_id,
            method="PATCH /admin/api/nodes/{id}",
            extra={"changes": changes, "impact": impact},
        )
        ctx.stream.publish("node.upsert", {"id": record.node_id, "changes": changes})
        return JSONResponse({"ok": True, "impact": impact, "audit_id": _audit_id(event) if event else None,
                             "id": record.node_id})

    async def revoke_node(request: Request) -> Response:
        session = guard(request)
        record = _find(request.path_params["id"])
        if ctx.registry is None:
            raise VadeError("NOT_FOUND", "registry is not available")
        closed = ctx.registry.revoke_fingerprint(record.fingerprint)
        # P1-2: the registry's `_revoked` set is process memory only, so on its
        # own this revoke is forgotten at the next start and the same key
        # handshakes again with the same (still whitelisted) entry. Unbind the
        # whitelist record too — that write is what survives the restart. Its
        # `on_revoke` callback is the App's, which also closes sockets that
        # never registered and drops the blob tokens minted for them.
        unbound: tuple[str, ...] = ()
        whitelist = getattr(ctx, "whitelist", None)
        if whitelist is not None:
            unbound = tuple(whitelist.revoke(record.fingerprint))
        event = ctx.write_audit(
            kind="node.revoked", status="ok", code=None, actor=actor_of(session),
            module_id=record.module_id, instance_id=record.instance_id,
            method="POST /admin/api/nodes/{id}/revoke",
            extra={"connections_closed": len(closed), "unbound": list(unbound)},
        )
        ctx.stream.publish("node.remove", {"id": record.node_id})
        return JSONResponse({"ok": True, "connections_closed": len(closed),
                             "unbound": list(unbound),
                             "audit_id": _audit_id(event) if event else None})

    async def disconnect_node(request: Request) -> Response:
        session = guard(request)
        record = _find(request.path_params["id"])
        await ctx.registry_api.disconnect(record.module_id, record.instance_id)
        event = ctx.write_audit(
            kind="node.disconnected", status="ok", code=None, actor=actor_of(session),
            module_id=record.module_id, instance_id=record.instance_id,
            method="POST /admin/api/nodes/{id}/disconnect", extra={},
        )
        return JSONResponse({"ok": True, "audit_id": _audit_id(event) if event else None})

    # ------------------------------------------------------------------ invites
    async def create_invite(request: Request) -> Response:
        session = guard(request)
        if ctx.pairing is None:
            raise VadeError("UNSUPPORTED", "pairing store is not available")
        body = await _json(request, required=False)
        purpose = str(body.get("purpose") or "module")
        module_id = body.get("module_id")
        ttl = int(body.get("ttl") or 600)
        if purpose == "module" and not module_id:
            raise VadeError("INVALID_ARGUMENT", "purpose=module requires module_id")
        # One code, one purpose: a shared code would leak both module identity
        # and browser session at once (31 §T4).
        code = ctx.pairing.create(Purpose(purpose), module_id=module_id, ttl=ttl) if purpose == "module" \
            else ctx.pairing.create(Purpose(purpose), ttl=ttl)
        invite_id = secrets.token_urlsafe(12)
        ctx._invites[invite_id] = code
        node_id = f"{module_id}#pending" if module_id else None
        ctx.write_audit(kind="invite.created", status="ok", code=None, actor=actor_of(session),
                        extra={"purpose": purpose, "invite_id": invite_id, "node_id": node_id})
        # Plaintext only here; the store keeps `scrypt(normalize(code))`.
        return JSONResponse({
            "invite_id": invite_id, "pair_code": code, "purpose": purpose,
            "node_id": node_id, "expires_at": int(time.time()) + ttl,
            "qr_payload": f"vade://pair?c={code}&p={purpose}",
        })

    async def delete_invite(request: Request) -> Response:
        session = guard(request)
        invite_id = request.path_params["id"]
        code = ctx._invites.pop(invite_id, None)
        if code is None or ctx.pairing is None:
            raise VadeError("NOT_FOUND", "unknown invite")
        ctx.pairing.revoke(code)
        ctx.write_audit(kind="invite.revoked", status="ok", code=None, actor=actor_of(session),
                        extra={"invite_id": invite_id})
        return JSONResponse({"ok": True})

    # -------------------------------------------------------------------- audit
    async def query_audit(request: Request) -> Response:
        guard(request)
        if ctx.audit is None:
            return JSONResponse({"items": [], "next_cursor": None})
        from ..audit.reader import AuditReader

        params = request.query_params
        reader = AuditReader(ctx.audit.path)
        result = reader.query(
            from_ts=_num(params.get("from")),
            to_ts=_num(params.get("to")),
            module_id=params.get("module_id"), kind=params.get("kind"),
            turn_id=params.get("turn_id"),
            limit=int(_num(params.get("limit")) or 100), cursor=params.get("cursor"),
        )
        return JSONResponse(result)

    # ------------------------------------------------------------------- config
    def _config_items() -> list[dict[str, Any]]:
        items = []
        for key, meta in CONFIG_ITEMS.items():
            value = ctx._config_overrides.get(key, meta["default"])
            source = "overrides" if key in ctx._config_overrides else "default"
            item = {"key": key, "value": value, "default": meta["default"],
                    "source": source, "mode": meta["mode"]}
            for extra in ("min", "max", "values"):
                if extra in meta:
                    item[extra] = meta[extra]
            items.append(item)
        return items

    async def get_config(request: Request) -> Response:
        guard(request)
        return JSONResponse({"items": _config_items()})

    async def put_config(request: Request) -> Response:
        session = guard(request)
        body = await _json(request)
        key = str(body.get("key") or "")
        if key not in CONFIG_ITEMS:
            raise VadeError("UNKNOWN_CONFIG_KEY", f"unknown config key {key!r}")
        meta = CONFIG_ITEMS[key]
        value = body.get("value")
        value = _coerce(key, meta, value)
        danger = _danger(key, meta, value)
        if danger and body.get("confirm") is not True:
            raise VadeError("CONFIRM_REQUIRED", f"{key}={value!r} needs confirm:true ({danger})")
        old = ctx._config_overrides.get(key, meta["default"])
        ctx._config_overrides[key] = value
        ctx._save_overrides()
        event = ctx.write_audit(
            kind="config.changed", status="ok", code=None, actor=actor_of(session),
            method="PUT /admin/api/config",
            extra={"key": key, "old": old, "new": value, "mode": meta["mode"]},
        )
        return JSONResponse({
            "applied": meta["mode"] == "hot",
            "needs_restart": meta["mode"] != "hot",
            "audit_id": _audit_id(event) if event else None,
        })

    async def config_usage(request: Request) -> Response:
        guard(request)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        reviews = proposals = tokens = 0
        if ctx.audit is not None:
            from ..audit.reader import AuditReader

            reader = AuditReader(ctx.audit.path)
            cursor = None
            while True:
                page = reader.query(kind="llm.call", limit=500, cursor=cursor)
                for row in page["items"]:
                    extra = row.get("extra") or {}
                    # RFC-0005 §6.5 / A12: this budget gates SELF-EVOLUTION
                    # only. Chat turns now write `llm.call` rows too
                    # (`purpose="chat"`), so without this filter ordinary
                    # conversation would silently eat the evolve budget.
                    if extra.get("purpose") == "chat":
                        continue
                    if time.strftime("%Y-%m-%d", time.gmtime(row["ts"])) != day:
                        continue
                    tokens += int(extra.get("tokens") or 0)
                # The reader clamps a page to 500 on purpose; a busy day of chat
                # rows must not push the evolve rows out of view.
                cursor = page.get("next_cursor")
                if not cursor:
                    break
        return JSONResponse({
            "day": day, "tokens_used": tokens,
            "tokens_budget": ctx._config_overrides.get("evolve.daily_token_budget",
                                                       CONFIG_ITEMS["evolve.daily_token_budget"]["default"]),
            "reviews_today": reviews, "proposals_today": proposals,
        })

    # ------------------------------------------------------------------- layout
    async def get_layout(request: Request) -> Response:
        guard(request)
        return JSONResponse(ctx._layout)

    async def patch_layout(request: Request) -> Response:
        session = guard(request)
        body = await _json(request)
        rev = body.get("rev")
        if rev is not None and int(rev) != int(ctx._layout.get("rev", 0)):
            # Layout edits are cheap and non-security; still, a lost update
            # should be visible rather than silently overwriting a teammate.
            raise VadeError("INVALID_ARGUMENT", "layout revision conflict")
        nodes = body.get("nodes")
        if not isinstance(nodes, list):
            raise VadeError("INVALID_ARGUMENT", "nodes must be a list")
        clean = []
        for node in nodes[:2000]:
            if not isinstance(node, dict) or "id" not in node:
                continue
            clean.append({
                "id": str(node["id"]), "x": float(node.get("x") or 0), "y": float(node.get("y") or 0),
                "label": (str(node["label"])[:64] if node.get("label") else None),
                "group_id": node.get("group_id"),
            })
        ctx._layout = {"rev": int(ctx._layout.get("rev", 0)) + 1, "nodes": clean}
        # L1 layout changes (including labels) are deliberately NOT audited:
        # aliasing a node would otherwise flood the audit table (31 §T4).
        return JSONResponse(ctx._layout)

    async def layout_auto(request: Request) -> Response:
        guard(request)
        views = _views()
        count = max(len(views), 1)
        nodes = []
        for index, view in enumerate(views):
            angle = 2 * 3.141592653589793 * index / count
            nodes.append({"id": view["id"], "x": round(600 + 260 * __import__("math").cos(angle), 2),
                          "y": round(360 + 260 * __import__("math").sin(angle), 2),
                          "label": view["label"], "group_id": None})
        ctx._layout = {"rev": int(ctx._layout.get("rev", 0)) + 1, "nodes": nodes}
        return JSONResponse(ctx._layout)

    # ------------------------------------------------------------------ not yet
    async def unsupported(request: Request) -> Response:
        guard(request)
        raise VadeError("UNSUPPORTED", f"{request.url.path} is not implemented in this batch")

    async def health(request: Request) -> Response:
        if _loopback(request):
            return JSONResponse({"ok": True, "version": ctx.version, "e2e_enabled": ctx.e2e_enabled})
        # Remote: alive is all a probe needs; the version is a fingerprinting gift.
        return JSONResponse({"ok": True})

    async def stream(request: Request) -> Response:
        guard(request)
        last = request.headers.get("last-event-id")

        async def generator():
            async for chunk in ctx.stream.events(last):
                yield chunk

        return StreamingResponse(generator(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive",
        })

    async def login_page(request: Request) -> Response:
        dist = Path(static_dist) if static_dist else None
        if dist and (dist / "index.html").is_file():
            return Response((dist / "index.html").read_text(encoding="utf-8"), media_type="text/html")
        return JSONResponse({
            "login": "POST /admin/api/login", "setup_required": not ctx.setup.initialized(),
            "fp8": ctx.identity.fp8 if _loopback(request) else None,
        })

    # ---------------------------------------------------------------- approvals
    async def decide_approval(request: Request) -> Response:
        session = guard(request)
        if ctx.approvals is None:
            raise VadeError("UNSUPPORTED", "approval channel is not installed")
        body = await _json(request)
        actor_kind = "device_key" if session.mode == "device" else "browser_session"
        try:
            ok, resolved = await ctx.approvals.decide(
                str(body.get("request_id") or ""), str(body.get("decision") or "deny"),
                str(body.get("scope") or "once"), actor_of(session), actor_kind=actor_kind,
            )
        except VadeError as exc:
            if exc.code == "INVALID_ARGUMENT" and "already resolved" in exc.message:
                return _error(exc, status=409)
            raise
        return JSONResponse({"ok": ok, **resolved})

    routes.extend([
        Route("/admin/setup", get_setup, methods=["GET"]),
        Route("/admin/setup/unlock", post_setup_unlock, methods=["POST"]),
        Route("/admin/setup", post_setup, methods=["POST"]),
        Route("/admin/bind/info", bind_info, methods=["GET", "POST"]),
        Route("/admin/bind", bind, methods=["POST"]),
        Route("/admin/api/health", health, methods=["GET"]),
        Route("/admin/api/login/challenge", login_challenge, methods=["POST"]),
        Route("/admin/api/login", login, methods=["POST"]),
        Route("/admin/api/token/refresh", token_refresh, methods=["POST"]),
        Route("/admin/api/logout", logout, methods=["POST"]),
        Route("/admin/api/logout-all", logout_all, methods=["POST"]),
        Route("/admin/api/password", change_password, methods=["POST"]),
        Route("/admin/api/devices", list_devices, methods=["GET"]),
        Route("/admin/api/devices", add_device, methods=["POST"]),
        Route("/admin/api/devices/{id}/revoke", revoke_device, methods=["POST"]),
        Route("/admin/api/devices/ticket", device_ticket, methods=["POST"]),
        Route("/admin/api/nodes", list_nodes, methods=["GET"]),
        Route("/admin/api/nodes/{id}", get_node, methods=["GET"]),
        Route("/admin/api/nodes/{id}", patch_node, methods=["PATCH"]),
        Route("/admin/api/nodes/{id}/revoke", revoke_node, methods=["POST"]),
        Route("/admin/api/nodes/{id}/disconnect", disconnect_node, methods=["POST"]),
        Route("/admin/api/invites", create_invite, methods=["POST"]),
        Route("/admin/api/invites/{id}", delete_invite, methods=["DELETE"]),
        Route("/admin/api/audit", query_audit, methods=["GET"]),
        Route("/admin/api/approvals/decide", decide_approval, methods=["POST"]),
        Route("/admin/api/config", get_config, methods=["GET"]),
        Route("/admin/api/config", put_config, methods=["PUT"]),
        Route("/admin/api/config/usage", config_usage, methods=["GET"]),
        Route("/admin/api/layout", get_layout, methods=["GET"]),
        Route("/admin/api/layout", patch_layout, methods=["PATCH"]),
        Route("/admin/api/layout/auto", layout_auto, methods=["POST"]),
    ])
    # Skills are C5b, implemented in `admin/skills.py`; they sit here rather
    # than in the 501 block below so every schema shape resolves to a handler.
    routes.extend(skill_routes(ctx, guard, _json))
    routes.extend([
        Route("/admin/api/stream", stream, methods=["GET"]),
        Route("/login", login_page, methods=["GET"]),
        Route("/", login_page, methods=["GET"]),
    ])
    # Memory is C5b. It is declared in `schema/admin-api.yaml` already, so
    # answer `UNSUPPORTED` (501) — not `NOT_FOUND` — for every shape the schema
    # defines, including nested actions that would otherwise miss the `{id}`
    # routes. A UI can then label the feature "not in this build".
    for path in (
        "/admin/api/memory", "/admin/api/memory/stats", "/admin/api/memory/{id}",
        "/admin/api/memory/{id}/promote", "/admin/api/memory/{id}/archive",
        "/admin/api/memory/{id}/restore",
    ):
        routes.append(Route(path, unsupported, methods=["GET", "POST", "PATCH", "DELETE", "PUT"]))

    starlette_app = Starlette(routes=routes)

    def on_vade_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, VadeError)
        return _error(exc)

    def on_http_error(request: Request, exc: Exception) -> Response:
        from starlette.exceptions import HTTPException as StarletteHTTPException

        assert isinstance(exc, StarletteHTTPException)
        code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_ARGUMENT"
        return JSONResponse({"code": code, "message": str(exc.detail)[:300]},
                            status_code=exc.status_code)

    def on_unhandled(request: Request, exc: Exception) -> Response:
        # Never echo internals: the message could contain a path or a DSN.
        return JSONResponse({"code": "INTERNAL", "message": "internal error"}, status_code=500)

    from starlette.exceptions import HTTPException as StarletteHTTPException

    starlette_app.add_exception_handler(VadeError, on_vade_error)
    starlette_app.add_exception_handler(StarletteHTTPException, on_http_error)
    starlette_app.add_exception_handler(Exception, on_unhandled)
    return GuardedApp(starlette_app, ctx)


def _num(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise VadeError("INVALID_ARGUMENT", f"{value!r} is not a number") from None


def _coerce(key: str, meta: Mapping[str, Any], value: Any) -> Any:
    kind = meta.get("type")
    if kind == "bool":
        if not isinstance(value, bool):
            raise VadeError("INVALID_ARGUMENT", f"{key} must be a boolean")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise VadeError("INVALID_ARGUMENT", f"{key} must be an integer")
        if "min" in meta and value < meta["min"]:
            raise VadeError("INVALID_ARGUMENT", f"{key} must be >= {meta['min']}")
        if "max" in meta and value > meta["max"]:
            raise VadeError("INVALID_ARGUMENT", f"{key} must be <= {meta['max']}")
        return value
    if kind == "enum":
        if value not in meta.get("values", []):
            raise VadeError("INVALID_ARGUMENT", f"{key} must be one of {meta.get('values')}")
        return value
    return value


def _danger(key: str, meta: Mapping[str, Any], value: Any) -> str | None:
    rule = meta.get("danger")
    if rule == "off" and value is False:
        return "turning evolution off stops new memories and proposals"
    if rule == "zero" and value == 0:
        return "a zero budget stops generation for the rest of the day"
    if rule == "low" and isinstance(value, int) and value < 5:
        return "an interval below 5 minutes will spend tokens quickly"
    return None


class GuardedApp:
    """Security headers + Host gate. No CORS header, ever — including OPTIONS."""

    CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
           "frame-ancestors 'none'; base-uri 'none'")

    def __init__(self, app: ASGIApp, ctx: AdminContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Content-Security-Policy", self.CSP)
                headers.setdefault("Strict-Transport-Security", "max-age=31536000")
                headers.setdefault("Cache-Control", "no-store")
                if scope["path"].startswith("/admin/api/"):
                    # A JSON API must never answer with HTML, whatever the
                    # handler did; WebView injection surface (31 §T4 ⑤).
                    content_type = headers.get("content-type", "")
                    if not content_type.startswith("application/json") and not content_type.startswith("text/event-stream"):
                        headers["content-type"] = "application/json"
                message = {**message, "headers": headers.raw}
            await send(message)

        if scope["path"].startswith("/admin"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            host = headers.get("host")
            if host not in self.ctx.config.allowed_hosts:
                # DNS rebinding: a page on evil.example resolving to 127.0.0.1
                # still sends its own Host header.
                response = JSONResponse(
                    {"code": "ORIGIN_MISMATCH", "message": "Host is not the configured admin host"},
                    status_code=403,
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
            if _declared_too_large(headers.get("content-length")):
                # Before routing and before any handler: the pre-auth routes
                # (`/admin/bind`, `/admin/setup*`) have no session to rate-limit
                # them, so the size gate cannot wait for `_json`.
                response = JSONResponse(
                    {"code": "PAYLOAD_TOO_LARGE", "message": "request body exceeds 1 MiB"},
                    status_code=413,
                )
                await response(scope, receive, send_with_headers)
                return

        await self.app(scope, receive, send_with_headers)
