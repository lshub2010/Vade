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
from .base import Delta, ToolCallDelta, flatten_usage

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
    def select(self, model: str | None = None) -> Any | None:
        """Highest-priority online model instance, or None.

        RFC-0005 §5.3: `model` is the id a turn resolved at dequeue. When it is
        a module id (`com/<name>` or `com/*`) only an instance that really
        serves that model is eligible — a pin must never be served by a
        neighbour, or a switch would silently answer with the old model.
        """
        if self.registry is None:
            return None
        candidates = []
        for record in self.registry.snapshot():
            manifest = record.manifest
            if manifest is None or record.connection_id is None:
                continue
            if self.model_module is not None and manifest.module_id != self.model_module:
                continue
            tools = getattr(manifest, "tools", None)
            if manifest.tool(MODEL_TOOL) is None or (tools is not None and len(tools) != 1):
                # `chat` must be the module's only tool: otherwise a memory/fs
                # module could become the provider and read every prompt.
                continue
            if model is not None:
                provider, _, name = model.partition("/")
                if provider and manifest.module_id != provider:
                    continue
                if name and name != "*":
                    declared = tuple(getattr(manifest, "models", ()) or ())
                    if not any(spec.name == name for spec in declared):
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
        model: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> AsyncIterator[Delta]:
        if model is not None and model.startswith("builtin/"):
            # The resolver picked the builtin: this adapter is only a facade in
            # module mode, so hand the call to the builtin provider it wraps.
            if self.fallback is None:
                raise VadeError("UNAVAILABLE", f"model {model!r} is not available")
            async for delta in self.fallback.chat(messages, tools=tools, stream=stream, model=model):
                yield delta
            return

        record = self.select(model)
        if record is None or self.router is None:
            if self.fallback is not None and model is None:
                async for delta in self.fallback.chat(messages, tools=tools, stream=stream):
                    yield delta
                return
            raise VadeError("UNAVAILABLE", "no model module is online")
        if attribution is not None:
            attribution.update({"module_id": record.manifest.module_id,
                                "instance_id": record.instance_id})

        args: dict[str, Any] = {"messages": [dict(m) for m in messages]}
        _, _, chosen = (model or "").partition("/")
        if chosen and chosen != "*":
            # §4.3: `/<module>/*` means "the module's own default", so the
            # request body carries no `model` at all in that case.
            args["model"] = chosen
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
    usage = flatten_usage(usage_raw) if isinstance(usage_raw, Mapping) else None
    return (
        Delta(
            text=str(text or ""),
            tool_calls=tuple(calls),
            usage=usage,
            finish_reason=payload.get("finish_reason"),
        ),
    )
