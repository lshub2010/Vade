"""The trusted half of the budget gate, end to end (§6.4).

`test_budget.py` proves the *untrusted* path (a `source="none"` ledger degrades
the block to a warning) and drives the gate with a hand-written usage record.
What was never proven is the other half, the one that has to work in
production:

* a streamed chat-completions call actually asks for usage
  (`stream_options.include_usage`) and records the trailing usage-only chunk,
  so the turn carries `source="provider"`;
* with that ledger `UsageAccounting._trusted()` is true and the next submit is
  really refused with `BUDGET_EXCEEDED` (not silently degraded, and not a
  number core invented);
* one failed turn (which the queue records as `source="none"`) must not erase
  the trust established by an earlier measured turn — a network error is not
  "this provider never reports";
* when the session and the day are both `exceeded`, the reported scope is the
  limit that is actually binding, not whichever candidate came first.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from vade.errors import VadeError
from vade.llm.catalog import ModelResolver
from vade.llm.openai_compat import OpenAICompatProvider
from vade.llm.usage import UsageAccounting
from vade.loop.engine import LoopEngine
from vade.loop.tools import build_snapshot
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry
from vade.router.dispatch import Router
from vade.session.manager import SessionLog
from vade.session.models import Session, Turn, TurnState
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

from .helpers import (FakeAuditReader, SequenceExecutor, chat_manifest, llm_row,
                      make_config, usage_record)

MODEL_ID = "com.example.model/qwen2.5-vl-72b"


class _SSEStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self):
        yield self._payload


def _sse(*objects) -> bytes:
    lines = [f"data: {json.dumps(obj, ensure_ascii=False)}" for obj in objects]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


class IdleModule:
    async def call(self, target, method, params, deadline_ms):  # pragma: no cover - no tools
        return {"ok": True, "result": {}}

    async def cancel(self, target, call_id):  # pragma: no cover - no tools
        return None


def _module_cfg(**overrides):
    return make_config(**{"llm.provider": "module", "llm.base_url": "http://127.0.0.1:9/v1",
                          "llm.model": "gpt-4o-mini", "llm.default_model": "",
                          **overrides})


def _registry() -> Registry:
    registry = Registry()
    registry.register(validate_manifest(chat_manifest(models=[{"name": "qwen2.5-vl-72b"}])),
                      fingerprint="fp-model", connection_id="conn-model")
    return registry


def _streaming_log(tmp_path, cfg, handler, registry):
    """A real `LoopEngine` + real OpenAI-compatible adapter over a fake wire."""
    provider = OpenAICompatProvider(base_url="http://llm.test/v1", model="m1",
                                    api_key_env="VADE_TEST_NO_KEY",
                                    transport=httpx.MockTransport(handler))
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=200))
    engine = LoopEngine(Router(IdleModule(), registry), provider,
                        lambda: build_snapshot(registry), log.emit)
    log.set_executor(engine.run_turn)
    log.set_model_resolver(ModelResolver(_module_cfg(), registry))
    log.set_usage_accounting(
        UsageAccounting(config=make_config(**cfg), store=log.store, emit=log.emit)
    )
    return log


def test_streamed_usage_is_recorded_and_the_next_submit_is_really_refused(tmp_path):
    """The whole chain: request bit -> usage chunk -> turn.usage -> gate."""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        chunks = [
            {"choices": [{"delta": {"content": "ok"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        # A real server only appends the usage chunk when the client asked for
        # it; emulating that is what makes a missing `stream_options` fail here
        # instead of passing on a fabricated measurement.
        if (body.get("stream_options") or {}).get("include_usage"):
            chunks.append({"choices": [], "usage": {"prompt_tokens": 30,
                                                    "completion_tokens": 12,
                                                    "total_tokens": 42}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_SSEStream(_sse(*chunks)))

    log = _streaming_log(tmp_path, {"usage.session_tokens": 10, "usage.enforce": "block"},
                         handler, _registry())
    session = log.create_session()

    async def scenario():
        first, _ = await log.submit(session.id, [{"type": "text", "text": "a"}], "m1")
        await log.wait_idle(session.id)
        with pytest.raises(VadeError) as excinfo:
            await log.submit(session.id, [{"type": "text", "text": "b"}], "m2")
        return first, excinfo.value

    first, error = asyncio.run(scenario())

    loaded = log.store.load(session.id)
    turn = loaded.turns[0]
    assert turn.id == first.id and turn.state is TurnState.COMPLETED
    assert turn.usage["total_tokens"] == 42
    assert turn.usage["prompt_tokens"] == 30
    assert turn.usage["completion_tokens"] == 12
    assert turn.usage["source"] == "provider", (
        "the trailing usage-only chunk must reach turn.usage; source=none here "
        "would mean the measurement was dropped"
    )
    assert log.usage._trusted(loaded) is True

    assert error.code == "BUDGET_EXCEEDED"
    assert error.data == {"state": "exceeded", "scope": "session", "used": 42, "limit": 10}
    assert len(requests) == 1, "the refused turn must never reach the provider"
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert [t.id for t in loaded.turns] == [first.id]


def _turn(turn_id: str, state: TurnState, usage: dict) -> Turn:
    return Turn(id=turn_id, session_id="s-t", state=state, user_text=turn_id, usage=dict(usage))


def _accounting() -> UsageAccounting:
    # `_trusted` is a pure read of the session: no store, no audit, no clock.
    return UsageAccounting(config=make_config(), store=None)


def test_trusted_is_true_once_a_real_usage_record_exists():
    accounting = _accounting()
    measured = _turn("t1", TurnState.COMPLETED, usage_record(40))
    failed = _turn("t2", TurnState.FAILED, usage_record(0, source="none"))

    assert accounting._trusted(Session(id="s-t", created_at=0)) is True, (
        "an empty ledger is trusted: there is nothing to block and nothing to invent"
    )
    assert accounting._trusted(Session(id="s-t", created_at=0, turns=(measured,))) is True
    assert accounting._trusted(
        Session(id="s-t", created_at=0, turns=(measured, failed))
    ) is True, (
        "a failure with no measurement is not evidence that the provider stopped reporting"
    )
    assert accounting._trusted(
        Session(id="s-t", created_at=0,
                turns=(failed, _turn("t3", TurnState.INTERRUPTED, usage_record(0, source="none"))))
    ) is False, "no measurement anywhere is still 'unknown', never 'trustworthy'"


def test_an_unmeasured_completed_turn_is_evidence_against_the_provider():
    accounting = _accounting()
    provider = _turn("t1", TurnState.COMPLETED, usage_record(40))
    unmeasured = _turn("t2", TurnState.COMPLETED, usage_record(0, source="none"))
    legacy = _turn("t3", TurnState.COMPLETED, {})

    assert accounting._trusted(Session(id="s-t", created_at=0, turns=(unmeasured,))) is False
    assert accounting._trusted(
        Session(id="s-t", created_at=0, turns=(provider, unmeasured))
    ) is False, "the newest completed turn says the provider stopped reporting"
    assert accounting._trusted(
        Session(id="s-t", created_at=0, turns=(legacy,))
    ) is False, "a pre-RFC-0005 row with no source is unmeasured, not zero"


class _FailSecondTurn:
    """First turn succeeds with a real measurement; every later one raises."""

    def __init__(self, first_usage: dict) -> None:
        self.first_usage = dict(first_usage)
        self.calls = 0

    async def __call__(self, turn, content):
        from vade.loop.engine import TurnOutcome

        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("provider exploded")
        return TurnOutcome(usage=dict(self.first_usage), assistant_text="ok",
                           stopped_reason="stop")


def test_a_failed_turn_does_not_disable_the_block_for_the_rest_of_the_session(tmp_path):
    """One error must not poison trust and silently turn the hard block off."""
    from dataclasses import replace

    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=50))
    log.set_executor(_FailSecondTurn(usage_record(50)))
    log.set_usage_accounting(UsageAccounting(
        config=make_config(**{"usage.session_tokens": 0, "usage.enforce": "block"}),
        store=log.store, emit=log.emit,
    ))
    session = log.create_session()

    async def scenario():
        await log.submit(session.id, [{"type": "text", "text": "a"}], "m1")
        await log.wait_idle(session.id)
        await log.submit(session.id, [{"type": "text", "text": "b"}], "m2")
        await log.wait_idle(session.id)
        # The operator tightens the limit after the failure (a hot `[usage]`).
        log.usage.budget = replace(log.usage.budget, session_tokens=10)
        return await log.usage.gate(log.store.load(session.id))

    decision = asyncio.run(scenario())

    loaded = log.store.load(session.id)
    assert [t.state for t in loaded.turns] == [TurnState.COMPLETED, TurnState.FAILED]
    assert loaded.turns[1].usage["source"] == "none", "the queue records a failure as unmeasured"
    assert log.usage._trusted(loaded) is True, "turn 1's real number is still on the ledger"
    assert decision["allowed"] is False
    assert decision["data"] == {"state": "exceeded", "scope": "session", "used": 50, "limit": 10}


def _exceeded_session(tmp_path, *, session_tokens: int, daily_tokens: int,
                      turn_tokens: int) -> tuple[SessionLog, Session]:
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=50))
    log.set_executor(SequenceExecutor(usage_record(turn_tokens)))
    log.set_usage_accounting(UsageAccounting(
        config=make_config(**{"usage.session_tokens": session_tokens,
                              "usage.daily_tokens": daily_tokens,
                              "usage.enforce": "block"}),
        store=log.store, emit=log.emit,
    ))
    session = log.create_session()

    async def scenario():
        await log.submit(session.id, [{"type": "text", "text": "a"}], "m1")
        await log.wait_idle(session.id)

    asyncio.run(scenario())
    return log, session


def test_both_scopes_exceeded_reports_the_day_when_it_is_further_over(tmp_path):
    """The first `max()` candidate used to win the tie: always `session`."""
    log, session = _exceeded_session(tmp_path, session_tokens=100, daily_tokens=100,
                                     turn_tokens=150)
    log.usage.audit_reader = FakeAuditReader([
        llm_row(session.id, tokens=150, ts=time.time()),
        llm_row("s-other", tokens=400, purpose="compaction", ts=time.time()),
    ])
    decision = asyncio.run(log.usage.gate(log.store.load(session.id)))

    assert decision["allowed"] is False
    assert decision["data"]["scope"] == "day", (
        "550/100 is the binding limit; reporting session (150/100) would tell "
        "the operator to raise the wrong budget"
    )
    assert decision["data"]["used"] == 550
    assert decision["data"]["limit"] == 100


def test_an_exact_tie_reports_day_not_session(tmp_path):
    """A daily limit refuses every session, so it owns an otherwise exact tie."""
    log, session = _exceeded_session(
        tmp_path, session_tokens=100, daily_tokens=100, turn_tokens=100)
    log.usage.audit_reader = FakeAuditReader([llm_row(session.id, tokens=100, ts=time.time())])
    decision = asyncio.run(log.usage.gate(log.store.load(session.id)))

    assert decision["allowed"] is False
    assert decision["data"] == {"state": "exceeded", "scope": "day", "used": 100, "limit": 100}


def test_the_more_over_budget_session_scope_still_wins_when_it_is_the_real_constraint(tmp_path):
    log, session = _exceeded_session(
        tmp_path, session_tokens=100, daily_tokens=1000, turn_tokens=300)
    log.usage.audit_reader = FakeAuditReader([
        llm_row(session.id, tokens=300, ts=time.time()),
        llm_row("s-other", tokens=900, purpose="compaction", ts=time.time()),
    ])
    decision = asyncio.run(log.usage.gate(log.store.load(session.id)))

    assert decision["allowed"] is False
    assert decision["data"] == {"state": "exceeded", "scope": "session", "used": 300, "limit": 100}, (
        "300/100 is three times the limit; the day total is only 1.2x its own"
    )
