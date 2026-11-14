"""Fixtures shared by the RFC-0005 tests.

Everything here is real except the LLM: the config is a real `Config` built from
`schema/config.schema.json` (so `[usage]`/`[llm]` defaults are the contract's),
manifests go through `validate_manifest`, and the registry is the real
`Registry`. That way a change to the schema defaults cannot make these tests
pass on numbers the product would never see.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Iterable, Mapping

from vade.config import Config, load_config
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry

__all__ = [
    "BASE_CONFIG",
    "make_config",
    "chat_tool",
    "chat_manifest",
    "registry_of",
    "usage_record",
    "ScriptedExecutor",
    "SequenceExecutor",
    "FakeAuditReader",
    "llm_row",
    "FakeConnection",
]

#: One real config load, reused as the default layer (schema + x-vade metadata).
BASE_CONFIG: Config = load_config(environ={})


def make_config(**overrides: Any) -> Config:
    """A real `Config` with dotted overrides applied over the schema defaults.

    `load_config(cli=...)` deliberately rejects keys inside a free-form table
    (`[llm.capabilities]` is `additionalProperties: true`, so `cli` sees
    "unknown key"), hence the direct deep copy. The schema — and therefore
    every `apply`/`owner` answer — is still the shipped one.
    """
    values = copy.deepcopy(dict(BASE_CONFIG.values))
    for dotted, value in overrides.items():
        node = values
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(values=values, schema=BASE_CONFIG.schema)


def chat_tool(name: str = "chat") -> dict[str, Any]:
    """The `chat` tool that makes a module a model module (M6)."""
    return {
        "name": name,
        "description": "one model turn",
        "input_schema": {
            "type": "object",
            "properties": {"messages": {"type": "array"}},
            "required": ["messages"],
        },
        "risk": "low",
        "idempotent": True,
        "timeout_ms": 30000,
    }


def chat_manifest(
    module_id: str = "com.example.model",
    instance_id: str = "m-01",
    *,
    models: Iterable[Mapping[str, Any]] | None = None,
    priority: int = 0,
    tools: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """A valid C-03 manifest; `models` becomes `capabilities.models[]`."""
    manifest: dict[str, Any] = {
        "module_id": module_id,
        "instance_id": instance_id,
        "version": "1.0.0",
        "protocol": "vade/v1",
        "concurrency": "pooled",
        "priority": priority,
        "capabilities": {"tools": list(tools) if tools is not None else [chat_tool()]},
        "limits": {"max_concurrency": 4, "max_payload": 65519},
        "injects": ["session_id"],
    }
    if models is not None:
        manifest["capabilities"]["models"] = [dict(m) for m in models]
    return manifest


def registry_of(*manifests: Mapping[str, Any], clock: Any = None) -> Registry:
    """A real `Registry` holding every manifest (registration order preserved)."""
    kwargs = {"clock": clock} if clock is not None else {}
    registry = Registry(**kwargs)
    for index, raw in enumerate(manifests):
        entry = validate_manifest(raw)
        registry.register(
            entry,
            fingerprint=f"fp-{entry.module_id}-{entry.instance_id}",
            connection_id=f"conn-{index}",
        )
    return registry


def usage_record(
    total_tokens: int,
    *,
    source: str = "provider",
    model_id: str | None = "builtin/gpt-4o-mini",
) -> dict[str, Any]:
    """`turn.completed.usage` as §6.1 defines it (three frozen + six appended)."""
    if source == "none":
        prompt = completion = total_tokens = 0
    else:
        prompt = total_tokens // 2
        completion = total_tokens - prompt
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total_tokens,
        "model_id": model_id,
        "provider": model_id.split("/", 1)[0] if model_id else None,
        "source": source,
        "steps": 1,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }


class ScriptedExecutor:
    """Turn executor that answers with a fixed outcome and keeps the turns.

    The queue persists `outcome.usage` on the turn, which is exactly what
    `session/usage.get` and the budget gate read back — so a test can put a
    known number on the ledger without a provider.
    """

    def __init__(self, usage: Mapping[str, Any] | None = None, text: str = "ok") -> None:
        self.usage = dict(usage or {})
        self.text = text
        self.turns: list[Any] = []

    async def __call__(self, turn: Any, content: Any) -> Any:
        from vade.loop.engine import TurnOutcome

        self.turns.append(turn)
        return TurnOutcome(usage=dict(self.usage), assistant_text=self.text, stopped_reason="stop")


class SequenceExecutor(ScriptedExecutor):
    """One usage record per turn, in order (an empty tail means "unmeasured")."""

    def __init__(self, *usages: Mapping[str, Any], text: str = "ok") -> None:
        super().__init__(text=text)
        self.usages = [dict(usage) for usage in usages]

    async def __call__(self, turn: Any, content: Any) -> Any:
        from vade.loop.engine import TurnOutcome

        self.turns.append(turn)
        usage = self.usages.pop(0) if self.usages else {}
        return TurnOutcome(usage=dict(usage), assistant_text=self.text, stopped_reason="stop")


def llm_row(
    session_id: str,
    *,
    tokens: int,
    purpose: str = "chat",
    ts: float | None = None,
    turn_id: str = "turn-1",
    model_id: str = "builtin/gpt-4o-mini",
) -> dict[str, Any]:
    """One `llm.call` audit row, the shape `audit/writer` stores."""
    return {
        "kind": "llm.call",
        "session_id": session_id,
        "turn_id": turn_id,
        "ts": time.time() if ts is None else float(ts),
        "extra": {"purpose": purpose, "total_tokens": int(tokens), "model": model_id},
    }


class FakeAuditReader:
    """`audit/reader.AuditReader.query` with a deliberately tiny page.

    `UsageAccounting` must page past the reader's 500-row clamp, so the fake
    returns at most `page` rows and a `next_cursor` until exhausted.
    """

    def __init__(self, rows: Iterable[Mapping[str, Any]], *, page: int = 2) -> None:
        self.rows = [dict(row) for row in rows]
        self.page = page
        self.queries: list[dict[str, Any]] = []

    def query(
        self,
        *,
        kind: str | None = None,
        from_ts: float | None = None,
        limit: int = 100,
        cursor: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.queries.append({"kind": kind, "from_ts": from_ts, "cursor": cursor, "limit": limit})
        rows = self.rows
        if kind is not None:
            rows = [row for row in rows if row.get("kind") == kind]
        if from_ts is not None:
            rows = [row for row in rows if float(row.get("ts", 0.0)) >= from_ts]
        start = int(cursor or 0)
        page = rows[start:start + min(int(limit), self.page)]
        end = start + len(page)
        return {"items": page, "next_cursor": str(end) if end < len(rows) else None}


class FakeConnection:
    """Minimal browser-session connection for the RPC surface."""

    def __init__(self, connection_id: str = "ui-1") -> None:
        self.connection_id = connection_id
        self.device_id = f"dev-{connection_id}"
        self.auth_mode = "device_key"
        self.events: list[dict[str, Any]] = []

    async def send_event(self, frame: Mapping[str, Any]) -> None:
        self.events.append(dict(frame["params"]))
