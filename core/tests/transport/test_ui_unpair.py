"""`DELETE /ui/session`: browser session self-revoke before switching cores.

The client needs to leave a core it no longer talks to, so the old core must
stop honouring the `__Host-ac_ui` cookie it minted — including closing the
`/ws` socket that cookie authenticated. Three rules are pinned here:

* the revoke is scoped to the *presented* cookie: a second logged-in browser
  keeps working, and a repeat/no-cookie call is `401`, never a wildcard revoke;
* the row is really gone, so every other reader of the same table (`/ws`, the
  `/ui/blob` cookie gate) denies the old token afterwards;
* each attempt writes `ui.unpaired` with `status: ok|denied`, and never the
  token itself.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from vade.audit.reader import AuditReader
from vade.audit.writer import AuditWriter
from vade.blob.http import BlobApp
from vade.blob.service import BlobService
from vade.blob.settings import AttachSettings
from vade.config import load_config
from vade.identity.pairing import PairingStore, Purpose
from vade.transport.server import CoreApp

ORIGIN = "http://127.0.0.1:8080"
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


def _config(tmp_path) -> Any:
    return load_config(cli={
        "core.data_dir": str(tmp_path),
        "server.ui_origins": [ORIGIN],
    })


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


async def _login(app: CoreApp, pairing: PairingStore) -> tuple[str, str, str]:
    """Drive a real `POST /ui/session`; return `(device_id, ui_token, csrf)`."""
    code = pairing.create(Purpose.BROWSER)
    response = await _http(
        app, "POST", "/ui/session",
        body=json.dumps({"pair_code": code, "client_name": "pytest"}).encode(),
    )
    assert response.status == 200, response.body
    cookies = response.all_headers("set-cookie")
    ui_cookie = next(c for c in cookies if c.startswith("__Host-ac_ui="))
    csrf_cookie = next(c for c in cookies if c.startswith("__Host-ac_csrf="))
    token = ui_cookie.split("__Host-ac_ui=", 1)[1].split(";", 1)[0]
    csrf = csrf_cookie.split("__Host-ac_csrf=", 1)[1].split(";", 1)[0]
    return response.json["device_id"], token, csrf


def _cookie(token: str, csrf: str) -> str:
    return f"__Host-ac_ui={token}; __Host-ac_csrf={csrf}"


async def _blob_get(app: CoreApp, token: str, csrf: str) -> Response:
    """A protected UI HTTP route that authenticates with the same cookie."""
    return await _http(
        app, "GET", f"/ui/blob/{MISSING_BLOB}",
        headers={
            "cookie": _cookie(token, csrf),
            "origin": ORIGIN,
            "sec-fetch-site": "same-origin",
            "x-vade-csrf": csrf,
        },
    )


async def _delete(app: CoreApp, cookie: str | None, *, origin: str = ORIGIN) -> Response:
    headers: dict[str, str] = {"origin": origin}
    if cookie is not None:
        headers["cookie"] = cookie
    return await _http(app, "DELETE", "/ui/session", headers=headers)


def _build(tmp_path, *, audit: Any = None) -> tuple[CoreApp, PairingStore]:
    blob = BlobApp(
        BlobService(tmp_path / "blob", AttachSettings(read_scope="core")),
        origins=(ORIGIN,),
    )
    pairing = PairingStore(tmp_path / "pairing.sqlite3")
    app = CoreApp(
        config=_config(tmp_path),
        pairing=pairing,
        connections={},
        blob=blob,
        audit=audit,
    )
    return app, pairing


# ----------------------------------------------------------------------- tests
@pytest.mark.asyncio
async def test_delete_revokes_only_the_session_that_presented_its_cookie(tmp_path):
    app, pairing = _build(tmp_path)
    device_a, token_a, csrf_a = await _login(app, pairing)
    device_b, token_b, csrf_b = await _login(app, pairing)
    assert device_a != device_b

    # Precondition: both cookies are live on a cookie-gated HTTP route.
    assert (await _blob_get(app, token_a, csrf_a)).status == 404
    assert (await _blob_get(app, token_b, csrf_b)).status == 404

    response = await _delete(app, _cookie(token_a, csrf_a))
    assert response.status == 204, response.body
    assert response.body == b""
    cleared = " ".join(response.all_headers("set-cookie"))
    assert "__Host-ac_ui=;" in cleared and "Max-Age=0" in cleared
    assert "__Host-ac_csrf=;" in cleared and "Max-Age=0" in cleared

    # The revoked cookie is dead everywhere the same table is read.
    gone = await _blob_get(app, token_a, csrf_a)
    assert gone.status == 401, gone.body
    assert gone.json["code"] == "AUTH_FAILED"

    # The other browser never noticed.
    other = await _blob_get(app, token_b, csrf_b)
    assert other.status == 404, other.body
    assert token_b in dict(app._sessions)  # type: ignore[attr-defined]
    assert token_a not in dict(app._sessions)  # type: ignore[attr-defined]

    # A live socket authenticated by the revoked cookie is closed with 1001.
    sock = _Socket()
    task = asyncio.create_task(app(
        {"type": "websocket", "path": "/ws",
         "headers": [(b"cookie", f"__Host-ac_ui={token_b}".encode())]},
        sock.receive, sock.send,
    ))
    await _wait(lambda: any(f["type"] == "websocket.accept" for f in sock.frames), what="accept")
    assert (await _delete(app, _cookie(token_b, csrf_b))).status == 204
    await _wait(lambda: any(f["type"] == "websocket.close" for f in sock.frames), what="close")
    close = next(f for f in sock.frames if f["type"] == "websocket.close")
    assert close["code"] == 1001
    sock.inbox.put_nowait({"type": "websocket.disconnect"})
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_a_repeat_delete_is_401_and_cannot_touch_another_session(tmp_path):
    app, pairing = _build(tmp_path)
    _device_a, token_a, csrf_a = await _login(app, pairing)
    _device_b, token_b, csrf_b = await _login(app, pairing)

    assert (await _delete(app, _cookie(token_a, csrf_a))).status == 204

    sessions = dict(app._sessions)  # type: ignore[attr-defined]
    assert set(sessions) == {token_b}

    for _ in range(2):
        again = await _delete(app, _cookie(token_a, csrf_a))
        assert again.status == 401, again.body
        assert again.json["code"] == "AUTH_FAILED"
        # The refused call must not be a wildcard: the live row survives it.
        assert dict(app._sessions) == sessions  # type: ignore[attr-defined]

    assert (await _blob_get(app, token_b, csrf_b)).status == 404


@pytest.mark.asyncio
async def test_delete_without_a_live_cookie_is_401_and_a_foreign_origin_is_403(tmp_path):
    app, pairing = _build(tmp_path)

    anonymous = await _delete(app, None)
    assert anonymous.status == 401
    assert anonymous.json["code"] == "AUTH_FAILED"

    forged = await _delete(app, "__Host-ac_ui=acc_not-a-real-token; __Host-ac_csrf=x")
    assert forged.status == 401
    assert forged.json["code"] == "AUTH_FAILED"

    # A real session must not be reachable from a cookie-less call, and a
    # cross-origin page must not be able to revoke it either.
    _device, token, csrf = await _login(app, pairing)
    assert (await _delete(app, None)).status == 401
    refused = await _delete(app, _cookie(token, csrf), origin="https://evil.example")
    assert refused.status == 403
    assert refused.json["code"] == "ORIGIN_MISMATCH"
    assert (await _blob_get(app, token, csrf)).status == 404


@pytest.mark.asyncio
async def test_unpair_writes_audit_rows_for_ok_and_denied_attempts(tmp_path):
    writer = AuditWriter(tmp_path / "audit.db", sync=True)
    app, pairing = _build(tmp_path, audit=writer)
    try:
        device_id, token, csrf = await _login(app, pairing)
        assert (await _delete(app, _cookie(token, csrf))).status == 204
        assert (await _delete(app, _cookie(token, csrf))).status == 401
        assert (await _delete(app, None)).status == 401
    finally:
        writer.close()

    rows = AuditReader(tmp_path / "audit.db").query(kind="ui.unpaired", limit=50)["items"]
    assert len(rows) == 3, rows
    by_status = {row["status"]: row for row in rows}
    assert set(by_status) == {"ok", "denied"}

    ok = by_status["ok"]
    assert ok["actor"] == device_id
    assert ok["method"] == "DELETE /ui/session"
    assert ok["extra"]["client_name"] == "pytest"
    # The credential itself must never be persisted — the log is readable.
    assert token not in json.dumps(rows, ensure_ascii=False)
    assert csrf not in json.dumps(rows, ensure_ascii=False)
    assert by_status["denied"]["actor"] is None
    assert by_status["denied"]["extra"]["reason"] == "no live session"


# --------------------------------------------------------------------- helpers
async def _wait(predicate, *, timeout: float = 2.0, what: str = "condition") -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")
