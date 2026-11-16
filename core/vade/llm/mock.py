"""Scripted provider for tests and the e2e lifecycle (no network, no cost).

A script is a list of turns; each turn is a list of `Delta`s. `calls` records
the exact messages/tools the loop sent, which is how tests assert that the tool
snapshot and prompt assembly reached the model.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping, Sequence

from .base import Delta, ToolCallDelta

__all__ = ["MockLLMProvider"]


class MockLLMProvider:
    def __init__(self, script: Sequence[Sequence[Delta]] | None = None) -> None:
        self.script = [list(turn) for turn in (script or [])]
        self.calls: list[dict[str, Any]] = []
        self._index = 0

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        stream: bool = True,
        model: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> AsyncIterator[Delta]:
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": list(tools or []), "stream": stream,
                           "model": model})
        if self._index >= len(self.script):
            yield Delta(text="", finish_reason="stop")
            return
        turn = self.script[self._index]
        self._index += 1
        for delta in turn:
            yield delta
