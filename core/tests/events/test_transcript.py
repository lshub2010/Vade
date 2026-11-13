"""Payload builders: split text, capped tail, redaction that tells the truth."""

from __future__ import annotations

from types import SimpleNamespace

from vade.events import transcript as tx
from vade.session.models import TurnState

from .helpers import add_turn, make_store


def test_turn_payload_keeps_user_and_assistant_separate(tmp_path):
    store = make_store(tmp_path)
    turn = add_turn(store, user="U1", assistant="A1",
                    tool_calls=({"tool": "fs/read", "args": {"path": "/etc/hosts"}, "ok": True},))

    payload = tx.turn_payload(store, "s1", turn["turn_id"])

    assert payload["user_text"] == "U1"
    assert payload["assistant_text"] == "A1"
    assert payload["tool_calls"] == [{
        "tool": "fs/read",
        "args_digest": tx.digest_args({"path": "/etc/hosts"}),
        "ok": True,
    }]


def test_turn_payload_is_none_when_the_row_is_not_there(tmp_path):
    store = make_store(tmp_path)
    assert tx.turn_payload(store, "s1", "nope") is None


def test_closed_tail_is_capped_and_long_text_is_truncated(tmp_path):
    store = make_store(tmp_path)
    for i in range(12):
        add_turn(store, user=f"q{i}", assistant="x" * (tx.TAIL_TEXT_MAX + 50))

    payload = tx.closed_payload(store, "s1", closed_at=123)

    assert payload["turns_total"] == 12
    assert [row["user_text"] for row in payload["tail"]] == [f"q{i}" for i in range(2, 12)]
    assert payload["tail"][0]["assistant_text"].endswith(tx._TRUNCATED)
    assert len(payload["tail"][0]["assistant_text"]) < tx.TAIL_TEXT_MAX + 100
    assert payload["closed_at"] == 123


def test_closed_marks_failed_turns_negative(tmp_path):
    store = make_store(tmp_path)
    add_turn(store, state=TurnState.COMPLETED)
    add_turn(store, state=TurnState.INTERRUPTED, terminal_type="turn.interrupted")

    payload = tx.closed_payload(store, "s1")

    assert [row["negative"] for row in payload["turns"]] == [False, True]
    assert [row["state"] for row in payload["turns"]] == ["completed", "interrupted"]


def test_self_corrected_is_passed_through_never_invented():
    assert tx._self_corrected(SimpleNamespace(self_corrected=True)) is True
    assert tx._self_corrected(SimpleNamespace(usage={"self_corrected": True})) is True
    # C3's Turn has no such field today: absence must read as False, not a guess.
    assert tx._self_corrected(SimpleNamespace(usage={})) is False


def test_redaction_hashes_the_named_class_and_flags_only_on_a_hit():
    payload = {"turn_id": "t1", "tool_calls": [{"tool": "fs/read", "args": {"path": "/secret"}}]}

    redacted, hit = tx.redact_payload(payload, ["tool_args"])

    assert hit is True
    assert redacted["redacted"] is True
    assert redacted["tool_calls"][0]["args"] == tx._hash({"path": "/secret"})
    assert redacted["tool_calls"][0]["tool"] == "fs/read"

    untouched, miss = tx.redact_payload({"turn_id": "t1"}, ["tool_args"])
    assert miss is False
    assert "redacted" not in untouched


def test_redaction_does_not_double_hash_a_digest():
    payload = {"tool_calls": [{"tool": "x", "args_digest": "sha256:abc"}]}

    redacted, hit = tx.redact_payload(payload, ["tool_args"])

    assert hit is False
    assert redacted["tool_calls"][0]["args_digest"] == "sha256:abc"
