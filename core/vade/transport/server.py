"""Data-plane ASGI app: HTTPS helpers + WS upgrade. Pure ASGI, no framework.

Routes on this listener: `/healthz`, `/ui/session`, `/ws`. Nothing else — in
particular `/admin*` **does not exist here** (404, not 403): running module
JavaScript in `/app` on the same origin as the admin plane would hand the
admin API to third-party code. C5a owns 127.0.0.1:8444 separately.

CORS: exact-match allowlist from `[server].ui_origins`, `Allow-Credentials:
true`, never `*`. A wildcard with credentials is rejected by browsers anyway,
and worse, it suggests a security model that does not exist.

Client IP: `X-Forwarded-For` is only honoured when the app is constructed with
`trust_forwarded_for=True` (i.e. behind a proxy that overwrites the header).
By default any client could send a fresh XFF on every request and reset the
5/5min pair-code budget, which is the whole rate limit.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import VadeError, http_status
from .connection import Connection

__all__ = ["CoreApp", "AsgiWebSocket", "CONNECTIONS"]

CORE_VERSION = "0.1.0"

#: A pair-code POST is a tiny JSON body; anything bigger is abuse.
MAX_UI_SESSION_BODY = 64 * 1024

#: Live connections by connection_id. CoreApp registers here by default so
#: governance hooks (`App._close_connection`) see the same objects.
CONNECTIONS: dict[str, Connection] = {}


class AsgiWebSocket:
    """Adapts ASGI websocket messages to `send(bytes)` / `receive()->bytes`."""

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]], receive: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        self._send = send
        self._receive = receive

    async def send(self, data: bytes) -> None:
        await self._send({"type": "websocket.send", "bytes": bytes(data)})

    async def receive(self) -> bytes | None:
        while True:
            message = await self._receive()
            if message["type"] == "websocket.disconnect":
                return None
            if message.get("bytes") is not None:
                return bytes(message["bytes"])
            if message.get("text") is not None:
                return str(message["text"]).encode("utf-8")

    async def close(self, code: int = 1000) -> None:
        await self._send({"type": "websocket.close", "code": code})


class CoreApp:
    def __init__(
        self,
        *,
        config: Any,
        identity: Any | None = None,
        registry: Any | None = None,
        make_crypto: Callable[[str], Any] | None = None,
        pairing: Any | None = None,
        whitelist: Any | None = None,
        on_rpc: Callable[[Connection, dict[str, Any]], Awaitable[Any]] | None = None,
        close_connection: Callable[[str], None] | None = None,
        policy_mode: str = "default",
        version: str = CORE_VERSION,
        trust_forwarded_for: bool = False,
        connections: dict[str, Connection] | None = None,
        blob: Any | None = None,
    ) -> None:
        self.config = config
        self.identity = identity
        self.registry = registry
        self._make_crypto = make_crypto
        self._pairing = pairing
        #: Trusted-key table, used only to name the device behind a verified
        #: Noise fingerprint. Optional: without it every native connection
        #: keeps `device_id=None` (fail-closed, same as before).
        self._whitelist = whitelist
        self._on_rpc = on_rpc
        self._close_connection = close_connection
        self._policy_mode = policy_mode
        self._version = version
        self._trust_forwarded_for = trust_forwarded_for
        self._ui_origins = tuple(config.get("server.ui_origins", []) or [])
        self._listen_hosts = tuple(config.get("server.listen_hosts", []) or [])
        self._sessions: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, Connection] = CONNECTIONS if connections is None else connections
        #: RFC-0004 `/ui/blob*` sub-app, installed by `vade.blob.install(app)`.
        #: The cookie gate needs the *live* session table, so it is handed over
        #: by reference instead of being rebuilt per request.
        self._blob = blob
        if blob is not None and hasattr(blob, "bind_sessions"):
            blob.bind_sessions(self._sessions)

    # ------------------------------------------------------------------ ASGI
    async def __call__(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        kind = scope.get("type")
        if kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._ws(scope, receive, send)
        elif kind == "lifespan":
            await self._lifespan(receive, send)
        else:
            return

    async def _lifespan(self, receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Minimal ASGI lifespan: it exists so background work owns a loop.

        RFC-0004's sweeper is a periodic task; without a startup/shutdown
        contract there is no long-lived loop to create it on (the component
        scan runs outside one). Anything that matters to a sub-app with a
        `start`/`stop` pair is delegated here, in order.
        """
        blob = self._blob
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    starter = getattr(blob, "start", None)
                    if callable(starter):
                        result = starter()
                        if inspect.isawaitable(result):
                            await result
                except Exception:  # pragma: no cover - fail closed, no silent serve
                    await send({"type": "lifespan.startup.failed", "message": "blob channel failed to start"})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                try:
                    stopper = getattr(blob, "stop", None)
                    if callable(stopper):
                        result = stopper()
                        if inspect.isawaitable(result):
                            await result
                finally:
                    await send({"type": "lifespan.shutdown.complete"})
                return

    # ------------------------------------------------------------------ http
    async def _http(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        path = str(scope.get("path", "/"))
        method = str(scope.get("method", "GET")).upper()
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

        if path == "/healthz":
            if method != "GET":
                await self._json(send, 405, {"code": "INVALID_ARGUMENT", "message": "method not allowed"})
                return
            # No config, no fingerprints, no module info: this endpoint is unauthenticated.
            await self._json(send, 200, {"ok": True, "version": self._version, "policy_mode": self._policy_mode})
            return

        if path == "/ui/session":
            if method != "POST":
                await self._json(send, 405, {"code": "INVALID_ARGUMENT", "message": "method not allowed"})
                return
            await self._ui_session(scope, receive, send, headers)
            return

        if path == "/ui/blob" or path.startswith("/ui/blob/"):
            # RFC-0004: the upload channel shares this listener but not this
            # module's credentials model — it owns its own auth and body limits.
            if self._blob is None:
                await self._json(send, 404, {"code": "NOT_FOUND", "message": "not found"})
                return
            await self._blob(scope, receive, send)
            return

        if path == "/admin" or path.startswith("/admin/"):
            # Deliberately indistinguishable from any other unknown path.
            await self._json(send, 404, {"code": "NOT_FOUND", "message": "not found"})
            return

        await self._json(send, 404, {"code": "NOT_FOUND", "message": "not found"})

    async def _ui_session(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]], headers: Mapping[str, str]) -> None:
        origin = headers.get("origin")
        cors = self._cors_headers(origin)
        if origin and cors is None:
            await self._json(send, 403, {"code": "ORIGIN_MISMATCH", "message": "origin not allowed"})
            return

        body = await self._read_body(receive)
        if body is None:
            await self._json(send, 413, {"code": "PAYLOAD_TOO_LARGE", "message": "body too large"}, cors)
            return
        try:
            payload = json.loads(body or b"{}")
            code = str(payload["pair_code"])
            client_name = str(payload.get("client_name", ""))[:64]
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
            await self._json(send, 400, {"code": "INVALID_ARGUMENT", "message": "pair_code is required"}, cors)
            return

        if self._pairing is None:
            await self._json(send, 400, {"code": "CODE_INVALID", "message": "pairing is not configured"}, cors)
            return

        client_ip = self._client_ip(scope, headers)
        try:
            record = self._pairing.consume(code, "browser", ip=client_ip)
        except VadeError as exc:
            extra = self._status_headers(exc)
            await self._json(send, http_status(exc.code), exc.to_dict(), cors, extra)
            return

        device_id = f"ui-{secrets.token_hex(8)}"
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self._sessions[token] = {
            "device_id": device_id,
            "client_name": client_name,
            "created_at": time.time(),
            "revoked": False,
            "read_only": False,
            # RFC-0004 §8.2: the CSRF token is a double-submit value bound to
            # this session. Only its digest is stored, and a new login rotates
            # it because the row is new.
            "csrf_hash": hashlib.sha256(csrf.encode("utf-8")).hexdigest(),
        }
        cookie = (
            f"__Host-ac_ui={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={30 * 24 * 3600}"
        )
        # Readable by the page on purpose (double-submit requires JS to echo it
        # in `x-vade-csrf`); it is useless without the session cookie.
        csrf_cookie = f"__Host-ac_csrf={csrf}; Path=/; Secure; SameSite=Lax; Max-Age={30 * 24 * 3600}"
        extra = {"Set-Cookie": [cookie, csrf_cookie]}
        await self._json(send, 200, {"device_id": device_id}, cors, extra)
        _ = record

    @staticmethod
    def _status_headers(exc: VadeError) -> dict[str, str]:
        if exc.code == "ADMIN_LOCKED" and exc.data and "retry_after" in exc.data:
            return {"Retry-After": str(int(exc.data["retry_after"]))}
        return {}

    def _cors_headers(self, origin: str | None) -> dict[str, str] | None:
        if not origin:
            return {}
        if origin in self._ui_origins:
            return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
        return None

    def _client_ip(self, scope: Mapping[str, Any], headers: Mapping[str, str]) -> str | None:
        if self._trust_forwarded_for and "x-forwarded-for" in headers:
            return headers["x-forwarded-for"].split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else None

    @staticmethod
    async def _read_body(receive: Callable[[], Awaitable[dict[str, Any]]]) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_UI_SESSION_BODY:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    async def _json(send: Callable[[dict[str, Any]], Awaitable[None]], status: int, payload: Mapping[str, Any], cors: Mapping[str, str] | None = None, extra: Mapping[str, str | Sequence[str]] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = [(b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store")]
        for key, value in (cors or {}).items():
            headers.append((key.lower().encode(), value.encode()))
        for key, value in (extra or {}).items():
            # `Set-Cookie` may repeat: one header line per cookie. Joining them
            # with a comma is not valid for cookies that carry attributes.
            values = value if isinstance(value, (list, tuple)) else (value,)
            for item in values:
                headers.append((key.lower().encode(), str(item).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    # ------------------------------------------------------------------ ws
    async def _ws(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        path = str(scope.get("path", "/"))
        if path not in ("/ws", "/"):
            await send({"type": "websocket.close", "code": 1008})
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = self._cookie(headers.get("cookie", ""), "__Host-ac_ui")
        session = self._sessions.get(token) if token is not None else None
        if session is not None and session["revoked"]:
            # A revoked row is no identity at all: the same `None` path as an
            # unknown token, so nothing downstream has to special-case it.
            session = None
        browser = session is not None
        # RFC-0004 §6.4: the device identity is the one `/ui/session` minted and
        # stored server-side (`ui-…`), read back through the cookie — never a
        # query parameter or header the client could type. An unknown or
        # revoked token leaves it None, so a `file_id` submit stays fail-closed.
        device_id = str(session["device_id"]) if session is not None else None

        if not browser and self._make_crypto is None:
            await send({"type": "websocket.close", "code": 1008})
            return

        await send({"type": "websocket.accept"})
        ws = AsgiWebSocket(send, receive)
        connection_id = secrets.token_hex(8)
        crypto = None
        if not browser:
            try:
                crypto = self._make_crypto(connection_id)  # type: ignore[misc]
            except VadeError:
                # No usable key pool / protocol binding: refuse before a reader
                # task is started. 1011 = internal error, never 1008 (policy)
                # because the peer did not do anything wrong.
                await send({"type": "websocket.close", "code": 1011})
                return
        connection = Connection(
            ws,
            crypto,
            connection_id=connection_id,
            max_queue=int(self.config.get("core.send_queue_frames", 256)),
            heartbeat_interval_s=float(self.config.get("core.heartbeat_interval_s", 10)),
            handshake_timeout_s=float(self.config.get("core.handshake_timeout_s", 5)),
            on_rpc=self._on_rpc,
            on_close=self._forget,
            browser_session=browser,
            device_id=device_id,
            # A native peer has no identity until the IK handshake proves which
            # static key it holds; this hook runs exactly then.
            on_established=None if browser else self._bind_device_identity,
        )
        connection.ui_token = token if browser else None  # type: ignore[attr-defined]
        self._connections[connection_id] = connection
        await connection.run()

    def _bind_device_identity(self, connection: Connection) -> None:
        """Name a Noise peer from the key the handshake just verified.

        `CryptoSession.read_handshake` computes the fingerprint of the peer's
        static key and refuses an unlisted one *before* msg2, so by the time
        this hook runs the key is already trusted. The whitelist then says
        which pairing owns it (`module_id#instance_id`).

        No match, no whitelist, or a crypto object without a fingerprint all
        leave `device_id=None`. That is deliberate and is the reason the
        attribute defaults to `None`: an unattributable connection must not be
        able to submit a `file_id`, and guessing an identity from anything the
        peer sent over the wire (a query string, a header, an RPC field) would
        turn a hint into a claim.
        """
        crypto = connection.crypto
        fingerprint = getattr(crypto, "remote_fingerprint", None) if crypto is not None else None
        if not fingerprint or self._whitelist is None:
            return
        resolver = getattr(self._whitelist, "device_for_fingerprint", None)
        if not callable(resolver):
            return
        device_id = resolver(str(fingerprint))
        if device_id:
            connection.device_id = str(device_id)

    @staticmethod
    def _cookie(header: str, name: str) -> str | None:
        for part in header.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def _forget(self, connection: Connection) -> None:
        self._connections.pop(connection.connection_id, None)

    # ------------------------------------------------------------------ governance
    def revoke_session(self, device_id: str) -> tuple[str, ...]:
        """Revoke one browser device: drop its tokens and close only ITS sockets.

        A revoke used to return every browser connection id, which would have
        kicked every logged-in UI when one device was revoked.
        """
        tokens = {token for token, meta in self._sessions.items() if meta["device_id"] == device_id}
        closed: list[str] = []
        for token in tokens:
            del self._sessions[token]
        for connection in list(self._connections.values()):
            if connection.browser_session and getattr(connection, "ui_token", None) in tokens:
                closed.append(connection.connection_id)
                connection.request_close(1001, "session revoked")
        return tuple(closed)
