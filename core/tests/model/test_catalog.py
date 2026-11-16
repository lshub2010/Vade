"""Block 1 — `model/list`: the wire shape, module self-report, revision caching.

The RFC's whole point is that a UI never guesses a capability: an undeclared
field must arrive as `null` (UNKNOWN), not as `false`/`[]`, and the list must
still show a module that declares nothing (`<module_id>/*`) or is offline
(`available: false`) — otherwise FR-8.46's switch would silently hide it.
"""

from __future__ import annotations

import asyncio

from vade.llm.catalog import ModelResolver, build_catalog
from vade.registry.manifest import validate_manifest
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore

from .helpers import FakeConnection, chat_manifest, make_config, registry_of

#: Exactly the keys `model/list.models[]` promises (rpc.json, RFC-0005 append).
ENTRY_KEYS = {
    "id", "provider", "display_name", "input_modalities", "max_image_bytes",
    "max_image_px", "max_images_per_turn", "context_window", "supports_tools",
    "priority", "available",
}

VL_CAPS = {
    "name": "qwen2.5-vl-72b",
    "input_modalities": ["text", "image"],
    "max_image_bytes": 4_194_304,
    "max_image_px": 2048,
    "max_images_per_turn": 6,
    "context_window": 131_072,
    "supports_tools": True,
}


def _module_cfg(**overrides):
    return make_config(
        **{
            "llm.provider": "module",
            "llm.base_url": "",
            "llm.fallback_to_builtin": False,
            **overrides,
        }
    )


def _rpc(tmp_path, cfg, registry):
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=100))
    log.set_model_resolver(ModelResolver(cfg, registry))
    return log, SessionRPC(log)


def test_model_list_row_shape_and_the_module_self_report(tmp_path):
    registry = registry_of(chat_manifest(models=[VL_CAPS], priority=3))
    log, rpc = _rpc(tmp_path, _module_cfg(), registry)
    conn = FakeConnection()

    answer = asyncio.run(rpc.dispatch(conn, "model/list", {}))

    assert answer["not_modified"] is False
    assert isinstance(answer["revision"], int)
    (entry,) = answer["models"]
    assert set(entry) == ENTRY_KEYS, f"model/list row drifted: {sorted(set(entry) ^ ENTRY_KEYS)}"
    # `model` is the id's second half, so it is not a second field on the wire.
    assert "model" not in entry
    assert entry["id"] == "com.example.model/qwen2.5-vl-72b"
    assert entry["provider"] == "com.example.model"
    assert entry["display_name"] == "qwen2.5-vl-72b", "display_name falls back to name"
    assert entry["input_modalities"] == ["text", "image"]
    assert (entry["max_image_bytes"], entry["max_image_px"]) == (4_194_304, 2048)
    assert (entry["max_images_per_turn"], entry["context_window"]) == (6, 131_072)
    assert entry["supports_tools"] is True
    assert entry["priority"] == 3, "priority comes from the manifest, not from the model block"
    assert entry["available"] is True
    # The default block names the layer that won, so the UI does not re-derive it.
    assert answer["default"] == {"model_id": "com.example.model/qwen2.5-vl-72b",
                                 "source": "priority", "resolved": True}
    assert log.store.load("nope") is None


def test_undeclared_capabilities_are_null_and_never_false(tmp_path):
    """UNKNOWN must stay distinguishable from "does not support it"."""
    registry = registry_of(chat_manifest(models=[
        {"name": "bare-model"},
        {"name": "known-model", "supports_tools": False, "input_modalities": ["text"]},
    ]))
    catalog = build_catalog(_module_cfg(), registry)
    bare = catalog.get("com.example.model/bare-model").to_wire()
    assert bare["input_modalities"] is None
    assert bare["supports_tools"] is None, "an omitted supports_tools is unknown, not False"
    for field in ("max_image_bytes", "max_image_px", "max_images_per_turn", "context_window"):
        assert bare[field] is None, field

    known = catalog.get("com.example.model/known-model").to_wire()
    assert known["supports_tools"] is False, "a declared False must survive as False"
    assert known["input_modalities"] == ["text"]


def test_a_field_the_schema_rejected_cannot_poison_the_entry():
    """The schema is the first line; `parse_models` is the second (older manifest).

    A manifest validated against a previous C-03 can carry values today's schema
    rejects. Dropping the whole module over one typo is C-03's explicit
    anti-goal, so the parser nulls the bad field and keeps the rest — and an
    entry with no usable `name` is dropped alone.
    """
    from vade.llm.capabilities import parse_models

    specs = parse_models([
        {"name": "good", "supports_tools": "yes", "max_image_px": -1,
         "input_modalities": ["text", "telepathy"]},
        {"name": "with/slash"},
        {"name": ""},
        "not-an-object",
        {"name": "good"},  # duplicate: dropped, first wins
    ])
    assert [spec.name for spec in specs] == ["good"]
    assert specs[0].supports_tools is None
    assert specs[0].max_image_px is None
    assert specs[0].input_modalities == ("text",)


def test_a_declared_star_model_name_is_dropped_before_it_collides():
    """`*` is the synthesised module default (§4.3), never a declared model."""
    from vade.llm.capabilities import parse_models

    warnings: list[str] = []
    specs = parse_models([{"name": "*"}, {"name": " * "}, {"name": "real"}], warnings=warnings)
    assert [spec.name for spec in specs] == ["real"]
    assert sum("'*' is reserved" in warning for warning in warnings) == 2


def test_a_chat_module_that_declares_nothing_appears_as_a_star_entry(tmp_path):
    """`capabilities.models` omitted != "serves no model" (§4.3)."""
    registry = registry_of(
        chat_manifest("com.example.silent", "s-1", models=None, priority=5),
    )
    catalog = build_catalog(_module_cfg(), registry)
    ids = [entry.id for entry in catalog.entries]
    assert ids == ["com.example.silent/*"], (
        f"a silent chat module must still be listed, not hidden: {ids}"
    )
    star = catalog.get("com.example.silent/*")
    assert star.display_name == "com.example.silent"
    assert star.available is True
    wire = star.to_wire()
    assert wire["input_modalities"] is None and wire["supports_tools"] is None


def test_a_non_chat_module_never_becomes_a_model(tmp_path):
    """Declaring `capabilities.models` without the `chat` tool is not a model."""
    read_file = {"name": "read_file", "description": "read", "input_schema": {"type": "object"},
                 "risk": "low", "idempotent": True, "timeout_ms": 1000}
    registry = registry_of(
        chat_manifest("com.example.silent", "s-1", models=None),
        chat_manifest("com.example.tools", "t-1", models=[VL_CAPS], tools=[read_file]),
    )
    catalog = build_catalog(_module_cfg(), registry)
    assert [entry.id for entry in catalog.entries] == ["com.example.silent/*"]


def test_an_offline_module_stays_in_the_list_with_available_false(tmp_path):
    registry = registry_of(chat_manifest(models=[VL_CAPS, {"name": "ghost-model"}]))
    registry.mark_disconnected("com.example.model", "m-01")
    catalog = build_catalog(_module_cfg(), registry)

    by_id = {entry.id: entry for entry in catalog.entries}
    assert set(by_id) == {"com.example.model/qwen2.5-vl-72b", "com.example.model/ghost-model"}
    assert all(entry.available is False for entry in by_id.values()), (
        "an offline module keeps its rows with available:false; it is not deleted"
    )
    assert catalog.default_entry() is None, "nothing available must not silently pick an offline model"


def test_revision_bumps_on_registry_mutation_and_if_revision_skips_the_body(tmp_path):
    registry = registry_of(chat_manifest(models=[VL_CAPS]))
    log, rpc = _rpc(tmp_path, _module_cfg(), registry)
    conn = FakeConnection()

    first = asyncio.run(rpc.dispatch(conn, "model/list", {}))
    cached = asyncio.run(rpc.dispatch(conn, "model/list", {"if_revision": first["revision"]}))
    assert cached == {"revision": first["revision"], "not_modified": True, "models": None}, (
        "a client that already has this revision gets no body and keeps its cache"
    )

    # A second instance changes the list, so the revision must move.
    registry.register(
        validate_manifest(chat_manifest(models=[{"name": "second-model"}], instance_id="m-02")),
        fingerprint="fp-2",
        connection_id="conn-2",
    )
    second = asyncio.run(rpc.dispatch(conn, "model/list", {"if_revision": first["revision"]}))
    assert second["not_modified"] is False
    assert second["revision"] != first["revision"]
    assert {entry["id"] for entry in second["models"]} == {
        "com.example.model/qwen2.5-vl-72b", "com.example.model/second-model",
    }
    # Disconnect is a list change too: availability flips.
    registry.mark_disconnected("com.example.model", "m-02")
    third = asyncio.run(rpc.dispatch(conn, "model/list", {"if_revision": second["revision"]}))
    assert third["revision"] not in (first["revision"], second["revision"])


def test_if_revision_rejects_a_non_integer(tmp_path):
    registry = registry_of(chat_manifest(models=[VL_CAPS]))
    _log, rpc = _rpc(tmp_path, _module_cfg(), registry)
    answer = asyncio.run(rpc.dispatch(FakeConnection(), "model/list", {"if_revision": "3"}))
    assert answer.code == "INVALID_ARGUMENT", "a stringly revision must not silently refresh"
