"""RFC-0015 §4.6/§6: the frame ceiling, the missing registry, and params tolerance.

A11 has two halves: an ordinary 8-page module must fit comfortably, and a
synthetic oversized registry must be REFUSED with `PAYLOAD_TOO_LARGE` +
`data={field,limit,actual}` — never silently truncated (§4.6: truncation makes
"this module has no entry" unexplainable).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from vade.errors import VadeError
from vade.modulepage.service import ModulePageService
from vade.registry.uipage import serialize

from .modules_helpers import browser, manifest, page, record, registry_without_component, service

ROOT = Path(__file__).resolve().parents[3]
FRAME_LIMIT = 65_519


def _eight_pages() -> tuple[dict, ...]:
    # C-19 freezes `maxItems: 8`; this is the honest worst case per module.
    # Page ids need >= 3 chars (`UIPage` validation), hence `page0`..`page7`.
    return tuple(page(f"page{index}", title="t" * 40, version=1) for index in range(8))


def test_registry_not_installed_is_unavailable():
    with pytest.raises(VadeError) as excinfo:
        registry_without_component().list_modules(browser())
    assert excinfo.value.code == "UNAVAILABLE"


def test_eight_page_module_fits_comfortably_under_a_frame():
    svc = service(record(manifest(display_name="下载管理", pages=_eight_pages())))
    payload = svc.list_modules(browser())
    size = len(serialize(payload))
    assert size <= 8 * 1024, f"8-page module used {size} bytes"


def test_oversized_registry_is_payload_too_large_and_never_truncated():
    template = manifest(display_name="模" * 64, pages=_eight_pages())
    records = tuple(
        # `replace` keeps the real (validated) page rows and only swaps identity,
        # so the size comes from data the wire could actually carry.
        record(replace(template, module_id=f"com.example.m{index:03d}", instance_id=f"i{index}"))
        for index in range(120)
    )
    svc = service(*records, revision=1)

    with pytest.raises(VadeError) as excinfo:
        svc.list_modules(browser())

    error = excinfo.value
    assert error.code == "PAYLOAD_TOO_LARGE"
    assert error.data["field"] == "modules"
    assert error.data["limit"] == FRAME_LIMIT
    assert error.data["actual"] > FRAME_LIMIT


def test_unknown_params_are_tolerated_and_identical_to_empty_params():
    """§4.1/§6: `params` is `{}`; unknown keys are ignored for forward compat."""
    import asyncio

    from vade.modulepage.rpc import ModulePageRPC

    svc = service(record(manifest(pages=(page("queue"),))))
    rpc = ModulePageRPC(svc)
    connection = browser()

    empty = asyncio.run(rpc.modules(connection, {}))
    noisy = asyncio.run(rpc.modules(connection, {"cursor": "x", "session_id": "session-1", "future": [1, 2]}))
    none_params = asyncio.run(rpc.modules(connection, None))

    assert noisy == empty
    assert none_params == empty


def test_declared_errors_are_exactly_the_three_existing_codes():
    """No new C-04 codes: the method may only raise what `errors.json` already has.

    The declaration half (rpc.json `errors`) is asserted too — a code that is
    served but not declared is the same drift in the other direction.
    """
    rpc_schema = json.loads((ROOT / "schema" / "rpc.json").read_text(encoding="utf-8"))
    errors_schema = json.loads((ROOT / "schema" / "errors.json").read_text(encoding="utf-8"))
    declared = rpc_schema["directions"]["ui_to_core"]["ui/modules"]["errors"]
    assert set(declared) == {"PERMISSION_DENIED", "UNAVAILABLE", "PAYLOAD_TOO_LARGE"}
    assert set(declared) <= set(errors_schema["codes"])
    assert len(errors_schema["codes"]) == 34  # C-04 untouched (RFC-0015 §6)


def test_service_is_the_same_object_the_component_exposes():
    """`app.modulepage` is what `list_modules()` lives on — no second service."""
    from types import SimpleNamespace

    from vade.modulepage import install
    from vade.registry.table import Registry

    app = SimpleNamespace(config=None, registry=Registry())
    install(app)
    assert isinstance(app.modulepage, ModulePageService)
    assert callable(app.modulepage.list_modules)
    assert app.module_pages is app.modulepage
