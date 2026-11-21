"""Admin API: auth modes, read-only browser sessions, nodes, invites, audit."""

from __future__ import annotations

import asyncio
import base64
import json

import dataclasses

import httpx
import pytest

from vade.admin.app import create_admin_app, login_message
from vade.admin import setup_token
from vade.admin.devices import DeviceStore
from vade.errors import VadeError
from vade.identity.pairing import PairingStore
from vade.registry.api import RegistryAPI
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry

from .conftest import (
    ADMIN_HOST, ORIGIN, PASSWORD, bearer, client_for, login_browser, login_with_device,
    make_context, new_device,
)


def _manifest(tool="read_file", risk="low", instance="fs-01"):
    return validate_manifest({
        "protocol": "vade/v1", "module_id": "com.example.fs", "instance_id": instance,
        "version": "1.0.0", "concurrency": "pooled",
        "limits": {"max_concurrency": 3, "max_inflight": 8, "max_payload": 65519},
        "capabilities": {"tools": [{
            "name": tool, "description": "d", "risk": risk, "timeout_ms": 10000,
            "idempotent": False, "input_schema": {"type": "object"},
        }]},
    })


def _registry(ctx, tool="read_file", risk="low"):
    registry = Registry()
    registry.register(_manifest(tool, risk), fingerprint="a" * 16, connection_id="c1")
    ctx.registry = registry
    ctx.registry_api = RegistryAPI(registry)
    return registry


# ------------------------------------------------------------------ setup gate
def test_setup_gate_hides_the_qr_from_remote_until_unlocked(tmp_path):
    ctx = make_context(tmp_path, initialize=False)
    app = create_admin_app(ctx)

    async def scenario():
        remote = client_for(app, ip="203.0.113.9")
        response = await remote.get("/admin/setup")
        assert response.status_code == 200
        body = response.json()
        assert body == {"setup_required": True, "requires_token": True}
        raw = response.text
        assert "bind_url" not in raw and "fp8" not in raw and "acs_" not in raw

        loopback = client_for(app)
        local = (await loopback.get("/admin/setup")).json()
        assert local["setup_required"] is True
        assert local["requires_token"] is False
        assert local["bind_url"].startswith("vade://bind?")
        assert len(local["fp8"]) == 16

        wrong = await remote.post("/admin/setup/unlock", json={"setup_token": "acs_" + "x" * 43})
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "SETUP_UNAUTHORIZED"

        token = setup_token.read_token(tmp_path)
        unlocked = await remote.post("/admin/setup/unlock", json={"setup_token": token})
        assert unlocked.status_code == 200
        payload = unlocked.json()
        ticket = payload["bind_url"].split("t=")[1].split("&")[0]
        assert len(ticket) == 26
        # The unlock cookie is what makes GET /admin/setup show the QR.
        after = await remote.get("/admin/setup")
        assert after.json()["fp8"] == payload["fp8"]
        assert "bind_url" in after.json()

    asyncio.run(scenario())
    ctx.audit.close()


def test_setup_is_closed_once_initialised(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        response = await client.get("/admin/setup", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/login"
        assert (await client.post("/admin/setup", json={"password": "x" * 16})).status_code == 404

    asyncio.run(scenario())
    ctx.audit.close()


# ----------------------------------------------------------------------- auth
def test_loopback_password_only_is_a_read_only_browser_session(ctx, app):
    asyncio.run(_browser_readonly(app))


async def _browser_readonly(app):
    client = client_for(app)
    response = await login_browser(client, None)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "browser"
    assert "access_token" not in body and "refresh_token" not in body

    cookie = response.headers.get("set-cookie", "")
    assert "__Host-ac_admin=" in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower()
    assert "Path=/" in cookie
    assert "Domain=" not in cookie

    same_origin = {"Sec-Fetch-Site": "same-origin"}
    assert (await client.get("/admin/api/nodes", headers=same_origin)).status_code == 200
    denied = await client.post("/admin/api/invites", json={"purpose": "browser"}, headers=same_origin)
    assert denied.status_code == 403
    assert denied.json()["code"] == "READ_ONLY_SESSION"
    # login itself is exempt, otherwise a browser could never start a session
    assert (await client.post("/admin/api/login/challenge", json={})).status_code == 200


def test_bearer_login_sets_no_cookie(ctx, app):
    async def scenario():
        client = client_for(app)
        response = await client.post(
            "/admin/api/login/challenge", json={},
        )
        challenge = response.json()
        logged = await client.post(
            "/admin/api/login",
            json={"challenge_id": challenge["challenge_id"], "password": PASSWORD},
            headers={"X-AC-Client": "cli"},
        )
        assert logged.status_code == 200
        payload = logged.json()
        assert payload["mode"] == "device" and payload["access_token"]
        assert "set-cookie" not in {k.lower() for k in logged.headers}
        nodes = await client.get("/admin/api/nodes", headers=bearer(payload["access_token"]))
        assert nodes.status_code == 200

    asyncio.run(scenario())


def test_remote_password_only_cannot_write_but_a_signed_device_can(ctx, app):
    async def scenario():
        remote = client_for(app, ip="203.0.113.9")
        readonly = await login_browser(remote, ctx)
        assert readonly.status_code == 200
        assert readonly.json()["mode"] == "browser"
        denied = await remote.post("/admin/api/invites", json={"purpose": "browser", "module_id": "x"},
                                   headers={"Sec-Fetch-Site": "same-origin"})
        assert denied.status_code == 403
        assert denied.json()["code"] == "READ_ONLY_SESSION"

        device, key = new_device(ctx, label="iphone")
        full = await login_with_device(remote, ctx, device, key)
        assert full.status_code == 200, full.text
        token = full.json()["access_token"]
        created = await remote.post("/admin/api/invites",
                                    json={"purpose": "browser"}, headers=bearer(token))
        assert created.status_code == 200, created.text
        assert len(created.json()["pair_code"]) >= 8

    asyncio.run(scenario())


def test_unregistered_key_and_wrong_password_are_one_error(ctx, app):
    async def scenario():
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        remote = client_for(app, ip="203.0.113.9")
        challenge = (await remote.post("/admin/api/login/challenge", json={})).json()
        stranger = Ed25519PrivateKey.generate()
        signature = base64.b64encode(stranger.sign(login_message(challenge["challenge"], ORIGIN))).decode()
        response = await remote.post("/admin/api/login", json={
            "challenge_id": challenge["challenge_id"], "password": PASSWORD,
            "device_id": "deadbeefdeadbeef", "signature": signature,
        }, headers={"Origin": ORIGIN})
        assert response.status_code == 401
        assert response.json() == {"code": "AUTH_FAILED", "message": "password or device is incorrect"}

        challenge = (await remote.post("/admin/api/login/challenge", json={})).json()
        wrong = await remote.post("/admin/api/login", json={
            "challenge_id": challenge["challenge_id"], "password": "not-the-password",
        })
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "AUTH_FAILED"

    asyncio.run(scenario())


def test_a_challenge_is_single_use(ctx, app):
    async def scenario():
        client = client_for(app)
        challenge = (await client.post("/admin/api/login/challenge", json={})).json()
        body = {"challenge_id": challenge["challenge_id"], "password": PASSWORD}
        assert (await client.post("/admin/api/login", json=body)).status_code == 200
        replay = await client.post("/admin/api/login", json=body)
        assert replay.status_code == 401
        assert replay.json()["code"] == "AUTH_FAILED"

    asyncio.run(scenario())


def test_origin_mismatch_is_refused_and_audited(ctx, app):
    async def scenario():
        device, key = new_device(ctx)
        client = client_for(app)
        logged = await login_with_device(client, ctx, device, key)
        token = logged.json()["access_token"]
        response = await client.get("/admin/api/nodes", headers={
            **bearer(token), "Origin": "http://127.0.0.1:8080",
        })
        assert response.status_code == 403
        assert response.json()["code"] == "ORIGIN_MISMATCH"
        from vade.audit.reader import AuditReader

        rejected = AuditReader(ctx.audit.path).query(kind="admin.rejected")["items"]
        assert any(r["code"] == "ORIGIN_MISMATCH" for r in rejected)

    asyncio.run(scenario())


def test_host_header_must_match(ctx, app):
    async def scenario():
        client = client_for(app)
        response = await client.get("/admin/api/health", headers={"Host": "evil.example"})
        assert response.status_code == 403
        assert response.json()["code"] == "ORIGIN_MISMATCH"

    asyncio.run(scenario())


# ---------------------------------------------------------------------- nodes
def test_node_list_is_whitelisted_and_endpoint_is_sanitised(ctx, app):
    async def scenario():
        registry = _registry(ctx)
        registry.register(_manifest("list_dir", instance="fs-02"), fingerprint="b" * 16, connection_id="c2")
        # Give the record an endpoint carrying userinfo and a token in the query.
        record = registry.get("com.example.fs", "fs-01")
        registry._records[("com.example.fs", "fs-01")] = dataclasses.replace(
            record, endpoint="wss://user:hunter2@127.0.0.1:9000/x?token=abc#f"
        )
        device, key = new_device(ctx)
        client = client_for(app)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]

        response = await client.get("/admin/api/nodes", headers=bearer(token))
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 2
        [node] = [n for n in items if n["instance_id"] == "fs-01"]
        assert node["endpoint"] == "wss://127.0.0.1:9000"
        assert set(node) == {
            "id", "kind", "module_id", "instance_id", "label", "capabilities", "state",
            "endpoint", "protocol", "version", "auth_mode", "rtt_ms", "last_seen",
            "x", "y", "revoked", "tool_count", "tool_names",
        }
        assert "manifest" not in json.dumps(items)

    asyncio.run(scenario())


def test_patch_node_returns_impact_and_writes_audit(ctx, app):
    async def scenario():
        _registry(ctx)
        device, key = new_device(ctx)
        client = client_for(app)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        response = await client.patch(
            "/admin/api/nodes/com.example.fs%23fs-01", json={"enabled": False}, headers=bearer(token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["impact"] == {
            "tool_count": 1, "tool_names": ["read_file"], "sessions_affected": 0,
            "changes": {"enabled": False},
        }
        assert body["audit_id"]
        assert ctx.registry.get("com.example.fs", "fs-01").enabled is False
        from vade.audit.reader import AuditReader

        rows = AuditReader(ctx.audit.path).query(kind="node.updated")["items"]
        assert rows[0]["actor"] == device.device_id
        assert rows[0]["extra"]["changes"] == {"enabled": False}

    asyncio.run(scenario())


def test_risk_downgrade_is_forbidden(ctx, app):
    async def scenario():
        _registry(ctx, tool="write_file", risk="high")
        device, key = new_device(ctx)
        client = client_for(app)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        response = await client.patch(
            "/admin/api/nodes/com.example.fs%23fs-01", json={"risk": {"write_file": "low"}},
            headers=bearer(token),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "RISK_DOWNGRADE_FORBIDDEN"

    asyncio.run(scenario())


def test_unknown_node_is_404(ctx, app):
    async def scenario():
        device, key = new_device(ctx)
        client = client_for(app)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        response = await client.get("/admin/api/nodes/com.example.nope%23x", headers=bearer(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    asyncio.run(scenario())


# -------------------------------------------------------------------- invites
def test_invite_plaintext_never_reaches_the_audit_log(ctx, app):
    async def scenario():
        device, key = new_device(ctx)
        client = client_for(app)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        created = await client.post("/admin/api/invites",
                                    json={"purpose": "module", "module_id": "com.example.fs"},
                                    headers=bearer(token))
        assert created.status_code == 200, created.text
        payload = created.json()
        assert payload["node_id"] == "com.example.fs#pending"
        code = payload["pair_code"]

        from vade.audit.reader import AuditReader

        dump = json.dumps(AuditReader(ctx.audit.path).query()["items"], ensure_ascii=False)
        assert code not in dump
        revoked = await client.delete(f"/admin/api/invites/{payload['invite_id']}", headers=bearer(token))
        assert revoked.status_code == 200
        ctx.pairing.close()

    asyncio.run(scenario())


# -------------------------------------------------------------------- devices
def test_device_list_never_returns_public_keys(ctx, app):
    async def scenario():
        device, key = new_device(ctx)
        client = client_for(app)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        response = await client.get("/admin/api/devices", headers=bearer(token))
        [item] = response.json()["items"]
        assert "pubkey" not in item
        assert len(item["fp8"]) == 16
        # §7.12.3 ③: the self-claim and "not verified" travel together so the UI
        # can label the row without guessing from a missing key.
        assert item["hardware_backed"] is True and item["attested"] is False

    asyncio.run(scenario())


def test_attested_is_persisted_false_and_survives_a_reload(ctx, app):
    """design §7.12.3 ③ stores `{hardware_backed, attested:false}`.

    `attested` must be on disk (not inferred from absence): the field is what
    tells the UI a row is a device's own claim, and it is never an auth input.
    """
    async def scenario():
        device, key = new_device(ctx, hardware_backed=True)
        assert device.attested is False
        path = ctx.config.data_dir / "admin" / "devices.json"
        [entry] = json.loads(path.read_text(encoding="utf-8"))["devices"]
        assert entry["hardware_backed"] is True
        assert entry["attested"] is False

        reloaded = DeviceStore(path).get(device.device_id)
        assert reloaded is not None and reloaded.attested is False

        client = client_for(app)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        [item] = (await client.get("/admin/api/devices", headers=bearer(token))).json()["items"]
        assert item["attested"] is False

    asyncio.run(scenario())


def test_device_registration_requires_step_up_password(ctx, app):
    async def scenario():
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        first, key = new_device(ctx)
        client = client_for(app)
        token = (await login_with_device(client, ctx, first, key)).json()["access_token"]
        new_key = Ed25519PrivateKey.generate()
        raw = new_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        body = {"pubkey": base64.b64encode(raw).decode(), "alg": "ed25519", "label": "tablet"}
        denied = await client.post("/admin/api/devices", json={**body, "password": "nope"},
                                   headers=bearer(token))
        assert denied.status_code == 401
        assert len(ctx.devices) == 1
        ok = await client.post("/admin/api/devices", json={**body, "password": PASSWORD},
                               headers=bearer(token))
        assert ok.status_code == 200, ok.text
        assert len(ctx.devices) == 2

    asyncio.run(scenario())


# --------------------------------------------------------------------- health
def test_health_is_unauthenticated_and_does_not_leak_version_remotely(ctx, app):
    async def scenario():
        remote = client_for(app, ip="203.0.113.9")
        assert (await remote.get("/admin/api/health")).json() == {"ok": True}
        local = await client_for(app).get("/admin/api/health")
        assert local.json()["ok"] is True and "version" in local.json()

    asyncio.run(scenario())


# --------------------------------------------------------------------- config
def test_config_rejects_unknown_keys_and_dangerous_changes_need_confirm(ctx, app):
    async def scenario():
        device, key = new_device(ctx)
        client = client_for(app)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        unknown = await client.put("/admin/api/config", json={"key": "evolve.foo", "value": 1},
                                   headers=bearer(token))
        assert unknown.status_code == 400
        assert unknown.json()["code"] == "UNKNOWN_CONFIG_KEY"

        # `evolve.enabled` defaults to False (schema), so switching it ON is not
        # the dangerous direction, and re-saving the default is not a change at
        # all — the confirm gate must not fire on either.
        enabled = await client.put("/admin/api/config",
                                   json={"key": "evolve.enabled", "value": True},
                                   headers=bearer(token))
        assert enabled.status_code == 200, enabled.text

        needs_confirm = await client.put("/admin/api/config",
                                         json={"key": "evolve.enabled", "value": False},
                                         headers=bearer(token))
        assert needs_confirm.status_code == 409
        assert needs_confirm.json()["code"] == "CONFIRM_REQUIRED"

        saved = await client.put("/admin/api/config",
                                 json={"key": "evolve.enabled", "value": False, "confirm": True},
                                 headers=bearer(token))
        assert saved.status_code == 200, saved.text
        # No evolve component exists in this core, so nothing picks the value up
        # live: the honest answer is needs_restart, not a fake `applied: true`.
        assert saved.json()["applied"] is False
        assert saved.json()["needs_restart"] is True

        items = {i["key"]: i for i in (await client.get("/admin/api/config", headers=bearer(token))).json()["items"]}
        assert items["evolve.enabled"]["value"] is False
        assert items["evolve.enabled"]["source"] == "overrides"
        assert items["evolve.enabled"]["mode"] == "restart"

    asyncio.run(scenario())


def test_unimplemented_feature_endpoints_answer_501_not_404(ctx, app):
    """C5b's memory routes are in the schema but not built in this batch.

    Every shape the schema declares — including nested actions — must answer 501
    `UNSUPPORTED`. A 404 reads as a broken build or a typo'd URL and leaves the
    UI unable to say "not in this build"; a 501 can be labelled. Skills were in
    this list until `admin/skills.py` landed; their coverage lives in
    `test_skills.py`.
    """
    paths = (
        "/admin/api/memory", "/admin/api/memory/stats", "/admin/api/memory/m1",
        "/admin/api/memory/m1/promote", "/admin/api/memory/m1/archive",
        "/admin/api/memory/m1/restore",
    )

    async def scenario():
        client = client_for(app)
        # A device bearer needs no Origin header (the cookie path does), and 501
        # must not depend on which credential the caller happened to use.
        device, key = new_device(ctx)
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        for path in paths:
            response = await client.get(path, headers=bearer(token))
            assert response.status_code == 501, (path, response.status_code, response.text)
            assert response.json()["code"] == "UNSUPPORTED", path

    asyncio.run(scenario())
