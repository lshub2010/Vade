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
import pytest

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


def test_a_streamed_request_asks_for_the_usage_chunk():
    """`stream_options.include_usage` is what makes a provider report at all."""
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_ChunkStream([_sse({"choices": [{"delta": {"content": "ok"}}]})]))

    async def scenario() -> list[Delta]:
        provider = OpenAICompatProvider(base_url="http://llm.test/v1", model="m1",
                                        api_key_env="VADE_TEST_NO_KEY",
                                        transport=httpx.MockTransport(handler))
        return [d async for d in provider.chat([{"role": "user", "content": "hi"}], stream=True)]

    deltas = asyncio.run(scenario())
    assert "".join(d.text for d in deltas) == "ok"
    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}, (
        "without this a chat-completions stream carries no usage, so every turn "
        "is recorded as source=none and the budget can never hard-block"
    )


def test_a_non_streamed_request_does_not_send_stream_options():
    """`stream_options` on a non-stream request is a 400 on strict servers."""
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_ChunkStream([_sse({"choices": [{"delta": {"content": "ok"}}]})]))

    async def scenario() -> None:
        provider = OpenAICompatProvider(base_url="http://llm.test/v1", model="m1",
                                        api_key_env="VADE_TEST_NO_KEY",
                                        transport=httpx.MockTransport(handler))
        [d async for d in provider.chat([{"role": "user", "content": "hi"}], stream=False)]

    asyncio.run(scenario())
    assert seen["body"]["stream"] is False
    assert "stream_options" not in seen["body"]


def test_the_trailing_usage_only_chunk_survives_the_whole_stream_path():
    """`choices: []` + `usage` is a measurement, not an empty end-of-stream.

    The chunk arrives after `finish_reason`; a consumer that stops at the finish
    chunk (or drops a delta with no text/tool calls) loses the only real number
    in the response.
    """
    payload = _sse(
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4,
                                  "total_tokens": 13,
                                  "completion_tokens_details": {"reasoning_tokens": 2}}},
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_ChunkStream([payload]))

    async def scenario() -> list[Delta]:
        provider = OpenAICompatProvider(base_url="http://llm.test/v1", model="m1",
                                        api_key_env="VADE_TEST_NO_KEY",
                                        transport=httpx.MockTransport(handler))
        return [d async for d in provider.chat([{"role": "user", "content": "hi"}])]

    deltas = asyncio.run(scenario())
    text, _calls, usage, finish = _accumulate(deltas)
    assert text == "hi"
    assert finish == "stop"
    assert usage == {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13,
                     "reasoning_tokens": 2}
    measurement = deltas[-1]
    assert measurement.text == "" and measurement.tool_calls == (), (
        "the usage delta must not invent content"
    )


class _ThenError(httpx.AsyncByteStream):
    """Deliver some bytes, then fail the way a dropped connection does."""

    def __init__(self, chunks: list[bytes], exc: Exception) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        raise self._exc


def test_a_mid_stream_failure_after_a_delta_is_not_retried():
    """The `HELLOHELLOHELLO` case: a retry must not replay consumed output."""
    calls = {"n": 0}
    partial = b'data: {"choices": [{"delta": {"content": "HE"}}]}\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_ThenError([partial], httpx.ReadTimeout("dropped")))
        # If the adapter wrongly retried, this response would make it visible.
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_ChunkStream([_sse({"choices": [{"delta": {"content": "HELLO"}}]})]))

    async def scenario() -> list[Delta]:
        provider = OpenAICompatProvider(base_url="http://llm.test/v1", model="m1",
                                        api_key_env="VADE_TEST_NO_KEY",
                                        transport=httpx.MockTransport(handler))
        seen: list[Delta] = []
        with pytest.raises(httpx.ReadTimeout):
            async for delta in provider.chat([{"role": "user", "content": "hi"}]):
                seen.append(delta)
        return seen

    deltas = asyncio.run(scenario())

    assert "".join(d.text for d in deltas) == "HE"
    assert calls["n"] == 1, "a committed stream must never be re-requested"


def test_a_failure_before_any_delta_is_still_retried():
    calls = {"n": 0}
    full = _sse({"choices": [{"delta": {"content": "ok"}}]})

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, content=b"busy")
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_ChunkStream([full]))

    async def scenario() -> str:
        provider = OpenAICompatProvider(base_url="http://llm.test/v1", model="m1",
                                        api_key_env="VADE_TEST_NO_KEY",
                                        transport=httpx.MockTransport(handler))
        return "".join([d.text async for d in provider.chat([{"role": "user", "content": "hi"}])])

    assert asyncio.run(scenario()) == "ok"
    assert calls["n"] == 2
