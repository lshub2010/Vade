"""Turn-scoped tool snapshot + OpenAI schema rendering.

The snapshot is taken once per turn. `capabilities.update` mid-turn does not
change what the model sees, which is what makes `agent-loop` a pure consumer of
`snapshot` entries (and testable by grepping for concrete tool names).

RFC-0003 §4.3 adds the hot/cold split on top: `resolve_hot` merges the four
hot layers (core built-ins, loaded skills' `requires_tools`, config, session
sticky set) against the FROZEN snapshot only, `ToolSnapshot.select` projects
the hot set, and `with_core` grafts core-native entries on without touching
the name map (core tools are never router-routed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..errors import VadeError
from ..router.names import ToolNameMap

__all__ = ["ToolEntry", "ToolSnapshot", "build_snapshot", "resolve_hot", "normalize_tool_key", "HOT_MAX_DEFAULT"]

HOT_MAX_DEFAULT = 12

#: Tools that are a *provider channel*, not a capability for the model. M6's
#: `chat` is how `ModuleLLMProvider` reaches a model module; exposing it in the
#: tool table let the model call its own provider recursively (and, with
#: several model modules online, handed it one callable per module). The router
#: still addresses `<module_id>/chat` — it reads the registry, not this
#: snapshot — so hiding it here costs nothing.
PROVIDER_TOOLS = frozenset({"chat"})


def normalize_tool_key(name: str) -> str:
    """`mcp-bridge__s_kb_recall`, `mcp_bridge/s_kb_recall` and `MCP-BRIDGE__S_KB_RECALL`
    compare equal — the same shape the skill loader uses for `requires_tools`."""
    return str(name or "").strip().lower().replace("/", "__").replace(".", "_").replace("-", "_")


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

    def select(self, names: Iterable[str]) -> "ToolSnapshot":
        """Project the hot set (RFC-0003 §4.3). Order is preserved (entries are
        name-sorted), so the projected schemas() diff has one insertion point."""
        wanted = set(names or ())
        if not wanted:
            return ToolSnapshot(entries=(), name_map=self.name_map)
        return ToolSnapshot(
            entries=tuple(e for e in self.entries if e.llm_name in wanted),
            name_map=self.name_map,
        )

    def with_core(self, extra: Sequence[ToolEntry]) -> "ToolSnapshot":
        """Graft core-native entries (RFC-0003 §4.2.4). A name the snapshot
        already carries is never shadowed; the merged table stays name-sorted.
        """
        add: list[ToolEntry] = []
        taken = {e.llm_name for e in self.entries}
        for entry in extra or ():
            # `extra` itself may repeat a name (two built-ins registered under
            # one label); the second copy used to reach the provider as a
            # duplicate schema, which is a 400 for the whole turn.
            if entry.llm_name in taken:
                continue
            taken.add(entry.llm_name)
            add.append(entry)
        if not add:
            return self
        return ToolSnapshot(
            entries=tuple(sorted(self.entries + tuple(add), key=lambda e: e.llm_name)),
            name_map=self.name_map,
        )

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


def resolve_hot(
    *,
    snapshot: ToolSnapshot,
    core_names: Sequence[str] = (),
    config_hot: Sequence[str] = (),
    session_known: Sequence[str] = (),
    skill_requires: Sequence[str] = (),
    hot_max: int = HOT_MAX_DEFAULT,
) -> tuple[tuple[str, ...], int]:
    """RFC-0003 §4.3.1: merge the four hot layers against the frozen snapshot.

    Priority (core > loaded-skill requires > config > session sticky) is ONLY a
    truncation order; the returned names are sorted by `llm_name` so the tools
    array diff has a single predictable insertion point. Everything is matched
    through `normalize_tool_key`, so `requires_tools` may use either the LLM
    name or the protocol `tool_id` form. Returns `(hot_names, dropped_count)`.
    """
    index: dict[str, ToolEntry] = {}
    for entry in snapshot.entries:
        keys = {normalize_tool_key(entry.llm_name), normalize_tool_key(entry.tool_id),
                str(entry.llm_name).lower()}
        for key in keys:
            index.setdefault(key, entry)
    layers = ((0, core_names), (1, skill_requires), (2, config_hot), (3, session_known))
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for rank, names in layers:
        for name in names or ():
            entry = index.get(normalize_tool_key(str(name)))
            if entry is not None and entry.llm_name not in seen:
                seen.add(entry.llm_name)
                ranked.append((rank, entry.llm_name))
    cap = max(1, int(hot_max))
    kept = sorted(ranked)[:cap]
    # The trailer's N counts every tool the snapshot holds that the model does
    # NOT see: not just cap-truncation, but any tool no hot layer named. Those
    # are exactly the tools `tools_search` can still surface (§4.3.1 trailer,
    # §4.3.2 "只搜本轮快照").
    return tuple(sorted(name for _, name in kept)), max(0, len(snapshot.entries) - len(kept))


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
            if spec.name in PROVIDER_TOOLS:
                # A model module's `chat` is the provider channel; it must not
                # become a tool the model can call (§FR-2.7 no reverse calls).
                continue
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
    seen_names: dict[str, str] = {}
    for (module_id, tool), slot in sorted(merged.items()):
        llm_name = name_map.llm_name(f"{module_id}/{tool}") or f"tool__{tool}"
        previous = seen_names.get(llm_name)
        if previous is not None:
            # The prefix map already disambiguates `fs`/`fs2`, so a collision
            # here means the fallback name (or a module whose tail normalises
            # to the same prefix) produced one llm name for two tools. Sending
            # both to a provider is a 400 at best and a silently wrong call at
            # worst, so fail at snapshot time with both ids named.
            raise VadeError(
                "MANIFEST_INVALID",
                f"two tools map to the same LLM name {llm_name!r}: "
                f"{previous!r} and {slot['tool_id']!r}",
            )
        seen_names[llm_name] = slot["tool_id"]
        entries.append(ToolEntry(
            llm_name=llm_name,
            tool_id=slot["tool_id"], module_id=module_id, description=slot["description"],
            input_schema=slot["input_schema"], risk=slot["risk"], timeout_ms=slot["timeout_ms"],
            idempotent=slot["idempotent"], injects=slot["injects"],
            instance_ids=tuple(sorted(slot["instances"])),
        ))
    return ToolSnapshot(entries=tuple(entries), name_map=name_map)


def _rank(risk: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(risk, 2)
