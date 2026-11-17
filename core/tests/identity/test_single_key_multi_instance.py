"""P1-3: one static key paired to several instances must register every one.

A pooled module runs N workers that share one key. The transport can only put
ONE name on `connection.device_id`; when the key matches several pairings it
leaves it `None`. `App.register_module` must therefore ask the whitelist "is
this key paired to `module#instance`?" — not "does it equal the lowest name in
sorted order", which refused every worker but the first and is exactly the bug
this file pins. The test drives the real `App.register_module` and the real
`Registry`, not a stub.
"""

from __future__ import annotations

from typing import Any

import pytest

from vade.app import App
from vade.errors import VadeError
from vade.identity.whitelist import Whitelist, fp8_hex
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry

MODULE_ID = "com.example.pool"
KEY = bytes(range(32))
FP = fp8_hex(KEY)


def _manifest(instance: str) -> dict[str, Any]:
    return {
        "protocol": "vade/v1",
        "module_id": MODULE_ID,
        "instance_id": instance,
        "version": "1.0.0",
        "concurrency": "pooled",
        "limits": {"max_concurrency": 4, "max_inflight": 8, "max_payload": 65519},
        "capabilities": {
            "tools": [
                {
                    "name": "pool_task",
                    "description": "one pooled worker's only tool",
                    "risk": "low",
                    "timeout_ms": 10000,
                    "idempotent": True,
                    "input_schema": {"type": "object"},
                }
            ]
        },
    }


class _Crypto:
    def __init__(self, fingerprint: str) -> None:
        self.remote_fingerprint = fingerprint


class _Connection:
    """Duck-typed `Connection`: the transport's handshake result + a slot name."""

    def __init__(self, connection_id: str, fingerprint: str, device_id: str | None) -> None:
        self.connection_id = connection_id
        self.crypto = _Crypto(fingerprint)
        self.device_id = device_id


def _app(tmp_path: Any) -> App:
    """Real App + real Registry; only the process lifecycle is skipped."""
    whitelist = Whitelist(tmp_path / "identity" / "whitelist.json")
    for instance in ("pool-0", "pool-1"):
        whitelist.add(MODULE_ID, instance, FP, public_key=KEY)

    app = App({"core.heartbeat_interval_s": 10})  # type: ignore[arg-type]
    app.whitelist = whitelist
    app.registry = Registry(
        heartbeat_interval_s=10,
        close_connection=lambda connection_id: None,
        paired_fingerprint=app._paired_fingerprint,
    )
    return app


def test_pooled_workers_sharing_one_key_all_register(tmp_path):
    app = _app(tmp_path)
    whitelist = app.whitelist

    # The transport's view is deliberately ambiguous: it must NOT pick the
    # lowest name, because that name would be a false claim for the sibling.
    assert whitelist.device_for_fingerprint(FP) is None
    assert whitelist.devices_for_fingerprint(FP) == {
        f"{MODULE_ID}#pool-0",
        f"{MODULE_ID}#pool-1",
    }

    # pool-1 sorts AFTER pool-0: the old "lowest name" rule rejected exactly
    # this worker as `PERMISSION_DENIED` even though its key was paired to it.
    early = _Connection("c0", FP, whitelist.device_for_fingerprint(FP))
    late = _Connection("c1", FP, whitelist.device_for_fingerprint(FP))
    result_late = app.register_module(late, _manifest("pool-1"))
    result_early = app.register_module(early, _manifest("pool-0"))

    assert result_late["accepted"] is True
    assert result_late["session_id"] == f"{MODULE_ID}#pool-1"
    assert result_early["accepted"] is True
    assert result_early["session_id"] == f"{MODULE_ID}#pool-0"

    # Each socket now carries the slot it proved, so blob tokens and `ui/*`
    # page authorisation see the right device rather than a sibling's name.
    assert early.device_id == f"{MODULE_ID}#pool-0"
    assert late.device_id == f"{MODULE_ID}#pool-1"
    assert app.registry.get(MODULE_ID, "pool-0") is not None
    assert app.registry.get(MODULE_ID, "pool-1") is not None


def test_a_slot_the_key_is_not_paired_to_is_still_refused(tmp_path):
    """Membership, not leniency: a third instance is not covered by the pairing."""
    app = _app(tmp_path)
    connection = _Connection("c2", FP, app.whitelist.device_for_fingerprint(FP))

    with pytest.raises(VadeError) as excinfo:
        app.register_module(connection, _manifest("pool-2"))
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.fatal is True
    assert app.registry.get(MODULE_ID, "pool-2") is None
    # The socket keeps no invented identity.
    assert connection.device_id is None


def test_an_unpaired_key_cannot_register(tmp_path):
    app = _app(tmp_path)
    stranger = bytes(range(32, 64))
    connection = _Connection("c3", fp8_hex(stranger), None)

    with pytest.raises(VadeError) as excinfo:
        app.register_module(connection, _manifest("pool-0"))
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.fatal is True
