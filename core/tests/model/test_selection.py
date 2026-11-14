"""Block 2 — selection order, a dangling override, and "no choice is not an error".

Order is a contract (D101): explicit session choice > `[llm].default_model` >
manifest priority. A dangling explicit choice is reported (`resolved: false`,
`reason: "model_unavailable"`) and never quietly replaced — the user asked for B
and must not be answered by A. With no explicit choice at all, an empty catalog
must not block: the provider raises its own error, as it always did.
"""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.llm.catalog import ModelResolver
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore

from .helpers import FakeConnection, ScriptedExecutor, chat_manifest, make_config, registry_of

MODEL_ID = "com.example.model/qwen2.5-vl-72b"
BUILTIN_ID = "builtin/gpt-4o-mini"


def _builtin_cfg(**overrides):
    """Module mode with a builtin fallback: the only shape that lists both.

    `[llm].provider = "openai_compatible"` means "builtin only" — the catalog
    then never scans the registry, so both layers can only coexist in module
    mode with `base_url` set.
    """
    return make_config(
        **{
            "llm.provider": "module",
            "llm.base_url": "http://127.0.0.1:9/v1",
            "llm.model": "gpt-4o-mini",
            "llm.default_model": "",
            "llm.fallback_to_builtin": True,
            **overrides,
        }
    )


def _log(tmp_path, cfg, registry, executor=None) -> SessionLog:
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=200))
    log.set_model_resolver(ModelResolver(cfg, registry))
    if executor is not None:
        log.set_executor(executor)
    return log


def test_session_choice_beats_the_global_default_which_beats_priority(tmp_path):
    registry = registry_of(chat_manifest(models=[{"name": "qwen2.5-vl-72b"}], priority=7))
    cfg = _builtin_cfg(**{"llm.default_model": BUILTIN_ID})
    resolver = ModelResolver(cfg, registry)

    catalog = resolver.catalog()
    assert {entry.id for entry in catalog.entries} == {MODEL_ID, BUILTIN_ID}

    # 1. explicit session choice
    chosen = resolver.resolve(MODEL_ID, catalog)
    assert (chosen.model_id, chosen.source, chosen.resolved) == (MODEL_ID, "session", True)

    # 2. operator default
    default = resolver.resolve(None, catalog)
    assert (default.model_id, default.source, default.resolved) == (BUILTIN_ID, "global", True)

    # 3. manifest priority: the module (7) outranks the builtin (0)
    no_default = ModelResolver(_builtin_cfg(), registry)
    by_priority = no_default.resolve(None, no_default.catalog())
    assert (by_priority.model_id, by_priority.source) == (MODEL_ID, "priority")


def test_a_dangling_explicit_choice_is_reported_not_replaced(tmp_path):
    registry = registry_of(chat_manifest(models=[{"name": "qwen2.5-vl-72b"}], priority=7))
    resolver = ModelResolver(_builtin_cfg(**{"llm.default_model": BUILTIN_ID}), registry)

    unknown = resolver.resolve("com.example.gone/model-x")
    assert unknown.resolved is False
    assert unknown.source == "session", "the source names the layer that was asked"
    assert unknown.model_id == "com.example.gone/model-x", "the request is echoed, not rewritten"
    assert unknown.effective is None
    assert unknown.reason == "model_unavailable"
    assert unknown.global_default == BUILTIN_ID, "the UI can offer the fallback without guessing"

    registry.mark_disconnected("com.example.model", "m-01")
    offline = resolver.resolve(MODEL_ID, resolver.catalog())
    assert (offline.resolved, offline.reason) == (False, "model_unavailable")

    # The same answer on the wire: `session/model.get` adds nothing of its own.
    log = _log(tmp_path, _builtin_cfg(**{"llm.default_model": BUILTIN_ID}), registry)
    rpc = SessionRPC(log)
    session = log.create_session()
    log.store.set_session_model(session.id, MODEL_ID)
    answer = asyncio.run(rpc.dispatch(FakeConnection(), "session/model.get",
                                      {"session_id": session.id}))
    assert answer["resolved"] is False
    assert answer["model_id"] == MODEL_ID
    assert answer["reason"] == "model_unavailable"
    assert answer["effective"] is None
    assert answer["global_default"] == BUILTIN_ID


def test_no_explicit_choice_does_not_block_a_turn(tmp_path):
    """An empty catalog is the provider's problem, not a new hard failure.

    `test_core_wiring.py` injects a Mock provider with no `base_url`; the
    dequeue hook must leave such a turn alone (`model_id` None), so the error
    the user sees is still the provider's `UNAVAILABLE`, not a model error.
    """
    cfg = make_config(**{"llm.provider": "module", "llm.base_url": "", "llm.default_model": ""})
    empty = registry_of()  # no module online
    executor = ScriptedExecutor(text="answered")
    log = _log(tmp_path, cfg, empty, executor)
    session = log.create_session()

    async def scenario():
        turn, _pos = await log.submit(session.id, [{"type": "text", "text": "hi"}], "m1")
        await log.wait_idle(session.id)
        return turn

    turn = asyncio.run(scenario())
    loaded = log.store.load(session.id).turns[0]
    assert loaded.state.value == "completed", f"{loaded.state} {loaded.error_code} {loaded.error_message}"
    assert loaded.model_id is None, "nothing was resolved, so nothing may be pinned"
    started = [e for e in log.store.read_since(session.id, 0) if e.type == "turn.started"]
    assert started and "model_id" not in started[0].payload, (
        "an unresolved turn must not advertise a model"
    )
    assert executor.turns and executor.turns[0].id == turn.id


def test_a_dangling_choice_fails_the_turn_loudly_and_never_runs_the_provider(tmp_path):
    """§5.2 row 1 / A11: a stored choice that vanished is an error, not a fallback."""
    cfg = make_config(**{"llm.provider": "module", "llm.base_url": ""})
    registry = registry_of(chat_manifest(models=[{"name": "qwen2.5-vl-72b"}]))
    executor = ScriptedExecutor()
    log = _log(tmp_path, cfg, registry, executor)
    session = log.create_session()
    log.set_session_model(session.id, "com.example.gone/model-x")

    async def scenario():
        await log.submit(session.id, [{"type": "text", "text": "hi"}], "m1")
        await log.wait_idle(session.id)

    asyncio.run(scenario())

    loaded = log.store.load(session.id).turns[0]
    assert loaded.state.value == "failed"
    assert loaded.error_code == "UNAVAILABLE"
    assert executor.turns == [], "the provider must not run behind a dangling choice"
    failed = [e for e in log.store.read_since(session.id, 0) if e.type == "turn.failed"]
    assert failed and failed[0].payload["data"] == {"reason": "model_unavailable"}
    assert failed[0].payload["usage"]["source"] == "none", (
        "a turn that never reached a model has nothing measured, and says so"
    )


def test_session_model_set_validates_and_clears(tmp_path):
    cfg = _builtin_cfg(**{"llm.default_model": BUILTIN_ID})
    registry = registry_of(chat_manifest(models=[{"name": "qwen2.5-vl-72b"}]))
    log = _log(tmp_path, cfg, registry)
    rpc = SessionRPC(log)
    session = log.create_session()
    conn = FakeConnection()

    async def scenario():
        first = await rpc.dispatch(conn, "session/model.set",
                                   {"session_id": session.id, "model_id": MODEL_ID})
        cleared = await rpc.dispatch(conn, "session/model.set",
                                     {"session_id": session.id, "model_id": None})
        malformed = await rpc.dispatch(conn, "session/model.set",
                                       {"session_id": session.id, "model_id": "no-slash"})
        unknown = await rpc.dispatch(conn, "session/model.set",
                                     {"session_id": session.id, "model_id": "com.example.model/nope"})
        return first, cleared, malformed, unknown

    first, cleared, malformed, unknown = asyncio.run(scenario())
    assert first == {"model_id": MODEL_ID, "source": "session", "effective_at": "next_turn"}
    assert cleared["model_id"] == BUILTIN_ID, "clearing falls back to the global default"
    assert cleared["source"] == "global", "the source is the one AFTER clearing"
    assert malformed.code == "INVALID_ARGUMENT"
    assert unknown.code == "NOT_FOUND" and unknown.data == {"reason": "unknown_model"}
    assert log.store.load(session.id).model_id is None, "a rejected write leaves no override"

    log.store.close_session(session.id)
    closed = asyncio.run(rpc.dispatch(conn, "session/model.set",
                                      {"session_id": session.id, "model_id": MODEL_ID}))
    assert closed.code == "READ_ONLY_SESSION"
    # get/list stay readable on a closed session; only the write is refused.
    readable = asyncio.run(rpc.dispatch(conn, "session/model.get", {"session_id": session.id}))
    assert readable["source"] == "global"


def test_model_set_unknown_session_is_not_found(tmp_path):
    log = _log(tmp_path, _builtin_cfg(), registry_of())
    rpc = SessionRPC(log)
    answer = asyncio.run(rpc.dispatch(FakeConnection(), "session/model.set",
                                      {"session_id": "s-missing", "model_id": None}))
    assert answer.code == "NOT_FOUND"


def test_attach_snapshot_lights_the_badge_without_a_second_call(tmp_path):
    """§5.1: a reconnecting device must not have to call session/model.get first."""
    cfg = _builtin_cfg(**{"llm.default_model": BUILTIN_ID})
    registry = registry_of(chat_manifest(models=[{"name": "qwen2.5-vl-72b"}]))
    log = _log(tmp_path, cfg, registry)
    rpc = SessionRPC(log)

    async def scenario():
        attached = await rpc.dispatch(FakeConnection(), "ui/attach",
                                      {"session_id": None, "last_seq": None})
        return attached

    attached = asyncio.run(scenario())
    block = attached["snapshot"]["session"]["model"]
    assert block == {"model_id": BUILTIN_ID, "source": "global"}


def test_resolver_never_infers_a_capability_from_the_model_name(tmp_path):
    """`qwen2.5-vl` is a vision model by convention — core must not assume it."""
    registry = registry_of(chat_manifest(models=[{"name": "qwen2.5-vl-72b"}]))
    catalog = ModelResolver(_builtin_cfg(), registry).catalog()
    entry = catalog.get(MODEL_ID).to_wire()
    assert entry["input_modalities"] is None, "an undeclared modality is unknown, name or not"
    assert entry["supports_tools"] is None
