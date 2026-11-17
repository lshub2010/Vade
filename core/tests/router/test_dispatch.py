"""Router: instance selection, ctx injection, deadline, error mapping, breaker."""

from __future__ import annotations

import asyncio
import time

import pytest

from vade.errors import VadeError
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry
from vade.router.breaker import CircuitBreaker
from vade.router.dispatch import Router, ToolCallResult, validate_arguments
from tests.session.helpers import make_manifest, wait_until


class FakeCaller:
    def __init__(self, behavior=None):
        self.behavior = behavior
        self.calls: list[tuple] = []
        self.cancelled: list[str] = []

    async def call(self, target, method, params, deadline_ms):
        self.calls.append((target, params))
        if self.behavior is not None:
            return await self.behavior(target, params)
        return {"ok": True, "result": {"instance": target.instance_id}}

    async def cancel(self, target, call_id):
        self.cancelled.append(call_id)


def _registry(*manifests) -> Registry:
    registry = Registry()
    for i, raw in enumerate(manifests):
        registry.register(validate_manifest(raw), fingerprint=f"fp{i}", connection_id=f"conn{i}")
    return registry


def test_exclusive_offline_instance_is_unavailable():
    registry = _registry(make_manifest(concurrency="exclusive"))
    registry.mark_disconnected("com.example.fs", "fs-01")
    result = asyncio.run(Router(FakeCaller(), registry).call(
        "com.example.fs/read_file", {"path": "a"}, session_id="s1"))
    assert result.ok is False and result.code == "UNAVAILABLE"


def test_pooled_picks_least_inflight_instance():
    async def scenario():
        manifests = [make_manifest(instance_id=f"fs-0{i}", max_concurrency=4) for i in (1, 2, 3)]
        registry = _registry(*manifests)
        started: list[str] = []
        release = asyncio.Event()

        async def behavior(target, params):
            started.append(target.instance_id)
            await release.wait()
            return {"ok": True, "result": {"by": target.instance_id}}

        router = Router(FakeCaller(behavior), registry)
        tasks = [asyncio.create_task(router.call("com.example.fs/read_file", {"path": "a"},
                                                 session_id="s1")) for _ in range(2)]
        assert await wait_until(lambda: len(started) == 2, 1)
        assert len(set(started)) == 2, "two concurrent calls must not share one instance"
        release.set()
        results = await asyncio.gather(*tasks)
        assert all(r.ok for r in results)
        assert router.inflight("com.example.fs", "fs-01") == 0

    asyncio.run(scenario())


def test_ctx_session_id_injected_only_when_declared():
    async def scenario():
        registry = _registry(make_manifest(injects=("session_id",)))
        caller = FakeCaller()
        await Router(caller, registry).call("com.example.fs/read_file", {"path": "a"}, session_id="sess-1")
        assert caller.calls[0][1]["ctx"] == {"session_id": "sess-1"}

        registry2 = _registry(make_manifest(module_id="com.other.tool", injects=()))
        caller2 = FakeCaller()
        await Router(caller2, registry2).call("com.other.tool/read_file", {"path": "a"}, session_id="sess-1")
        assert "ctx" not in caller2.calls[0][1], "not declared -> no ctx, not an empty one"

    asyncio.run(scenario())


def test_model_supplied_session_id_is_overwritten():
    async def scenario():
        schema = {"type": "object", "properties": {"path": {"type": "string"},
                                                   "session_id": {"type": "string"}}}
        registry = _registry(make_manifest(schema=schema, injects=("session_id",)))
        caller = FakeCaller()
        await Router(caller, registry).call(
            "com.example.fs/read_file", {"path": "a", "session_id": "forged"}, session_id="real")
        assert caller.calls[0][1]["args"]["session_id"] == "real"

    asyncio.run(scenario())


def test_additional_properties_false_rejects_extra_session_id():
    async def scenario():
        schema = {"type": "object", "properties": {"path": {"type": "string"}},
                  "additionalProperties": False}
        registry = _registry(make_manifest(schema=schema))
        caller = FakeCaller()
        result = await Router(caller, registry).call(
            "com.example.fs/read_file", {"path": "a", "session_id": "forged"}, session_id="real")
        assert result.code == "INVALID_ARGUMENT"
        assert caller.calls == []

    asyncio.run(scenario())


def test_timeout_maps_to_timeout_and_sends_cancel():
    async def scenario():
        finished = {"value": False}

        async def slow(target, params):
            await asyncio.sleep(0.5)
            finished["value"] = True
            return {"ok": True, "result": {}}

        registry = _registry(make_manifest(timeout_ms=100))
        caller = FakeCaller(slow)
        router = Router(caller, registry)
        result = await router.call("com.example.fs/read_file", {"path": "a"}, session_id="s1")
        assert result.code == "TIMEOUT" and result.retriable is True
        assert finished["value"] is False, "router must return before the module finishes"
        await asyncio.sleep(0.02)
        assert caller.cancelled, "core must send tools/cancel without waiting"

    asyncio.run(scenario())


def test_error_codes_are_mapped_and_unknown_codes_become_internal():
    async def scenario():
        registry = _registry(make_manifest())

        async def deny(target, params):
            return {"ok": False, "error": {"code": "PERMISSION_DENIED", "message": "no", "retriable": False}}

        result = await Router(FakeCaller(deny), registry).call(
            "com.example.fs/read_file", {"path": "a"}, session_id="s1")
        assert (result.code, result.message, result.retriable) == ("PERMISSION_DENIED", "no", False)

        async def bogus(target, params):
            return {"ok": False, "error": {"code": "MADE_UP", "message": "x"}}

        result = await Router(FakeCaller(bogus), registry).call(
            "com.example.fs/read_file", {"path": "a"}, session_id="s1")
        assert result.code == "INTERNAL"
        assert "MADE_UP" in result.message

    asyncio.run(scenario())


def test_breaker_opens_after_five_timeouts_and_half_opens_after_30s():
    async def scenario():
        now = [100.0]
        breaker = CircuitBreaker(clock=lambda: now[0])

        async def timeout(target, params):
            raise asyncio.TimeoutError

        registry = _registry(make_manifest(timeout_ms=100))
        caller = FakeCaller(timeout)
        router = Router(caller, registry, breaker=breaker)
        for _ in range(5):
            assert (await router.call("com.example.fs/read_file", {"path": "a"},
                                      session_id="s1")).code == "TIMEOUT"
        assert breaker.state("com.example.fs", "fs-01") == "open"

        attempts = len(caller.calls)
        fast = await router.call("com.example.fs/read_file", {"path": "a"}, session_id="s1")
        assert fast.code == "UNAVAILABLE" and "circuit-open" in fast.message
        assert len(caller.calls) == attempts, "open breaker must not reach the module"

        now[0] += 31
        assert breaker.state("com.example.fs", "fs-01") == "half_open"
        async def ok(target, params):
            return {"ok": True, "result": {"probe": True}}
        caller.behavior = ok
        probe = await router.call("com.example.fs/read_file", {"path": "a"}, session_id="s1")
        assert probe.ok is True
        assert breaker.state("com.example.fs", "fs-01") == "closed"

    asyncio.run(scenario())


def test_breaker_reset_clears_an_open_window_for_a_reconnected_module():
    """A module that re-registers after a restart gets a fresh link.

    Reproduced for real by `sdk/python/tests/e2e_real_core.py`: during the
    restart the router records five UNAVAILABLEs, the breaker opens for 30s,
    and the module's first post-reconnect call is refused with
    "all instances ... are circuit-open" even though it is registered again.
    `App.register_module` now calls `reset`.
    """
    async def scenario():
        breaker = CircuitBreaker()
        for _ in range(5):
            breaker.record_failure("com.example.echo", "main")
        assert breaker.state("com.example.echo", "main") == "open"
        assert breaker.allow("com.example.echo", "main") is False

        breaker.reset("com.example.echo", "main")

        assert breaker.state("com.example.echo", "main") == "closed"
        assert breaker.allow("com.example.echo", "main") is True
        # 只清这一个实例，不碰同模块的其它实例。
        breaker.record_failure("com.example.echo", "other")
        for _ in range(4):
            breaker.record_failure("com.example.echo", "other")
        assert breaker.state("com.example.echo", "other") == "open"
        assert breaker.state("com.example.echo", "main") == "closed"

    asyncio.run(scenario())


def test_oversized_arguments_are_rejected_before_the_frame():
    async def scenario():
        registry = _registry(make_manifest())
        caller = FakeCaller()
        result = await Router(caller, registry).call(
            "com.example.fs/read_file", {"path": "x" * 70_000}, session_id="s1")
        assert result.code == "PAYLOAD_TOO_LARGE"
        assert caller.calls == []

    asyncio.run(scenario())


def test_validate_arguments_required_and_types():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert validate_arguments(schema, {}) == "missing required argument 'n'"
    assert validate_arguments(schema, {"n": "x"}) == "argument 'n' must be integer"
    assert validate_arguments(schema, {"n": 1}) is None


def test_clear_cancelled_forgets_only_that_turn():
    router = Router(FakeCaller(), _registry(make_manifest()))
    router.cancel_turn("turn-1")
    router.cancel_turn("turn-2")
    assert router.was_cancelled("turn-1") is True

    router.clear_cancelled("turn-1")
    assert router.was_cancelled("turn-1") is False
    assert router.was_cancelled("turn-2") is True  # other turns are untouched

    router.clear_cancelled("never-seen")  # idempotent: no key error, no flag
    assert router.was_cancelled("never-seen") is False
