"""C-14 is an append, not a rewrite: C-06 stays byte-identical, the three topics exist.

The second half of each test is a drift guard: the `required` list in the frozen
file must equal the keys the payload builders actually emit, otherwise a consumer
reads the contract and gets a different object.
"""

from __future__ import annotations

import json
from pathlib import Path

from vade.events import transcript as tx

from .helpers import add_turn, make_store

SCHEMA = Path(__file__).resolve().parents[3] / "schema" / "events.json"

C06_EVENTS = {
    "session.attached", "turn.queued", "turn.started", "turn.delta", "turn.tool_call",
    "turn.tool_result", "turn.approval_required", "turn.approval_resolved",
    "turn.input_required", "turn.input_resolved",
    "turn.completed", "turn.failed", "turn.interrupted", "turn.cancelled",
    "queue.updated", "module.status", "turn.state",
}

C06_REQUIRED = {
    "session.attached": ["session_id", "from_seq", "state", "snapshot"],
    "turn.queued": ["turn_id", "state"],
    "turn.started": ["turn_id", "state"],
    "turn.delta": ["turn_id", "text"],
    "turn.tool_call": ["turn_id", "call_id", "tool", "args_digest"],
    "turn.tool_result": ["turn_id", "call_id", "ok", "summary"],
    "turn.approval_required": ["turn_id", "request_id", "tool", "args", "risk", "expires_at"],
    "turn.approval_resolved": ["turn_id", "request_id", "decision", "by", "scope", "state"],
    # RFC-0008 §6.1/§6.2: both are state-change events, so `state` is required.
    "turn.input_required": ["input_id", "turn_id", "state", "kind", "prompt", "schema",
                            "options", "timeout_s", "expires_at"],
    "turn.input_resolved": ["input_id", "turn_id", "state", "status", "value", "by"],
    "turn.completed": ["turn_id", "state", "usage"],
    "turn.failed": ["turn_id", "state", "code", "message"],
    "turn.interrupted": ["turn_id", "state", "by"],
    "turn.cancelled": ["turn_id", "state", "by"],
    "queue.updated": ["session_id", "running", "pending"],
    "module.status": ["module_id", "instance_id", "online"],
}


def load() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_c06_is_untouched():
    data = load()
    assert data["id"] == "C-06"
    # RFC-0008 §6.4: the enum was widened (7 -> 8 states), which is breaking for
    # an exhaustive validator, so the version moved with it. This assertion is
    # the drift guard for that decision — an extra state without a major bump
    # (or a major bump with no state) fails here.
    assert data["version"] == "2.0.0"
    assert data["status"] == "frozen"
    assert data["owner"] == "C3"
    assert C06_EVENTS <= set(data["events"])
    for name, required in C06_REQUIRED.items():
        assert data["events"][name]["required"] == required, name
    assert data["turn_states"] == ["queued", "waiting_approval", "waiting_input", "running",
                                   "completed", "failed", "interrupted", "cancelled"]
    assert any("C3d may APPEND session.turn / session.closed" in rule for rule in data["rules"])


def test_rfc0008_append_block_declares_the_enum_widening():
    data = load()
    blocks = data["x-vade"]["appends"]
    [block] = [b for b in blocks if "RFC-0008" in b["doc"]]
    assert block["version"] == "2.0.0"
    assert set(block["adds"]) == {"turn.input_required", "turn.input_resolved"}
    assert block["enum_widen"]["turn_states"] == ["waiting_input"]
    assert "none" in block["rewrites"]


def test_c14_appends_three_topics():
    data = load()
    append = data["x-vade"]["append"]
    assert append["contract"] == "C-14"
    assert set(append["adds"]) == {"session.turn", "session.closed", "skill.proposed"}
    for topic in append["adds"]:
        assert topic in data["events"], topic


def test_session_turn_required_matches_the_builder(tmp_path):
    store = make_store(tmp_path)
    turn = add_turn(store)
    payload = tx.turn_payload(store, "s1", turn["turn_id"])

    required = load()["events"]["session.turn"]["required"]

    assert set(payload) == set(required)
    assert payload["state"] in load()["turn_states"]


def test_session_closed_required_matches_the_builder(tmp_path):
    store = make_store(tmp_path)
    add_turn(store)
    payload = tx.closed_payload(store, "s1", closed_at=1)

    required = load()["events"]["session.closed"]["required"]

    assert set(payload) == set(required)
    assert set(payload["turns"][0]) == {"turn_id", "state", "tool_calls", "negative",
                                        "self_corrected"}
    assert set(payload["tail"][0]) == {"turn_id", "user_text", "assistant_text"}


def test_skill_proposed_is_metadata_only():
    event = load()["events"]["skill.proposed"]

    assert event["required"] == ["name", "origin", "revision", "base_revision", "status"]
    assert "content" not in json.dumps(event["payload"])
    assert event["payload"]["status"] == "pending"
