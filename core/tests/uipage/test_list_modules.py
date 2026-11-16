"""RFC-0015 `ui/modules`: the row/ordering/revision rules (acceptance A1–A6, A9).

The service is the unit under test: it decides *what* the whole list says. The
wire (dispatch table + a real socket) is covered by
`core/tests/e2e/test_ui_modules_ws.py`; visibility/auth is in
`test_list_modules_auth.py`; the frame ceiling is in
`test_list_modules_limits.py`.
"""

from __future__ import annotations

import json

import pytest

from vade.errors import VadeError
from vade.modulepage.rpc import ModulePageRPC
from vade.registry.table import Registry

from .modules_helpers import (
    ids,
    manifest,
    module_peer,
    page,
    real_registry,
    record,
    registry_without_component,
    service,
    browser,
)

MODULE_ROW_KEYS = {"module_id", "instance_id", "display_name", "version", "online", "pages"}
PAGE_ROW_KEYS = {"id", "title", "icon", "entry", "version", "requires", "refresh_s"}


# ---------------------------------------------------------------- A1: empty + shape
def test_empty_registry_returns_no_modules_and_revision_zero():
    # A real (empty) Registry, not a fake: `revision` starts at 0 by contract
    # (`table.py:122`) and this method must not invent a counter of its own.
    payload = service(registry=Registry()).list_modules(browser())
    assert payload == {"revision": 0, "modules": []}


def test_one_module_with_pages_projects_every_declared_field():
    pages = (
        page("queue", title="下载队列", icon="list", entry="auto", version=1),
        page("settings", title="下载设置", icon="settings", entry="panel",
             version=2, requires=["session"], refresh_s=5),
    )
    svc = service(record(manifest(display_name="下载管理", version="0.3.1", pages=pages)), revision=42)

    payload = svc.list_modules(browser())

    assert payload["revision"] == 42
    assert len(payload["modules"]) == 1
    row = payload["modules"][0]
    assert set(row) == MODULE_ROW_KEYS
    assert row["module_id"] == "com.example.download"
    assert row["instance_id"] == "dl-1"
    assert row["display_name"] == "下载管理"
    assert row["version"] == "0.3.1"          # the module SemVer, not a page revision
    assert row["online"] is True
    assert row["pages"] == [
        {"id": "queue", "title": "下载队列", "icon": "list", "entry": "auto",
         "version": 1, "requires": [], "refresh_s": 0},
        {"id": "settings", "title": "下载设置", "icon": "settings", "entry": "panel",
         "version": 2, "requires": ["session"], "refresh_s": 5},
    ]
    assert set(row["pages"][0]) == PAGE_ROW_KEYS


def test_pages_without_ui_block_are_an_empty_list_not_a_missing_key():
    svc = service(record(manifest(display_name="笔记")))
    [row] = svc.list_modules(browser())["modules"]
    assert row["pages"] == []
    # FR-8.79: `()` from `pages()` is "show no entry", and the wire says the
    # same thing with `[]` — never by dropping the key.
    assert svc.pages(row["module_id"], row["instance_id"]) == ()


# ---------------------------------------------------------------- A2: == pages()
def test_page_rows_equal_the_pages_method_key_by_key():
    """A2: the wire row and `pages()` must be the same dict, not two spellings.

    Renaming `id` to `page_id` anywhere on one side (or adding a key to the other)
    must fail here before a client ever sees the mismatch.
    """
    pages = (page("queue"), page("stats", version=3, requires=["session"]))
    svc = service(record(manifest(pages=pages)))

    [row] = svc.list_modules(browser())["modules"]
    from_method = svc.pages(row["module_id"], row["instance_id"])

    assert [dict(item) for item in from_method] == row["pages"]
    for wire_row, method_row in zip(row["pages"], from_method):
        assert list(wire_row) == list(method_row)
        assert set(wire_row) == PAGE_ROW_KEYS
        assert wire_row == method_row
    # The module's SemVer and the page's schema revision are different numbers;
    # a projection that mixed them would pass a loose equality but fail this.
    assert row["version"] == "1.0.0"
    assert [item["version"] for item in row["pages"]] == [1, 3]


# ---------------------------------------------------------------- ordering
def test_module_order_is_the_registry_snapshot_order():
    """Ordering rule (written down so it is not re-guessed): `snapshot()` order.

    `Registry.snapshot()` sorts by `(module_id, instance_id)` (`table.py:380`),
    so the wire order is alphabetical, NOT first-registration order. The RFC's
    parenthetical says "首次注册顺序"; the normative half ("snapshot() 顺序") is
    what this method follows, and this test pins it against a real Registry.
    """
    registry = real_registry(
        manifest("com.example.zebra", "z-1"),
        manifest("com.example.alpha", "a-1", concurrency="pooled", max_concurrency=2),
        manifest("com.example.alpha", "a-2", concurrency="pooled", max_concurrency=2),
    )
    svc = service(registry=registry)

    payload = svc.list_modules(browser())

    assert ids(payload) == [
        f"{row.module_id}#{row.instance_id}" for row in registry.snapshot()
    ]
    assert ids(payload) == [
        "com.example.alpha#a-1",
        "com.example.alpha#a-2",
        "com.example.zebra#z-1",
    ]


def test_two_calls_without_a_mutation_return_the_same_order():
    svc = service(
        record(manifest("com.example.b", "b-1")),
        record(manifest("com.example.a", "a-1")),
        revision=7,
    )
    first = svc.list_modules(browser())
    second = svc.list_modules(browser())
    assert first == second
    assert ids(first) == ["com.example.b#b-1", "com.example.a#a-1"]


# ---------------------------------------------------------------- A5: online/offline
def test_offline_module_is_still_listed_with_its_pages_and_online_false():
    pages = (page("queue"),)
    svc = service(
        record(manifest("com.example.download", "dl-1", display_name="下载", pages=pages), online=True),
        record(manifest("com.example.notes", "no-1", display_name="笔记", pages=pages), online=False),
        revision=1,
    )

    rows = {row["module_id"]: row for row in svc.list_modules(browser())["modules"]}

    assert rows["com.example.download"]["online"] is True
    assert rows["com.example.notes"]["online"] is False
    # §4.5: offline is a banner/card, not a disappearing entry.
    assert len(rows["com.example.notes"]["pages"]) == 1


def test_online_needs_both_health_and_a_connection_id():
    """The exact `module.status` predicate (`table.py:147`), one spelling."""
    healthy_but_detached = record(manifest("com.example.a", "a-1"))
    healthy_but_detached.connection_id = None
    svc = service(healthy_but_detached)
    [row] = svc.list_modules(browser())["modules"]
    assert row["online"] is False


# ---------------------------------------------------------------- A3: display_name
def test_missing_display_name_is_null_and_never_borrows_module_id():
    # C-03's `display_name` is OPTIONAL but not nullable: a manifest that wants
    # no title omits the key. The wire field is required-with-null (§4.2), so
    # "absent" must become `null` — and must never become `module_id`.
    with pytest.raises(Exception) as excinfo:
        manifest(display_name=None)
    assert "display_name" in str(excinfo.value)

    svc = service(record(manifest()))
    [row] = svc.list_modules(browser())["modules"]
    assert row["display_name"] is None
    assert row["display_name"] != row["module_id"]


def test_manifest_without_the_key_also_projects_null():
    # `display_name` is optional in C-03; "absent" and "null" must look the same
    # on the wire, and neither may raise.
    svc = service(record(manifest()))
    [row] = svc.list_modules(browser())["modules"]
    assert "display_name" in row and row["display_name"] is None


def test_display_name_is_carried_by_the_manifest_dataclass():
    """The field C-03 always had and `Manifest` used to drop (RFC-0015 §4.2).

    `validate_manifest` is where the projection's only source of truth lives, so
    the assertion belongs here: if this regresses, every row silently loses its
    entry title.
    """
    assert manifest(display_name="下载管理").display_name == "下载管理"
    assert manifest().display_name is None


# ---------------------------------------------------------------- A6: revision
def test_revision_increments_on_register_and_disconnect():
    registry = Registry()
    svc = service(registry=registry)
    assert svc.list_modules(browser())["revision"] == 0

    registry.register(manifest("com.example.a", "a-1"), fingerprint="fp-a", connection_id="conn-a")
    after_register = svc.list_modules(browser())
    assert after_register["revision"] == 1
    assert after_register["modules"][0]["module_id"] == "com.example.a"

    registry.mark_disconnected("com.example.a", "a-1")
    after_disconnect = svc.list_modules(browser())
    assert after_disconnect["revision"] == 2
    assert after_disconnect["modules"][0]["online"] is False


def test_revision_is_stable_across_reads_and_moves_with_capabilities_update():
    first = manifest("com.example.download", "dl-1", pages=(page("queue"),))
    registry = real_registry(first)
    svc = service(registry=registry)

    before = svc.list_modules(browser())
    again = svc.list_modules(browser())
    assert again == before, "a read must not bump the revision (no side effects)"
    assert [item["id"] for item in before["modules"][0]["pages"]] == ["queue"]

    updated = manifest(
        "com.example.download", "dl-1", version="1.1.0",
        pages=(page("queue"), page("stats", version=2)),
    )
    registry.update_capabilities(updated, fingerprint="fp-0")

    after = svc.list_modules(browser())
    assert after["revision"] == before["revision"] + 1
    assert after["modules"][0]["version"] == "1.1.0"
    assert [item["id"] for item in after["modules"][0]["pages"]] == ["queue", "stats"]


# ---------------------------------------------------------------- A9: no bodies
def test_response_carries_no_page_body_or_schema_keys():
    """A9: the machine judgement for "body does not cross the wire"."""
    pages = (page("queue"), page("stats"))
    svc = service(record(manifest(pages=pages)))
    payload = svc.list_modules(browser())

    def walk(value):
        if isinstance(value, dict):
            assert not ({"schema", "children", "$defs"} & set(value)), value
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    text = json.dumps(payload, ensure_ascii=False)
    for forbidden in ('"schema"', '"children"', '"$defs"', '"node"'):
        assert forbidden not in text, f"{forbidden} leaked into ui/modules"


# ---------------------------------------------------------------- registration
def test_handler_is_registered_in_the_rpc_table():
    """A declaration in rpc.json is not a wire method; the handler table is."""
    svc = service(record(manifest(pages=(page("queue"),))))
    handlers = ModulePageRPC(svc).handlers()
    assert set(handlers) == {"ui/page.get", "ui/state.get", "ui/tool.call", "ui/modules"}


def test_install_publishes_the_handler_into_session_handlers():
    """`install()` is what a real core runs; without it the socket answers C-04 NOT_FOUND
    with an `unknown method` message (not JSON-RPC -32601, though clients accept both)."""
    from types import SimpleNamespace

    from vade.modulepage import install

    # Path 1 (normal for a real core): `session` is not installed yet, so
    # `modulepage` hands its table to `handler_providers` and `session.install()`
    # drains it. Alphabetical order makes this the provider branch.
    app = SimpleNamespace(config=None, registry=Registry())
    install(app)
    assert "ui/modules" in app.handler_providers[0]()

    # Path 2: `session_handlers` already exists (e.g. re-install / tests).
    app2 = SimpleNamespace(config=None, registry=Registry(), session_handlers={})
    install(app2)
    assert "ui/modules" in app2.session_handlers
    assert app2.session_handlers["ui/modules"] == app2.modulepage_rpc.modules


def test_install_survives_session_replacing_the_handler_table():
    """Real startup order: an earlier component creates the dict, `session`
    then REPLACES it (`session/__init__.py:232`).

    The handlers must come back through `handler_providers`, or `ui/modules`
    (and the three C-19 methods) answer "unknown method" on a real socket while
    every direct-service test still passes — the exact gap the WS e2e caught.
    """
    from types import SimpleNamespace

    from vade.modulepage import install

    app = SimpleNamespace(config=None, registry=Registry(), session_handlers={})
    install(app)
    assert "ui/modules" in app.session_handlers  # immediate write

    # What `session.install()` does: a brand-new table plus a provider drain.
    original = app.session_handlers["ui/modules"]
    app.session_handlers = {"ui/submit": object()}
    for provider in tuple(app.handler_providers):
        for name, handler in provider().items():
            app.session_handlers.setdefault(name, handler)

    assert app.session_handlers["ui/modules"] == original
    assert {"ui/page.get", "ui/state.get", "ui/tool.call"} <= set(app.session_handlers)
