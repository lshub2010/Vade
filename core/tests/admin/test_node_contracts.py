"""Regressions for w1 (`f961862`): layout 409, capability kinds, SSE upsert.

Three fixes shipped without tests:

1. `PATCH /admin/api/layout` — a stale `rev` is a *conflict* (`409
   TOOL_CONFLICT`, message carries the server's current rev), not a bad
   argument; a `rev` that is not an integer is `400 INVALID_ARGUMENT`.
2. `to_public_view` — `capabilities[]` carries capability KINDS only
   (`tools`/`prompts`/`events`/`model`/`memory`/`bridge`), never tool names;
   names/count live in `tool_names`/`tool_count`; `model` comes from
   `manifest.models`, `memory`/`bridge` from the module-id convention.
3. SSE `node.upsert` — the payload is `{id, node: <full public view>}` (same
   whitelist as `/admin/api/nodes`, `state` included), and a heartbeat that
   brings a node back online publishes exactly one upsert.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from vade.admin.app import create_admin_app
from vade.admin.views import CAPABILITY_KINDS, NODE_FIELDS, to_public_view
from vade.registry.api import RegistryAPI
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry

from .conftest import bearer, client_for, login_with_device, new_device

NODE_ID = "com.example.fs#fs-01"
NODE_PATH = "/admin/api/nodes/com.example.fs%23fs-01"


# ----------------------------------------------------------------------- helpers
def _manifest(
    *,
    module_id: str = "com.example.fs",
    instance: str = "fs-01",
    tools: tuple[str, ...] = ("read_file",),
    prompts: tuple[str, ...] = (),
    events: tuple[str, ...] = (),
    models: tuple[str, ...] = (),
):
    capabilities: dict = {
        "tools": [
            {
                "name": name, "description": "d", "risk": "low", "timeout_ms": 10000,
                "idempotent": False, "input_schema": {"type": "object"},
            }
            for name in tools
        ]
    }
    if prompts:
        capabilities["prompts"] = [{"id": name, "content": "prompt body"} for name in prompts]
    if events:
        capabilities["events"] = list(events)
    if models:
        capabilities["models"] = [{"name": name} for name in models]
    return validate_manifest({
        "protocol": "vade/v1", "module_id": module_id, "instance_id": instance,
        "version": "1.0.0", "concurrency": "pooled",
        "limits": {"max_concurrency": 3, "max_inflight": 8, "max_payload": 65519},
        "capabilities": capabilities,
    })


def _registry(ctx, *manifests):
    registry = Registry()
    for index, manifest in enumerate(manifests or (_manifest(),)):
        registry.register(manifest, fingerprint=chr(ord("a") + index) * 16, connection_id=f"c{index}")
    ctx.registry = registry
    ctx.registry_api = RegistryAPI(registry)
    return registry


async def _token(client, ctx) -> str:
    device, key = new_device(ctx)
    response = await login_with_device(client, ctx, device, key)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _upserts(ctx, *, after: str = "0"):
    """`node.upsert` events still in the stream buffer, oldest first."""
    return [event for event in ctx.stream.replay(after) if event["type"] == "node.upsert"]


# ------------------------------------------------------------------------ layout
def test_layout_stale_rev_is_409_conflict_with_the_current_rev(ctx, app):
    async def scenario():
        client = client_for(app)
        token = await _token(client, ctx)

        first = await client.patch(
            "/admin/api/layout",
            json={"nodes": [{"id": NODE_ID, "x": 10, "y": 20}]},
            headers=bearer(token),
        )
        assert first.status_code == 200, first.text
        assert first.json()["rev"] == 1

        # The request itself is well formed; the caller's snapshot is old. That
        # is a conflict (design §7.6), and it must NOT be reported as a 400 the
        # client could mistake for "fix your body and retry".
        stale = await client.patch(
            "/admin/api/layout", json={"rev": 0, "nodes": []}, headers=bearer(token),
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "TOOL_CONFLICT"
        assert "current rev is 1" in stale.json()["message"]

        # A rejected write leaves the server layout untouched.
        layout = (await client.get("/admin/api/layout", headers=bearer(token))).json()
        assert layout == {
            "rev": 1,
            "nodes": [{"id": NODE_ID, "x": 10.0, "y": 20.0, "label": None, "group_id": None}],
        }

        # The rev the 409 handed back is the one that succeeds.
        accepted = await client.patch(
            "/admin/api/layout", json={"rev": 1, "nodes": []}, headers=bearer(token),
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {"rev": 2, "nodes": []}

        # Same request again is now stale, and the message tracks the new rev.
        again = await client.patch(
            "/admin/api/layout", json={"rev": 1, "nodes": []}, headers=bearer(token),
        )
        assert again.status_code == 409
        assert "current rev is 2" in again.json()["message"]

    asyncio.run(scenario())


@pytest.mark.parametrize("bad_rev", ["one", "1.0", [], {"rev": 1}])
def test_layout_malformed_rev_is_400_invalid_argument(ctx, app, bad_rev):
    async def scenario():
        client = client_for(app)
        token = await _token(client, ctx)
        response = await client.patch(
            "/admin/api/layout", json={"rev": bad_rev, "nodes": []}, headers=bearer(token),
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "INVALID_ARGUMENT"
        assert "rev must be an integer" in response.json()["message"]
        assert ctx._layout == {"rev": 0, "nodes": []}

    asyncio.run(scenario())


# -------------------------------------------------------------------- capabilities
def test_capabilities_are_kinds_and_tool_names_stay_separate():
    registry = Registry()
    registry.register(_manifest(tools=("read_file", "list_dir")), fingerprint="a" * 16, connection_id="c1")
    record = registry.get("com.example.fs", "fs-01")
    view = to_public_view(record, tool_names=record.manifest.tool_names)

    assert view["capabilities"] == ["tools"]
    # The bug: capabilities used to be the tool names, so the canvas picked a
    # node shape by string-matching tools.
    assert "read_file" not in view["capabilities"]
    assert "list_dir" not in view["capabilities"]
    assert set(view["capabilities"]) <= set(CAPABILITY_KINDS)
    assert view["tool_names"] == ["read_file", "list_dir"]
    assert view["tool_count"] == 2


def test_every_declared_manifest_section_maps_to_its_kind_in_stable_order():
    registry = Registry()
    registry.register(
        _manifest(prompts=("greeting",), events=("tool.call",), models=("gpt-x",)),
        fingerprint="a" * 16, connection_id="c1",
    )
    view = to_public_view(registry.get("com.example.fs", "fs-01"))

    # CAPABILITY_KINDS order, not manifest/set iteration order: a diff between
    # two polls of the same node must be meaningful.
    assert view["capabilities"] == ["tools", "prompts", "events", "model"]
    assert "model" in view["capabilities"] and "models" not in view["capabilities"]


@pytest.mark.parametrize(
    ("module_id", "expected"),
    [
        ("com.example.memory", ["tools", "memory"]),
        ("com.example.mcp", ["tools", "bridge"]),
        ("com.example.bridge-service", ["tools", "bridge"]),
    ],
)
def test_memory_and_bridge_are_derived_from_module_id(module_id, expected):
    registry = Registry()
    registry.register(_manifest(module_id=module_id), fingerprint="a" * 16, connection_id="c1")
    view = to_public_view(registry.get(module_id, "fs-01"))
    assert view["capabilities"] == expected


def test_a_record_without_a_manifest_declares_no_capabilities():
    registry = Registry()
    record = registry.mark_rejected("com.example.stranger", "x1", "not whitelisted", fingerprint="0" * 16)
    view = to_public_view(record)
    assert view["capabilities"] == []
    assert view["tool_names"] == []
    assert view["tool_count"] == 0


def test_nodes_endpoint_splits_capability_kinds_from_tool_list(ctx, app):
    async def scenario():
        _registry(ctx, _manifest(models=("gpt-x",)))
        client = client_for(app)
        token = await _token(client, ctx)
        response = await client.get(NODE_PATH, headers=bearer(token))
        assert response.status_code == 200, response.text
        node = response.json()
        assert set(node) == set(NODE_FIELDS)
        assert node["capabilities"] == ["tools", "model"]
        assert node["tool_names"] == ["read_file"]
        assert node["tool_count"] == 1

    asyncio.run(scenario())


# --------------------------------------------------------------------------- SSE
def test_patch_node_publishes_one_full_view_not_a_sparse_change(ctx, app):
    async def scenario():
        _registry(ctx, _manifest(tools=("read_file", "list_dir")))
        client = client_for(app)
        token = await _token(client, ctx)
        # The canvas's first GET is what binds registry status to the stream in
        # production as well (admin installs before registry, so the context is
        # built with `registry=None`).
        assert (await client.get("/admin/api/nodes", headers=bearer(token))).status_code == 200

        response = await client.patch(NODE_PATH, json={"label": "prod fs"}, headers=bearer(token))
        assert response.status_code == 200, response.text

        events = _upserts(ctx)
        assert len(events) == 1, events
        data = events[0]["data"]
        assert set(data) == {"id", "node", "changes"}
        assert data["id"] == NODE_ID
        assert data["changes"] == {"label": "prod fs"}

        node = data["node"]
        # A full Node view, not `{id, changes}`: enabled/concurrency/risk are not
        # in the whitelist, so a sparse change can never update the canvas.
        assert set(node) == set(NODE_FIELDS)
        assert node["state"] == "online"
        assert node["label"] == "prod fs"
        assert node["capabilities"] == ["tools"]
        assert node["tool_names"] == ["list_dir", "read_file"]
        assert node["tool_count"] == 2
        # ...and it is byte-for-byte what the detail endpoint returns, so the
        # panel and the canvas converge on the same object.
        fetched = (await client.get(NODE_PATH, headers=bearer(token))).json()
        assert node == fetched

        # Wire form: the SSE `data:` object is the payload plus `type`.
        chunk = ctx.stream.format(events[0])
        assert chunk.startswith(f"id: {events[0]['id']}\n")
        payload = json.loads(chunk.split("data: ", 1)[1])
        assert payload["type"] == "node.upsert"
        assert payload["id"] == NODE_ID
        assert payload["node"] == node

    asyncio.run(scenario())


def test_a_disable_that_flips_health_still_publishes_complete_views(ctx, app):
    async def scenario():
        _registry(ctx)
        client = client_for(app)
        token = await _token(client, ctx)
        assert (await client.get("/admin/api/nodes", headers=bearer(token))).status_code == 200

        response = await client.patch(NODE_PATH, json={"enabled": False}, headers=bearer(token))
        assert response.status_code == 200, response.text

        # Disabling also flips health offline, so the registry status listener
        # and the explicit PATCH publish can both fire. A duplicate upsert is
        # idempotent (replace, not delta), so assert every frame is complete
        # rather than pinning a count.
        events = _upserts(ctx)
        assert events
        fetched = (await client.get(NODE_PATH, headers=bearer(token))).json()
        for event in events:
            assert event["data"]["id"] == NODE_ID
            assert event["data"]["node"] == fetched
            assert set(event["data"]["node"]) == set(NODE_FIELDS)
            assert set(event["data"]) <= {"id", "node", "changes"}
        with_changes = [event for event in events if "changes" in event["data"]]
        assert len(with_changes) == 1
        assert with_changes[0]["data"]["changes"] == {"enabled": False}
        assert with_changes[0]["data"]["node"]["state"] == "offline"

    asyncio.run(scenario())


def test_heartbeat_restore_publishes_exactly_one_upsert(ctx):
    async def scenario():
        registry = _registry(ctx)
        # Registry present at construction: this is also the path that binds the
        # status listener for an already-connected SSE client.
        create_admin_app(ctx)

        registry.mark_disconnected("com.example.fs", "fs-01")
        gone = _upserts(ctx)
        assert len(gone) == 1, gone
        assert gone[0]["data"]["id"] == NODE_ID
        assert gone[0]["data"]["node"]["state"] == "offline"

        registry.touch("com.example.fs", "fs-01")
        restored = _upserts(ctx, after=str(gone[0]["id"]))
        assert len(restored) == 1, restored
        node = restored[0]["data"]["node"]
        assert node["id"] == NODE_ID
        assert node["state"] == "online"
        assert set(node) == set(NODE_FIELDS)

        # A heartbeat on an already-online node is not a state change; it must
        # not flood the topology with an upsert per frame.
        registry.touch("com.example.fs", "fs-01")
        assert _upserts(ctx, after=str(restored[0]["id"])) == []

    asyncio.run(scenario())


def test_stream_events_carry_the_full_view_on_the_wire(ctx):
    async def scenario():
        registry = _registry(ctx)
        create_admin_app(ctx)
        registry.mark_disconnected("com.example.fs", "fs-01")

        # The exact generator `GET /admin/api/stream` wraps (httpx's ASGITransport
        # buffers to completion, so the infinite response cannot be consumed
        # through the route in a test).
        events = ctx.stream.events("0")
        chunk = await asyncio.wait_for(events.__anext__(), 2.0)
        await events.aclose()

        assert chunk.endswith("\n\n")
        [line] = [item for item in chunk.splitlines() if item.startswith("data: ")]
        payload = json.loads(line[len("data: "):])
        assert payload["type"] == "node.upsert"
        assert payload["id"] == NODE_ID
        assert set(payload["node"]) == set(NODE_FIELDS)
        assert payload["node"]["state"] == "offline"

    asyncio.run(scenario())
