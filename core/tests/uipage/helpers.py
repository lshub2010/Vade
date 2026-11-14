"""Fixtures for the C-19 service tests: a registry record, a caller, a router.

No sockets and no real module: the service's job is to decide *what* to send and
*what to refuse*, so a recording fake is the honest test surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping

from vade.errors import VadeError
from vade.modulepage.service import ModulePageService, PageLimits
from vade.policy.approval import ApprovalManager
from vade.policy.engine import PolicyEngine
from vade.registry.manifest import Manifest, validate_manifest
from vade.router.dispatch import ToolCallResult

TOOL = {
    "name": "refresh",
    "description": "Refresh the download queue",
    "input_schema": {"type": "object", "properties": {"all": {"type": "boolean"}}},
    "risk": "low",
    "idempotent": True,
    "timeout_ms": 10000,
}

HIGH_TOOL = {**TOOL, "name": "purge", "description": "Delete every download", "risk": "high"}


def button(node_id: str, action: Mapping[str, Any], **props: Any) -> dict:
    return {
        "type": "component",
        "component": "Button",
        "id": node_id,
        "props": {"text": node_id, **props},
        "on": {"click": dict(action)},
    }


def page(page_id: str = "queue", *, children: list | None = None, requires: list | None = None) -> dict:
    body: dict[str, Any] = {
        "id": page_id,
        "title": f"page {page_id}",
        "version": 1,
        "schema": {
            "type": "column",
            "children": children if children is not None else [
                {"type": "text", "data_ref": "page.state.queue_len", "props": {"format": "int"}},
                button("refresh-btn", {"kind": "tools/call", "tool": "refresh", "args": {"all": True}}),
            ],
        },
    }
    if requires is not None:
        body["requires"] = requires
    return body


def make_manifest(*pages: dict, tools: tuple[dict, ...] = (TOOL,), module_id: str = "com.example.download") -> Manifest:
    return validate_manifest({
        "module_id": module_id,
        "instance_id": "dl-01",
        "version": "1.0.0",
        "protocol": "vade/v1",
        "capabilities": {"tools": [dict(tool) for tool in tools]},
        "limits": {"max_concurrency": 1, "max_payload": 65519},
        "ui": {"pages": [dict(item) for item in pages]},
    })


@dataclass
class FakeRecord:
    module_id: str
    instance_id: str
    manifest: Manifest
    connection_id: str | None = "conn-1"
    enabled: bool = True


class FakeRegistry:
    def __init__(self, *records: FakeRecord) -> None:
        self._records = records

    def snapshot(self) -> tuple[FakeRecord, ...]:
        return tuple(self._records)


class FakeCaller:
    """Records core→module calls; replies come from a method→payload table."""

    def __init__(self, replies: Mapping[str, Any] | None = None) -> None:
        self.replies = dict(replies or {})
        self.calls: list[tuple[str, dict]] = []

    async def call(self, target: Any, method: str, params: Mapping[str, Any], deadline_ms: int) -> Any:
        self.calls.append((method, dict(params)))
        reply = self.replies.get(method)
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            raise VadeError("NOT_FOUND", f"{method} is not implemented")
        return dict(reply)


class FakeRouter:
    def __init__(self, *, ok: bool = True, result: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._ok = ok
        self._result = result

    async def call(self, tool_id: str, args: Mapping[str, Any], *, session_id: str,
                   turn_id: str | None = None, call_id: str | None = None,
                   deadline_ms: int | None = None) -> ToolCallResult:
        self.calls.append({
            "tool_id": tool_id, "args": dict(args), "session_id": session_id,
            "turn_id": turn_id, "call_id": call_id,
        })
        return ToolCallResult(self._ok, tool_id, result=self._result if self._result is not None else {"done": True})


class FakeConnection:
    def __init__(self, *, device_id: str = "dev-1", read_only: bool = False) -> None:
        self.device_id = device_id
        self.connection_id = "ui-conn"
        self.read_only = read_only
        self.auth_mode = "device_key"


def make_service(
    *pages: dict,
    tools: tuple[dict, ...] = (TOOL,),
    router: FakeRouter | None = None,
    caller: FakeCaller | None = None,
    approval: ApprovalManager | None = None,
    state_cache_s: int = 5,
    pending_calls: int = 256,
) -> tuple[ModulePageService, FakeRecord, FakeCaller, FakeRouter]:
    manifest = make_manifest(*pages, tools=tools)
    record = FakeRecord(manifest.module_id, manifest.instance_id, manifest)
    caller = caller or FakeCaller()
    router = router or FakeRouter()
    app = SimpleNamespace(
        registry=FakeRegistry(record),
        module_caller=caller,
        router=router,
        approval_port=approval,
        session_handlers={},
    )
    service = ModulePageService(
        app, limits=PageLimits(state_cache_s=state_cache_s, pending_calls=pending_calls)
    )
    return service, record, caller, router


def make_approval(*, timeout_s: float = 5.0) -> ApprovalManager:
    return ApprovalManager(timeout_s=timeout_s, engine=PolicyEngine(), approver_probe=lambda: True)
