"""The gate: an evolve module may receive only what all four permissions allow."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.events.subscribe import MAX_TOPICS

from .helpers import (
    ALLOWED_FP, FakeConnection, FakeManifest, FakeRecord, evolve_connection,
    make_bus, make_store,
)


def subscribe(bus, connection, params):
    return asyncio.run(bus.on_subscribe(connection, params))


def test_unknown_topic_is_rejected_not_an_error(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    result = subscribe(bus, connection, {"topics": ["session.turns"]})

    assert result["accepted"] == []
    assert result["rejected"] == [{"topic": "session.turns", "reason": "unknown topic"}]


def test_topic_not_declared_in_manifest(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection(topics=("session.turn",))
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    result = subscribe(bus, connection, {"topics": ["session.closed"]})

    assert result["rejected"] == [{"topic": "session.closed", "reason": "not in manifest"}]
    # Rejection is per topic: the connection stays and can still use tools.
    assert bus.table.get(connection.connection_id) is not None


def test_module_not_in_the_allowlist(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection},
                      allowlist=[])

    result = subscribe(bus, connection, {"topics": ["session.turn"]})

    assert result["rejected"][0]["reason"] == "not allowlisted"


def test_fingerprint_must_match_the_allowlist_entry(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection(fingerprint="cd" * 32)
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    result = subscribe(bus, connection, {"topics": ["session.turn"]})

    assert result["rejected"][0]["reason"] == "not allowlisted"


def test_allowlist_accepts_short_fp_forms(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    for entry in (f"com.example.evolve:{ALLOWED_FP}",
                  f"com.example.evolve@{ALLOWED_FP}",
                  ("com.example.evolve", ALLOWED_FP),
                  {"module_id": "com.example.evolve", "fingerprint": ALLOWED_FP[:16]}):
        bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection},
                          allowlist=[entry])
        result = subscribe(bus, connection, {"topics": ["session.turn"]})
        assert result["accepted"] == ["session.turn"], entry


def test_transcript_switch_off_disables_session_topics(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection},
                      allow_transcript=False)

    result = subscribe(bus, connection, {"topics": ["session.turn", "session.closed"]})

    assert result["accepted"] == []
    assert {r["reason"] for r in result["rejected"]} == {"disabled by config"}


def test_a_tool_module_never_sees_a_transcript(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection(role="tool")
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    result = subscribe(bus, connection, {"topics": ["session.turn"]})

    assert result["rejected"][0]["reason"] == "role not allowed"


def test_ui_may_subscribe_to_skill_proposed_without_the_evolve_allowlist(tmp_path):
    store = make_store(tmp_path)
    connection = FakeConnection("ui-1", module_id="com.example.ui", instance_id="ui-1")
    record = FakeRecord(module_id="com.example.ui", instance_id="ui-1", connection_id="ui-1",
                        role="ui", manifest=FakeManifest(subscriptions=("skill.proposed",)))
    bus, _ = make_bus(store, records=[record], connections={"ui-1": connection}, allowlist=[])

    result = subscribe(bus, connection, {"topics": ["skill.proposed"]})

    assert result["accepted"] == ["skill.proposed"]


def test_mixed_request_keeps_the_good_topics(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    result = subscribe(bus, connection, {"topics": ["session.turn", "session.closed", "nope"]})

    assert result["accepted"] == ["session.turn", "session.closed"]
    assert result["rejected"] == [{"topic": "nope", "reason": "unknown topic"}]
    assert bus.table.get("c1").topics == frozenset({"session.turn", "session.closed"})


def test_resubscribe_overwrites(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    subscribe(bus, connection, {"topics": ["session.turn"]})
    subscribe(bus, connection, {"topics": ["session.closed"]})

    assert bus.table.get("c1").topics == frozenset({"session.closed"})
    assert len(bus.table) == 1


def test_redact_empty_is_an_override_attempt_not_a_permission(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection},
                      redact=["tool_args", "file_content"])

    subscribe(bus, connection, {"topics": ["session.turn"], "redact": []})

    assert bus.table.get("c1").redact == ("tool_args", "file_content")
    assert bus.table.redact_override_attempts == 1
    assert any(r["event"] == "events.redact_override_denied" for r in bus.audit_records())


def test_redact_may_only_be_narrowed(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection},
                      redact=["tool_args"])

    subscribe(bus, connection, {"topics": ["session.turn"], "redact": ["user_text"]})

    assert bus.table.get("c1").redact == ("tool_args", "user_text")


def test_since_seq_is_validated(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    for bad in (-1, "3", 3.5, True):
        with pytest.raises(VadeError) as caught:
            subscribe(bus, connection, {"topics": ["session.turn"], "since_seq": bad})
        assert caught.value.code == "INVALID_ARGUMENT"


def test_topics_must_be_a_non_empty_string_array(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    for params in ({}, {"topics": []}, {"topics": "session.turn"}, {"topics": [1]}):
        with pytest.raises(VadeError) as caught:
            subscribe(bus, connection, params)
        assert caught.value.code == "INVALID_ARGUMENT"


def test_rejected_topics_are_deduplicated(tmp_path):
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    result = subscribe(bus, connection,
                       {"topics": ["nope", "session.turn", "nope", "session.turn", "nope"]})

    assert result["accepted"] == ["session.turn"]
    assert result["rejected"] == [{"topic": "nope", "reason": "unknown topic"}]


def test_the_topic_cap_is_a_refusal_not_a_truncation(tmp_path):
    """64 topics is the ceiling; 65 is INVALID_ARGUMENT, never a silent trim."""
    store = make_store(tmp_path)
    connection, record = evolve_connection()
    bus, _ = make_bus(store, records=[record], connections={connection.connection_id: connection})

    at_cap = subscribe(bus, connection, {"topics": ["nope"] * MAX_TOPICS})
    assert at_cap["accepted"] == []
    assert at_cap["rejected"] == [{"topic": "nope", "reason": "unknown topic"}]

    with pytest.raises(VadeError) as caught:
        subscribe(bus, connection, {"topics": ["nope"] * (MAX_TOPICS + 1)})

    assert caught.value.code == "INVALID_ARGUMENT"
    assert str(MAX_TOPICS) in caught.value.message
    # The refused request did not overwrite the subscription that was there.
    kept = bus.table.get(connection.connection_id)
    assert kept is not None and kept.topics == frozenset()


def test_declare_is_the_seam_when_the_manifest_is_not_surfaced(tmp_path):
    """C2's Manifest drops unknown fields today; `declare()` keeps the gate real."""
    store = make_store(tmp_path)
    connection = FakeConnection("c9", instance_id="evo-9")
    record = FakeRecord(module_id="com.example.evolve", instance_id="evo-9",
                        connection_id="c9", role="evolve", manifest=FakeManifest(subscriptions=()))
    bus, _ = make_bus(store, records=[record], connections={"c9": connection})

    first = subscribe(bus, connection, {"topics": ["session.turn"]})
    assert first["rejected"] == [{"topic": "session.turn", "reason": "not in manifest"}]

    bus.declare(module_id="com.example.evolve", instance_id="evo-9", topics=("session.turn",))
    second = subscribe(bus, connection, {"topics": ["session.turn"]})
    assert second["accepted"] == ["session.turn"]
