"""OpenAI-compatible SSE adapter, exercised without a network.

The interesting failure is not "JSON parses": it is `tool_calls[].function.
arguments` arriving as fragments whose boundaries fall in the middle of a TCP
chunk *and* in the middle of the outer SSE JSON. `parse_chunk` is pure, but the
line reassembly belongs to httpx; only a fake transport proves the whole path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from vade.llm.base import Delta
from vade.llm.openai_compat import OpenAICompatProvider, parse_chunk


class _ChunkStream(httpx.AsyncByteStream):
    """Yields bytes exactly as split, so a `data:` line can straddle chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _sse(*objects: Any) -> bytes:
    lines = [f"data: {json.dumps(obj, ensure_ascii=False)}" for obj in objects]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


def _accumulate(deltas: list[Delta]) -> tuple[str, dict[int, dict[str, str]], dict[str, int], str | None]:
    """The loop's own merge rule: concatenate fragments by index."""
    text: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    usage: dict[str, int] = {}
    finish: str | None = None
    for delta in deltas:
        text.append(delta.text)
        for fragment in delta.tool_calls:
            slot = calls.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
            if fragment.id:
                slot["id"] = fragment.id
            if fragment.name:
                slot["name"] += fragment.name
            if fragment.arguments:
                slot["arguments"] += fragment.arguments
        if delta.usage:
            usage.update(delta.usage)
        if delta.finish_reason:
            finish = delta.finish_reason
    return "".join(text), calls, usage, finish


def test_sharded_sse_reassembles_tool_arguments_across_chunks():
    chunks_payload = _sse(
        {"choices": [{"delta": {"role": "assistant"}}]},
        # index 0: name intact, arguments split mid-JSON-string
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "fs__read_file", "arguments": '{"pa'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'th":"a.txt"}'}}]}}]},
        # index 1: a second call, also fragmented
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "call_2", "function": {"name": "fs__write", "arguments": '{"path":"b"'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": ',"n":2}'}}]}}]},
        {"choices": [{"delta": {"content": "hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3,
                                  "total_tokens": 14, "extra": "not-an-int"}},
    )
    # Deliberately ugly boundaries: every 37 bytes cuts both lines and JSON.
    shards = [chunks_payload[i:i + 37] for i in range(0, len(chunks_payload), 37)]
    assert len(shards) > 5

    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_ChunkStream(shards))

    async def scenario() -> None:
        provider = OpenAICompatProvider(base_url="http://llm.test/v1/", model="m1",
                                        api_key_env="VADE_TEST_NO_KEY",
                                        transport=httpx.MockTransport(handler))
        tools = [{"type": "function", "function": {"name": "fs__read_file"}}]
        deltas = [d async for d in provider.chat([{"role": "user", "content": "hi"}], tools=tools)]

        text, calls, usage, finish = _accumulate(deltas)
        assert text == "hello"
        assert calls[0] == {"id": "call_1", "name": "fs__read_file", "arguments": '{"path":"a.txt"}'}
        assert json.loads(calls[0]["arguments"]) == {"path": "a.txt"}
        assert calls[1] == {"id": "call_2", "name": "fs__write", "arguments": '{"path":"b","n":2}'}
        assert json.loads(calls[1]["arguments"]) == {"path": "b", "n": 2}
        assert usage == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
        assert finish == "tool_calls"

        assert seen["url"] == "http://llm.test/v1/chat/completions"
        assert seen["body"]["stream"] is True
        assert seen["body"]["model"] == "m1"
        assert seen["body"]["tools"] == tools
        assert seen["auth"] is None, "an unset env var must not send an empty Bearer"

    asyncio.run(scenario())


def test_parse_chunk_keeps_index_and_ignores_non_data_lines():
    delta = parse_chunk({"choices": [{"delta": {"tool_calls": [
        {"index": 3, "function": {"arguments": "{}"}}]}}]})
    assert delta.tool_calls[0].index == 3
    assert delta.tool_calls[0].id is None
    # A comment/blank line is skipped by the provider, never parsed as JSON.
    assert parse_chunk({"choices": [], "usage": {"total_tokens": 1, "x": "y"}}).usage == {"total_tokens": 1}
