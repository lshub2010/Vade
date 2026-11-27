"""FR-6.3 — several model modules online at once (regression, not preference).

Every M6 module declares the single discovery tool `chat`. The registry used to
treat that name as globally unique and rejected the second model module with
`TOOL_CONFLICT`, so FR-6.3's "multiple model modules" could never hold. Routing
is always `<module_id>/chat`, so the fixtures here go through the real
`Registry`/catalog and assert the namespaced target the provider actually calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

from vade.llm.catalog import build_catalog
from vade.llm.module import ModuleLLMProvider
from vade.router.dispatch import ToolCallResult

from .helpers import chat_manifest, make_config, registry_of

ALPHA = "com.example.alpha"
BETA = "com.example.beta"


def _module_cfg(**overrides):
    return make_config(
        **{
            "llm.provider": "module",
            "llm.base_url": "",
            "llm.fallback_to_builtin": False,
            **overrides,
        }
    )


def _two_modules():
    """Alpha (priority 2) and beta (priority 9), each with one served model."""
    return registry_of(
        chat_manifest(ALPHA, "a-1", models=[{"name": "alpha-large"}], priority=2),
        chat_manifest(BETA, "b-1", models=[{"name": "beta-fast"}], priority=9),
    )


class RecordingRouter:
    """Stand-in for `Router` that records the namespaced tool id it was given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, tool_id, args, *, session_id, call_id, deadline_ms):
        self.calls.append({"tool_id": tool_id, "args": dict(args), "session_id": session_id,
                           "call_id": call_id, "deadline_ms": deadline_ms})
        return ToolCallResult(ok=True, tool_id=tool_id, result={"message": {"content": "ok"}})


def _collect(provider: ModuleLLMProvider) -> list[str]:
    async def scenario() -> list[str]:
        return [delta.text async for delta in provider.chat([{"role": "user", "content": "hi"}])]

    return asyncio.run(scenario())


def test_two_model_modules_coexist_and_both_appear_in_the_catalog():
    registry = _two_modules()

    bindings = registry.bindings_for("chat")
    assert {b.tool_id for b in bindings} == {f"{ALPHA}/chat", f"{BETA}/chat"}

    catalog = build_catalog(_module_cfg(), registry)
    assert {entry.id for entry in catalog.entries} == {
        f"{ALPHA}/alpha-large",
        f"{BETA}/beta-fast",
    }
    assert catalog.default_entry().id == f"{BETA}/beta-fast", (
        "FR-6.3: the highest manifest priority is the default, across modules"
    )


def test_provider_selects_the_higher_priority_module_and_calls_its_namespaced_chat():
    registry = _two_modules()
    router = RecordingRouter()
    provider = ModuleLLMProvider(router, registry)

    assert provider.select().module_id == BETA
    assert _collect(provider) == ["ok"]
    assert router.calls[0]["tool_id"] == f"{BETA}/chat", (
        "a model call must never reach the bare `chat` name or a neighbour module"
    )
    assert router.calls[0]["session_id"] == ""


def test_llm_model_module_still_pins_selection_and_routing():
    registry = _two_modules()
    cfg = _module_cfg(**{"llm.model_module": ALPHA})
    router = RecordingRouter()
    provider = ModuleLLMProvider(router, registry, model_module=cfg.get("llm.model_module"))

    catalog = build_catalog(cfg, registry)
    assert [entry.id for entry in catalog.entries] == [f"{ALPHA}/alpha-large"], (
        "the explicit pin hides the non-pinned module from the picker"
    )

    assert provider.select().module_id == ALPHA
    assert _collect(provider) == ["ok"]
    assert router.calls[0]["tool_id"] == f"{ALPHA}/chat", (
        "priority must not override an explicit llm.model_module pin"
    )
