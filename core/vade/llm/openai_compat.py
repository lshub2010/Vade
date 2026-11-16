"""OpenAI-compatible `/v1/chat/completions` adapter (builtin provider).

Rules (22-C3 §T5):
  * the key is read ONLY from the env var named in `[llm].api_key_env`;
    it is never written to config or logs;
  * retry only 429 / 5xx / connection errors, max 2, exponential + jitter;
    4xx is never retried. **Once a delta has been yielded the attempt is
    committed**: a mid-stream failure is re-raised instead of retried, because
    a retry replays text / tool-call fragments the caller already consumed and
    bills the same prompt twice;
  * streamed `tool_calls[].function.arguments` fragments are concatenated by
    index — `parse_chunk` is pure and unit-tested without a network.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any, AsyncIterator, Mapping, Sequence

from .base import Delta, ToolCallDelta, flatten_usage

__all__ = ["OpenAICompatProvider", "parse_chunk", "RETRIABLE_STATUS"]

RETRIABLE_STATUS = {408, 429, 500, 502, 503, 504}


def parse_chunk(chunk: Mapping[str, Any]) -> Delta:
    """Translate one SSE JSON object into a Delta (pure)."""
    choices = chunk.get("choices") or []
    usage = chunk.get("usage")
    usage_map = flatten_usage(usage) if isinstance(usage, Mapping) else None
    if not choices:
        return Delta(usage=usage_map)
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    text = delta.get("content") or ""
    calls: list[ToolCallDelta] = []
    for raw in delta.get("tool_calls") or ():
        function = raw.get("function") or {}
        calls.append(
            ToolCallDelta(
                index=int(raw.get("index", 0)),
                id=raw.get("id"),
                name=function.get("name"),
                arguments=function.get("arguments") or "",
            )
        )
    return Delta(text=text, tool_calls=tuple(calls), usage=usage_map,
                 finish_reason=choice.get("finish_reason"))


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str = "VADE_LLM_API_KEY",
        timeout_s: float = 120.0,
        max_retries: int = 2,
        transport: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._transport = transport  # injectable httpx.AsyncBaseTransport for tests

    def _headers(self) -> dict[str, str]:
        key = os.environ.get(self.api_key_env)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        stream: bool = True,
        model: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> AsyncIterator[Delta]:
        import httpx  # local import: the core must start without httpx

        # RFC-0005: a turn may be pinned to `builtin/<name>`; strip the provider
        # prefix before it reaches the wire. `attribution` stays empty: a
        # builtin call has no module, which is exactly what `llm.call` records.
        requested = model.split("/", 1)[1] if model and model.startswith("builtin/") else model
        body: dict[str, Any] = {"model": requested or self.model, "messages": list(messages), "stream": stream}
        if tools:
            body["tools"] = list(tools)
        last_error: Exception | None = None
        #: Set on the first yielded delta. From that point a retry would send
        #: the prompt a second time and replay fragments the caller already
        #: accumulated (observed as `HELLOHELLOHELLO` and triple billing), so a
        #: later failure is final; the recovery path is a new turn, not a loop.
        committed = False
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as client:
                    async with client.stream("POST", f"{self.base_url}/chat/completions",
                                             headers=self._headers(), json=body) as response:
                        if response.status_code in RETRIABLE_STATUS and attempt < self.max_retries:
                            await response.aread()
                            last_error = RuntimeError(f"HTTP {response.status_code}")
                        elif response.status_code >= 400:
                            await response.aread()
                            raise RuntimeError(f"LLM HTTP {response.status_code}")
                        else:
                            async for line in response.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if not data or data == "[DONE]":
                                    continue
                                delta = parse_chunk(json.loads(data))
                                committed = True
                                yield delta
                            return
            except (httpx.ConnectError, httpx.ReadTimeout, ConnectionError) as exc:
                last_error = exc
                if committed:
                    raise
                if attempt >= self.max_retries:
                    raise
            if attempt < self.max_retries:
                await asyncio.sleep((2 ** attempt) * 0.25 + random.random() * 0.1)
        if last_error is not None:
            raise last_error
