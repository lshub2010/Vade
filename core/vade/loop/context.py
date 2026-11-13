"""Context window fitting — every number comes from `[compaction]`.

No hardcoded 0.5/0.2/20/3 here: a config change must change behaviour without
touching code, and `tail_mode` is a restart item (it breaks prefix cache), so it
is read but never rewritten by this module.

Order of attack (22-C3 §T4):
  1. no-LLM pruning first: old oversized tool results -> summary + blob_id;
  2. then drop the earliest assistant text;
  3. only then drop whole oldest non-protected messages.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..session.models import Message

__all__ = ["CompactionConfig", "BlobStore", "ContextBuilder", "estimate_tokens"]


def estimate_tokens(text: str) -> int:
    """Cheap deterministic estimate (~4 chars/token); good enough for a budget."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    threshold: float = 0.50
    target_ratio: float = 0.20
    protect_last_n: int = 20
    protect_first_n: int = 3
    tail_mode: str = "restart"
    proactive_prune_tokens: int = 0
    min_result_chars: int = 2000
    max_tool_result_tokens: int = 4000
    ctx_tokens: int = 32_000

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "CompactionConfig":
        data = data or {}
        defaults = cls()
        values: dict[str, Any] = {}
        for name in ("threshold", "target_ratio", "protect_last_n", "protect_first_n",
                     "tail_mode", "proactive_prune_tokens", "min_result_chars",
                     "max_tool_result_tokens"):
            if name in data:
                values[name] = data[name]
        cfg = cls(**values)
        if not (0 < cfg.threshold <= 1) or not (0 < cfg.target_ratio < cfg.threshold):
            raise ValueError("invalid [compaction]: need 0 < target_ratio < threshold <= 1")
        return cfg


class BlobStore:
    """Full tool output kept out of the context window (`data/blobs/`)."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def put(self, text: str) -> str:
        blob_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        path = self.directory / f"{blob_id}.txt"
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        return blob_id

    def get(self, blob_id: str) -> str | None:
        path = self.directory / f"{blob_id}.txt"
        return path.read_text(encoding="utf-8") if path.exists() else None


class ContextBuilder:
    def __init__(
        self,
        config: CompactionConfig | None = None,
        *,
        blob_store: BlobStore | None = None,
        token_counter: Callable[[str], int] = estimate_tokens,
    ) -> None:
        self.config = config or CompactionConfig()
        self.blobs = blob_store
        self._count = token_counter

    def tokens(self, messages: Sequence[Message]) -> int:
        total = 0
        for message in messages:
            total += self._count(message.content) + 4
            for call in message.tool_calls:
                total += self._count(str(call.get("function", {}).get("arguments", "")))
        return total

    def fit(
        self,
        messages: Sequence[Message],
        *,
        ctx_tokens: int | None = None,
        current_turn_id: str | None = None,
    ) -> list[Message]:
        ctx = ctx_tokens or self.config.ctx_tokens
        cfg = self.config
        if self.tokens(messages) <= cfg.threshold * ctx:
            return list(messages)

        keep = self._protected_indices(messages, current_turn_id)
        working = list(messages)

        # 1) prune old oversized tool results without calling a model
        if cfg.proactive_prune_tokens > 0 and self.blobs is not None:
            for index, message in enumerate(working):
                if index in keep or message.role != "tool":
                    continue
                if len(message.content) <= cfg.min_result_chars:
                    continue
                blob_id = self.blobs.put(message.content)
                working[index] = _replace_content(
                    message, f"[pruned tool result blob_id={blob_id}] " + message.content[:200]
                )
                if self.tokens(working) <= cfg.threshold * ctx:
                    break

        # 2) earliest assistant text first
        for index, message in enumerate(working):
            if self.tokens(working) <= cfg.target_ratio * ctx:
                return working
            if index in keep or message.role != "assistant" or message.tool_calls:
                continue
            working[index] = _replace_content(message, "[trimmed]")

        # 3) drop whole oldest non-protected messages
        index = 0
        while index < len(working) and self.tokens(working) > cfg.target_ratio * ctx:
            if index in keep:
                index += 1
                continue
            del working[index]
            keep = {i - 1 if i > index else i for i in keep}
        return working

    def _protected_indices(self, messages: Sequence[Message], current_turn_id: str | None) -> set[int]:
        cfg = self.config
        protected: set[int] = set()
        for index, message in enumerate(messages):
            if message.role == "system":
                protected.add(index)
            if current_turn_id is not None and message.turn_id == current_turn_id:
                protected.add(index)
        protected.update(range(min(cfg.protect_first_n, len(messages))))
        if cfg.protect_last_n:
            protected.update(range(max(0, len(messages) - cfg.protect_last_n), len(messages)))
        return protected


def _replace_content(message: Message, content: str) -> Message:
    from dataclasses import replace

    return replace(message, content=content)
