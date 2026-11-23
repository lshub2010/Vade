"""`GET /models` discovery: ids only, auth header only when the env key exists."""

from __future__ import annotations

import asyncio
import os

import httpx

from vade.llm.openai_compat import OpenAICompatProvider


def test_list_models_returns_ids_in_order_and_deduplicates(monkeypatch):
    monkeypatch.setenv("VADE_TEST_KEY", "secret-value")
    seen: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "m-b"},
            {"id": "m-a"},
            {"id": "m-b"},           # duplicate
            {"id": "  m-c  "},
            {"id": ""},              # empty is not a model
            {"no_id": True},         # shape noise
            "not-an-object",
        ]})

    async def scenario() -> list[str]:
        provider = OpenAICompatProvider(
            base_url="http://llm.test/v1/", model="m1",
            api_key_env="VADE_TEST_KEY",
            transport=httpx.MockTransport(handler),
        )
        return await provider.list_models()

    ids = asyncio.run(scenario())
    assert ids == ["m-b", "m-a", "m-c"]
    assert seen["url"] == "http://llm.test/v1/models"
    assert seen["auth"] == "Bearer secret-value"


def test_list_models_omits_authorization_without_a_key(monkeypatch):
    monkeypatch.delenv("VADE_TEST_NO_KEY", raising=False)
    seen: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": []})

    async def scenario() -> list[str]:
        provider = OpenAICompatProvider(
            base_url="http://llm.test/v1", model="m1",
            api_key_env="VADE_TEST_NO_KEY",
            transport=httpx.MockTransport(handler),
        )
        return await provider.list_models()

    assert asyncio.run(scenario()) == []
    assert seen["auth"] is None


def test_list_models_raises_on_http_error_instead_of_returning_empty():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    async def scenario() -> None:
        provider = OpenAICompatProvider(
            base_url="http://llm.test/v1", model="m1",
            api_key_env="VADE_TEST_NO_KEY",
            transport=httpx.MockTransport(handler),
        )
        await provider.list_models()

    try:
        asyncio.run(scenario())
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 503
    else:
        raise AssertionError("a 503 must not be reported as an empty model list")
