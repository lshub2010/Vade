"""Fixtures for RFC-0015 `ui/modules`.

Two surfaces, on purpose:

* a hand-built snapshot (`FakeRegistry`) for the row/ordering/visibility rules —
  they are pure projections and a fake says exactly one thing per test;
* the REAL `Registry` for `revision` — monotonicity is a property of
  `registry/table.py`, and asserting it against another fake would only test the
  fake. `register` / `capabilities.update` / `mark_disconnected` all `_bump()`
  next to `_publish_status(...)`, which is the whole §5 argument.

Manifests go through `validate_manifest`, never a dict literal: the row keys are
C-03's, so a fixture that skipped validation could invent a shape the wire would
never carry (`module_id` without a dot, a page with an unknown icon, ...).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from vade.modulepage.service import ModulePageService
from vade.registry.health import HealthState
from vade.registry.manifest import Manifest, validate_manifest
from vade.registry.table import Registry

_UNSET = object()

BROWSER_DEVICE = "ui-0123456789abcdef"


def manifest(
    module_id: str = "com.example.download",
    instance_id: str = "dl-1",
    *,
    display_name: Any = _UNSET,
    version: str = "1.0.0",
    pages: tuple[dict[str, Any], ...] = (),
    concurrency: str = "exclusive",
    max_concurrency: int = 1,
) -> Manifest:
    """A valid C-03 manifest; `display_name` is omitted unless asked for."""
    raw: dict[str, Any] = {
        "module_id": module_id,
        "instance_id": instance_id,
        "version": version,
        "protocol": "vade/v1",
        "concurrency": concurrency,
        "capabilities": {"tools": []},
        "limits": {"max_concurrency": max_concurrency, "max_payload": 4096},
    }
    if display_name is not _UNSET:
        raw["display_name"] = display_name
    if pages:
        raw["ui"] = {"pages": [dict(item) for item in pages]}
    return validate_manifest(raw)


def page(
    page_id: str = "queue",
    *,
    title: str | None = None,
    version: int = 1,
    requires: list[str] | None = None,
    icon: str | None = None,
    entry: str | None = None,
    refresh_s: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": page_id,
        "title": title if title is not None else f"page {page_id}",
        "version": version,
        "schema": {"type": "column", "children": []},
    }
    if requires is not None:
        body["requires"] = list(requires)
    if icon is not None:
        body["icon"] = icon
    if entry is not None:
        body["entry"] = entry
    if refresh_s is not None:
        body["refresh_s"] = refresh_s
    return body


def record(
    manifest_obj: Manifest | None = None,
    *,
    module_id: str | None = None,
    instance_id: str | None = None,
    enabled: bool = True,
    online: bool = True,
) -> SimpleNamespace:
    """A NodeRecord-shaped row; `manifest=None` models a rejected node."""
    if manifest_obj is not None:
        mid = manifest_obj.module_id if module_id is None else module_id
        iid = manifest_obj.instance_id if instance_id is None else instance_id
    else:
        if module_id is None or instance_id is None:
            raise ValueError("a record without a manifest needs module_id and instance_id")
        mid, iid = module_id, instance_id
    return SimpleNamespace(
        module_id=mid,
        instance_id=iid,
        manifest=manifest_obj,
        enabled=enabled,
        health=HealthState.ONLINE if online else HealthState.OFFLINE,
        connection_id=f"conn-{mid}" if online else None,
    )


class FakeRegistry:
    """Snapshot order is whatever was handed in; revision is a fixed number."""

    def __init__(self, records: tuple[SimpleNamespace, ...] = (), revision: int = 0) -> None:
        self._records = tuple(records)
        self._revision = revision

    def snapshot(self) -> tuple[SimpleNamespace, ...]:
        return self._records

    def revision(self) -> int:
        return self._revision


def service(
    *records: SimpleNamespace,
    revision: int = 0,
    registry: Any = _UNSET,
) -> ModulePageService:
    app = SimpleNamespace(
        registry=registry if registry is not _UNSET else FakeRegistry(records, revision)
    )
    return ModulePageService(app)


def registry_without_component() -> ModulePageService:
    """`service._registry()` returns None — the UNAVAILABLE branch."""
    return ModulePageService(SimpleNamespace(registry=None))


def browser(**kwargs: Any) -> SimpleNamespace:
    values = {"device_id": BROWSER_DEVICE, "browser_session": True, "read_only": False}
    values.update(kwargs)
    return SimpleNamespace(**values)


def module_peer(module_id: str = "com.example.download", instance_id: str = "dl-1") -> SimpleNamespace:
    return SimpleNamespace(device_id=f"{module_id}#{instance_id}", browser_session=False)


def anonymous() -> SimpleNamespace:
    return SimpleNamespace(device_id="", browser_session=False)


def real_registry(*manifests: Manifest) -> Registry:
    """A real `Registry` with one connection per manifest (all ONLINE)."""
    registry = Registry()
    for index, item in enumerate(manifests):
        registry.register(item, fingerprint=f"fp-{index}", connection_id=f"conn-{index}")
    return registry


def ids(payload: dict[str, Any]) -> list[str]:
    return [f"{row['module_id']}#{row['instance_id']}" for row in payload["modules"]]
