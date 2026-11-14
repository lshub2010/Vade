"""The hard invariants of `session/usage.get` (RFC-0005 §6.3), one per test.

`test_usage.py` already pins: the `llm.call`/turn reconciliation on a running
engine, aux coming from the audit with the reader paging past its 500-row cap,
`cancelled` staying out of `chat`, the same-day filter with a two-day-old row,
`usage.include_aux`, the RPC verbatim/NOT_FOUND/UNAVAILABLE answers and the
`source="none"` flag on a **zero-token** failed/interrupted pair — plus
`test_budget.py`'s exact `limit=0` session dict.

What none of them pinned, and what this file pins:

* `total_tokens == chat + aux` over completed + failed + interrupted together;
* an interrupted (and a failed) turn's **non-zero** tokens reaching `chat` —
  the old test only ever gave those states 0 tokens, so a filter that dropped
  them would have stayed green;
* `partial_turns` counting unmeasured terminal turns only (an empty `usage`
  dict is the documented pre-RFC-0005 row, RFC-0005 Q4, not a measured zero);
* `aux` excluding every spelling of a chat call (explicit, missing, `null`);
* the UTC day boundary at the exact second, and its independence from the
  host's local midnight;
* `limit=0` -> `ratio: null` on the session budget **and** `off` on the day
  budget, with a non-empty ledger;
* the four `budget.state` values at their exact edges (`used == limit * w`,
  `used == limit`), both on `budget_state()` and through the report;
* two attached devices reading the same session and getting identical fields;
* an empty session (zero turns) reporting zeros instead of crashing, with the
  ratio still following the limit;
* `by_model` keeping and merging a `model_id: null` bucket (§6.3 table).
"""

from __future__ import annotations

import asyncio
import calendar

import pytest

from vade.llm.usage import UsageAccounting, budget_state
from vade.session.manager import SessionLog
from vade.session.models import Turn, TurnState
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore

from .helpers import (FakeAuditReader, FakeConnection, llm_row, make_config,
                      usage_record)

MODEL_ID = "com.example.model/qwen2.5-vl-72b"

#: A fixed instant so `day` cannot move between two calls in one test.
NOW = float(calendar.timegm((2031, 3, 5, 12, 0, 0, 0, 0, 0))) + 0.25


def _log(tmp_path, cfg=None, *, clock=NOW, db="sessions.db") -> SessionLog:
    log = SessionLog(SQLiteStore(tmp_path / db), replay=ReplayBuffer(maxlen=50))
    log.set_usage_accounting(UsageAccounting(
        config=cfg if cfg is not None else make_config(),
        store=log.store,
        clock=lambda: clock,
    ))
    return log


def _turn(log: SessionLog, session_id: str, turn_id: str, state: TurnState,
          usage: dict) -> None:
    """Put one terminal turn with a known usage record on the ledger."""
    log.store.update_turn(session_id, Turn(id=turn_id, session_id=session_id, state=state,
                                           user_text=turn_id, usage=dict(usage)))


def _report(log: SessionLog, session_id: str) -> dict:
    return log.usage.report(log.store.load(session_id))


def test_total_is_chat_plus_aux_over_completed_failed_and_interrupted(tmp_path):
    """`total_tokens = chat + aux` is the number the budget judges (§6.3)."""
    log = _log(tmp_path)
    session = log.create_session()
    _turn(log, session.id, "t-completed", TurnState.COMPLETED, usage_record(100))
    _turn(log, session.id, "t-failed", TurnState.FAILED, usage_record(40))
    _turn(log, session.id, "t-interrupted", TurnState.INTERRUPTED, usage_record(60))
    log.usage.audit_reader = FakeAuditReader([
        llm_row(session.id, tokens=100, turn_id="t-completed", ts=NOW),
        llm_row(session.id, tokens=40, turn_id="t-failed", ts=NOW),
        llm_row(session.id, tokens=60, turn_id="t-interrupted", ts=NOW),
        llm_row(session.id, tokens=25, purpose="compaction", ts=NOW),
        llm_row(session.id, tokens=15, purpose="memory", ts=NOW),
    ])
    report = _report(log, session.id)

    assert report["chat"] == {"prompt_tokens": 100, "completion_tokens": 100,
                              "total_tokens": 200, "turns": 3, "partial_turns": 0}, (
        "all three terminal states are in `chat`, and a chat `llm.call` row is not aux"
    )
    assert report["aux"] == {"total_tokens": 40, "calls": 2}
    assert report["total_tokens"] == 240
    assert report["total_tokens"] == report["chat"]["total_tokens"] + report["aux"]["total_tokens"]
    assert report["budget"]["used"] == report["total_tokens"], (
        "the budget judges total_tokens (chat + aux), never chat alone"
    )
    assert report["day"]["core_total_tokens"] == 240
    assert report["day"]["session_tokens"] == 240


def test_an_interrupted_turn_contributes_its_tokens_to_chat(tmp_path):
    """The most fragile one: `interrupted` is terminal *and* measurable.

    The pre-existing test only ever gave an interrupted turn 0 tokens, so a
    `TERMINAL_MEASURED` that quietly dropped `interrupted` would still pass it.
    """
    log = _log(tmp_path)
    session = log.create_session()
    _turn(log, session.id, "t-int", TurnState.INTERRUPTED, usage_record(70))
    _turn(log, session.id, "t-done", TurnState.COMPLETED, usage_record(30))
    report = _report(log, session.id)

    assert report["chat"]["turns"] == 2
    assert report["chat"]["prompt_tokens"] == 50, "35 from the interrupted turn + 15 from the completed one"
    assert report["chat"]["completion_tokens"] == 50
    assert report["chat"]["total_tokens"] == 100
    assert report["chat"]["partial_turns"] == 0, "a provider-reported interrupted turn is measured"
    assert report["total_tokens"] == 100


def test_a_failed_turn_contributes_tokens_and_cancelled_contributes_none(tmp_path):
    log = _log(tmp_path)
    session = log.create_session()
    _turn(log, session.id, "t-failed", TurnState.FAILED, usage_record(40))
    _turn(log, session.id, "t-cancelled", TurnState.CANCELLED, usage_record(999))
    _turn(log, session.id, "t-done", TurnState.COMPLETED, usage_record(10))
    report = _report(log, session.id)

    assert report["chat"]["turns"] == 2, "cancelled never reached the model, so it is not a turn"
    assert report["chat"]["total_tokens"] == 50, "failed 40 + completed 10, cancelled 999 excluded"
    assert report["total_tokens"] == 50


def test_partial_turns_counts_unmeasured_terminal_turns_only(tmp_path):
    """`partial_turns` = terminal turns whose usage core had to make up.

    RFC-0005 Q4: a historical turn's `usage` is `{}` and is counted honestly.
    A zero the provider really reported is measured, not partial.
    """
    log = _log(tmp_path)
    session = log.create_session()
    _turn(log, session.id, "t-measured", TurnState.COMPLETED, usage_record(100))
    _turn(log, session.id, "t-real-zero", TurnState.COMPLETED, usage_record(0, source="provider"))
    _turn(log, session.id, "t-none", TurnState.FAILED, usage_record(0, source="none"))
    _turn(log, session.id, "t-legacy", TurnState.INTERRUPTED, {})
    _turn(log, session.id, "t-empty-fields", TurnState.COMPLETED, {})
    report = _report(log, session.id)

    assert report["chat"]["turns"] == 5, "every terminal (completed/failed/interrupted) turn counts"
    assert report["chat"]["partial_turns"] == 3, (
        "source=\"none\" and an empty usage record are unmeasured; a provider-reported 0 is not"
    )
    assert report["chat"]["total_tokens"] == 100


def test_aux_excludes_a_chat_call_however_its_purpose_is_spelled(tmp_path):
    """`aux` is `purpose != "chat"`; a row without a purpose is a chat call (§6.2)."""
    log = _log(tmp_path)
    session = log.create_session()
    log.usage.audit_reader = FakeAuditReader([
        llm_row(session.id, tokens=70, purpose="chat", ts=NOW),
        # A row whose `extra` never got a `purpose` (an older writer): chat.
        {"kind": "llm.call", "session_id": session.id, "turn_id": "t1", "ts": NOW,
         "extra": {"total_tokens": 11}},
        # An explicit null is the same thing as missing, not "aux".
        {"kind": "llm.call", "session_id": session.id, "turn_id": "t1", "ts": NOW,
         "extra": {"purpose": None, "total_tokens": 13}},
        llm_row(session.id, tokens=30, purpose="compaction", ts=NOW),
        llm_row(session.id, tokens=20, purpose="memory", ts=NOW),
        llm_row(session.id, tokens=10, purpose="evolve", ts=NOW),
        llm_row("s-other", tokens=500, purpose="compaction", ts=NOW),
    ])
    report = _report(log, session.id)

    assert report["aux"] == {"total_tokens": 60, "calls": 3}, (
        "chat rows (explicit, missing purpose, null purpose) and other sessions never enter aux"
    )
    assert report["chat"]["total_tokens"] == 0
    assert report["total_tokens"] == 60


def test_the_day_boundary_is_utc_midnight_to_the_second(tmp_path):
    """A row at 00:00:00Z counts; 1 ms earlier does not (§6.3, same as day_key)."""
    day_start = float(calendar.timegm((2031, 3, 5, 0, 0, 0, 0, 0, 0)))
    log = _log(tmp_path, clock=day_start + 43_200)
    session = log.create_session()
    log.usage.audit_reader = FakeAuditReader([
        llm_row(session.id, tokens=100, turn_id="t1", ts=day_start),
        llm_row(session.id, tokens=50, turn_id="t1", ts=day_start + 86_399.0),
        llm_row(session.id, tokens=7, turn_id="t1", ts=day_start - 0.001),
        llm_row(session.id, tokens=9, turn_id="t1", ts=day_start - 3_600),
    ])
    report = _report(log, session.id)

    assert report["day"]["day"] == "2031-03-05"
    assert report["day"]["core_total_tokens"] == 150, (
        "the last millisecond of the previous UTC day and the first of this one are different days"
    )
    assert report["day"]["session_tokens"] == 150


def test_the_day_key_is_utc_even_when_the_host_local_date_differs(tmp_path):
    """20:30Z is already "tomorrow" in UTC+8; `day` must stay on the UTC date."""
    now = float(calendar.timegm((2031, 3, 5, 20, 30, 0, 0, 0, 0)))
    before_local_midnight = float(calendar.timegm((2031, 3, 5, 8, 0, 0, 0, 0, 0)))
    after_local_midnight = float(calendar.timegm((2031, 3, 5, 17, 0, 0, 0, 0, 0)))
    log = _log(tmp_path, clock=now)
    session = log.create_session()
    log.usage.audit_reader = FakeAuditReader([
        # 16:00 local on the 5th: past UTC midnight, before the local one.
        llm_row(session.id, tokens=121, purpose="compaction", ts=before_local_midnight),
        # 01:00 local on the 6th: still the 5th in UTC.
        llm_row(session.id, tokens=50, purpose="compaction", ts=after_local_midnight),
    ])
    report = _report(log, session.id)

    assert report["day"]["day"] == "2031-03-05", (
        "a local-midnight day start would report the UTC+8 date here"
    )
    assert report["day"]["core_total_tokens"] == 171, (
        "a local-midnight boundary would drop the 16:00 local row"
    )
    assert report["aux"] == {"total_tokens": 171, "calls": 2}


def test_zero_limits_are_off_with_a_null_ratio_on_both_budgets(tmp_path):
    """`limit=0` -> `state="off"`, `ratio=null`, and the spend is still reported."""
    cfg = make_config(**{"usage.session_tokens": 0, "usage.daily_tokens": 0})
    log = _log(tmp_path, cfg)
    session = log.create_session()
    _turn(log, session.id, "t1", TurnState.COMPLETED, usage_record(100))
    log.usage.audit_reader = FakeAuditReader([
        llm_row(session.id, tokens=100, turn_id="t1", ts=NOW),
        llm_row(session.id, tokens=25, purpose="compaction", ts=NOW),
    ])
    report = _report(log, session.id)

    assert report["budget"] == {"scope": "session", "limit": 0, "used": 125,
                                "ratio": None, "state": "off", "enforced": False}, (
        "a zero limit has no ratio — null, not 0.0 and not a division error"
    )
    assert report["day"]["core_limit"] == 0
    assert report["day"]["state"] == "off"
    assert report["day"]["core_total_tokens"] == 125, "off is a gate state, not a blindfold"
    assert report["day"]["session_tokens"] == 125


@pytest.mark.parametrize("used,limit,warn_ratio,expected", [
    (79, 100, 0.8, "ok"),         # one token below the warn edge
    (80, 100, 0.8, "warn"),       # exactly limit * warn_ratio
    (99, 100, 0.8, "warn"),
    (100, 100, 0.8, "exceeded"),  # exactly the limit
    (101, 100, 0.8, "exceeded"),
    (0, 100, 0.8, "ok"),
    (0, 0, 0.8, "off"),
    (10_000, 0, 0.8, "off"),
    (99, 100, 1.0, "ok"),         # warn is unreachable at warn_ratio=1.0: it would
                                  # need used >= limit AND used < limit
    (100, 100, 1.0, "exceeded"),
    (0, 1, 0.8, "ok"),
    (1, 1, 0.8, "exceeded"),
])
def test_budget_state_edges(used, limit, warn_ratio, expected):
    assert budget_state(used, limit, warn_ratio) == expected


def test_the_report_carries_the_state_edges_and_their_ratio(tmp_path):
    """The same edges through `report()`, with the ratio a UI divides by."""
    cfg = make_config(**{"usage.session_tokens": 100, "usage.warn_ratio": 0.8})

    def budget_for(tokens: int) -> dict:
        log = _log(tmp_path, cfg, db=f"s-{tokens}.db")
        session = log.create_session()
        _turn(log, session.id, "t1", TurnState.COMPLETED, usage_record(tokens))
        return _report(log, session.id)["budget"]

    assert budget_for(79) == {"scope": "session", "limit": 100, "used": 79,
                              "ratio": 0.79, "state": "ok", "enforced": False}
    assert budget_for(80) == {"scope": "session", "limit": 100, "used": 80,
                              "ratio": 0.8, "state": "warn", "enforced": False}
    assert budget_for(100) == {"scope": "session", "limit": 100, "used": 100,
                               "ratio": 1.0, "state": "exceeded", "enforced": True}
    assert budget_for(101) == {"scope": "session", "limit": 100, "used": 101,
                               "ratio": 1.01, "state": "exceeded", "enforced": True}


def test_two_attached_devices_read_identical_numbers(tmp_path):
    """§6.3: two attached devices must get identical fields and budget.state."""
    cfg = make_config(**{"usage.session_tokens": 250, "usage.daily_tokens": 0})
    log = _log(tmp_path, cfg)
    session = log.create_session()
    _turn(log, session.id, "t-done", TurnState.COMPLETED, usage_record(100))
    _turn(log, session.id, "t-int", TurnState.INTERRUPTED, usage_record(50))
    _turn(log, session.id, "t-failed", TurnState.FAILED, usage_record(0, source="none"))
    log.usage.audit_reader = FakeAuditReader([
        llm_row(session.id, tokens=100, turn_id="t-done", ts=NOW),
        llm_row(session.id, tokens=50, turn_id="t-int", ts=NOW),
        llm_row(session.id, tokens=100, purpose="compaction", ts=NOW),
        llm_row("s-other", tokens=400, purpose="memory", ts=NOW),
    ])
    rpc = SessionRPC(log)
    device_a, device_b = FakeConnection("ui-a"), FakeConnection("ui-b")
    assert device_a.device_id != device_b.device_id

    async def scenario():
        await rpc.dispatch(device_a, "ui/attach", {"session_id": session.id, "last_seq": None})
        await rpc.dispatch(device_b, "ui/attach", {"session_id": session.id, "last_seq": None})
        first = await rpc.dispatch(device_a, "session/usage.get", {"session_id": session.id})
        second = await rpc.dispatch(device_b, "session/usage.get", {"session_id": session.id})
        return first, second

    first, second = asyncio.run(scenario())

    assert first["chat"] == {"prompt_tokens": 75, "completion_tokens": 75,
                             "total_tokens": 150, "turns": 3, "partial_turns": 1}
    assert first["aux"] == {"total_tokens": 100, "calls": 1}
    assert first["total_tokens"] == 250
    assert first["day"]["core_total_tokens"] == 650, "other sessions are aggregated, not hidden"
    assert first["budget"]["state"] == "exceeded"
    assert first == second, "two devices attached to one session must read the same record"
    assert first["budget"]["state"] == second["budget"]["state"]


def test_an_empty_session_reports_zeros_and_a_ratio_that_follows_the_limit(tmp_path):
    log = _log(tmp_path, make_config(**{"usage.session_tokens": 0}))
    session = log.create_session()
    log.usage.audit_reader = FakeAuditReader([
        llm_row("s-other", tokens=70, purpose="compaction", ts=NOW),
    ])
    report = _report(log, session.id)

    assert report["session_id"] == session.id
    assert report["chat"] == {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0, "turns": 0, "partial_turns": 0}
    assert report["aux"] == {"total_tokens": 0, "calls": 0}
    assert report["total_tokens"] == 0
    assert report["by_model"] == []
    assert report["day"]["session_tokens"] == 0
    assert report["day"]["core_total_tokens"] == 70
    assert report["budget"] == {"scope": "session", "limit": 0, "used": 0,
                                "ratio": None, "state": "off", "enforced": False}

    bounded = UsageAccounting(config=make_config(**{"usage.session_tokens": 200}),
                              store=log.store, audit_reader=log.usage.audit_reader,
                              clock=lambda: NOW).report(log.store.load(session.id))
    assert bounded["budget"] == {"scope": "session", "limit": 200, "used": 0,
                                 "ratio": 0.0, "state": "ok", "enforced": False}, (
        "an empty ledger against a real limit is ratio 0.0, not null"
    )


def test_by_model_keeps_a_null_model_id_bucket(tmp_path):
    """§6.3: a historical turn with no `model_id` is a `model_id: null` row, not a drop."""
    log = _log(tmp_path)
    session = log.create_session()
    _turn(log, session.id, "t-old-1", TurnState.COMPLETED, usage_record(30, model_id=None))
    _turn(log, session.id, "t-old-2", TurnState.INTERRUPTED, usage_record(20, model_id=None))
    _turn(log, session.id, "t-new", TurnState.COMPLETED, usage_record(60, model_id=MODEL_ID))
    report = _report(log, session.id)

    assert report["by_model"] == [
        {"model_id": MODEL_ID, "total_tokens": 60, "turns": 1},
        {"model_id": None, "total_tokens": 50, "turns": 2},
    ], "the null bucket survives and both null turns land in the same row"
    assert report["chat"]["total_tokens"] == 110
