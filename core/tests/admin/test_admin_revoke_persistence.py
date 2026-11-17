"""P1-2: `POST /admin/api/nodes/{id}/revoke` must persist the unbind.

The route used to call `Registry.revoke_fingerprint()` only, and that method's
`_revoked` set lives in process memory. After a restart the set was empty while
`data/identity/whitelist.json` still listed the key — the same static key
handshook again. The permanent half is `Whitelist.revoke()`, which rewrites the
0600 file; this test proves the file is what a restarted core reads.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from vade.__main__ import build_module_crypto
from vade.admin.app import create_admin_app
from vade.app import App
from vade.errors import VadeError
from vade.identity.whitelist import Whitelist, fp8_hex
from vade.registry.api import RegistryAPI
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry

from .conftest import bearer, client_for, login_with_device, make_context, new_device

MODULE_ID = "com.example.fs"
INSTANCE_ID = "fs-01"
NODE = f"{MODULE_ID}#{INSTANCE_ID}"
KEY = bytes(range(32))
FP = fp8_hex(KEY)


def _manifest():
    return validate_manifest({
        "protocol": "vade/v1", "module_id": MODULE_ID, "instance_id": INSTANCE_ID,
        "version": "1.0.0", "concurrency": "exclusive",
        "limits": {"max_concurrency": 1, "max_payload": 65519},
        "capabilities": {"tools": [{
            "name": "read_file", "description": "d", "risk": "low", "timeout_ms": 10000,
            "idempotent": True, "input_schema": {"type": "object"},
        }]},
    })


def test_admin_node_revoke_unbinds_the_key_on_disk_and_a_restart_refuses_it(tmp_path):
    ctx = make_context(tmp_path)
    try:
        whitelist = Whitelist(tmp_path / "identity" / "whitelist.json")
        whitelist.add(MODULE_ID, INSTANCE_ID, FP, public_key=KEY)
        registry = Registry()
        registry.register(_manifest(), fingerprint=FP, connection_id="c1")
        ctx.registry = registry
        ctx.registry_api = RegistryAPI(registry)
        ctx.whitelist = whitelist
        app = create_admin_app(ctx)

        async def scenario():
            device, key = new_device(ctx)
            client = client_for(app)
            token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
            response = await client.post(
                f"/admin/api/nodes/{MODULE_ID}%23{INSTANCE_ID}/revoke", headers=bearer(token),
            )
            assert response.status_code == 200, response.text
            return response.json()

        body = asyncio.run(scenario())
        assert body["ok"] is True
        assert body["unbound"] == [NODE]

        # The process now rejects the key...
        assert registry.is_revoked(FP) is True
        # ...and the unbind is on disk, not only in `_revoked`.
        raw = json.loads((tmp_path / "identity" / "whitelist.json").read_text(encoding="utf-8"))
        assert raw["entries"] == []

        # A restarted core builds its trust pool from that file: the same key
        # is no longer a candidate, so `build_module_crypto` fails closed
        # (PERMISSION_DENIED, empty pool) instead of letting it handshake.
        reloaded = Whitelist(tmp_path / "identity" / "whitelist.json")
        assert reloaded.allows(FP) is False
        assert reloaded.candidate_keys() == ()

        restarted = App({"core.heartbeat_interval_s": 10})  # type: ignore[arg-type]
        # The refusal happens before any key material is touched (empty pool),
        # so a stand-in ffi is enough and keeps this test off the native lib.
        restarted.identity = types.SimpleNamespace(ffi=object())  # type: ignore[assignment]
        restarted.whitelist = reloaded
        make_crypto = build_module_crypto(restarted)
        with pytest.raises(VadeError) as excinfo:
            make_crypto("c1")
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert excinfo.value.fatal is True
    finally:
        if ctx.audit is not None:
            ctx.audit.close()

