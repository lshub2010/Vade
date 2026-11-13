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

import json
import secrets
import time
from typing import Any, Awaitable, Callable, Mapping

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
        on_rpc: Callable[[Connection, dict[str, Any]], Awaitable[Any]] | None = None,
        close_connection: Callable[[str], None] | None = None,
        policy_mode: str = "default",
        version: str = CORE_VERSION,
        trust_forwarded_for: bool = False,
        connections: dict[str, Connection] | None = None,
    ) -> None:
        self.config = config
        self.identity = identity
        self.registry = registry
        self._make_crypto = make_crypto
        self._pairing = pairing
        self._on_rpc = on_rpc
        self._close_connection = close_connection
        self._policy_mode = policy_mode
        self._version = version
        self._trust_forwarded_for = trust_forwarded_for
        self._ui_origins = tuple(config.get("server.ui_origins", []) or [])
        self._listen_hosts = tuple(config.get("server.listen_hosts", []) or [])
        self._sessions: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, Connection] = CONNECTIONS if connections is None else connections

    # ------------------------------------------------------------------ ASGI
    async def __call__(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        kind = scope.get("type")
        if kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._ws(scope, receive, send)
        else:  # lifespan is handled by the server runner
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
        self._sessions[token] = {"device_id": device_id, "client_name": client_name, "created_at": time.time(), "revoked": False}
        cookie = (
            f"__Host-ac_ui={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={30 * 24 * 3600}"
        )
        extra = {"Set-Cookie": cookie}
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
    async def _json(send: Callable[[dict[str, Any]], Awaitable[None]], status: int, payload: Mapping[str, Any], cors: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = [(b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store")]
        for key, value in (cors or {}).items():
            headers.append((key.lower().encode(), value.encode()))
        for key, value in (extra or {}).items():
            headers.append((key.lower().encode(), value.encode()))
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
        browser = token is not None and token in self._sessions and not self._sessions[token]["revoked"]

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
        )
        connection.ui_token = token if browser else None  # type: ignore[attr-defined]
        self._connections[connection_id] = connection
        await connection.run()

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
