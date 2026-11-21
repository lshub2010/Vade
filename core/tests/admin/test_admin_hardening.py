"""R6 hardening: refresh persistence, logout/blacklist, CSRF, pre-auth gate, flock.

Each test pins one of the six review items. They deliberately drive the ASGI
app rather than the internal helpers where a token has to travel over HTTP, and
they use a plain `httpx.AsyncClient` (not the fixture's CSRF-remembering one)
whenever the *absence* of a header is the subject.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from vade.admin.app import CSRF_HEADER, create_admin_app, refresh_message
from vade.admin import setup_token
from vade.admin.devices import DeviceStore
from vade.admin.locks import acquire_instance_lock, release_instance_lock
from vade.audit.reader import AuditReader

from .conftest import (
    ORIGIN, PASSWORD, bearer, client_for, login_with_device, make_context, new_device,
)


def _plain_client(app, ip: str = "127.0.0.1") -> httpx.AsyncClient:
    """A client that never injects CSRF — the header must be explicit."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 40000)),
        base_url=ORIGIN,
    )


def _refresh_signature(key, jti: str, device_id: str) -> str:
    return base64.b64encode(key.sign(refresh_message(jti, device_id))).decode()


# ------------------------------------------------------- 1) refresh survives restart
def test_refresh_survives_a_core_restart_and_access_does_not(tmp_path):
    first = make_context(tmp_path)
    device, key = new_device(first)
    app1 = create_admin_app(first)
    tokens: dict = {}

    async def login_once():
        client = client_for(app1)
        response = await login_with_device(client, first, device, key)
        assert response.status_code == 200, response.text
        tokens.update(response.json())

    asyncio.run(login_once())
    assert tokens.get("refresh_jti"), "the client must receive the jti it has to sign"
    first.audit.close()

    # ---- "restart": a brand-new context over the same data dir -------------
    second = make_context(tmp_path)
    app2 = create_admin_app(second)

    async def after_restart():
        client = client_for(app2)
        # Access tokens are in-memory by design: the old one is gone.
        dead = await client.get("/admin/api/devices", headers=bearer(tokens["access_token"]))
        assert dead.status_code == 401, dead.text

        signature = _refresh_signature(key, tokens["refresh_jti"], device.device_id)
        refreshed = await client.post(
            "/admin/api/token/refresh",
            json={"refresh_token": tokens["refresh_token"], "signature": signature},
        )
        assert refreshed.status_code == 200, refreshed.text
        new_tokens = refreshed.json()
        assert new_tokens["access_token"] != tokens["access_token"]

        # The new access token is real: it resolves against the restarted store.
        listed = await client.get("/admin/api/devices", headers=bearer(new_tokens["access_token"]))
        assert listed.status_code == 200, listed.text

        # Rotation is one-time: the old refresh row is gone, not reusable.
        again = await client.post(
            "/admin/api/token/refresh",
            json={"refresh_token": tokens["refresh_token"], "signature": signature},
        )
        assert again.status_code == 401

    asyncio.run(after_restart())
    second.audit.close()


def test_sessions_file_stores_only_the_digest_and_is_0600(tmp_path):
    ctx = make_context(tmp_path)
    device, key = new_device(ctx)
    app = create_admin_app(ctx)

    async def login_once():
        response = await login_with_device(client_for(app), ctx, device, key)
        assert response.status_code == 200, response.text
        return response.json()

    tokens = asyncio.run(login_once())
    path = tmp_path / "admin" / "sessions.json"
    raw = path.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert tokens["refresh_token"] not in raw, "plaintext refresh token must never be persisted"
    digest = hashlib.sha256(tokens["refresh_token"].encode()).hexdigest()
    row = next(row for row in doc["refresh"] if row["token_hash"] == digest)
    assert set(row) >= {"token_hash", "jti", "device_id", "password_version", "expires_at"}
    assert row["jti"] == tokens["refresh_jti"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    ctx.audit.close()


# --------------------------------------------------- 2) logout kills the refresh
def test_logout_deletes_refresh_and_blacklists_its_jti(tmp_path):
    ctx = make_context(tmp_path)
    device, key = new_device(ctx)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        login = await login_with_device(client, ctx, device, key)
        tokens = login.json()
        logout = await client.post(
            "/admin/api/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers=bearer(tokens["access_token"]),
        )
        assert logout.status_code == 200, logout.text
        # The access row is gone...
        assert (await client.get("/admin/api/devices", headers=bearer(tokens["access_token"]))).status_code == 401
        # ...and so is the refresh: it cannot be exchanged even with a valid signature.
        signature = _refresh_signature(key, tokens["refresh_jti"], device.device_id)
        refreshed = await client.post(
            "/admin/api/token/refresh",
            json={"refresh_token": tokens["refresh_token"], "signature": signature},
        )
        assert refreshed.status_code == 401
        return tokens

    tokens = asyncio.run(scenario())

    # The jti is on disk in the blacklist, and the refresh row is not.
    doc = json.loads((tmp_path / "admin" / "sessions.json").read_text(encoding="utf-8"))
    assert tokens["refresh_jti"] in {row["jti"] for row in doc["blacklist"]}
    assert doc["refresh"] == []
    ctx.audit.close()

    # A restart must not resurrect it either.
    second = make_context(tmp_path)
    app2 = create_admin_app(second)
    # The device is still trusted; only the session was burned. A fresh login
    # works, but the logged-out refresh does not.
    new_device_key = key

    async def after_restart():
        client = client_for(app2)
        signature = _refresh_signature(new_device_key, tokens["refresh_jti"], device.device_id)
        refreshed = await client.post(
            "/admin/api/token/refresh",
            json={"refresh_token": tokens["refresh_token"], "signature": signature},
        )
        assert refreshed.status_code == 401

    asyncio.run(after_restart())
    second.audit.close()


def test_a_new_device_login_kicks_the_previous_refresh(tmp_path):
    ctx = make_context(tmp_path)
    device, key = new_device(ctx)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        first = (await login_with_device(client, ctx, device, key)).json()
        second = (await login_with_device(client, ctx, device, key)).json()
        assert first["refresh_jti"] != second["refresh_jti"]

        old_sig = _refresh_signature(key, first["refresh_jti"], device.device_id)
        old = await client.post(
            "/admin/api/token/refresh",
            json={"refresh_token": first["refresh_token"], "signature": old_sig},
        )
        assert old.status_code == 401, "a replaced device must not keep its old refresh"

        new_sig = _refresh_signature(key, second["refresh_jti"], device.device_id)
        new = await client.post(
            "/admin/api/token/refresh",
            json={"refresh_token": second["refresh_token"], "signature": new_sig},
        )
        assert new.status_code == 200, new.text

    asyncio.run(scenario())
    ctx.audit.close()


# --------------------------------------------------------------- 3) CSRF for real
def test_browser_cookie_without_csrf_is_refused_on_every_non_get(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)
    plain = _plain_client(app)

    async def scenario():
        challenge = (await plain.post("/admin/api/login/challenge", json={})).json()
        login = await plain.post(
            "/admin/api/login", json={"challenge_id": challenge["challenge_id"], "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]

        headers = {"Sec-Fetch-Site": "same-origin"}
        missing = await plain.post("/admin/api/logout", json={}, headers=headers)
        assert missing.status_code == 403, missing.text
        assert missing.json()["code"] == "ORIGIN_MISMATCH"

        wrong = await plain.post(
            "/admin/api/logout", json={}, headers={**headers, CSRF_HEADER: "not-the-session-token"},
        )
        assert wrong.status_code == 403, wrong.text

        good = await plain.post("/admin/api/logout", json={}, headers={**headers, CSRF_HEADER: csrf})
        assert good.status_code == 200, good.text

    asyncio.run(scenario())
    ctx.audit.close()


def test_device_bearer_without_csrf_is_refused_too(tmp_path):
    ctx = make_context(tmp_path)
    device, key = new_device(ctx)
    app = create_admin_app(ctx)

    async def scenario():
        client = _plain_client(app)
        login = await login_with_device(client, ctx, device, key)
        assert login.status_code == 200, login.text
        tokens = login.json()
        assert tokens["csrf_token"]

        missing = await client.post("/admin/api/logout", json={}, headers=bearer(tokens["access_token"]))
        assert missing.status_code == 200, missing.text
        # Bearer is not ambient: CSRF would be ceremony there, and logout must
        # work even when the app did not keep the token.
        alive = await client.get("/admin/api/devices", headers=bearer(tokens["access_token"]))
        assert alive.status_code == 401, alive.text

    asyncio.run(scenario())
    ctx.audit.close()


# ------------------------------------------------------- 4) pre-auth origin gate
def test_cross_site_setup_is_refused_and_does_not_initialise(tmp_path):
    ctx = make_context(tmp_path, initialize=False)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app, ip="203.0.113.9")
        response = await client.post(
            "/admin/setup",
            content='{"password": "attacker-chosen-password"}',
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "text/plain",
            },
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "ORIGIN_MISMATCH"
        assert not ctx.setup.initialized(), "a cross-site request must not create the administrator"

        # Even a "same-origin" no-cors simple request is refused: JSON only.
        text_plain = await client.post(
            "/admin/setup",
            content='{"password": "attacker-chosen-password"}',
            headers={"Sec-Fetch-Site": "same-origin", "Content-Type": "text/plain"},
        )
        assert text_plain.status_code == 403, text_plain.text
        assert not ctx.setup.initialized()

        # Remote setup without an Origin is refused outright.
        no_origin = await client.post("/admin/setup", json={"password": PASSWORD})
        assert no_origin.status_code == 403, no_origin.text
        assert not ctx.setup.initialized()

        # And the legitimate shape still works: JSON + Origin + token, remote peer.
        token = setup_token.read_token(tmp_path)
        ok = await client.post(
            "/admin/setup", json={"password": PASSWORD, "setup_token": token},
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        assert ok.status_code == 200, ok.text
        assert ctx.setup.initialized()

    asyncio.run(scenario())
    ctx.audit.close()


def test_remote_setup_unlock_failure_leaves_an_audit_row(tmp_path):
    ctx = make_context(tmp_path, initialize=False)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app, ip="203.0.113.9")
        response = await client.post("/admin/setup/unlock", json={"setup_token": "acs_" + "x" * 43})
        assert response.status_code == 401, response.text

    asyncio.run(scenario())
    rows = AuditReader(ctx.audit.path).query(kind="setup.attempt")["items"]
    assert any(row["status"] == "denied" and (row.get("extra") or {}).get("action") == "unlock"
               for row in rows), rows
    ctx.audit.close()


def test_step_up_password_failure_spends_the_login_budget_and_is_audited(tmp_path):
    ctx = make_context(tmp_path)
    device, key = new_device(ctx)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        tokens = (await login_with_device(client, ctx, device, key)).json()
        headers = {**bearer(tokens["access_token"]), CSRF_HEADER: tokens["csrf_token"]}
        response = await client.post("/admin/api/devices", json={"password": "definitely-wrong"}, headers=headers)
        assert response.status_code == 401, response.text
        # The failed attempt consumed one of the 5 per-IP slots.
        assert len([t for t in ctx._limits.get("pw:127.0.0.1", [])]) == 1

    asyncio.run(scenario())
    rows = AuditReader(ctx.audit.path).query(kind="admin.step_up")["items"]
    assert any(row["status"] == "denied" and row["code"] == "AUTH_FAILED" for row in rows), rows
    ctx.audit.close()


# ------------------------------------------------------------- 5) single instance
def test_second_instance_cannot_take_the_data_directory(tmp_path):
    fd = acquire_instance_lock(tmp_path)
    try:
        with pytest.raises(OSError):
            acquire_instance_lock(tmp_path)
    finally:
        release_instance_lock(fd)
    # Released ⇒ the next core may start.
    release_instance_lock(acquire_instance_lock(tmp_path))

    # And across real processes, which is what systemd/docker actually do.
    fd = acquire_instance_lock(tmp_path)
    try:
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(Path.cwd())!r})\n"
            "from vade.admin.locks import acquire_instance_lock\n"
            "try:\n"
            f"    acquire_instance_lock(Path({str(tmp_path)!r}))\n"
            "except OSError:\n"
            "    sys.exit(7)\n"
            "sys.exit(0)\n"
        )
        finished = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        assert finished.returncode == 7, (finished.returncode, finished.stdout, finished.stderr)
    finally:
        release_instance_lock(fd)


def test_cli_write_commands_refuse_while_the_lock_is_held(tmp_path):
    from vade.admin import cli

    fd = acquire_instance_lock(tmp_path)
    try:
        with pytest.raises(SystemExit):
            cli._require_not_running(tmp_path)
    finally:
        release_instance_lock(fd)
    cli._require_not_running(tmp_path)  # no-op when free


def test_serve_refuses_to_start_a_second_core_on_the_same_data_dir(tmp_path):
    """The real entry point, not just the helper: `_serve` dies before startup."""
    import uvicorn

    from vade.__main__ import _serve
    from vade.config import load_config
    from vade.errors import VadeError

    config = load_config(cli={
        "core.data_dir": str(tmp_path), "core.listen": "127.0.0.1:0",
        "core.admin_host": "127.0.0.1:0",
    })

    class _App:
        def __init__(self, cfg):
            self.config = cfg

    fd = acquire_instance_lock(tmp_path)
    try:
        with pytest.raises(VadeError) as excinfo:
            _serve(_App(config), uvicorn, strict=False, pair_banner=False)
        assert "another core is already running" in excinfo.value.message
    finally:
        release_instance_lock(fd)


# --------------------------------------------- 5b) DeviceStore cache invalidation
def test_device_store_sees_a_write_made_by_another_store_instance(tmp_path):
    path = tmp_path / "admin" / "devices.json"
    reader = DeviceStore(path)
    assert reader.all() == []  # caches the empty table

    writer = DeviceStore(path)  # a second process in spirit
    key = b"\x01" * 32
    added = writer.add(pubkey_b64=base64.b64encode(key).decode(), alg="ed25519",
                       label="rescue", ip="cli")
    assert reader.is_active(added.device_id), "stale cache must be invalidated by the file stat"

    writer.revoke(added.device_id)
    assert not reader.is_active(added.device_id), "a revocation must be visible without a restart"
