"""Data-plane ASGI app: HTTPS helpers + WS upgrade. Pure ASGI, no framework.

Routes on this listener: `/healthz`, `/ui/session` (`POST` mints a browser
session, `DELETE` revokes the one that presented its cookie), `/ws`. Nothing
else — in particular `/admin*` **does not exist here** (404, not 403): running
module JavaScript in `/app` on the same origin as the admin plane would hand the
admin API to third-party code. C5a owns 127.0.0.1:8444 separately.

CORS: exact-match allowlist from `[server].ui_origins`, `Allow-Credentials:
true`, never `*`. A wildcard with credentials is rejected by browsers anyway,
and worse, it suggests a security model that does not exist.

Client IP: `X-Forwarded-For` is only honoured when the app is constructed with
`trust_forwarded_for=True` (i.e. behind a proxy that overwrites the header).
By default any client could send a fresh XFF on every request and reset the
5/5min pair-code budget, which is the whole rate limit.

Browser session lifetime: `__Host-ac_ui` carried `Max-Age=30d` while the server
row lived forever, so the cookie — not the server — decided when a session
ended. Any copy of the token (a backup, a leaked `Cookie` header, a browser
profile synced to another machine) stayed valid until the process restarted.
The table below is now the authority: every minted row carries `expires_at`,
lookups treat an expired row as absent, and a periodic sweep (plus the sweep on
every lookup) is what removes it. `/ui/blob` holds this same object by
reference, so expiry is enforced for uploads too — the check lives in the
table, not in each caller.

Surviving a restart: a core restart used to log every browser out, because the
table was process memory only. `UiSessions` now optionally writes itself to
`<data_dir>/ui_sessions.json` (0600, atomic `tmp` + `rename`) and reloads on
startup. The file holds `sha256(token) -> row` — never the token: a backup of
`data_dir`, or a directory listing an operator pastes into a ticket, must not
be a credential. Callers keep passing the raw cookie value; the key transform
happens inside the table (`session_key`), so no reader has to know.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import secrets
from urllib.parse import urlsplit
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .. import __version__
from ..errors import VadeError, http_status
from ..logging import get_logger
from .connection import Connection

__all__ = [
    "CoreApp",
    "AsgiWebSocket",
    "CONNECTIONS",
    "UiSessions",
    "UI_SESSION_TTL_S",
    "UI_SESSIONS_FILE",
    "session_key",
]

log = get_logger("vade.transport.server")


CORE_VERSION = __version__

#: A pair-code POST is a tiny JSON body; anything bigger is abuse.
MAX_UI_SESSION_BODY = 64 * 1024

#: Server-side lifetime of a browser session. The cookie's `Max-Age` is derived
#: from this value (`_ui_session`), so the two can never disagree — a cookie
#: that outlives the row is a token the server no longer honours.
UI_SESSION_TTL_S = 30 * 24 * 3600

#: How often the sweeper looks for expired rows. Expiry is enforced on every
#: lookup regardless; this only bounds how long a dead row (and the socket it
#: authenticated) keeps occupying memory.
UI_SESSION_SWEEP_S = 60.0

#: Filename of the browser-session table inside `[core].data_dir`. Fixed (not
#: configurable) so an operator knows exactly which file holds live sessions.
UI_SESSIONS_FILE = "ui_sessions.json"

#: On-disk shape version. An unknown version is ignored like a corrupt file:
#: guessing at a newer layout could resurrect a revoked session, and the only
#: cost of ignoring it is one re-pair.
UI_SESSIONS_FILE_VERSION = 1

#: The data-plane HTTP surface owned by this module. Frozen in
#: `schema/ui-api.yaml` and machine-compared by `tools/check_contracts.py`, so
#: a route cannot exist in the code and be missing from the contract (or the
#: reverse). `/ui/blob*` is RFC-0004's family and is not listed here.
UI_SESSION_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/ui/session"),
    ("DELETE", "/ui/session"),
    # CORS preflight. A browser page on an allowlisted origin sends
    # `content-type: application/json` (and the opt-in token header), both of
    # which trigger a preflight; without an answer `fetch` never issues the
    # POST and first-run pairing is unreachable from any page not served by
    # this exact listener. Preflights carry no credentials and are answered
    # only for an origin the same gate would accept.
    ("OPTIONS", "/ui/session"),
})

#: Opt-in request header for the browser token channel. The value is `1`.
#:
#: Why opt-in: `session_token` is the same secret as `__Host-ac_ui`, and the
#: cookie is `HttpOnly` on purpose — handing the value to page JavaScript
#: removes that protection for the page's whole lifetime (an XSS can then
#: exfiltrate a credential that outlives the tab). A client that can read
#: `Set-Cookie` must not get it; only a browser that explicitly says it cannot
#: (wasm) and is on an allowlisted Origin does.
UI_SESSION_TOKEN_HEADER = "x-vade-session-token"

#: Request headers a `/ui/session` preflight may ask for.
UI_SESSION_ALLOWED_HEADERS = "content-type, x-vade-session-token, x-vade-csrf"


#: Live connections by connection_id. CoreApp registers here by default so
#: governance hooks (`App._close_connection`) see the same objects.
CONNECTIONS: dict[str, Connection] = {}

#: Sentinel for "the token is gone", so `None` stays a legal row value.
_MISSING = object()


def session_key(token: Any) -> str:
    """`sha256(token)` hex — the only shape a key has inside the table.

    Every path into `UiSessions` (mint, lookup, revoke, load, persist) funnels
    through this function, so there is exactly one place that decides what a
    key is and no path that keeps a usable credential. Callers never see it:
    they pass the cookie value and get the row.

    A non-string (a `None` cookie, an already-hashed blob handed in by mistake)
    becomes `""`, which no stored key can equal — a miss, never a crash and
    never an accidental wildcard match.
    """
    if not isinstance(token, str):
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UiSessions(dict):
    """`sha256(token) -> session row` with a hard server-side TTL.

    A `dict` subclass on purpose: `vade.blob`'s `/ui/blob` gate is handed this
    exact object by reference (`bind_sessions`) and only ever calls `.get()`.
    Replacing the table with a wrapper would need that component's cooperation;
    overriding the read paths means *every* reader — the WS upgrade, the blob
    cookie gate, governance — sees the same answer for "is this token still
    alive", with no second place to forget the check. For the same reason the
    key transform lives in the overrides: the blob gate passes the raw cookie
    value and calls `.get()`, and it keeps working without knowing that the
    stored key is a digest.

    A row without a numeric `created_at` (only hand-built fixtures or rows
    written by an older build) is treated as non-expiring: expiry is a function
    of a value the mint always writes, and inventing a timestamp for a row that
    has none would expire someone's session at process start.

    Persistence is optional: with `path=None` this is process memory and
    behaves exactly as before. With a path, the table loads at construction,
    drops what has already expired, and writes the whole set after every
    mutation (`mint`, `revoke`, `sweep`, lazy eviction) — atomically, via a
    `0600` temp file in the same directory and `os.replace`, so a crash
    mid-write leaves the previous file intact rather than a truncated one.
    """

    def __init__(
        self,
        ttl_s: float = UI_SESSION_TTL_S,
        *,
        clock: Callable[[], float] = time.time,
        path: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__()
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self.path: Path | None = Path(path) if path is not None else None
        #: Serialises writers. `revoke_session` is also reachable from
        #: governance hooks off the event loop, and two half-written files
        #: would be worse than a late one.
        self._write_lock = threading.Lock()
        if self.path is not None:
            self._load()

    # ------------------------------------------------------------------ storage
    def _write(self) -> None:
        """Persist the whole table atomically. Never raises: a full disk must
        not turn a successful login into a 500 — the session is still live in
        memory, only its survival across a restart is at risk."""
        path = self.path
        if path is None:
            return
        with self._write_lock:
            try:
                rows = {key: _jsonable_row(row) for key, row in list(dict.items(self))}
            except RuntimeError as exc:  # pragma: no cover - concurrent resize
                log.warning("browser session snapshot skipped: %s", exc)
                return
            body = json.dumps(
                {"version": UI_SESSIONS_FILE_VERSION, "sessions": rows},
                ensure_ascii=False,
                sort_keys=True,
            )
            tmp: str | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                tmp = None
            except OSError as exc:
                log.error("browser session store write failed: %s", exc)
            finally:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)

    def _load(self) -> int:
        """Read the persisted table; return how many rows were kept.

        Anything unreadable — missing file, bad JSON, wrong shape, a key that
        is not a digest, a row that already expired — is dropped, and the file
        is rewritten so the next start does not see it again. A corrupt store
        must never stop the core from serving: the worst case is that browsers
        have to pair again, which is exactly the pre-persistence behaviour.
        """
        path = self.path
        if path is None:
            return 0
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        except OSError as exc:
            log.warning("browser session store unreadable, starting empty: %s", exc)
            return 0
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("browser session store is not valid JSON, ignoring it: %s", exc)
            return 0
        if not isinstance(payload, Mapping):
            log.warning("browser session store has an unexpected shape, ignoring it")
            return 0
        if payload.get("version") != UI_SESSIONS_FILE_VERSION:
            log.warning(
                "browser session store version %r is not %r, ignoring it",
                payload.get("version"), UI_SESSIONS_FILE_VERSION,
            )
            return 0
        rows = payload.get("sessions")
        if not isinstance(rows, Mapping):
            log.warning("browser session store holds no `sessions` mapping, ignoring it")
            return 0

        kept = 0
        dropped = 0
        now = self._clock()
        for key, row in rows.items():
            if not _is_digest(key) or not isinstance(row, Mapping):
                dropped += 1
                continue
            row = dict(row)
            # `device_id` is the identity every reader names (`/ws` puts it on
            # the connection, the blob gate puts it on the principal); a row
            # without one is not a session, whatever else it says. The rest get
            # the same defaults the mint writes, so a hand-edited file cannot
            # turn a missing key into a `KeyError` on the WS upgrade path.
            if not isinstance(row.get("device_id"), str) or not row["device_id"]:
                dropped += 1
                continue
            row.setdefault("revoked", False)
            row.setdefault("read_only", False)
            row.setdefault("csrf_hash", "")
            deadline = self.expires_at(row)
            if deadline is not None and now >= deadline:
                # Already dead: keeping it would only make the next sweep write
                # the file again to remove it.
                dropped += 1
                continue
            dict.__setitem__(self, key, row)
            kept += 1
        if dropped:
            log.info(
                "browser session store: %d row(s) recovered, %d dropped",
                kept, dropped,
            )
            # Rewrite so a dead or malformed row is gone from disk too, not
            # just from memory.
            self._write()
        return kept

    # ------------------------------------------------------------------ keys
    def __setitem__(self, token: Any, row: Any) -> None:  # type: ignore[override]
        dict.__setitem__(self, session_key(token), row)
        self._write()

    def __delitem__(self, token: Any) -> None:  # type: ignore[override]
        dict.__delitem__(self, session_key(token))
        self._write()

    # ------------------------------------------------------------------ queries
    def expires_at(self, row: Mapping[str, Any] | None) -> float | None:
        if not row:
            return None
        created = row.get("created_at")
        if not isinstance(created, (int, float)) or isinstance(created, bool):
            return None
        return float(created) + self.ttl_s

    def is_expired(self, row: Mapping[str, Any] | None) -> bool:
        deadline = self.expires_at(row)
        return deadline is not None and self._clock() >= deadline

    def get(self, token: Any, default: Any = None) -> Any:  # type: ignore[override]
        key = session_key(token)
        row = dict.get(self, key)
        if row is None or self.is_expired(row):
            if row is not None:
                # Lazy eviction: an expired token must stop working the moment
                # it expires, not at the next sweep tick. The file has to agree,
                # or a restart would resurrect the row this call just denied.
                dict.pop(self, key, None)
                self._write()
            return default
        return row

    def __getitem__(self, token: Any) -> Any:  # type: ignore[override]
        row = self.get(token, _MISSING)
        if row is _MISSING:
            raise KeyError(token)
        return row

    def __contains__(self, token: Any) -> bool:  # type: ignore[override]
        return self.get(token, _MISSING) is not _MISSING

    def pop(self, token: Any, default: Any = _MISSING) -> Any:  # type: ignore[override]
        key = session_key(token)
        if default is _MISSING:
            row = dict.pop(self, key)
        else:
            row = dict.pop(self, key, _MISSING)
            if row is _MISSING:
                return default
        self._write()
        return row

    def setdefault(self, token: Any, default: Any = None) -> Any:  # type: ignore[override]
        row = self.get(token)
        if row is not None:
            return row
        self[token] = default
        return default

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        """Hash the keys of a bulk insert, exactly like `__setitem__` does."""
        for token, row in dict(*args, **kwargs).items():
            self[token] = row

    def clear(self) -> None:  # type: ignore[override]
        if not dict.__len__(self):
            return
        dict.clear(self)
        self._write()

    # ------------------------------------------------------------------ maintenance
    def prune(self) -> list[str]:
        """Drop every expired row; return the hashed keys that were removed."""
        now = self._clock()
        dead = [
            key
            for key, row in list(dict.items(self))
            if (deadline := self.expires_at(row)) is not None and now >= deadline
        ]
        for key in dead:
            dict.pop(self, key, None)
        if dead:
            self._write()
        return dead

    def discard_many(self, keys: Iterable[str]) -> int:
        """Remove already-hashed `keys` in one pass and one write.

        `sweep_sessions`/`revoke_session` speak in keys (they read them back
        out of `items()`), so they must not go through `pop`, which would hash
        a digest a second time and silently remove nothing.
        """
        removed = 0
        for key in keys:
            if key and dict.pop(self, key, _MISSING) is not _MISSING:
                removed += 1
        if removed:
            self._write()
        return removed


def _is_digest(value: Any) -> bool:
    """A stored key must be a sha256 hex digest, or the file is not ours."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _jsonable_row(row: Any) -> dict[str, Any]:
    """Keep only what `json.dumps` round-trips; a row is flat scalars today.

    Filtering (rather than `default=str`) is deliberate: a session row that
    somehow grew an object field must lose that field, not gain a stringified
    repr that a later start would read back as a different value.
    """
    if not isinstance(row, Mapping):
        return {}
    return {
        str(key): value
        for key, value in row.items()
        if value is None or isinstance(value, (str, int, float, bool))
    }


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
        audit: Any | None = None,
        ui_session_ttl_s: float = UI_SESSION_TTL_S,
        ui_session_sweep_s: float = UI_SESSION_SWEEP_S,
        data_dir: str | os.PathLike[str] | None = None,
        ui_session_path: str | os.PathLike[str] | None = None,
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
        self._ui_session_ttl_s = float(ui_session_ttl_s)
        self._ui_session_sweep_s = float(ui_session_sweep_s)
        #: Where the table survives a restart. `data_dir` is the assembly
        #: point's (`__main__._serve`) `[core].data_dir`; `ui_session_path`
        #: overrides the file name. Neither given (the default in tests) means
        #: process memory only, which is what every caller had before.
        if ui_session_path is not None:
            session_store = Path(ui_session_path)
        elif data_dir is not None:
            session_store = Path(data_dir) / UI_SESSIONS_FILE
        else:
            session_store = None
        #: Browser sessions. `UiSessions` is the authority on expiry: the WS
        #: upgrade, the blob cookie gate and `revoke_session` all read it.
        self._sessions: UiSessions = UiSessions(self._ui_session_ttl_s, path=session_store)
        self._session_sweeper: asyncio.Task[None] | None = None
        self._connections: dict[str, Connection] = CONNECTIONS if connections is None else connections
        #: RFC-0004 `/ui/blob*` sub-app, installed by `vade.blob.install(app)`.
        #: The cookie gate needs the *live* session table, so it is handed over
        #: by reference instead of being rebuilt per request.
        self._blob = blob
        if blob is not None and hasattr(blob, "bind_sessions"):
            blob.bind_sessions(self._sessions)
        #: Audit sink for the data plane (`ui.unpaired` today). Duck-typed on
        #: `record(event)` so this module does not import the audit package —
        #: the admin plane and this listener share one `AuditWriter`, and a
        #: test can hand in anything with that one method.
        self._audit = audit

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

        The browser-session sweeper is started here for the same reason: an
        expired `__Host-ac_ui` row must be *removed*, not merely ignored, and
        nothing else in the process owns a loop that could do it on a timer.
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
                self._session_sweeper = asyncio.create_task(self._sweep_loop(), name="ui-session-sweeper")
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                try:
                    if self._session_sweeper is not None:
                        self._session_sweeper.cancel()
                        self._session_sweeper = None
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
            if method == "POST":
                await self._ui_session(scope, receive, send, headers)
                return
            if method == "DELETE":
                await self._ui_unpair(scope, send, headers)
                return
            if method == "OPTIONS":
                await self._ui_session_preflight(send, headers)
                return
            await self._json(
                send, 405, {"code": "INVALID_ARGUMENT", "message": "method not allowed"},
                extra={"Allow": "POST, DELETE, OPTIONS"},
            )
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
        cors = self._cors_headers(origin, headers.get("host", ""))
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
        created_at = time.time()
        self._sessions[token] = {
            "device_id": device_id,
            "client_name": client_name,
            "created_at": created_at,
            # The row, not the cookie, decides when this session ends. Written
            # once here so nothing downstream has to recompute it.
            "expires_at": created_at + self._ui_session_ttl_s,
            "revoked": False,
            "read_only": False,
            # RFC-0004 §8.2: the CSRF token is a double-submit value bound to
            # this session. Only its digest is stored, and a new login rotates
            # it because the row is new.
            "csrf_hash": hashlib.sha256(csrf.encode("utf-8")).hexdigest(),
        }
        max_age = int(self._ui_session_ttl_s)
        cookie = f"__Host-ac_ui={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        # Readable by the page on purpose (double-submit requires JS to echo it
        # in `x-vade-csrf`); it is useless without the session cookie.
        csrf_cookie = f"__Host-ac_csrf={csrf}; Path=/; Secure; SameSite=Lax; Max-Age={max_age}"
        extra = {"Set-Cookie": [cookie, csrf_cookie]}
        body_out: dict[str, Any] = {"device_id": device_id}
        # Browser token channel (T5/D8). A wasm page cannot read `Set-Cookie`,
        # so the only credential it can learn from this response is one this
        # body carries. Three conditions, all required:
        #   * the caller opted in by name (`x-vade-session-token: 1`), so a
        #     curl/JVM/Android client that can read the cookie never gets a
        #     JS-readable copy of it;
        #   * an `Origin` was sent — i.e. this is a page, not a native client;
        #   * that Origin passed the same gate `DELETE` and `/ws` use.
        # The value is byte-identical to the cookie, so it is the same row and
        # the same revoke path (`DELETE /ui/session` kills it). It is never
        # logged: this function has no log statement, and the audit sink only
        # ever sees `device_id`.
        if origin and headers.get(UI_SESSION_TOKEN_HEADER, "") == "1":
            body_out["session_token"] = token
        await self._json(send, 200, body_out, cors, extra)
        _ = record

    async def _ui_session_preflight(self, send: Callable[[dict[str, Any]], Awaitable[None]], headers: Mapping[str, str]) -> None:
        """CORS preflight for `POST/DELETE /ui/session`.

        A page on an allowlisted origin cannot even send the mint request
        without this: `content-type: application/json` and the opt-in token
        header are both non-simple, so the browser asks first and a 405 here
        aborts the fetch. The answer is the same allowlist decision the POST
        makes (`_cors_headers`), returns no body and no credentials, and
        never reflects a wildcard.
        """
        origin = headers.get("origin")
        if not origin:
            # Not a browser preflight. Keep the generic method answer so a
            # probe learns nothing this path did not already say.
            await self._json(
                send, 405, {"code": "INVALID_ARGUMENT", "message": "method not allowed"},
                extra={"Allow": "POST, DELETE, OPTIONS"},
            )
            return
        cors = self._cors_headers(origin, headers.get("host", ""))
        if cors is None:
            await self._json(send, 403, {"code": "ORIGIN_MISMATCH", "message": "origin not allowed"})
            return
        await self._no_content(send, cors, extra={
            "Access-Control-Allow-Methods": "POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": UI_SESSION_ALLOWED_HEADERS,
            "Access-Control-Max-Age": "600",
        })

    async def _ui_unpair(self, scope: Mapping[str, Any], send: Callable[[dict[str, Any]], Awaitable[None]], headers: Mapping[str, str]) -> None:
        """`DELETE /ui/session`: revoke exactly the session that presents its cookie.

        Same credential model as `POST /ui/session`: the `__Host-ac_ui` cookie
        (plus the same `[server].ui_origins` gate), no bearer, no body. The
        client calls this before switching cores so the old core does not keep
        a live session — and its open `/ws` socket — pointed at a core the user
        has left.

        Nothing else is revoked, and that is the whole model, not an omission:
        the `device_id` a browser session carries (`ui-…`) is a per-session
        label minted here. It is not a row in any device table — `DeviceStore`
        (admin plane) keys devices by `SHA-256(pubkey)[:16]` for keys that can
        sign challenges, and `Whitelist` keys module instances by
        `module_id#instance_id`. A browser never owns a key, so there is no
        device record to revoke alongside the session; `revoke_device(ui-…)`
        would be a no-op by construction. A repeat call (or a cookie-less one)
        is `401` and must not touch any other session.
        """
        origin = headers.get("origin")
        cors = self._cors_headers(origin, headers.get("host", ""))
        if origin and cors is None:
            await self._json(send, 403, {"code": "ORIGIN_MISMATCH", "message": "origin not allowed"})
            return

        token = self._cookie(headers.get("cookie", ""), "__Host-ac_ui")
        # `UiSessions.get` already treats an expired row as absent, so an
        # expired cookie is the same 401 as a forged one.
        row = self._sessions.get(token) if token is not None else None
        if row is None:
            self._note_unpaired("denied", None, scope, headers, reason="no live session")
            await self._json(send, 401, {"code": "AUTH_FAILED", "message": "no live ui session"}, cors)
            return

        device_id = str(row.get("device_id") or "")
        self._drop_sessions({session_key(token)}, reason="session unpaired")
        self._note_unpaired(
            "ok", device_id or None, scope, headers,
            client_name=str(row.get("client_name") or "")[:64] or None,
        )
        # 204 + expire both cookies: the browser must forget a credential whose
        # row is gone, or its next request would carry a dead token that looks
        # like an authentication failure rather than "already unpaired".
        expired = [
            "__Host-ac_ui=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0",
            "__Host-ac_csrf=; Path=/; Secure; SameSite=Lax; Max-Age=0",
        ]
        await self._no_content(send, cors, extra={"Set-Cookie": expired})

    def _note_unpaired(self, status: str, actor: str | None, scope: Mapping[str, Any], headers: Mapping[str, str], *, reason: str | None = None, client_name: str | None = None) -> None:
        """One `ui.unpaired` row per attempt, refusals included.

        The token never enters the row: the audit log is readable by a
        read-only admin session, so persisting a live credential there would
        hand out the very session the row is about.
        """
        audit = self._audit
        if audit is None:
            return
        extra: dict[str, Any] = {}
        ip = self._client_ip(scope, headers)
        if ip:
            extra["ip"] = ip
        if reason:
            extra["reason"] = reason
        if client_name:
            extra["client_name"] = client_name
        try:
            audit.record({
                "kind": "ui.unpaired",
                "status": status,
                "actor": actor,
                "method": "DELETE /ui/session",
                "extra": extra,
            })
        except Exception as exc:  # noqa: BLE001 - a bad audit row must not 500 the revoke
            log.error(
                "audit write failed (kind=ui.unpaired): %s", exc,
                extra={"code": None, "msg": "audit_unavailable"},
            )

    @staticmethod
    async def _no_content(send: Callable[[dict[str, Any]], Awaitable[None]], cors: Mapping[str, str] | None = None, extra: Mapping[str, str | Sequence[str]] | None = None) -> None:
        """`204 No Content` with the same header plumbing as `_json`."""
        headers: list[tuple[bytes, bytes]] = [
            (b"cache-control", b"no-store"),
            (b"content-length", b"0"),
        ]
        for key, value in (cors or {}).items():
            headers.append((key.lower().encode(), value.encode()))
        for key, value in (extra or {}).items():
            values = value if isinstance(value, (list, tuple)) else (value,)
            for item in values:
                headers.append((key.lower().encode(), str(item).encode()))
        await send({"type": "http.response.start", "status": 204, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    def _status_headers(exc: VadeError) -> dict[str, str]:
        if exc.code == "ADMIN_LOCKED" and exc.data and "retry_after" in exc.data:
            return {"Retry-After": str(int(exc.data["retry_after"]))}
        return {}

    def _cors_headers(self, origin: str | None, host: str = "") -> dict[str, str] | None:
        """CORS answer for `origin`, or `None` when the origin is refused.

        Same decision as the `/ws` upgrade (`_origin_allowed`), deliberately:
        a page that may hold a socket must also be able to mint and revoke the
        session that socket proves. With no `[server].ui_origins` configured,
        a page served by this listener (Origin netloc == Host) is accepted —
        that is the zero-config page-shell deployment, and `_origin_allowed`
        already treats it as same-origin. A configured allowlist stays
        exact-match only.
        """
        if not origin:
            return {}
        if not self._origin_allowed(origin, host):
            return None
        return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}

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
        # `UiSessions.get` is expiry-aware: an expired token is absent, exactly
        # like an unknown one, so no downstream caller can forget the check.
        session = self._sessions.get(token) if token is not None else None
        if session is not None and session["revoked"]:
            # A revoked row is no identity at all: the same `None` path as an
            # unknown token, so nothing downstream has to special-case it.
            session = None
        has_cookie = session is not None
        # Browser pages always send Origin; native clients (the app, the CLI)
        # do not. CSRF only defends an ambient browser credential, so the
        # browser flag is "cookie + Origin" — a cookie socket with no Origin
        # is a native client and stays exempt (it still has to pass the
        # loopback-only origin gate below).
        origin = headers.get("origin")
        browser = has_cookie
        # CSRF defends an ambient *browser* credential; a cookie-bearing native
        # client sends no Origin and cannot read a CSRF cookie, so it is exempt.
        browser_page = has_cookie and origin is not None
        # RFC-0004 §6.4: the device identity is the one `/ui/session` minted and
        # stored server-side (`ui-…`), read back through the cookie — never a
        # query parameter or header the client could type. An unknown or
        # revoked token leaves it None, so a `file_id` submit stays fail-closed.
        device_id = str(session["device_id"]) if session is not None else None

        # CSWSH gate (AC-33): a cookie is ambient authority — the browser
        # attaches it to a handshake the page did not intend, and `SameSite`
        # does not help across ports on the same host (same-site ignores the
        # port). So a cookie-bearing upgrade must name an Origin on the
        # `[server].ui_origins` allowlist, and an upgrade that carries the
        # cookie but no Origin at all is refused: only a non-browser can strip
        # it, and a non-browser has no business using an ambient credential.
        # Refused BEFORE `websocket.accept`, so the peer never sees a socket;
        # 1008 is the WS spelling of the 403 the HTTP routes return.
        if has_cookie and not self._origin_allowed(origin, headers.get("host", ""), scope.get("client")):
            log.warning("ws upgrade refused: origin", extra={
                "code": "ORIGIN_MISMATCH", "connection_id": None,
                "msg": f"origin={headers.get('origin')!r}"})
            self._note_ws_refused(scope, headers, "origin")
            await send({"type": "websocket.close", "code": 1008})
            return

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
            # The credential this socket PROVED, not one it claims. A cookie
            # upgrade proved the `__Host-ac_ui` row; a native one proves
            # nothing until the IK handshake completes, so `auth_mode` stays
            # `None` (fail closed) and `_bind_device_identity` fills it in.
            # `read_only` travels with the row that authenticated the cookie,
            # which is what makes `READ_ONLY_SESSION` true server-side and not
            # only in a hidden button (FR-8.75).
            auth_mode="browser_session" if has_cookie else None,
            read_only=bool(session.get("read_only")) if session is not None else False,
            csrf_hash=str(session.get("csrf_hash") or "") if session is not None else None,
            # A native peer has no identity until the IK handshake proves which
            # static key it holds; this hook runs exactly then.
            on_established=None if has_cookie else self._bind_device_identity,
        )
        connection.ui_token = token if has_cookie else None
        # CSRF only makes sense for a page that could read __Host-ac_csrf;
        # a cookie-bearing native client (no Origin) cannot, and the upgrade
        # origin gate already refused every cross-site caller.
        connection._csrf_exempt = origin is None  # type: ignore[attr-defined]  # type: ignore[attr-defined]
        self._connections[connection_id] = connection
        await connection.run()

    def _origin_allowed(self, origin: str | None, host_header: str = "", client: object = None) -> bool:
        """May this upgrade use the ambient `__Host-ac_ui` cookie?

        Browser pages always send `Origin`; native clients do not. Three rules:
        - a configured allowlist is exact-match only (`*` is refused at config
          load);
        - with no allowlist, the origin must be same-origin with the `Host` the
          upgrade was addressed to (the UI is served by this core, so that is
          the normal case and needs no per-deployment config);
        - a cookie socket with no Origin at all is a native client: browsers
          always send Origin, and a page cannot strip it, so there is no CSWSH
          to defend against. It is accepted (the app/CLI has no Origin), and
          `_csrf_exempt` keeps CSRF off for it because it cannot read the
          `__Host-ac_csrf` cookie either.
        """
        if origin:
            if self._ui_origins:
                return origin in self._ui_origins
            try:
                return urlsplit(origin).netloc == host_header
            except ValueError:
                return False
        return True

    def _note_ws_refused(self, scope: Mapping[str, Any], headers: Mapping[str, str], reason: str) -> None:
        """Audit a refused upgrade: a CSWSH attempt must leave a trace."""
        audit = self._audit
        if audit is None:
            return
        extra: dict[str, Any] = {"reason": reason}
        origin = headers.get("origin")
        if origin:
            extra["origin"] = origin
        ip = self._client_ip(scope, headers)
        if ip:
            extra["ip"] = ip
        try:
            audit.record({
                "kind": "ui.ws_refused",
                "status": "denied",
                "actor": None,
                "method": "WS /ws",
                "extra": extra,
            })
        except Exception as exc:  # noqa: BLE001 - a bad audit row must not 500 the upgrade
            log.error("audit write failed (kind=ui.ws_refused): %s", exc,
                      extra={"code": None, "msg": "audit_unavailable"})


    def _bind_device_identity(self, connection: Connection) -> None:
        """Name a Noise peer from the key the handshake just verified.

        `CryptoSession.read_handshake` computes the fingerprint of the peer's
        static key and refuses an unlisted one *before* msg2, so by the time
        this hook runs the key is already trusted. The whitelist then says
        which pairing owns it (`module_id#instance_id`).

        The credential itself is recorded here and nowhere else: `device_key`
        is the fingerprint the handshake proved (a fact about the key, not a
        claim) and `auth_mode` becomes `device_key`. That attribute used to be
        missing from `Connection` entirely, so `approval/decide` — which reads
        it — answered `APPROVER_INVALID` for every peer that had actually done
        the work, including the device key it is supposed to accept. A peer
        that registers as a module is demoted to `module_key` by
        `App.register_module`: proving a key says nothing about *whose* turn a
        module may approve.

        No match, no whitelist, or a crypto object without a fingerprint still
        leaves `device_id=None` — an unattributable connection must not be able
        to submit a `file_id`, and guessing an identity from anything the peer
        sent over the wire (a query string, a header, an RPC field) would turn
        a hint into a claim. The credential class is independent of that: the
        key was verified either way.
        """
        crypto = connection.crypto
        fingerprint = getattr(crypto, "remote_fingerprint", None) if crypto is not None else None
        if not fingerprint:
            return
        connection.device_key = str(fingerprint)
        # The whitelist names MODULES (`module_id#instance_id`), not UI devices.
        # Granting `device_key` to every verified static key let a whitelisted
        # module approve its own `ui/tool.call` (same connection, same
        # request_id). Only a resolved UI identity may approve; everything else
        # stays `module_key` and is refused by `ALLOWED_APPROVER_KINDS`.
        connection.auth_mode = "module_key"
        if self._whitelist is None:
            return
        resolver = getattr(self._whitelist, "device_for_fingerprint", None)
        if not callable(resolver):
            return
        device_id = resolver(str(fingerprint))
        if device_id:
            # The identity is still recorded for blob ownership; only the
            # *credential class* distinguishes a UI device from a module.
            connection.device_id = str(device_id)
            if "#" not in str(device_id) and str(device_id).startswith("ui-"):
                connection.auth_mode = "device_key"

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
        keys = {key for key, meta in self._sessions.items() if meta["device_id"] == device_id}
        return self._drop_sessions(keys, reason="session revoked")

    def sweep_sessions(self) -> tuple[str, ...]:
        """Remove expired browser sessions and close the sockets they own.

        Expiry already *denies* an expired token on every lookup; this is what
        also takes the row and its live socket out of the process. A socket
        whose cookie expired mid-session must be closed for the same reason a
        revoked one is: the identity behind it no longer exists, and leaving it
        open would let an expired session keep receiving events.

        Sync on purpose: `Registry`/governance hooks run off-loop and need to
        call it, and the only effect is `pop` + `request_close` (thread-safe).
        """
        return self._drop_sessions(set(self._sessions.prune()), reason="session expired")

    def _drop_sessions(self, keys: set[str], *, reason: str) -> tuple[str, ...]:
        """Drop already-hashed `keys` from the table and close their sockets.

        `keys` are hashes, because that is the only thing the table can hand
        back (`items()`, `prune()`); the socket still carries the raw cookie
        value in `ui_token`, so the comparison hashes it through the same
        `session_key`. An empty set (or a `None`/empty token) must never match
        a connection: `session_key` maps those to `""`, which is filtered out.
        """
        keys = {key for key in keys if key}
        if not keys:
            return ()
        self._sessions.discard_many(keys)
        closed: list[str] = []
        for connection in list(self._connections.values()):
            if not connection.browser_session:
                continue
            if session_key(getattr(connection, "ui_token", None)) in keys:
                closed.append(connection.connection_id)
                connection.request_close(1001, reason)
        return tuple(closed)

    async def _sweep_loop(self) -> None:
        """Periodic expiry sweep; the interval only bounds memory, not access."""
        while True:
            await asyncio.sleep(self._ui_session_sweep_s)
            try:
                closed = self.sweep_sessions()
            except Exception:  # pragma: no cover - a sweeper must not die silently
                continue
            if closed:
                log.info(
                    "expired browser sessions closed",
                    extra={"code": None, "msg": f"{len(closed)} connection(s)"},
                )
