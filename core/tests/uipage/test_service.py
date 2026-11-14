"""`ui/page.get` / `ui/state.get` (RFC-0009 §7.1 / §9.3) — served, cached, checked.

A5/A13 live here: `pages()` is the entry list, and it is empty when the module
declared no page. A7's core half is the binding root test in
`registry/test_ui_pages.py`: an out-of-root path never reaches this service
because the page is refused before it is stored.
"""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.modulepage.service import PageLimits
from vade.registry.uipage import page_digest

from .helpers import FakeCaller, make_service, page


def _body(**props: object) -> dict:
    return {"type": "column", "children": [{"type": "text", "props": {"text": "x", **props}}]}


def test_pages_is_empty_without_ui():
    service, record, _caller, _router = make_service()
    assert service.pages(record.module_id, record.instance_id) == ()


def test_pages_lists_entries_with_entry_and_requires():
    service, record, _c, _r = make_service(page("queue"), page("stats", children=[]))
    entries = service.pages(record.module_id, record.instance_id)
    assert [entry["id"] for entry in entries] == ["queue", "stats"]
    assert entries[0]["entry"] == "auto" and entries[0]["requires"] == []


def test_inline_page_is_returned_with_its_digest():
    service, record, _c, _r = make_service(page("queue"))
    result = asyncio.run(service.get_page(record.module_id, record.instance_id, "queue"))
    assert result["schema"]["type"] == "column"
    assert result["sha256"] == page_digest(result["schema"])


def test_unknown_page_is_not_found():
    service, record, _c, _r = make_service(page("queue"))
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(service.get_page(record.module_id, record.instance_id, "nope"))
    assert excinfo.value.code == "NOT_FOUND"


def test_ref_page_is_fetched_checked_and_cached():
    body = _body()
    digest = page_digest(body)
    ref_page = {
        "id": "big", "title": "big page", "version": 1,
        "schema": {"ref": {"page_id": "big", "sha256": digest}},
    }
    caller = FakeCaller({"page/get": {"schema": body}})
    service, record, _c, _r = make_service(ref_page, caller=caller)

    first = asyncio.run(service.get_page(record.module_id, record.instance_id, "big"))
    second = asyncio.run(service.get_page(record.module_id, record.instance_id, "big"))
    assert first["sha256"] == digest == second["sha256"]
    assert [method for method, _params in caller.calls] == ["page/get"], "second read is cached"


def test_ref_page_digest_mismatch_is_manifest_invalid():
    ref_page = {
        "id": "big", "title": "big page", "version": 1,
        "schema": {"ref": {"page_id": "big", "sha256": "0" * 64}},
    }
    caller = FakeCaller({"page/get": {"schema": _body()}})
    service, record, _c, _r = make_service(ref_page, caller=caller)
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(service.get_page(record.module_id, record.instance_id, "big"))
    assert excinfo.value.code == "MANIFEST_INVALID"
    assert "sha256" in excinfo.value.message


def test_ref_page_oversize_is_manifest_invalid():
    body = {"type": "column", "children": [
        {"type": "text", "props": {"text": "z" * 4000}} for _ in range(20)
    ]}
    ref_page = {
        "id": "big", "title": "big page", "version": 1,
        "schema": {"ref": {"page_id": "big", "sha256": page_digest(body)}},
    }
    service, record, _c, _r = make_service(ref_page, caller=FakeCaller({"page/get": {"schema": body}}))
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(service.get_page(record.module_id, record.instance_id, "big"))
    assert excinfo.value.code == "MANIFEST_INVALID"
    assert "64" in excinfo.value.message or "bytes" in excinfo.value.message


def test_ref_body_is_revalidated_at_fetch_time():
    """A ref body is checked with the same table as a declaration, not trusted."""
    body = {"type": "column", "children": [
        {"type": "component", "component": "MessageBubble", "props": {"text": "hi"}},
    ]}
    ref_page = {
        "id": "big", "title": "big page", "version": 1,
        "schema": {"ref": {"page_id": "big", "sha256": page_digest(body)}},
    }
    service, record, _c, _r = make_service(ref_page, caller=FakeCaller({"page/get": {"schema": body}}))
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(service.get_page(record.module_id, record.instance_id, "big"))
    assert excinfo.value.code == "MANIFEST_INVALID"


def test_state_get_is_cached_and_rev_is_monotonic():
    caller = FakeCaller({"state/get": {"rev": 3, "state": {"queue_len": 2}}})
    service, record, _c, _r = make_service(page("queue"), caller=caller, state_cache_s=60)

    first = asyncio.run(service.get_state(record.module_id, record.instance_id, "queue"))
    second = asyncio.run(service.get_state(record.module_id, record.instance_id, "queue"))
    assert first == second
    assert first["rev"] == 3 and first["state"] == {"queue_len": 2}
    assert [m for m, _p in caller.calls] == ["state/get"], "cached within [ui].state_cache_s"

    # a fresh answer with a LOWER rev still advances: clients may render a stale
    # rev first, so it must never go backwards (§9.3).
    caller.replies["state/get"] = {"rev": 1, "state": {"queue_len": 5}}
    service.limits = PageLimits(state_cache_s=0)
    third = asyncio.run(service.get_state(record.module_id, record.instance_id, "queue"))
    assert third["rev"] == 4 and third["state"] == {"queue_len": 5}


def test_state_get_unsupported_is_reported_as_unsupported():
    service, record, _c, _r = make_service(page("queue"), caller=FakeCaller({}))
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(service.get_state(record.module_id, record.instance_id, "queue"))
    assert excinfo.value.code == "UNSUPPORTED"


def test_state_get_rejects_non_flat_state():
    caller = FakeCaller({"state/get": {"rev": 1, "state": {"nested": {"a": 1}}}})
    service, record, _c, _r = make_service(page("queue"), caller=caller)
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(service.get_state(record.module_id, record.instance_id, "queue"))
    assert excinfo.value.code == "UNAVAILABLE"


def test_install_publishes_handlers_before_session_exists():
    """`modulepage` installs alphabetically before `session` (m < s)."""
    from types import SimpleNamespace

    from vade.modulepage import install

    app = SimpleNamespace(config=None)
    install(app)
    assert app.modulepage is app.module_pages
    providers = getattr(app, "handler_providers", None)
    assert isinstance(providers, list) and len(providers) == 1
    assert set(providers[0]()) == {"ui/page.get", "ui/state.get", "ui/tool.call"}


def test_install_merges_into_an_existing_handler_table():
    from types import SimpleNamespace

    from vade.modulepage import install

    app = SimpleNamespace(config=None, session_handlers={"ui/submit": object()})
    install(app)
    assert set(app.session_handlers) == {"ui/submit", "ui/page.get", "ui/state.get", "ui/tool.call"}
