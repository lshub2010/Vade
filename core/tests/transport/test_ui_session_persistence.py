"""Browser sessions outlive a core restart, without a plaintext token on disk.

The user-visible bug this file pins: `UiSessions` lived in process memory only,
so every core restart (an upgrade, a crash, a `systemctl restart`) silently
un-paired every already-paired browser — the browser still held
`__Host-ac_ui`, the core had never heard of it, and the only recovery was a new
pair code.

The rules, in the order they matter:

* a cookie minted before the restart is still accepted after it, by the HTTP
  gate (`/ui/blob`, the protected route) **and** by the WS upgrade;
* `DELETE /ui/session` before the restart means the token stays dead after it —
  persisting a credential must not turn a revoke into a "revives on reboot";
* what is on disk is `sha256(token)` and the row, never the token: a `data_dir`
  backup is not a set of live browser credentials;
* an expired row is not loaded (a restart is not a way to extend a TTL);
* a corrupt file is ignored, not fatal, and a sweep/lazy expiry keeps the file
  in step with memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from vade.blob.http import BlobApp
from vade.blob.service import BlobService
from vade.blob.settings import AttachSettings
from vade.config import load_config
from vade.identity.pairing import PairingStore, Purpose
from vade.transport.server import (
    UI_SESSION_TTL_S,
    UI_SESSIONS_FILE,
    CoreApp,
    UiSessions,
    session_key,
)

ORIGIN = "http://127.0.0.1:8080"
#: Shape-valid, never stored: `/ui/blob` authenticates before it looks the id up.
MISSING_BLOB = "blb_00000000000000000000000000"
TTL = UI_SESSION_TTL_S


# ------------------------------------------------------------------- transport
@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.body or b"{}")

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


def _build(tmp_path) -> tuple[CoreApp, PairingStore]:
    """One core, as `__main__._serve` assembles it — `data_dir` included.

    Called twice against the same `tmp_path` this *is* the restart: every
    in-memory structure is new, only `<data_dir>/ui_sessions.json` is shared.
    """
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
        data_dir=tmp_path,
    )
    return app, pairing


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


async def _delete(app: CoreApp, cookie: str | None) -> Response:
    headers: dict[str, str] = {"origin": ORIGIN}
    if cookie is not None:
        headers["cookie"] = cookie
    return await _http(app, "DELETE", "/ui/session", headers=headers)


async def _ws_first_frame(app: CoreApp, token: str) -> dict[str, Any]:
    """Open `/ws` with `token` as the cookie; return the first frame sent.

    `origin` is not decoration: a cookie-bearing upgrade without one is
    refused (the CSWSH gate), so a helper that omitted it would only be able
    to test the refusal.
    """
    sock = _Socket()
    task = asyncio.create_task(app(
        {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"cookie", f"__Host-ac_ui={token}".encode()),
                        (b"origin", ORIGIN.encode())],
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


def _store(tmp_path) -> Path:
    return tmp_path / UI_SESSIONS_FILE


def _grep_tree(root: Path, needle: str) -> list[Path]:
    """Every file under `root` whose bytes contain `needle` (the literal ask)."""
    hits: list[Path] = []
    raw = needle.encode("utf-8")
    for path in sorted(root.rglob("*")):
        if path.is_file() and raw in path.read_bytes():
            hits.append(path)
    return hits


# ----------------------------------------------------------------------- tests
@pytest.mark.asyncio
async def test_a_paired_browser_still_passes_http_and_ws_after_the_restart(tmp_path):
    first, pairing = _build(tmp_path)
    device_id, token, csrf = await _login(first, pairing)
    assert (await _blob_get(first, token, csrf)).status == 404
    assert (await _ws_first_frame(first, token))["type"] == "websocket.accept"

    # Restart: a brand-new app object, nothing in memory carried over.
    second, _pairing = _build(tmp_path)
    assert second._sessions is not first._sessions  # type: ignore[attr-defined]
    assert len(second._sessions) == 1  # type: ignore[attr-defined]
    assert second._sessions[token]["device_id"] == device_id  # type: ignore[attr-defined]

    # The old cookie is still a live session, on both credential paths.
    reloaded = await _blob_get(second, token, csrf)
    assert reloaded.status == 404, reloaded.body  # authenticated; blob is absent
    assert (await _ws_first_frame(second, token))["type"] == "websocket.accept"

    # Persistence must not become a wildcard: an unknown token is still refused.
    assert (await _blob_get(second, "acc_never-minted", csrf)).status == 401
    refused = await _ws_first_frame(second, "acc_never-minted")
    assert refused["type"] == "websocket.close"
    assert refused["code"] == 1008


@pytest.mark.asyncio
async def test_a_revoked_session_does_not_come_back_after_the_restart(tmp_path):
    first, pairing = _build(tmp_path)
    _device, token, csrf = await _login(first, pairing)
    _other_device, other_token, other_csrf = await _login(first, pairing)

    assert (await _delete(first, _cookie(token, csrf))).status == 204

    second, _pairing = _build(tmp_path)
    gone = await _blob_get(second, token, csrf)
    assert gone.status == 401, gone.body
    assert gone.json["code"] == "AUTH_FAILED"
    assert token not in second._sessions  # type: ignore[attr-defined]
    # The revoke was scoped: the session nobody deleted is still there.
    assert (await _blob_get(second, other_token, other_csrf)).status == 404

    # And the deleted row is not merely hidden: it is absent from the file.
    assert session_key(token) not in _store(tmp_path).read_text(encoding="utf-8")


def test_an_expired_row_is_not_loaded_and_is_dropped_from_disk(tmp_path):
    now = time.time()
    live = {"device_id": "ui-live", "created_at": now, "revoked": False, "read_only": False}
    stale = {"device_id": "ui-stale", "created_at": now - TTL - 60, "revoked": False, "read_only": False}
    path = _store(tmp_path)
    path.write_text(
        json.dumps({"version": 1, "sessions": {session_key("live-token"): live, session_key("stale-token"): stale}}),
        encoding="utf-8",
    )

    table = UiSessions(path=path)

    assert "live-token" in table
    assert "stale-token" not in table
    assert list(json.loads(path.read_text(encoding="utf-8"))["sessions"]) == [session_key("live-token")]


@pytest.mark.asyncio
async def test_the_store_never_contains_a_plaintext_token_or_csrf(tmp_path):
    app, pairing = _build(tmp_path)
    _device_id, token, csrf = await _login(app, pairing)

    raw = _store(tmp_path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert set(payload["sessions"]) == {session_key(token)}
    assert session_key(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()
    # The row survived, only its key is a digest.
    row = payload["sessions"][session_key(token)]
    assert row["device_id"].startswith("ui-")
    assert isinstance(row["created_at"], float)
    assert row["csrf_hash"] == hashlib.sha256(csrf.encode("utf-8")).hexdigest()

    # `grep -r <token> data_dir` must find nothing, anywhere.
    assert _grep_tree(tmp_path, token) == []
    assert _grep_tree(tmp_path, csrf) == []
    # 0600: the file is the whole credential set, nobody else may read it.
    assert stat_mode(_store(tmp_path)) == 0o600


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.asyncio
async def test_a_corrupt_store_is_ignored_and_the_core_still_serves(tmp_path):
    path = _store(tmp_path)
    path.write_text("{ this is not json", encoding="utf-8")

    app, pairing = _build(tmp_path)  # must not raise

    assert len(app._sessions) == 0  # type: ignore[attr-defined]
    # It still mints, and the first mint replaces the corrupt file with a real one.
    _device, token, csrf = await _login(app, pairing)
    assert (await _blob_get(app, token, csrf)).status == 404
    assert set(json.loads(path.read_text(encoding="utf-8"))["sessions"]) == {session_key(token)}

    # A structurally-wrong file (wrong version, non-digest key) is ignored too.
    path.write_text(
        json.dumps({"version": 999, "sessions": {session_key("x"): {"device_id": "ui-x"}}}),
        encoding="utf-8",
    )
    assert len(UiSessions(path=path)) == 0
    path.write_text(
        json.dumps({"version": 1, "sessions": {"a-literal-token": {"device_id": "ui-x"}}}),
        encoding="utf-8",
    )
    assert len(UiSessions(path=path)) == 0


def test_sweep_and_lazy_expiry_keep_the_file_in_step_with_memory(tmp_path):
    path = _store(tmp_path)
    clock = [1_000_000.0]
    table = UiSessions(path=path, clock=lambda: clock[0])
    table["swept"] = {"device_id": "ui-swept", "created_at": clock[0]}
    table["lazy"] = {"device_id": "ui-lazy", "created_at": clock[0]}
    assert len(json.loads(path.read_text(encoding="utf-8"))["sessions"]) == 2

    # `get` past the deadline: denied *and* removed from disk, so a restart
    # cannot resurrect a row this lookup already refused.
    clock[0] += TTL + 1
    assert table.get("lazy") is None
    assert session_key("lazy") not in path.read_text(encoding="utf-8")

    assert table.prune() == [session_key("swept")]
    assert json.loads(path.read_text(encoding="utf-8"))["sessions"] == {}


def test_every_access_path_hashes_the_key_the_same_way(tmp_path):
    """`get`/`[]`/`in`/`pop`/`del` must all speak the caller's raw token.

    The blob cookie gate is a plain `Mapping.get(token)` handed the raw cookie
    value, so a table whose keys are digests is only safe if every accessor
    applies the transform. Asserting on `dict(table)` proves the storage side;
    the assertions through the API prove the read side.
    """
    table = UiSessions(path=_store(tmp_path))
    token = "acc_" + "x" * 43
    table[token] = {"device_id": "ui-1", "created_at": time.time()}

    assert session_key(token) in dict(table)
    assert token not in dict(table)  # the raw credential is not a stored key

    assert table[token]["device_id"] == "ui-1"
    assert table.get(token) is not None
    assert table.get(token) is not None  # repeat: no state was consumed
    assert token in table

    assert table.pop(token)["device_id"] == "ui-1"
    assert token not in table
    assert table.get(token) is None
    assert table.pop(token, "absent") == "absent"

    table[token] = {"device_id": "ui-2", "created_at": time.time()}
    del table[token]
    assert token not in table
    with pytest.raises(KeyError):
        table[token]
    with pytest.raises(KeyError):
        table.pop(token)


@pytest.mark.asyncio
async def test_without_a_data_dir_the_table_stays_in_memory(tmp_path):
    """Every existing test (and any embedder) keeps the old behaviour."""
    app = CoreApp(config=_config(tmp_path), pairing=PairingStore(tmp_path / "p.sqlite3"), connections={})
    assert app._sessions.path is None  # type: ignore[attr-defined]
    assert not _store(tmp_path).exists()
    # `core.data_dir` in the config is not enough: the assembly point decides.
    assert app._sessions.get("anything") is None  # type: ignore[attr-defined]

    # And the restart really does end the session here: without a file there is
    # nothing to reload, which is exactly why the tests above are meaningful.
    _device_id, token, _csrf = await _login(app, _pairing(tmp_path))
    assert (await _ws_first_frame(app, token))["type"] == "websocket.accept"
    restarted = CoreApp(config=_config(tmp_path), pairing=PairingStore(tmp_path / "p.sqlite3"), connections={})
    assert token not in restarted._sessions  # type: ignore[attr-defined]
    refused = await _ws_first_frame(restarted, token)
    assert refused["type"] == "websocket.close"
    assert refused["code"] == 1008


def _pairing(tmp_path) -> PairingStore:
    return PairingStore(tmp_path / "p.sqlite3")
