"""Turn-scoped tool snapshot + OpenAI schema rendering.

The snapshot is taken once per turn. `capabilities.update` mid-turn does not
change what the model sees, which is what makes `agent-loop` a pure consumer of
`snapshot` entries (and testable by grepping for concrete tool names).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..router.names import ToolNameMap

__all__ = ["ToolEntry", "ToolSnapshot", "build_snapshot"]


@dataclass(frozen=True, slots=True)
class ToolEntry:
    llm_name: str
    tool_id: str
    module_id: str
    description: str
    input_schema: Mapping[str, Any]
    risk: str
    timeout_ms: int
    idempotent: bool
    injects: tuple[str, ...] = ()
    instance_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSnapshot:
    entries: tuple[ToolEntry, ...] = ()
    name_map: ToolNameMap = field(default_factory=lambda: ToolNameMap.build(()))

    def get(self, llm_name: str) -> ToolEntry | None:
        for entry in self.entries:
            if entry.llm_name == llm_name:
                return entry
        return None

    def names(self) -> tuple[str, ...]:
        return tuple(e.llm_name for e in self.entries)

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI `tools` array. Deterministic order (sorted by llm name)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": e.llm_name,
                    "description": e.description,
                    "parameters": dict(e.input_schema) or {"type": "object", "properties": {}},
                },
            }
            for e in self.entries
        ]

    def render_block(self) -> str:
        if not self.entries:
            return ""
        lines = ["Available tools (call by exact name):"]
        for entry in self.entries:
            lines.append(f"- {entry.llm_name}: {entry.description} [risk={entry.risk}]")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


def build_snapshot(registry: Any) -> ToolSnapshot:
    """Freeze `registry` into a per-turn snapshot (never re-read afterwards).

    A tool appears once per module even when several pooled instances exist;
    the router owns instance selection, the snapshot only owns naming.
    """
    if registry is None:
        return ToolSnapshot()
    pairs: list[tuple[str, str]] = []
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for record in registry.snapshot():
        if not record.enabled or record.manifest is None or record.connection_id is None:
            continue
        health = getattr(record.health, "value", str(record.health))
        if health in ("offline", "rejected"):
            continue
        for spec in record.manifest.tools:
            key = (record.module_id, spec.name)
            pairs.append(key)
            slot = merged.setdefault(key, {
                "module_id": record.module_id,
                "tool_id": f"{record.module_id}/{spec.name}",
                "description": spec.description,
                "input_schema": spec.input_schema,
                "risk": spec.risk,
                "timeout_ms": spec.timeout_ms,
                "idempotent": spec.idempotent,
                "injects": tuple(record.manifest.injects),
                "instances": [],
            })
            slot["instances"].append(record.instance_id)
            # Effective risk is computed by the registry; take the highest.
            for binding in registry.bindings_for(spec.name):
                if binding.module_id == record.module_id:
                    if _rank(binding.effective_risk) > _rank(slot["risk"]):
                        slot["risk"] = binding.effective_risk
    name_map = ToolNameMap.build(pairs)
    entries = []
    for (module_id, tool), slot in sorted(merged.items()):
        entries.append(ToolEntry(
            llm_name=name_map.llm_name(f"{module_id}/{tool}") or f"tool__{tool}",
            tool_id=slot["tool_id"], module_id=module_id, description=slot["description"],
            input_schema=slot["input_schema"], risk=slot["risk"], timeout_ms=slot["timeout_ms"],
            idempotent=slot["idempotent"], injects=slot["injects"],
            instance_ids=tuple(sorted(slot["instances"])),
        ))
    return ToolSnapshot(entries=tuple(entries), name_map=name_map)


def _rank(risk: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(risk, 2)
