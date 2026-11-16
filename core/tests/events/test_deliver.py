"""Delivery: exactly once, bounded, replayable, and never logging the transcript."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.events.deliver import DeliveryQueue
from vade.session.models import Session, TurnState, now_ms

from .helpers import (
    FakeConnection, evolve_connection, make_bus, make_store, add_turn, pump,
)


def subscribe(bus, connection, params):
    return asyncio.run(bus.on_subscribe(connection, params))


def test_one_delivery_per_turn_even_when_pumped_repeatedly(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})

    turn = add_turn(store)

    assert pump(bus) == 1
    assert pump(bus) == 0           # the same turn is never delivered twice
    assert pump(bus) == 0
    frames = connection.delivered()
    assert len(frames) == 1
    assert frames[0]["seq_from"] == turn["seq_from"]
    assert frames[0]["seq_to"] == turn["seq_to"]


def test_payload_shape_is_the_contract(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})
    add_turn(store, user="why did the deploy fail?",
             assistant="because the certificate expired",
             tool_calls=({"tool": "shell/run", "args": {"cmd": "ls"}, "ok": False},))

    pump(bus)

    params = connection.delivered()[0]
    payload = params["payload"]
    assert params["topic"] == "session.turn"
    assert payload["session_id"] == "s1"
    assert payload["state"] == "completed"
    assert payload["user_text"] == "why did the deploy fail?"
    assert payload["assistant_text"] == "because the certificate expired"
    assert payload["tool_calls"][0]["tool"] == "shell/run"
    assert payload["tool_calls"][0]["args_digest"].startswith("sha256:")
    assert payload["tool_calls"][0]["ok"] is False


def test_a_failed_turn_is_delivered_with_its_state(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})
    add_turn(store, state=TurnState.FAILED, terminal_type="turn.failed", error_code="TIMEOUT")

    pump(bus)

    assert connection.delivered()[0]["payload"]["state"] == "failed"


def test_a_slow_consumer_drops_the_oldest_and_never_blocks(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection},
                      capacity=2)
    subscribe(bus, connection, {"topics": ["session.turn"]})

    turns = [add_turn(store, user=f"q{i}") for i in range(4)]
    bus.pump()                       # nothing can drain: there is no loop here

    stats = bus.stats()
    assert stats["queued"] == 2
    assert stats["dropped"] == 2
    # Delivery is best-effort on purpose; the session keeps running.
    assert stats["watermarks"]["s1"] == turns[-1]["seq_to"]


def test_drain_sends_everything_queued(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})
    for i in range(3):
        add_turn(store, user=f"q{i}")
    bus.pump()

    sent = asyncio.run(bus.drain())

    assert sent == 3
    assert len(connection.delivered()) == 3


def test_reconnect_with_since_seq_fills_the_gap_without_duplicates(tmp_path):
    store = make_store(tmp_path)
    first, record = evolve_connection("c1", instance_id="evo-1")
    bus, registry = make_bus(store, records=[record], connections={"c1": first})

    subscribe(bus, first, {"topics": ["session.turn"]})
    turn1 = add_turn(store, user="one")
    pump(bus)
    assert len(first.delivered()) == 1

    asyncio.run(bus.on_disconnect(first))
    turn2 = add_turn(store, user="two")
    turn3 = add_turn(store, user="three")
    pump(bus)                        # module is away: journal only

    second, record2 = evolve_connection("c2", instance_id="evo-1")
    registry.records[:] = [record2]
    bus._connections["c2"] = second
    result = subscribe(bus, second, {"topics": ["session.turn"], "session_id": "s1",
                                     "since_seq": turn1["seq_to"]})

    assert result["replayed"] == 2
    replayed = second.delivered()
    assert [p["payload"]["user_text"] for p in replayed] == ["two", "three"]
    assert [p["seq_to"] for p in replayed] == [turn2["seq_to"], turn3["seq_to"]]
    # Contiguous by seq: the next turn's first event follows the previous turn's last.
    assert replayed[0]["seq_from"] == turn2["seq_from"]
    assert replayed[1]["seq_from"] == replayed[0]["seq_to"] + 1
    assert replayed[1]["seq_to"] == turn3["seq_to"]

    # A later pump must not re-send what the replay already carried.
    assert pump(bus) == 0
    assert len(second.delivered()) == 2


def test_since_seq_replay_from_the_journal_on_a_fresh_subscribe(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection("c1")
    bus, _ = make_bus(store, records=[record], connections={"c1": connection})
    add_turn(store)
    pump(bus)                         # journaled even though nobody subscribed yet

    result = subscribe(bus, connection, {"topics": ["session.turn"], "session_id": "s1",
                                         "since_seq": 0})

    assert result["replayed"] == 1
    assert connection.delivered()[0]["payload"]["user_text"] == "how do I roll back?"


def test_closed_carries_the_tail_and_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})
    subscribe(bus, connection, {"topics": ["session.turn", "session.closed"]})
    for i in range(12):
        add_turn(store, user=f"q{i}", assistant=f"a{i}")

    bus.emit_closed("s1")
    bus.emit_closed("s1")             # idempotent: a second close adds nothing
    asyncio.run(bus.drain())

    closed = [p for p in connection.delivered() if p["topic"] == "session.closed"]
    assert len(closed) == 1
    payload = closed[0]["payload"]
    assert payload["turns_total"] == 12
    assert len(payload["tail"]) == 10
    assert payload["tail"][0]["user_text"] == "q2"
    assert payload["tail"][-1]["assistant_text"] == "a11"
    assert "messages" not in payload             # never the whole transcript
    assert "summary" not in payload              # M4b accumulates its own
    assert payload["closed_at"] > 0
    turns = [p for p in connection.delivered() if p["topic"] == "session.turn"]
    assert len(turns) == 12                     # closing does not swallow the last turn


def test_closed_reports_per_turn_metadata(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})
    subscribe(bus, connection, {"topics": ["session.closed"]})
    add_turn(store, state=TurnState.FAILED, terminal_type="turn.failed", error_code="TIMEOUT")

    bus.emit_closed("s1")
    asyncio.run(bus.drain())

    payload = connection.delivered()[0]["payload"]
    assert payload["turns"][0]["negative"] is True
    assert payload["turns"][0]["self_corrected"] is False
    assert payload["turns"][0]["tool_calls"] == []


def test_audit_never_records_content(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})
    add_turn(store, user="SECRET-QUESTION", assistant="SECRET-ANSWER")

    pump(bus)

    records = bus.audit_records()
    assert [r for r in records if r["event"] == "events.deliver"]
    allowed = {"event", "module_id", "instance_id", "session_id", "turns", "bytes", "ok",
               "reason", "accepted", "rejected", "replayed"}
    for record in records:
        assert set(record) <= allowed, record
        assert "SECRET" not in repr(record)


def test_redaction_is_applied_and_flagged(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection},
                      redact=["user_text"])
    subscribe(bus, connection, {"topics": ["session.turn"]})
    add_turn(store, user="my key is hunter2")

    pump(bus)

    payload = connection.delivered()[0]["payload"]
    assert payload["redacted"] is True
    assert payload["user_text"] != "my key is hunter2"
    assert len(payload["user_text"]) == 16
    assert payload["assistant_text"] == "use the rollback command"


def test_a_dead_connection_counts_a_failure_and_does_not_raise(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={})
    subscribe(bus, connection, {"topics": ["session.turn"]})
    add_turn(store)

    pump(bus)

    assert bus.stats()["failed"] == 1
    assert any(r["event"] == "events.delivery_failed" for r in bus.audit_records())


def test_skill_proposed_is_metadata_only(tmp_path):
    store = make_store(tmp_path)
    ui = FakeConnection("ui-1", module_id="com.example.ui", instance_id="ui-1")
    from .helpers import FakeManifest, FakeRecord
    record = FakeRecord(module_id="com.example.ui", instance_id="ui-1", connection_id="ui-1",
                        role="ui", manifest=FakeManifest(subscriptions=("skill.proposed",)))
    bus, _ = make_bus(store, records=[record], connections={"ui-1": ui}, allowlist=[])
    subscribe(bus, ui, {"topics": ["skill.proposed"]})

    bus.emit_skill_proposed(name="code-review", origin="proposal", revision=4,
                            base_revision=3, status="pending")
    asyncio.run(bus.drain())

    params = ui.delivered()[0]
    assert params["topic"] == "skill.proposed"
    assert params["session_id"] is None
    assert params["seq_from"] == params["seq_to"] == 0
    assert set(params["payload"]) == {"name", "origin", "revision", "base_revision", "status"}
    assert "content" not in params["payload"]


# ---------------------------------------------------------------- bounded retry


def test_a_turn_row_that_lands_late_is_delivered_once(tmp_path):
    """The normal C3 race: terminal event first, `turns` row a moment later."""
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={"c1": connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})

    turn = add_turn(store, user="late", persist_turn=False)
    assert pump(bus) == 0                    # held at the terminal event
    store.update_turn("s1", turn["turn"])

    assert pump(bus) == 1
    assert pump(bus) == 0
    frames = connection.delivered()
    assert [f["payload"]["user_text"] for f in frames] == ["late"]
    assert frames[0]["seq_from"] == turn["seq_from"]
    assert not [r for r in bus.audit_records() if r["event"] == "events.turn_missing"]


def test_a_turn_row_that_never_lands_is_audited_and_does_not_stall_the_session(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={"c1": connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})

    add_turn(store, user="ghost", persist_turn=False)   # no row will ever arrive
    add_turn(store, user="later")

    for _ in range(3):                       # bounded retries, then give up
        assert pump(bus) == 0
    assert pump(bus) == 1                    # the later turn is not blocked forever

    assert [f["payload"]["user_text"] for f in connection.delivered()] == ["later"]
    missing = [r for r in bus.audit_records() if r["event"] == "events.turn_missing"]
    assert missing and missing[0]["session_id"] == "s1"
    assert missing[0]["ok"] is False


class _ExplodingLoad:
    """A store whose `load()` fails for one session only."""

    def __init__(self, store, bad: str) -> None:
        self._store = store
        self._bad = bad

    def __getattr__(self, name):
        return getattr(self._store, name)

    def load(self, session_id):
        if session_id == self._bad:
            raise RuntimeError("broken session")
        return self._store.load(session_id)


def test_one_broken_session_does_not_stop_the_others(tmp_path):
    store = make_store(tmp_path)
    store.create_session(Session(id="s2", created_at=now_ms() + 10))
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={"c1": connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})
    add_turn(store, session_id="s1", user="fine")
    add_turn(store, session_id="s2", user="broken")
    bus._store = _ExplodingLoad(store, "s2")

    assert pump(bus) == 1

    assert [f["payload"]["user_text"] for f in connection.delivered()] == ["fine"]
    failed = [r for r in bus.audit_records() if r["event"] == "events.pump_failed"]
    assert {r["session_id"] for r in failed} == {"s2"}


# ------------------------------------------------------------ since_seq scope


def test_since_seq_replays_only_the_sessions_it_names(tmp_path):
    store = make_store(tmp_path)
    store.create_session(Session(id="s2", created_at=now_ms() + 10))
    first, record = evolve_connection("c1")
    bus, registry = make_bus(store, records=[record], connections={"c1": first})

    subscribe(bus, first, {"topics": ["session.turn"]})
    add_turn(store, session_id="s1", user="s1-only")
    s2_first = add_turn(store, session_id="s2", user="s2-first")
    s2_second = add_turn(store, session_id="s2", user="s2-second")
    pump(bus)
    assert len(first.delivered()) == 3
    asyncio.run(bus.on_disconnect(first))

    second, record2 = evolve_connection("c2")
    registry.records[:] = [record2]
    bus._connections["c2"] = second
    result = subscribe(bus, second, {"topics": ["session.turn"],
                                     "since_seq": {"s2": s2_first["seq_to"]}})

    assert result["replayed"] == 1
    frames = second.delivered()
    assert [f["session_id"] for f in frames] == ["s2"]
    assert frames[0]["payload"]["user_text"] == "s2-second"
    assert frames[0]["seq_from"] == s2_second["seq_from"]
    # s1 was not named, so its history is not invented for this connection.
    assert all(f["session_id"] != "s1" for f in frames)


def test_since_seq_accepts_session_id_with_an_integer(tmp_path):
    store = make_store(tmp_path)
    store.create_session(Session(id="s2", created_at=now_ms() + 10))
    connection, record = evolve_connection("c1")
    bus, _ = make_bus(store, records=[record], connections={"c1": connection})
    add_turn(store, session_id="s1", user="s1-only")
    add_turn(store, session_id="s2", user="s2-only")
    pump(bus)                               # journal both

    result = subscribe(bus, connection, {"topics": ["session.turn"],
                                         "session_id": "s2", "since_seq": 0})

    assert result["replayed"] == 1
    assert [f["session_id"] for f in connection.delivered()] == ["s2"]


def test_since_seq_without_a_session_is_rejected(tmp_path):
    """One bare watermark for every session is the bug, not a shorthand."""
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={"c1": connection})

    with pytest.raises(VadeError) as caught:
        subscribe(bus, connection, {"topics": ["session.turn"], "since_seq": 3})

    assert caught.value.code == "INVALID_ARGUMENT"
    assert "session_id" in caught.value.message


def test_replay_never_relabels_session_closed_as_a_turn(tmp_path):
    store = make_store(tmp_path)
    first, record = evolve_connection("c1")
    bus, registry = make_bus(store, records=[record], connections={"c1": first})
    subscribe(bus, first, {"topics": ["session.turn", "session.closed"]})
    add_turn(store)
    pump(bus)
    asyncio.run(bus.on_disconnect(first))
    bus.emit_closed("s1")                   # journaled, deliberately not replayable

    second, record2 = evolve_connection("c2")
    registry.records[:] = [record2]
    bus._connections["c2"] = second
    result = subscribe(bus, second, {"topics": ["session.turn", "session.closed"],
                                     "since_seq": {"s1": 0}})

    assert result["replayed"] == 1
    frames = second.delivered()
    assert [f["topic"] for f in frames] == ["session.turn"]
    assert "turns_total" not in frames[0]["payload"]
    assert "tail" not in frames[0]["payload"]


# ------------------------------------------------------------------ accounting


def test_offer_audits_the_envelope_it_actually_drops():
    records: list[dict] = []
    queue = DeliveryQueue(instance_id="i1", connection_id="c1", capacity=1,
                          _audit=lambda event, **fields: records.append({"event": event, **fields}))
    first = {"topic": "session.turn", "session_id": "s1", "seq_from": 1, "seq_to": 2, "payload": {}}
    second = {"topic": "session.turn", "session_id": "s2", "seq_from": 1, "seq_to": 2, "payload": {}}

    assert queue.offer(first) is True
    assert queue.offer(second) is True
    assert queue.pending() == 1

    dropped = [r for r in records if r["event"] == "events.delivery_dropped"]
    assert [r["session_id"] for r in dropped] == ["s1"]   # the one that left, not the arrival


def test_a_closed_queue_is_not_counted_as_sent(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={"c1": connection})
    subscribe(bus, connection, {"topics": ["session.turn"]})
    sub = bus.table.get("c1")
    assert sub is not None
    queue = bus._queue_for(sub)
    assert queue is not None
    queue.close()

    add_turn(store)

    assert bus.pump() == 0                   # dispatch refused, so nothing was sent
    assert bus.stats()["dropped"] == 1
    dropped = [r for r in bus.audit_records() if r["event"] == "events.delivery_dropped"]
    assert dropped and dropped[0]["reason"] == "queue closed"
