"""Block 5 — `session/usage.get` reconciled with the audit `llm.call` rows (D102).

One dataset, two views: `chat` comes from `turns.usage`, `aux` from the audit
trail (the only place a compaction/memory/evolve call exists). The two must add
up to the same number the budget judges, and the per-turn numbers must equal the
sum of that turn's `llm.call` rows — otherwise the UI, the budget and the audit
each tell a different story about the same tokens.
"""

from __future__ import annotations

import asyncio
import time

from vade.llm.base import Delta
from vade.llm.catalog import ModelResolver
from vade.llm.mock import MockLLMProvider
from vade.llm.usage import UsageAccounting
from vade.loop.engine import LoopEngine
from vade.loop.tools import build_snapshot
from vade.registry.manifest import validate_manifest
from vade.router.dispatch import Router
from vade.session.manager import SessionLog
from vade.session.models import Turn, TurnState
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore

from .helpers import (FakeAuditReader, FakeConnection, chat_manifest, llm_row,
                      make_config, usage_record)

MODEL_ID = "com.example.model/qwen2.5-vl-72b"


class IdleModule:
    async def call(self, target, method, params, deadline_ms):  # pragma: no cover - no tools
        return {"ok": True, "result": {}}

    async def cancel(self, target, call_id):  # pragma: no cover - no tools
        return None


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event):
        self.events.append(dict(event))


def _accounting(cfg, store, reader=None, *, clock=None) -> UsageAccounting:
    kwargs = {"clock": clock} if clock is not None else {}
    return UsageAccounting(config=cfg, store=store, audit_reader=reader, **kwargs)


def _module_cfg(**overrides):
    return make_config(**{"llm.provider": "module", "llm.base_url": "http://127.0.0.1:9/v1",
                          "llm.model": "gpt-4o-mini", "llm.default_model": "", **overrides})


def test_each_turn_total_equals_the_sum_of_its_llm_call_rows(tmp_path):
    """The engine writes one `llm.call` per model call; the turn sums them.

    Runs the real `LoopEngine` against a real `Registry` and a mock provider, so
    `_usage_record` and `_audit_llm_call` are both exercised. If one of them
    drifts, the two numbers below stop agreeing.
    """
    from vade.registry.table import Registry

    registry = Registry()
    registry.register(validate_manifest(chat_manifest(models=[{"name": "qwen2.5-vl-72b"}])),
                      fingerprint="fp-model", connection_id="conn-model")
    audit = RecordingAudit()
    llm = MockLLMProvider([[
        Delta(text="hello", usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
                                   "cached_tokens": 2}),
    ]])
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=200))
    engine = LoopEngine(Router(IdleModule(), registry), llm, lambda: build_snapshot(registry),
                        log.emit, audit=audit)
    log.set_executor(engine.run_turn)
    log.set_model_resolver(ModelResolver(_module_cfg(), registry))
    session = log.create_session()

    async def scenario():
        await log.submit(session.id, [{"type": "text", "text": "hi"}], "m1")
        await log.wait_idle(session.id)

    asyncio.run(scenario())

    turn = log.store.load(session.id).turns[0]
    assert turn.state is TurnState.COMPLETED
    assert turn.model_id == MODEL_ID, "the resolved model is pinned on the turn"
    assert turn.usage["total_tokens"] == 10
    assert turn.usage["source"] == "provider"
    assert turn.usage["cached_tokens"] == 2, "a provider-reported cached count survives the record"
    assert turn.usage["steps"] == 1

    rows = [e for e in audit.events if e["kind"] == "llm.call"]
    assert len(rows) == 1
    assert rows[0]["turn_id"] == turn.id
    assert rows[0]["extra"]["total_tokens"] == turn.usage["total_tokens"], (
        "the audit row and the turn must report the same tokens"
    )
    assert rows[0]["extra"]["source"] == "provider"
    assert rows[0]["extra"]["purpose"] == "chat"

    # Same dataset through the reader: `chat` and the audit agree exactly. The
    # writer (not the engine) stamps `ts`, so the fake rows get one here.
    stamped = [{**row, "ts": time.time()} for row in rows]
    accounting = _accounting(_module_cfg(), log.store, FakeAuditReader(stamped, page=1))
    report = accounting.report(log.store.load(session.id))
    assert report["chat"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
                              "turns": 1, "partial_turns": 0}
    assert report["aux"] == {"total_tokens": 0, "calls": 0}, "a chat call is not auxiliary"
    assert report["total_tokens"] == 10
    assert report["by_model"] == [{"model_id": MODEL_ID, "total_tokens": 10, "turns": 1}]
    assert report["day"]["core_total_tokens"] == 10
    assert report["day"]["session_tokens"] == 10


def test_aux_calls_come_from_the_audit_trail_and_page_past_the_reader_cap(tmp_path):
    """Compaction/memory calls exist only in the audit trail; none may be truncated."""
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=50))
    session = log.create_session()
    log.store.update_turn(session.id, Turn(id="t1", session_id=session.id, state=TurnState.COMPLETED,
                                         user_text="a", usage=usage_record(40, model_id=MODEL_ID)))
    log.store.update_turn(session.id, Turn(id="t2", session_id=session.id, state=TurnState.COMPLETED,
                                         user_text="b", usage=usage_record(60, model_id="builtin/gpt-4o-mini")))

    rows = [
        llm_row(session.id, tokens=40, turn_id="t1", model_id=MODEL_ID),
        llm_row(session.id, tokens=60, turn_id="t2"),
        llm_row(session.id, tokens=30, purpose="compaction"),
        llm_row(session.id, tokens=20, purpose="memory"),
        llm_row(session.id, tokens=10, purpose="evolve"),
        llm_row("other-session", tokens=500, purpose="compaction"),
    ]
    reader = FakeAuditReader(rows, page=2)  # 3 pages: the cursor walk must not stop at 2
    accounting = _accounting(make_config(), log.store, reader)

    report = accounting.report(log.store.load(session.id))
    assert report["chat"]["total_tokens"] == 100
    assert report["chat"]["turns"] == 2
    assert report["aux"] == {"total_tokens": 60, "calls": 3}, "only this session's aux calls"
    assert report["total_tokens"] == 160
    assert report["by_model"] == [
        {"model_id": "builtin/gpt-4o-mini", "total_tokens": 60, "turns": 1},
        {"model_id": MODEL_ID, "total_tokens": 40, "turns": 1},
    ], "by_model is ordered by tokens, then id"
    assert any(query["cursor"] is not None for query in reader.queries), (
        "the reader must be walked with next_cursor, not read as a single page"
    )

    chat_only = _accounting(make_config(**{"usage.include_aux": False}), log.store, reader)
    assert chat_only.report(log.store.load(session.id))["budget"]["used"] == 100, (
        "include_aux=false judges chat only, while still reporting aux"
    )
    assert accounting.report(log.store.load(session.id))["budget"]["used"] == 160


def test_failed_and_interrupted_turns_are_accounted_and_cancelled_is_not(tmp_path):
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=50))
    session = log.create_session()
    for turn_id, state, usage in (
        ("t1", TurnState.COMPLETED, usage_record(10)),
        ("t2", TurnState.FAILED, usage_record(20, source="none")),
        ("t3", TurnState.INTERRUPTED, usage_record(0, source="none")),
        ("t4", TurnState.CANCELLED, usage_record(999)),  # never reached the model
    ):
        log.store.update_turn(session.id, Turn(id=turn_id, session_id=session.id, state=state,
                                             user_text=turn_id, usage=usage))
    report = _accounting(make_config(), log.store).report(log.store.load(session.id))
    assert report["chat"]["turns"] == 3, "cancelled is not a measured turn"
    assert report["chat"]["total_tokens"] == 10
    assert report["chat"]["partial_turns"] == 2, "the unmeasured failed/interrupted turns are flagged"


def test_the_day_aggregate_uses_the_utc_boundary_and_hides_other_sessions(tmp_path):
    fixed = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(fixed))
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=50))
    session = log.create_session()
    log.store.update_turn(session.id, Turn(id="t1", session_id=session.id, state=TurnState.COMPLETED,
                                         user_text="a", usage=usage_record(10)))
    rows = [
        llm_row(session.id, tokens=10, turn_id="t1"),
        llm_row("other-session", tokens=90, purpose="compaction", ts=fixed),
        llm_row("other-session", tokens=555, purpose="compaction", ts=fixed - 2 * 86_400),
    ]
    report = _accounting(make_config(), log.store, FakeAuditReader(rows), clock=lambda: fixed).report(
        log.store.load(session.id)
    )
    assert report["day"]["day"] == day
    assert report["day"]["core_total_tokens"] == 100, "today only, other sessions aggregated only"
    assert report["day"]["session_tokens"] == 10, "this session's share of today"
    assert report["day"]["state"] == "off", "no [usage].daily_tokens configured"


def test_usage_get_over_rpc_is_the_report_verbatim(tmp_path):
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=50))
    cfg = make_config(**{"usage.session_tokens": 100})
    log.set_usage_accounting(UsageAccounting(config=cfg, store=log.store))
    session = log.create_session()
    log.store.update_turn(session.id, Turn(id="t1", session_id=session.id, state=TurnState.COMPLETED,
                                         user_text="a", usage=usage_record(50)))
    rpc = SessionRPC(log)

    answer = asyncio.run(rpc.dispatch(FakeConnection(), "session/usage.get", {"session_id": session.id}))
    assert answer == log.usage.report(log.store.load(session.id))
    assert answer["session_id"] == session.id
    assert answer["budget"] == {"scope": "session", "limit": 100, "used": 50, "ratio": 0.5,
                                "state": "ok", "enforced": False}

    missing = asyncio.run(rpc.dispatch(FakeConnection(), "session/usage.get", {"session_id": "s-missing"}))
    assert missing.code == "NOT_FOUND"

    bare_log = SessionLog(SQLiteStore(tmp_path / "bare.db"), replay=ReplayBuffer(maxlen=10))
    bare_session = bare_log.create_session()
    unwired = asyncio.run(SessionRPC(bare_log).dispatch(
        FakeConnection(), "session/usage.get", {"session_id": bare_session.id}))
    assert unwired.code == "UNAVAILABLE", "a core without accounting says so, it does not report 0"


def test_flatten_usage_lifts_nested_details_without_inflating_the_totals():
    """§6.1: `cached_tokens`/`reasoning_tokens` ride along, the totals stay additive.

    The audit sums `total_tokens`; if a nested `*_details` object were added to
    it the ledger would silently double-count, so only flat integers travel.
    """
    from vade.llm.base import flatten_usage

    flat = flatten_usage({
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 3},
        "notes": "x",
        "nested": {"a": 1},
    })
    assert flat == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 4,
        "reasoning_tokens": 3,
    }
    assert sum(flat.values()) > 15, "the two details are informational, not part of the total"
