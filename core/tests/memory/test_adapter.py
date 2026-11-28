"""`vade.memory.MemoryAdapter`: the core-side `MemoryPort` over a fake router.

Nothing here touches a real module or SQLite. The adapter's whole job is
resolution (which registered node is memory?) and translation (`memory/*`
private RPC <-> `MemoryPort`), so a fake registry + fake caller is the exact
seam to test. The real module is M4a's suite, not this one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from vade.errors import VadeError
from vade.memory.adapter import MemoryAdapter


class FakeRegistry:
    def __init__(self, records):
        self._records = list(records)

    def snapshot(self):
        return tuple(self._records)


class FakeCaller:
    def __init__(self, replies: dict[str, object] | None = None):
        self.replies = dict(replies or {})
        self.calls: list[tuple[str, str, dict, int]] = []

    async def call(self, target, method, params, deadline_ms):
        self.calls.append((target.module_id, method, dict(params), deadline_ms))
        reply = self.replies.get(method, {"ok": True})
        if callable(reply):
            return reply(params)
        return reply

    async def cancel(self, target, call_id):
        return None


def _record(module_id="vade.memory", instance_id="mem-1", **overrides):
    base = {
        "module_id": module_id,
        "instance_id": instance_id,
        "connection_id": "conn-1",
        "manifest": object(),  # only `is None` matters to the adapter
        "enabled": True,
        "health": SimpleNamespace(value="online"),
    }
    base.update(overrides)
    base["node_id"] = f"{base['module_id']}#{base['instance_id']}"
    return SimpleNamespace(**base)


def _app(records=(), replies=None):
    registry = FakeRegistry(records)
    caller = FakeCaller(replies)
    return SimpleNamespace(registry=registry, module_caller=caller), caller


def test_read_session_calls_the_private_rpc_and_wraps_items():
    app, caller = _app(
        [_record()],
        {"memory/read_session": {"items": [
            {"id": "01J0000000000000000000000A", "text": "prefers dark",
             "tags": ["pref"], "created_at": 5},
            {"id": "01J0000000000000000000000B", "text": "project Orion",
             "tags": [], "created_at": 6},
        ]}},
    )
    items = asyncio.run(MemoryAdapter(app).read_session("01J0000000000000000000000S", limit=20))
    assert [item.text for item in items] == ["prefers dark", "project Orion"]
    assert items[0].id == "01J0000000000000000000000A"
    assert items[0].meta["tags"] == ["pref"]
    module_id, method, params, deadline_ms = caller.calls[0]
    assert (module_id, method) == ("vade.memory", "memory/read_session")
    assert params == {"session_id": "01J0000000000000000000000S", "limit": 20}
    assert deadline_ms > 0


def test_no_memory_node_is_unsupported_not_an_empty_list():
    """C-08: a build without a memory backend answers 501, not "entry missing".

    `NOT_FOUND` made the admin page say "this entry is gone" when the module was
    simply not installed — the exact data-loss ambiguity the adapter forbids.
    """
    app, _caller = _app([])
    with pytest.raises(VadeError) as caught:
        asyncio.run(MemoryAdapter(app).read_session("01J0000000000000000000000S"))
    assert caught.value.code == "UNSUPPORTED"


def test_offline_and_disabled_nodes_are_skipped():
    app, caller = _app(
        [_record(module_id="vade.memory", instance_id="off", health=SimpleNamespace(value="offline")),
         _record(module_id="vade.memory", instance_id="off2", connection_id=None),
         _record(module_id="vade.memory", instance_id="off3", enabled=False)],
    )
    with pytest.raises(VadeError) as caught:
        asyncio.run(MemoryAdapter(app).admin_stats())
    # No eligible node is "no backend installed" (501), not "stats missing".
    assert caught.value.code == "UNSUPPORTED"
    assert caller.calls == []


def test_admin_list_drops_unset_filters_and_defaults_limit():
    app, caller = _app(
        [_record()],
        {"memory/admin_list": {"items": [], "next_cursor": "c2"}},
    )
    items, cursor = asyncio.run(MemoryAdapter(app).admin_list(
        {"layer": "global", "namespace": None, "status": "active",
         "from": None, "to": 9.0, "q": "", "limit": None, "cursor": None},
    ))
    assert items == [] and cursor == "c2"
    _module, method, params, _deadline = caller.calls[0]
    assert method == "memory/admin_list"
    assert params == {"layer": "global", "status": "active", "to": 9.0, "limit": 50}


def test_admin_get_unwraps_item_and_chain():
    app, _caller = _app(
        [_record()],
        {"memory/admin_get": {
            "item": {"id": "01J0000000000000000000000A", "text": "new", "status": "active",
                     "layer": "global", "namespace": "user:default", "created_at": 2},
            "chain": [
                {"id": "01J0000000000000000000000B", "text": "old", "status": "superseded",
                 "created_at": 1},
            ],
        }},
    )
    item, chain = asyncio.run(MemoryAdapter(app).admin_get("01J0000000000000000000000A"))
    assert item.id == "01J0000000000000000000000A"
    assert "item" not in item.meta and "chain" not in item.meta
    assert [row.id for row in chain] == ["01J0000000000000000000000B"]
    assert chain[0].meta["status"] == "superseded"


def test_admin_writes_forward_by_and_reason():
    app, caller = _app(
        [_record()],
        {"memory/admin_promote": {"ok": True, "id": "m1"},
         "memory/admin_archive": {"ok": True},
         "memory/admin_restore": {"ok": True}},
    )
    port = MemoryAdapter(app)
    assert asyncio.run(port.admin_promote("m1", "admin:me")) is True
    assert asyncio.run(port.admin_archive("m1", "wrong", "admin:me")) is True
    assert asyncio.run(port.admin_restore("m1", "admin:me")) is True
    (_, _, promote, _), (_, _, archive, _), (_, _, restore, _) = caller.calls
    assert promote == {"id": "m1", "by": "admin:me"}
    assert archive == {"id": "m1", "reason": "wrong", "by": "admin:me"}
    assert restore == {"id": "m1", "by": "admin:me"}


def test_admin_stats_returns_the_bare_result():
    app, _caller = _app(
        [_record()],
        {"memory/admin_stats": {"global_count": 1, "global_limit": 5000,
                                "staging_count": 0, "archived_count": 2, "oldest_at": None}},
    )
    stats = asyncio.run(MemoryAdapter(app).admin_stats())
    assert stats["archived_count"] == 2 and stats["oldest_at"] is None


def test_a_tool_envelope_is_unwrapped_and_an_error_envelope_raises():
    app, _caller = _app(
        [_record()],
        {"memory/read_session": {"ok": True, "result": {"items": [
            {"id": "01J0000000000000000000000A", "text": "wrapped"},
        ]}}},
    )
    items = asyncio.run(MemoryAdapter(app).read_session("01J0000000000000000000000S"))
    assert [item.text for item in items] == ["wrapped"]

    failing, _ = _app(
        [_record()],
        {"memory/admin_stats": {"ok": False, "error": {"code": "UNAVAILABLE", "message": "down"}}},
    )
    with pytest.raises(VadeError) as caught:
        asyncio.run(MemoryAdapter(failing).admin_stats())
    assert caught.value.code == "UNAVAILABLE"


def test_an_unknown_module_error_code_cannot_leak_through():
    app, _caller = _app(
        [_record()],
        {"memory/admin_stats": {"ok": False, "error": {"code": "MADE_UP", "message": "?"}}},
    )
    with pytest.raises(VadeError) as caught:
        asyncio.run(MemoryAdapter(app).admin_stats())
    assert caught.value.code == "INTERNAL"


def test_the_router_caller_is_used_when_module_caller_is_absent():
    class Router:
        def __init__(self, caller):
            self._caller = caller

    caller = FakeCaller({"memory/admin_stats": {"global_count": 0, "global_limit": 5000,
                                                "staging_count": 0, "archived_count": 0,
                                                "oldest_at": None}})
    app = SimpleNamespace(registry=FakeRegistry([_record()]), router=Router(caller))
    assert asyncio.run(MemoryAdapter(app).admin_stats())["global_count"] == 0
    assert caller.calls[0][1] == "memory/admin_stats"


def test_install_sets_the_optional_app_slots():
    from vade.memory import install

    app = SimpleNamespace()
    install(app)
    assert isinstance(app.memory_port, MemoryAdapter)
    # One object, two names: a test that injects `app.memory` cannot be shadowed
    # by a stale `app.memory_port` (and vice versa).
    assert app.memory is app.memory_port
