"""Bidirectional `module_id/tool` <-> `{prefix}__{tool}` mapping.

The prefix is the last segment of `module_id`; two modules whose last segment
collides get `fs`, `fs2`, ... in sorted module order. The map is built from a
turn's tool snapshot and never re-read from the live registry, so a
`capabilities.update` mid-turn cannot rename a tool the model already saw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

__all__ = ["ToolNameMap", "prefix_for", "LLM_NAME_RE"]

LLM_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_UNSAFE = re.compile(r"[^a-z0-9_]")


def prefix_for(module_id: str) -> str:
    tail = module_id.rsplit(".", 1)[-1].lower()
    tail = _UNSAFE.sub("_", tail)
    if not tail or not tail[0].isalnum():
        tail = f"m_{tail}" if tail else "module"
    return tail


@dataclass(frozen=True, slots=True)
class ToolNameMap:
    """Frozen mapping. Two dicts, no mutation after construction."""

    forward: Mapping[str, str]  # tool_id -> llm name
    backward: Mapping[str, str]  # llm name -> tool_id
    prefixes: Mapping[str, str]  # module_id -> prefix

    @classmethod
    def build(cls, pairs: Iterable[tuple[str, str]]) -> "ToolNameMap":
        """`pairs` is (module_id, tool_name). Deterministic for equal input."""
        by_module: dict[str, list[str]] = {}
        for module_id, tool in pairs:
            by_module.setdefault(module_id, [])
            if tool not in by_module[module_id]:
                by_module[module_id].append(tool)

        prefixes: dict[str, str] = {}
        taken: dict[str, str] = {}  # prefix -> module_id
        for module_id in sorted(by_module):
            base = prefix_for(module_id)
            candidate = base
            suffix = 2
            while candidate in taken and taken[candidate] != module_id:
                candidate = f"{base}{suffix}"
                suffix += 1
            taken[candidate] = module_id
            prefixes[module_id] = candidate

        forward: dict[str, str] = {}
        backward: dict[str, str] = {}
        for module_id in sorted(by_module):
            for tool in sorted(by_module[module_id]):
                tool_id = f"{module_id}/{tool}"
                llm_name = f"{prefixes[module_id]}__{tool}"
                forward[tool_id] = llm_name
                backward[llm_name] = tool_id
        return cls(forward=MappingProxyType(forward), backward=MappingProxyType(backward),
                   prefixes=MappingProxyType(prefixes))

    def llm_name(self, tool_id: str) -> str | None:
        return self.forward.get(tool_id)

    def tool_id(self, llm_name: str) -> str | None:
        return self.backward.get(llm_name)

    def __len__(self) -> int:
        return len(self.forward)
