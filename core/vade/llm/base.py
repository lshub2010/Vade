"""LLM provider protocol + streaming delta shapes.

One interface, two providers (builtin OpenAI-compatible, module-provided).
The loop consumes `Delta` and never touches HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence, runtime_checkable

__all__ = ["ToolCallDelta", "Delta", "LLMProvider", "Usage", "flatten_usage"]


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


def flatten_usage(raw: Mapping[str, Any]) -> dict[str, int]:
    """One provider usage blob -> flat integer counts (RFC-0005 §6.1).

    Only integers travel: OpenAI's nested `*_details` are lifted to
    `cached_tokens` / `reasoning_tokens` when present, because the UI needs
    them and they are NOT additive to the three frozen totals.
    """
    out: dict[str, int] = {
        key: value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    for source, target in (("prompt_tokens_details", "cached_tokens"),
                           ("completion_tokens_details", "reasoning_tokens")):
        details = raw.get(source)
        if isinstance(details, Mapping):
            value = details.get(target)
            if isinstance(value, int) and not isinstance(value, bool):
                out.setdefault(target, value)
    return out


@runtime_checkable
class LLMProvider(Protocol):
    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        stream: bool = True,
        model: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> AsyncIterator[Delta]: ...
    """RFC-0005 §7.1 row 9 (internal, breaking for implementors).

    `model` is the full `<provider>/<model>` id a turn resolved at dequeue;
    `attribution` is an out-parameter the provider fills with the module it
    actually routed to (`module_id`/`instance_id`), so the `llm.call` audit row
    has exactly one owner. Both default to `None`: a provider that ignores them
    keeps its current behaviour.
    """
