"""RFC-0015 §10: who may enumerate what (`ui/modules` acceptance A4).

Identity comes from the connection only (`modulepage/auth.py`, review B1). These
tests are the boundary: a param that names another module must change nothing,
a disabled module is invisible to a browser but visible to itself, and an
unresolved socket is refused rather than served "probably fine".
"""

from __future__ import annotations

import pytest

from vade.errors import VadeError

from .modules_helpers import anonymous, browser, manifest, module_peer, page, record, service


def test_unknown_caller_is_permission_denied():
    svc = service(record(manifest(pages=(page("queue"),))))
    with pytest.raises(VadeError) as excinfo:
        svc.list_modules(anonymous())
    assert excinfo.value.code == "PERMISSION_DENIED"


def test_browser_only_sees_enabled_modules():
    svc = service(
        record(manifest("com.example.on", "o-1", display_name="开")),
        record(manifest("com.example.off", "f-1", display_name="关"), enabled=False),
    )
    payload = svc.list_modules(browser())
    assert [row["module_id"] for row in payload["modules"]] == ["com.example.on"]


def test_module_peer_sees_only_its_own_row():
    svc = service(
        record(manifest("com.example.download", "dl-1")),
        record(manifest("com.example.other", "o-1")),
    )
    payload = svc.list_modules(module_peer("com.example.download", "dl-1"))
    assert [row["module_id"] for row in payload["modules"]] == ["com.example.download"]


def test_module_peer_still_sees_itself_when_disabled():
    """§4.5: `enabled=false` hides a module from browsers, not from itself.

    A draining module asking for its own entry list is not an information leak,
    and answering `[]` would make "my pages vanished" indistinguishable from
    "I am disabled" — both are already reported elsewhere.
    """
    svc = service(record(manifest("com.example.download", "dl-1"), enabled=False))
    payload = svc.list_modules(module_peer("com.example.download", "dl-1"))
    assert [row["module_id"] for row in payload["modules"]] == ["com.example.download"]


def test_a_module_peer_with_no_registered_row_gets_an_empty_list_not_someone_elses():
    svc = service(record(manifest("com.example.other", "o-1")))
    payload = svc.list_modules(module_peer("com.example.download", "dl-1"))
    assert payload["modules"] == []


def test_rejected_record_without_a_manifest_is_never_listed():
    svc = service(
        record(None, module_id="com.example.rejected", instance_id="r-1", online=False),
        record(manifest("com.example.ok", "o-1")),
    )
    payload = svc.list_modules(browser())
    assert [row["module_id"] for row in payload["modules"]] == ["com.example.ok"]


def test_read_only_browser_is_served():
    """§6: `READ_ONLY_SESSION` gates writes; this method is a read."""
    svc = service(record(manifest(pages=(page("queue"),))))
    payload = svc.list_modules(browser(read_only=True))
    assert len(payload["modules"]) == 1


def test_params_naming_another_module_cannot_widen_the_view():
    """B1: the identity is the socket, never a field the caller controls."""
    from vade.modulepage.rpc import ModulePageRPC

    svc = service(
        record(manifest("com.example.download", "dl-1")),
        record(manifest("com.example.other", "o-1")),
    )
    rpc = ModulePageRPC(svc)
    import asyncio

    result = asyncio.run(
        rpc.modules(
            module_peer("com.example.download", "dl-1"),
            {"module_id": "com.example.other", "instance_id": "o-1", "device_id": "com.example.other#o-1"},
        )
    )
    assert [row["module_id"] for row in result["modules"]] == ["com.example.download"]
