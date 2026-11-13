"""Shared fixtures for session tests (no network, no LLM)."""

from __future__ import annotations

import asyncio
import time
from typing import Any


def make_manifest(
    module_id: str = "com.example.fs",
    instance_id: str = "fs-01",
    tool: str = "read_file",
    *,
    concurrency: str = "pooled",
    timeout_ms: int = 2000,
    injects: tuple[str, ...] = ("session_id",),
    risk: str = "low",
    schema: dict[str, Any] | None = None,
    max_concurrency: int = 4,
) -> dict[str, Any]:
    return {
        "module_id": module_id,
        "instance_id": instance_id,
        "version": "1.0.0",
        "protocol": "vade/v1",
        "concurrency": concurrency,
        "capabilities": {
            "tools": [{
                "name": tool,
                "description": f"{tool} for tests",
                "input_schema": schema or {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                "risk": risk,
                "idempotent": True,
                "timeout_ms": timeout_ms,
            }]
        },
        "limits": {"max_concurrency": max_concurrency, "max_payload": 65519},
        "injects": list(injects),
    }


class CollectSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    def seqs(self) -> list[int]:
        return [int(e["seq"]) for e in self.events]

    def types(self) -> list[str]:
        return [str(e["type"]) for e in self.events]


class GateExecutor:
    """Turn executor that blocks until released; records arrival order."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.order: list[str] = []

    async def __call__(self, turn: Any, content: Any) -> Any:
        from vade.loop.engine import TurnOutcome

        self.order.append(turn.id)
        await self.gate.wait()
        return TurnOutcome(assistant_text=f"echo:{turn.user_text}", stopped_reason="stop")


async def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()
