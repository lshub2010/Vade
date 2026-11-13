"""AC-19: nothing secret may appear in an admin response body (31-C5a §T4/§六).

Two complementary checks:

* no *value* leaks — password, bearer tokens, setup token, public-key material;
* no response carries a key that could hold one (`pubkey`, `manifest`, ...).

The keyword-only scan the task book describes would flag audit *kind* labels such
as `admin.password_changed`, which are names, not secrets; the value scan is the
one that can actually fail for the right reason.
"""

from __future__ import annotations

import json
import re

from vade.admin import setup_token

from .conftest import ORIGIN, PASSWORD, bearer, client_for, login_with_device, new_device

#: Keys that must never appear in a governance response payload.
FORBIDDEN_KEYS = {
    "pubkey", "public_key", "private_key",
    "access_token", "refresh_token", "setup_token", "manifest", "pair_code",
    "authorization", "cookie",
}
# `password` / `secret` stay out of the key set: the audit row keeps the *name*
# with a `[redacted]` value on purpose (the page must show that a field was
# dropped, not silently omit it). Their values are covered below.

B64_BLOB = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_admin_payloads_carry_no_secret_values_or_keys(ctx, app, tmp_path):
    from vade.registry.api import RegistryAPI
    from vade.registry.manifest import validate_manifest
    from vade.registry.table import Registry

    registry = Registry()
    registry.register(validate_manifest({
        "protocol": "vade/v1", "module_id": "com.example.fs", "instance_id": "fs-01",
        "version": "1.0.0", "concurrency": "pooled",
        "limits": {"max_concurrency": 3, "max_inflight": 8, "max_payload": 65519},
        "capabilities": {"tools": [{
            "name": "read_file", "description": "d", "risk": "low", "timeout_ms": 10000,
            "idempotent": False, "input_schema": {"type": "object"},
        }]},
    }), fingerprint="a" * 16, connection_id="c1")
    ctx.registry, ctx.registry_api = registry, RegistryAPI(registry)

    async def scenario():
        device, key = new_device(ctx)
        client = client_for(app)
        logged = await login_with_device(client, ctx, device, key)
        token = logged.json()["access_token"]
        headers = bearer(token)

        # Produce audit rows of every interesting kind, including a tool call
        # whose arguments contain a password.
        ctx.write_audit(kind="tool_call", tool="com.example.fs/read_file",
                        args={"path": "/tmp/a", "password": "hunter2"}, status="ok")
        ctx.write_audit(kind="config.changed", status="ok",
                        extra={"key": "evolve.enabled", "old": True, "new": False})

        paths = [
            "/admin/api/nodes", "/admin/api/nodes/com.example.fs%23fs-01",
            "/admin/api/devices", "/admin/api/audit",
            "/admin/api/config", "/admin/api/config/usage",
            "/admin/api/layout", "/admin/api/health",
        ]
        payloads = {}
        for path in paths:
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["content-type"].startswith("application/json")
            assert response.headers["x-content-type-options"] == "nosniff"
            payloads[path] = response.json()

        blob = json.dumps(payloads, ensure_ascii=False)
        assert PASSWORD not in blob
        assert token not in blob
        assert logged.json()["refresh_token"] not in blob
        setup_value = setup_token.read_token(tmp_path)
        assert setup_value is None or setup_value not in blob     # consumed at setup
        assert "hunter2" not in blob                              # audit redaction
        assert "[redacted]" in blob
        assert not B64_BLOB.search(blob), B64_BLOB.search(blob).group(0)

        keys = set(_walk_keys(payloads))
        assert not (keys & FORBIDDEN_KEYS), keys & FORBIDDEN_KEYS
        # The whitelist really is a whitelist, not a blacklist of one field.
        assert "endpoint" in keys and "tool_names" in keys

    import asyncio

    asyncio.run(scenario())


def test_api_is_json_never_html_and_has_no_cors(ctx, app):
    async def scenario():
        client = client_for(app)
        # With an Origin (the browser case) the request reaches the auth layer.
        unauthorised = await client.get("/admin/api/nodes", headers={"Origin": ORIGIN})
        assert unauthorised.status_code == 401
        # Without Origin/Sec-Fetch-Site and without a bearer it is refused earlier.
        bare = await client.get("/admin/api/nodes")
        assert bare.status_code == 403 and bare.json()["code"] == "ORIGIN_MISMATCH"
        assert unauthorised.json()["code"] == "AUTH_FAILED"
        assert unauthorised.headers["content-type"].startswith("application/json")
        assert unauthorised.headers["x-content-type-options"] == "nosniff"

        preflight = await client.options("/admin/api/nodes", headers={
            "Origin": "http://127.0.0.1:8080",
            "Access-Control-Request-Method": "POST",
        })
        assert "access-control-allow-origin" not in {k.lower() for k in preflight.headers}

        spoofed = await client.get("/admin/api/health", headers={"Origin": "http://127.0.0.1:8080"})
        assert "access-control-allow-origin" not in {k.lower() for k in spoofed.headers}
        assert "frame-ancestors 'none'" in spoofed.headers["content-security-policy"]

        missing = await client.get("/admin/api/nope")
        assert missing.status_code in (404, 405)
        assert missing.headers["content-type"].startswith("application/json")

    import asyncio

    asyncio.run(scenario())
