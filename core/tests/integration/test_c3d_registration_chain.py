"""The real registration chain C-14 stands on.

The C3d review found the channel was wired but could never be switched on:
`manifest.schema.json` had no `subscriptions`, and the `Manifest` dataclass
dropped the field even when the schema's `additionalProperties: true` let it
through. `EventBus.identify()` reads `record.manifest.subscriptions`, so every
module that registered through `module/register` was rejected `not in manifest`
and acceptance criteria #1-#5 could only be demonstrated against the unit-test
fake manifest.

These tests go through `App.startup()` + `install_components()` +
`make_rpc_handler()` -- the same calls `__main__._serve` makes -- with the real
schema, the real `Registry`, the real SQLite store and a real connection table.
Nothing here is stubbed at the manifest, registry or dispatch layer.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from vade.app import App, install_components, make_rpc_handler
from vade.config import load_config
from vade.errors import VadeError
from vade.registry.manifest import ManifestError, validate_manifest
from vade.session.models import Session, now_ms
from tests.events.helpers import ALLOWED_FP, FakeConnection, add_turn

MODULE = "com.example.evolve"
OTHER = "com.example.other"


def manifest_dict(**overrides: Any) -> dict[str, Any]:
    """A valid `module/register` payload for an evolve module."""
    raw: dict[str, Any] = {
        "module_id": MODULE,
        "instance_id": "evo-1",
        "version": "1.0.0",
        "protocol": "vade/v1",
        "role": "evolve",
        "concurrency": "exclusive",
        "capabilities": {"tools": []},
        "limits": {"max_concurrency": 1, "max_payload": 65519},
        "subscriptions": ["session.turn", "session.closed"],
    }
    raw.update(overrides)
    return raw


@pytest_asyncio.fixture
async def app(tmp_path):
    config = load_config(cli={
        "core.data_dir": str(tmp_path),
        "evolve.enabled": True,
        "evolve.allow_transcript": True,
        "evolve.allowlist": [f"{MODULE}:{ALLOWED_FP}"],
        "evolve.redact": ["user_text"],
    })
    instance = App(config)
    await instance.startup()
    install_components(instance, strict=False)
    yield instance
    await instance.shutdown()


def _connection(app: App, connection_id: str = "c-evo", *,
                module_id: str = MODULE, instance_id: str = "evo-1") -> FakeConnection:
    connection = FakeConnection(connection_id, module_id=module_id, instance_id=instance_id)
    # `App._connections()` prefers a transport dict; a dict here is the same
    # shape `_serve` publishes, minus the socket.
    app.transport = {"connections": {connection_id: connection}}
    return connection


async def _register(app: App, connection: FakeConnection, payload: dict[str, Any]) -> Any:
    handler = make_rpc_handler(app)
    result = await handler(connection, {"method": "module/register", "params": payload})
    assert not isinstance(result, Exception), result
    return result


@pytest.mark.asyncio
async def test_registered_subscriptions_survive_validation(app: App) -> None:
    """RFC-0001: the field is on the dataclass, not just in the raw dict."""
    manifest = validate_manifest(manifest_dict())
    assert manifest.subscriptions == ("session.turn", "session.closed")
    # Unchanged behaviour for a manifest that predates the field.
    legacy = manifest_dict()
    del legacy["subscriptions"]
    assert validate_manifest(legacy).subscriptions == ()


@pytest.mark.asyncio
async def test_real_registration_chain_delivers_a_turn(app: App) -> None:
    """register -> subscribe -> pump -> `events/deliver` on the socket."""
    connection = _connection(app)
    registration = await _register(app, connection, manifest_dict())
    assert registration["accepted"] is True
    assert registration["session_id"] == f"{MODULE}#evo-1"

    handler = make_rpc_handler(app)
    subscription = await handler(connection, {
        "method": "events/subscribe", "params": {"topics": ["session.turn", "session.closed"]},
    })
    assert subscription == {"accepted": ["session.turn", "session.closed"], "rejected": [], "replayed": 0}

    store = app.session_store
    store.create_session(Session(id="s1", created_at=now_ms()))
    add_turn(store, session_id="s1", user="how do I roll back?",
             assistant="use the rollback command")

    assert app.event_bus.pump() == 1
    await app.event_bus.drain()

    frames = connection.delivered()
    assert [f["topic"] for f in frames] == ["session.turn"]
    payload = frames[0]["payload"]
    assert payload["session_id"] == "s1"
    assert payload["assistant_text"] == "use the rollback command"
    assert payload["state"] == "completed"
    # Redaction is core-side and applies on the real chain: the fixture sets
    # `[evolve] redact = ["user_text"]`, so the user's words are replaced by
    # sha256(value)[:16] while the assistant text stays readable.
    assert payload["user_text"] == "df9c877bcb9df251"
    assert payload["redacted"] is True


@pytest.mark.asyncio
async def test_manifest_without_subscriptions_is_still_rejected(app: App) -> None:
    """Fail-closed: the gate is driven by the manifest, not by the request."""
    connection = _connection(app, "c-other", module_id=OTHER, instance_id="other-1")
    await _register(app, connection, manifest_dict(
        module_id=OTHER, instance_id="other-1", subscriptions=[],
    ))
    handler = make_rpc_handler(app)
    result = await handler(connection, {
        "method": "events/subscribe", "params": {"topics": ["session.turn"]},
    })
    assert result == {
        "accepted": [],
        "rejected": [{"topic": "session.turn", "reason": "not in manifest"}],
        "replayed": 0,
    }


@pytest.mark.asyncio
async def test_capabilities_update_can_add_a_subscription(app: App) -> None:
    """A second real registration entry point must carry the field too."""
    connection = _connection(app)
    await _register(app, connection, manifest_dict(subscriptions=[]))
    handler = make_rpc_handler(app)

    rejected = await handler(connection, {"method": "events/subscribe",
                                          "params": {"topics": ["session.closed"]}})
    assert rejected["rejected"] == [{"topic": "session.closed", "reason": "not in manifest"}]

    updated = await handler(connection, {
        "method": "module/capabilities.update",
        "params": manifest_dict(subscriptions=["session.closed"]),
    })
    assert updated["accepted"] is True

    accepted = await handler(connection, {"method": "events/subscribe",
                                          "params": {"topics": ["session.closed"]}})
    assert accepted["accepted"] == ["session.closed"]


@pytest.mark.asyncio
async def test_registration_refuses_a_connection_paired_as_another_node(app: App) -> None:
    """Review D1: key, pairing record and manifest must name the same node.

    `device_id` comes from the verified key, never from the manifest; without
    the comparison a paired module A could register as B and inherit B's tools.
    """
    connection = _connection(app)  # paired as `com.example.evolve#evo-1`
    handler = make_rpc_handler(app)
    with pytest.raises(VadeError) as excinfo:
        await handler(connection, {
            "method": "module/register",
            "params": manifest_dict(module_id=OTHER, instance_id="other-1"),
        })
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.fatal is True
    assert app.registry is not None
    assert app.registry.get(OTHER, "other-1") is None


@pytest.mark.asyncio
async def test_registration_fails_closed_without_a_paired_identity(app: App) -> None:
    """No pairing ⇒ no registration, even with a perfectly valid manifest."""
    connection = _connection(app, "c-anon")
    connection.device_id = None
    handler = make_rpc_handler(app)
    with pytest.raises(VadeError) as excinfo:
        await handler(connection, {"method": "module/register", "params": manifest_dict()})
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.fatal is True


def test_subscriptions_shape_is_enforced_by_the_schema() -> None:
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(manifest_dict(subscriptions="session.turn"))
    assert excinfo.value.code == "MANIFEST_INVALID"
    assert "subscriptions" in str(excinfo.value)
