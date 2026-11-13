"""LLM provider protocol + streaming delta shapes.

One interface, two providers (builtin OpenAI-compatible, module-provided).
The loop consumes `Delta` and never touches HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence, runtime_checkable

__all__ = ["ToolCallDelta", "Delta", "LLMProvider", "Usage"]


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """One fragment of a streamed tool call.

    `arguments` arrives in pieces; the loop concatenates by `index`. This is the
    single most error-prone part of OpenAI streaming, so it has its own unit test.
    """

    index: int = 0
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class Delta:
    text: str = ""
    tool_calls: tuple[ToolCallDelta, ...] = ()
    usage: Mapping[str, int] | None = None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens}


@runtime_checkable
class LLMProvider(Protocol):
    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[Delta]: ...
