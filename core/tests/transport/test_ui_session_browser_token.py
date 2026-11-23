"""`POST /ui/session` can hand a browser the session it cannot read from `Set-Cookie`.

T5/D8: a wasm page cannot read `Set-Cookie` (fetch forbids it), so first-run
pairing used to burn the one-time code and then fail at the `/ws` upgrade. The
fix is a second, **opt-in** channel on the same mint: a browser that says it
cannot read the cookie gets the same credential in the JSON body.

What this file pins, in the order it matters:

* cookie mode is byte-identical when nobody asks — same body, same two
  `Set-Cookie` lines, and the cookie still authenticates `/ui/blob` and `/ws`;
* the body token appears only for a request that (a) carries
  `x-vade-session-token: 1`, (b) carries an `Origin`, and (c) passes the same
  origin gate `DELETE /ui/session` and `/ws` use — so a JVM/Android client and
  a foreign page never get a JS-readable copy of an `HttpOnly` credential;
* it is the *same* credential: the value equals `__Host-ac_ui`, `/ws` accepts
  it, and `DELETE /ui/session` revokes it for every reader;
* it never reaches a log, the audit trail or `data_dir`;
* the browser can even send its preflight: `OPTIONS /ui/session` answers the
  allowlisted origin (and only it), because `content-type: application/json`
  and the opt-in header are both non-simple;
* with no `[server].ui_origins`, the page-shell case (Origin netloc == Host)
  is accepted by the same rule `/ws` already used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from vade.blob.http import BlobApp
from vade.blob.service import BlobService
from vade.blob.settings import AttachSettings
from vade.config import load_config
from vade.identity.pairing import PairingStore, Purpose
from vade.transport.server import UI_SESSION_TOKEN_HEADER, CoreApp

ORIGIN = "http://127.0.0.1:8080"
SAME_ORIGIN = "http://127.0.0.1:8443"
#: Shape-valid, never stored: `/ui/blob` authenticates before it looks the id up.
MISSING_BLOB = "blb_00000000000000000000000000"


# ------------------------------------------------------------------- transport
@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.body or b"{}")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def all_headers(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]


class _Socket:
    """Minimal ASGI endpoint: collected frames in, queued messages out."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(self, message: dict[str, Any]) -> None:
        self.frames.append(message)

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()


async def _http(
    app: CoreApp,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> Response:
    sock = _Socket()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 54321),
    }
    await app(scope, receive, sock.send)
    start = sock.frames[0]
    payload = sock.frames[1] if len(sock.frames) > 1 else {"body": b""}
    return Response(
        status=int(start["status"]),
        headers=[(k.decode(), v.decode()) for k, v in start.get("headers", [])],
        body=payload.get("body") or b"",
    )


def _build(tmp_path, *, ui_origins: list[str] | None = None) -> tuple[CoreApp, PairingStore]:
    overrides: dict[str, Any] = {"core.data_dir": str(tmp_path)}
    if ui_origins is not None:
        overrides["server.ui_origins"] = ui_origins
    blob = BlobApp(
        BlobService(tmp_path / "blob", AttachSettings(read_scope="core")),
        origins=(ORIGIN,),
    )
    pairing = PairingStore(tmp_path / "pairing.sqlite3")
    app = CoreApp(
        config=load_config(cli=overrides),
        pairing=pairing,
        connections={},
        blob=blob,
        data_dir=tmp_path,
    )
    return app, pairing


async def _mint(
    app: CoreApp,
    pairing: PairingStore,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    code = pairing.create(Purpose.BROWSER)
    return await _http(
        app, "POST", "/ui/session",
        headers=headers,
        body=json.dumps({"pair_code": code, "client_name": "pytest"}).encode(),
    )


def _cookie_token(response: Response) -> str:
    ui_cookie = next(c for c in response.all_headers("set-cookie") if c.startswith("__Host-ac_ui="))
    return ui_cookie.split("__Host-ac_ui=", 1)[1].split(";", 1)[0]


async def _blob_get(app: CoreApp, token: str, csrf: str) -> Response:
    return await _http(
        app, "GET", f"/ui/blob/{MISSING_BLOB}",
        headers={
            "cookie": f"__Host-ac_ui={token}; __Host-ac_csrf={csrf}",
            "origin": ORIGIN,
            "sec-fetch-site": "same-origin",
            "x-vade-csrf": csrf,
        },
    )


async def _ws_first_frame(app: CoreApp, token: str, *, origin: str = ORIGIN) -> dict[str, Any]:
    sock = _Socket()
    task = asyncio.create_task(app(
        {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"cookie", f"__Host-ac_ui={token}".encode()),
                        (b"origin", origin.encode())],
        },
        sock.receive, sock.send,
    ))
    deadline = asyncio.get_running_loop().time() + 2
    while not sock.frames:
        if asyncio.get_running_loop().time() > deadline:
            task.cancel()
            raise AssertionError("the WS upgrade produced no frame")
        await asyncio.sleep(0.01)
    first = sock.frames[0]
    sock.inbox.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(task, timeout=2)
    return first


def _grep_tree(root: Path, needle: str) -> list[Path]:
    raw = needle.encode("utf-8")
    return [
        path for path in sorted(root.rglob("*"))
        if path.is_file() and raw in path.read_bytes()
    ]


# ----------------------------------------------------------------------- tests
@pytest.mark.asyncio
async def test_cookie_mode_is_unchanged_when_the_client_does_not_opt_in(tmp_path):
    app, pairing = _build(tmp_path, ui_origins=[ORIGIN])

    response = await _mint(app, pairing)

    assert response.status == 200, response.body
    assert set(response.json) == {"device_id"}, "no token without an explicit request"
    cookies = response.all_headers("set-cookie")
    assert len(cookies) == 2, cookies
    assert any(c.startswith("__Host-ac_ui=") for c in cookies)
    assert any(c.startswith("__Host-ac_csrf=") for c in cookies)

    # The cookie is still the credential every other reader accepts.
    token = _cookie_token(response)
    csrf = next(c for c in cookies if c.startswith("__Host-ac_csrf=")).split("=", 1)[1].split(";", 1)[0]
    assert (await _blob_get(app, token, csrf)).status == 404  # authenticated, blob absent
    assert (await _ws_first_frame(app, token))["type"] == "websocket.accept"


@pytest.mark.asyncio
async def test_the_token_is_returned_only_to_an_explicit_browser_on_an_allowed_origin(tmp_path):
    app, pairing = _build(tmp_path, ui_origins=[ORIGIN])
    opt_in = {UI_SESSION_TOKEN_HEADER: "1"}

    requested = await _mint(app, pairing, headers={**opt_in, "origin": ORIGIN})
    assert requested.status == 200, requested.body
    assert requested.json["session_token"] == _cookie_token(requested)
    assert requested.header("access-control-allow-origin") == ORIGIN

    # A native client (no Origin) can read Set-Cookie; it must not get the copy.
    native = await _mint(app, pairing, headers=opt_in)
    assert native.status == 200, native.body
    assert "session_token" not in native.json

    # A page on an origin the gate refuses gets nothing at all.
    foreign = await _mint(app, pairing, headers={**opt_in, "origin": "https://evil.example"})
    assert foreign.status == 403
    assert foreign.json["code"] == "ORIGIN_MISMATCH"

    # Only the exact opt-in value counts; a stray truthy value changes nothing.
    loose = await _mint(app, pairing, headers={UI_SESSION_TOKEN_HEADER: "true", "origin": ORIGIN})
    assert loose.status == 200, loose.body
    assert "session_token" not in loose.json


@pytest.mark.asyncio
async def test_the_token_is_the_same_credential_and_the_same_revoke_kills_it(tmp_path):
    app, pairing = _build(tmp_path, ui_origins=[ORIGIN])
    minted = await _mint(app, pairing, headers={UI_SESSION_TOKEN_HEADER: "1", "origin": ORIGIN})
    token = minted.json["session_token"]
    csrf = next(
        c for c in minted.all_headers("set-cookie") if c.startswith("__Host-ac_csrf=")
    ).split("=", 1)[1].split(";", 1)[0]

    # The body copy authenticates exactly like the cookie it mirrors.
    assert (await _blob_get(app, token, csrf)).status == 404
    assert (await _ws_first_frame(app, token))["type"] == "websocket.accept"

    revoked = await _http(
        app, "DELETE", "/ui/session",
        headers={"origin": ORIGIN, "cookie": f"__Host-ac_ui={token}; __Host-ac_csrf={csrf}"},
    )
    assert revoked.status == 204, revoked.body

    assert (await _blob_get(app, token, csrf)).status == 401
    refused = await _ws_first_frame(app, token)
    assert refused["type"] == "websocket.close"
    assert refused["code"] == 1008
    assert token not in app._sessions  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_token_never_reaches_a_log_an_audit_row_or_the_data_dir(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app, pairing = _build(tmp_path, ui_origins=[ORIGIN])

    minted = await _mint(app, pairing, headers={UI_SESSION_TOKEN_HEADER: "1", "origin": ORIGIN})
    token = minted.json["session_token"]

    assert token not in caplog.text
    assert _grep_tree(tmp_path, token) == []
    # The only place the value is readable is the field that names it.
    assert minted.body.count(token.encode("utf-8")) == 1
    # ... and the response that does carry it is uncacheable.
    assert minted.header("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_preflight_answers_the_allowed_origin_and_refuses_everyone_else(tmp_path):
    app, _pairing = _build(tmp_path, ui_origins=[ORIGIN])

    allowed = await _http(app, "OPTIONS", "/ui/session", headers={"origin": ORIGIN})
    assert allowed.status == 204, allowed.body
    assert allowed.header("access-control-allow-origin") == ORIGIN
    assert allowed.header("access-control-allow-credentials") == "true"
    assert allowed.header("access-control-allow-methods") == "POST, DELETE, OPTIONS"
    allow_headers = allowed.header("access-control-allow-headers") or ""
    assert UI_SESSION_TOKEN_HEADER in allow_headers
    assert "content-type" in allow_headers
    assert allowed.header("access-control-max-age") == "600"
    assert allowed.body == b""

    refused = await _http(app, "OPTIONS", "/ui/session", headers={"origin": "https://evil.example"})
    assert refused.status == 403
    assert refused.header("access-control-allow-origin") is None

    # No Origin at all is not a browser preflight: keep the generic 405.
    not_a_preflight = await _http(app, "OPTIONS", "/ui/session")
    assert not_a_preflight.status == 405
    assert "OPTIONS" in (not_a_preflight.header("allow") or "")


@pytest.mark.asyncio
async def test_a_same_origin_page_needs_no_allowlist(tmp_path):
    """The page-shell deployment: served by this listener, so Origin == Host."""
    app, pairing = _build(tmp_path)  # no [server].ui_origins
    page = {"origin": SAME_ORIGIN, "host": "127.0.0.1:8443", UI_SESSION_TOKEN_HEADER: "1"}

    preflight = await _http(app, "OPTIONS", "/ui/session", headers=page)
    assert preflight.status == 204, preflight.body

    minted = await _mint(app, pairing, headers=page)
    assert minted.status == 200, minted.body
    assert minted.json["session_token"] == _cookie_token(minted)

    # A different page origin on the same host is still refused (exact match).
    foreign = await _mint(
        app, pairing,
        headers={"origin": "https://evil.example:8443", "host": "127.0.0.1:8443",
                 UI_SESSION_TOKEN_HEADER: "1"},
    )
    assert foreign.status == 403

    # DELETE takes the same decision, so the page can leave the core cleanly.
    token = minted.json["session_token"]
    revoked = await _http(
        app, "DELETE", "/ui/session",
        headers={"origin": SAME_ORIGIN, "host": "127.0.0.1:8443",
                 "cookie": f"__Host-ac_ui={token}"},
    )
    assert revoked.status == 204, revoked.body
