"""`LLMProvider` backed by an M6 `model` module (T5, second half).

Selection is registry-driven, never model-driven: online instances that declare
the `chat` tool, highest `priority` first, ties broken by the freshest
heartbeat. The module is addressed through the same `Router` as any tool, so
deadlines, circuit breaking and error mapping are shared, not re-implemented.

v1 is non-streaming on the module side (45-M6 §六): the module returns one
complete OpenAI-shaped message and this adapter yields it as a single `Delta`.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping, Sequence

from ..errors import VadeError
from ..router.dispatch import Router
from .base import Delta, ToolCallDelta

__all__ = ["ModuleLLMProvider", "to_deltas"]

#: M6's single tool; a candidate that does not declare it is not a model module.
MODEL_TOOL = "chat"


class ModuleLLMProvider:
    def __init__(
        self,
        router: Router | None,
        registry: Any,
        *,
        model_module: str | None = None,
        fallback: Any = None,
        temperature: float = 0.7,
        timeout_ms: int = 120_000,
    ) -> None:
        self.router = router
        self.registry = registry
        self.model_module = model_module
        self.fallback = fallback
        self.temperature = temperature
        self.timeout_ms = timeout_ms

    # ------------------------------------------------------------------ selection
    def select(self) -> Any | None:
        """Highest-priority online model instance, or None."""
        if self.registry is None:
            return None
        candidates = []
        for record in self.registry.snapshot():
            manifest = record.manifest
            if manifest is None or record.connection_id is None:
                continue
            if self.model_module is not None and manifest.module_id != self.model_module:
                continue
            if manifest.tool(MODEL_TOOL) is None:
                continue
            health = getattr(record.health, "value", str(record.health))
            if health in ("offline", "rejected"):
                continue
            candidates.append(record)
        if not candidates:
            return None
        candidates.sort(key=lambda r: (-r.manifest.priority, -(r.last_frame_at or 0.0)))
        return candidates[0]

    # ------------------------------------------------------------------ provider
    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[Delta]:
        record = self.select()
        if record is None or self.router is None:
            if self.fallback is not None:
                async for delta in self.fallback.chat(messages, tools=tools, stream=stream):
                    yield delta
                return
            raise VadeError("UNAVAILABLE", "no model module is online")

        args: dict[str, Any] = {"messages": [dict(m) for m in messages]}
        if tools:
            args["tools"] = list(tools)
        args["temperature"] = self.temperature
        tool_id = f"{record.manifest.module_id}/{MODEL_TOOL}"
        # A model module is not session scoped: no `injects` on M6, so no ctx.
        result = await self.router.call(
            tool_id, args, session_id="", call_id=f"llm-{record.instance_id}", deadline_ms=self.timeout_ms
        )
        if not result.ok:
            raise VadeError(result.code or "UNAVAILABLE", result.message or "model module call failed")
        for delta in to_deltas(result.result):
            yield delta


def to_deltas(payload: Any) -> tuple[Delta, ...]:
    """Translate M6's reply into unified deltas (v1: exactly one)."""
    if not isinstance(payload, Mapping):
        raise VadeError("INTERNAL", "model module returned a non-object result")
    message = payload.get("message")
    if not isinstance(message, Mapping):
        # Not fatal on its own: an empty answer is still an answer.
        message = {}
    text = message.get("content")
    calls: list[ToolCallDelta] = []
    raw_calls = message.get("tool_calls") or ()
    if not isinstance(raw_calls, (list, tuple)):
        raise VadeError("INTERNAL", "model module returned tool_calls that is not an array")
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, Mapping):
            raise VadeError("INTERNAL", "model module returned a malformed tool call")
        function = raw.get("function") or {}
        arguments = function.get("arguments", "")
        if isinstance(arguments, Mapping):
            # 45-M6 §五: arguments MUST be a string; an object silently loses
            # the call downstream, so normalise instead of trusting it.
            arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        calls.append(
            ToolCallDelta(
                index=index,
                id=str(raw.get("id")) if raw.get("id") is not None else None,
                name=function.get("name"),
                arguments=str(arguments or ""),
            )
        )
    usage_raw = payload.get("usage")
    usage = None
    if isinstance(usage_raw, Mapping):
        usage = {k: v for k, v in usage_raw.items() if isinstance(v, int)}
    return (
        Delta(
            text=str(text or ""),
            tool_calls=tuple(calls),
            usage=usage,
            finish_reason=payload.get("finish_reason"),
        ),
    )
