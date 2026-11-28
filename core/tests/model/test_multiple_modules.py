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

import pytest

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


def test_the_provider_channel_is_not_a_tool_the_model_can_call():
    """`chat` reaches core through the router, never through the tool table.

    Regression: `build_snapshot` published every manifest tool, so a model
    module's `chat` appeared as `alpha__chat` in the prompt — the model could
    call its own provider (FR-2.7 forbids module/core reverse calls), and with
    several model modules online it got one such callable per module.
    """
    from vade.loop.tools import build_snapshot

    registry = _two_modules()
    snapshot = build_snapshot(registry)
    assert snapshot.names() == ()
    # ...while the router still reaches each module's namespaced chat.
    assert [record.manifest.module_id for record in registry.snapshot()] != []
    assert all(
        record.manifest.tool("chat") is not None for record in registry.snapshot()
    )


def test_with_core_never_adds_the_same_llm_name_twice():
    """A duplicated core entry used to reach the provider as two schemas."""
    from vade.loop.tools import ToolEntry, ToolSnapshot

    def entry(name: str) -> ToolEntry:
        return ToolEntry(
            llm_name=name, tool_id=f"core/{name}", module_id="core", description="",
            input_schema={}, risk="low", timeout_ms=1000, idempotent=True,
        )

    merged = ToolSnapshot().with_core([entry("kb_search"), entry("kb_search")])
    assert merged.names() == ("kb_search",)


def test_a_llm_name_collision_across_modules_is_rejected_not_shadowed():
    """The prefix map should make this impossible; if it ever does not, say so."""
    from types import SimpleNamespace

    from vade.errors import VadeError
    from vade.loop.tools import build_snapshot

    def record(module_id: str, tools: list[str]):
        manifest = SimpleNamespace(
            tools=[
                SimpleNamespace(name=name, description=name, input_schema={}, risk="low",
                                timeout_ms=1000, idempotent=True)
                for name in tools
            ],
            injects=[],
        )
        return SimpleNamespace(
            module_id=module_id, instance_id="i-1", connection_id="c-1",
            manifest=manifest, enabled=True, health=SimpleNamespace(value="online"),
            node_id=f"{module_id}#i-1",
        )

    class Registry:
        def snapshot(self):
            # `a.b` and `a-b` normalise to the same prefix (`a_b`), so the map
            # gives the second one `a_b2`; the guard must not fire here.
            return (record("com.example.a.b", ["go"]), record("com.example.a-b", ["go"]))

        def bindings_for(self, name):
            return ()

    names = build_snapshot(Registry()).names()
    # `com.example.a.b` -> prefix `b`, `com.example.a-b` -> prefix `a_b`; the
    # point is that the two differ, not which one won.
    assert names == ("a_b__go", "b__go")

    class Colliding(Registry):
        def snapshot(self):
            return (record("com.example.dup", ["x", "y"]),)

    # Same module, two tools: prefixes are per module, so no collision either.
    assert build_snapshot(Colliding()).names() == ("dup__x", "dup__y")

    class Broken(Registry):
        def snapshot(self):
            return (record("com.example.one", ["same"]), record("com.example.one", ["same"]))

    # Two instances of one module are one entry per tool, not a collision.
    assert build_snapshot(Broken()).names() == ("one__same",)

    from vade.loop.tools import ToolNameMap

    class Duplicate(Registry):
        def snapshot(self):
            return (record("com.example.p", ["t"]), record("com.example.q", ["t"]))

    class FixedMap(ToolNameMap):
        pass

    # Force the pathological case: a name map that returns one name for two ids.
    original = ToolNameMap.build.__func__

    def broken_build(cls, pairs):
        pairs = list(pairs)
        mapping = original(cls, pairs)
        forward = {tool_id: "same__name" for tool_id in mapping.forward}
        return ToolNameMap(
            forward=type(mapping.forward)(forward),
            backward=type(mapping.backward)({"same__name": next(iter(forward))}),
            prefixes=mapping.prefixes,
        )

    ToolNameMap.build = classmethod(broken_build)
    try:
        with pytest.raises(VadeError) as caught:
            build_snapshot(Duplicate())
        assert caught.value.code == "MANIFEST_INVALID"
        assert "same__name" in caught.value.message
    finally:
        ToolNameMap.build = classmethod(original)
