"""Block 3/4/6 — the budget gate on `ui/submit` (§6.4, D102).

Three rules that are easy to get subtly wrong:

* `BUDGET_EXCEEDED` must refuse the turn **before** it is queued (FR-8.49), not
  fail it afterwards — a refusal that leaves a queued turn is a lie;
* an idempotent replay of an accepted submit is not a new turn, so the gate must
  not answer it with an error;
* an untrustworthy ledger (`usage.source == "none"`) may not swallow a message:
  the hard block degrades to `degraded: true` — and `warn` never blocks at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from vade.errors import VadeError
from vade.llm.usage import UsageAccounting
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

from .helpers import SequenceExecutor, usage_record

BUDGET_CFG = {"usage.session_tokens": 10, "usage.enforce": "block"}


def _log(tmp_path, cfg_overrides, executor) -> SessionLog:
    from .helpers import make_config

    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    log.set_usage_accounting(
        UsageAccounting(config=make_config(**cfg_overrides), store=log.store, emit=log.emit)
    )
    return log


def _budget_events(log: SessionLog, session_id: str) -> list[dict]:
    return [e.payload for e in log.store.read_since(session_id, 0) if e.type == "usage.budget"]


def test_budget_exceeded_refuses_a_new_turn_before_it_is_queued(tmp_path):
    executor = SequenceExecutor(usage_record(50), usage_record(50))
    log = _log(tmp_path, BUDGET_CFG, executor)
    session = log.create_session()

    async def scenario():
        first, pos = await log.submit(session.id, [{"type": "text", "text": "first"}], "m1")
        assert pos == 1
        await log.wait_idle(session.id)
        with pytest.raises(VadeError) as excinfo:
            await log.submit(session.id, [{"type": "text", "text": "second"}], "m2")
        return first, excinfo.value

    first, error = asyncio.run(scenario())

    assert error.code == "BUDGET_EXCEEDED"
    assert error.retriable is False, "retrying spends more, so it must not be labelled retriable"
    assert error.data == {"state": "exceeded", "scope": "session", "used": 50, "limit": 10}

    loaded = log.store.load(session.id)
    assert [turn.id for turn in loaded.turns] == [first.id], "the refused turn must not exist"
    events = log.store.read_since(session.id, 0)
    assert [e.payload["turn_id"] for e in events if e.type == "turn.queued"] == [first.id]
    assert [m.content for m in loaded.messages if m.role == "user"] == ["first"], (
        "a refused submit must not persist the user message either"
    )
    assert log.queue_state(session.id) == {"running": None, "running_state": None, "pending": []}
    assert len(executor.turns) == 1, "the provider ran for the accepted turn only"


def test_an_idempotent_replay_is_answered_before_the_gate(tmp_path):
    """A client retrying its own accepted submit must not get BUDGET_EXCEEDED.

    The budget can flip between the original submit and the retry (the turn it
    queued is what spent the tokens). Answering the retry with an error tells
    the user their message was refused when it was in fact delivered.
    """
    executor = SequenceExecutor(usage_record(50))
    log = _log(tmp_path, BUDGET_CFG, executor)
    session = log.create_session()

    async def scenario():
        first, _ = await log.submit(session.id, [{"type": "text", "text": "only"}], "same-id")
        await log.wait_idle(session.id)
        # The budget is now exhausted; the SAME client_msg_id is a replay.
        replay, _ = await log.submit(session.id, [{"type": "text", "text": "only"}], "same-id")
        with pytest.raises(VadeError):
            await log.submit(session.id, [{"type": "text", "text": "new"}], "fresh-id")
        return first, replay

    first, replay = asyncio.run(scenario())
    assert replay.id == first.id
    assert len(log.store.load(session.id).turns) == 1
    assert len(executor.turns) == 1


def test_an_untrusted_ledger_degrades_the_block_to_a_warning(tmp_path):
    """`source="none"` must not be allowed to swallow a message (§6.4).

    Turn 1 measured 100 tokens, turn 2's provider reported nothing. Then the
    operator lowers the limit (`[usage]` is entirely `hot`, so this is a routine
    mid-session change). The total is now over the limit, but the last
    measurement is `none`, so the number is not trustworthy: the refusal becomes
    `degraded: true` and the turn runs.
    """
    executor = SequenceExecutor(usage_record(100), usage_record(0, source="none"))
    log = _log(tmp_path, {"usage.session_tokens": 0, "usage.enforce": "block"}, executor)
    session = log.create_session()

    async def scenario():
        for index in range(2):
            await log.submit(session.id, [{"type": "text", "text": f"m{index}"}], f"m{index}")
            await log.wait_idle(session.id)
        # A hot `[usage]` change: same accounting object, tighter limit.
        log.usage.budget = replace(log.usage.budget, session_tokens=10)
        third, _ = await log.submit(session.id, [{"type": "text", "text": "third"}], "m3")
        await log.wait_idle(session.id)
        return third

    third = asyncio.run(scenario())
    loaded = log.store.load(session.id)
    assert [turn.state.value for turn in loaded.turns] == ["completed"] * 3, (
        "the block was downgraded, so the turn must really run"
    )
    assert any(turn.id == third.id for turn in loaded.turns)

    events = _budget_events(log, session.id)
    assert events, "an exceeded-but-degraded budget must still tell the UI"
    event = events[0]
    assert (event["scope"], event["state"]) == ("session", "exceeded")
    assert event["degraded"] is True, "degraded means exactly: should have blocked, ledger untrusted"
    assert event["used"] >= event["limit"] > 0
    assert event["session_id"] == session.id

    report = log.usage.report(loaded)
    assert report["budget"]["state"] == "exceeded"
    assert report["budget"]["enforced"] is True, "the limit IS enforced — only this turn was exempt"
    assert report["chat"]["partial_turns"] == 2, (
        "both unmeasured turns are counted as partial, and neither is silently a zero"
    )


def test_usage_budget_notifies_once_per_scope_state_and_period(tmp_path):
    executor = SequenceExecutor(usage_record(100), usage_record(0, source="none"))
    log = _log(tmp_path, {"usage.session_tokens": 0, "usage.enforce": "block"}, executor)
    session = log.create_session()

    async def scenario():
        for index in range(2):
            await log.submit(session.id, [{"type": "text", "text": f"m{index}"}], f"m{index}")
            await log.wait_idle(session.id)
        log.usage.budget = replace(log.usage.budget, session_tokens=10)
        for index in range(2, 4):
            await log.submit(session.id, [{"type": "text", "text": f"m{index}"}], f"m{index}")
            await log.wait_idle(session.id)

    asyncio.run(scenario())
    keys = [(e["scope"], e["state"]) for e in _budget_events(log, session.id)]
    assert len(keys) == len(set(keys)), f"the same (scope, state, period) notified twice: {keys}"


def test_enforce_warn_only_warns_and_lets_the_turn_through(tmp_path):
    """`warn` never blocks; `warn` -> `exceeded` notifies each transition once."""
    cfg = {"usage.session_tokens": 100, "usage.warn_ratio": 0.8, "usage.enforce": "warn"}
    executor = SequenceExecutor(usage_record(80), usage_record(40), usage_record(0))
    log = _log(tmp_path, cfg, executor)
    session = log.create_session()

    async def scenario():
        for index in range(3):
            await log.submit(session.id, [{"type": "text", "text": f"m{index}"}], f"m{index}")
            await log.wait_idle(session.id)

    asyncio.run(scenario())
    loaded = log.store.load(session.id)
    assert [turn.state.value for turn in loaded.turns] == ["completed"] * 3, (
        "enforce=warn must never refuse a turn"
    )
    states = [(e["scope"], e["state"], e["degraded"]) for e in _budget_events(log, session.id)]
    assert ("session", "warn", False) in states, f"the warn threshold never notified: {states}"
    assert ("session", "exceeded", False) in states, f"exceeded never notified: {states}"
    assert all(degraded is False for *_rest, degraded in states), (
        "degraded is reserved for a downgraded hard block, never for warn"
    )
    report = log.usage.report(loaded)
    assert report["chat"]["total_tokens"] == 120
    assert report["budget"]["state"] == "exceeded"
    assert report["budget"]["enforced"] is False, "enforce=warn does not enforce"


def test_disabled_budget_keeps_accounting_but_never_blocks_or_notifies(tmp_path):
    cfg = {"usage.session_tokens": 1, "usage.enabled": False}
    executor = SequenceExecutor(usage_record(500), usage_record(500))
    log = _log(tmp_path, cfg, executor)
    session = log.create_session()

    async def scenario():
        for index in range(2):
            await log.submit(session.id, [{"type": "text", "text": f"m{index}"}], f"m{index}")
            await log.wait_idle(session.id)

    asyncio.run(scenario())
    loaded = log.store.load(session.id)
    assert [turn.state.value for turn in loaded.turns] == ["completed"] * 2
    assert _budget_events(log, session.id) == [], "a disabled budget must not notify"
    report = log.usage.report(loaded)
    assert report["chat"]["total_tokens"] == 1000, "accounting keeps running when the gate is off"
    assert report["budget"]["state"] == "exceeded", "the number is still reported, only not enforced"
    assert report["budget"]["enforced"] is False


def test_a_zero_limit_means_off_and_never_blocks(tmp_path):
    executor = SequenceExecutor(usage_record(10_000))
    log = _log(tmp_path, {"usage.session_tokens": 0}, executor)
    session = log.create_session()

    async def scenario():
        await log.submit(session.id, [{"type": "text", "text": "x"}], "m1")
        await log.wait_idle(session.id)
        await log.submit(session.id, [{"type": "text", "text": "y"}], "m2")
        await log.wait_idle(session.id)

    asyncio.run(scenario())
    report = log.usage.report(log.store.load(session.id))
    assert report["budget"] == {"scope": "session", "limit": 0, "used": 10_000,
                                "ratio": None, "state": "off", "enforced": False}
    assert _budget_events(log, session.id) == []
