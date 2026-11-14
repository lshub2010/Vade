"""RFC-0008 A3: validation is closed in core and never moves the state machine."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.input import InputOption, InputRequest, InputConfig, normalize_request

from .helpers import build, events, of_type, start_turn, wait_pending

OPTIONS = [InputOption("a", "A"), InputOption("b", "B")]


def _ask(tmp_path, kind, **kwargs):
    async def scenario():
        log, manager, executor = build(tmp_path, config=InputConfig(max_free_text_chars=20))
        choice = kind in ("single_choice", "multi_choice")
        executor.ask(kind=kind, prompt="q", options=OPTIONS if choice else (), **kwargs)
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        return log, manager, session, input_id

    return scenario()


def _reject(tmp_path, kind, value, code="INVALID_ARGUMENT", **kwargs):
    async def scenario():
        log, manager, session, input_id = await _ask(tmp_path, kind, **kwargs)
        before = [event.seq for event in events(log, session.id)]
        with pytest.raises(VadeError) as excinfo:
            await manager.submit(session_id=session.id, input_id=input_id, value=value)
        assert excinfo.value.code == code
        assert excinfo.value.data.get("field") == "value"
        # §4.4 step 1: no event, no state change, the id is still answerable.
        assert [event.seq for event in events(log, session.id)] == before
        assert of_type(log, session.id, "turn.input_resolved") == []
        assert manager.pending_count == 1
        assert log.turn_state(session.id, manager.pending_requests()[0]["turn_id"]) == "waiting_input"
        return excinfo.value

    return asyncio.run(scenario())


def test_unknown_single_choice_id_is_rejected(tmp_path):
    error = _reject(tmp_path, "single_choice", "zzz")
    assert "expected" in error.data


def test_multi_choice_duplicate_items_are_an_error_not_deduplicated(tmp_path):
    """§4.4 step 2: de-duplicating would hide the client's rendering bug."""
    _reject(tmp_path, "multi_choice", ["a", "a"])


def test_multi_choice_over_max_select(tmp_path):
    _reject(tmp_path, "multi_choice", ["a", "b", "a"])


def test_multi_choice_unknown_id(tmp_path):
    _reject(tmp_path, "multi_choice", ["a", "c"])


def test_blank_free_text_is_rejected(tmp_path):
    _reject(tmp_path, "free_text", "   \n\t ")


def test_too_long_free_text_is_payload_too_large(tmp_path):
    error = _reject(tmp_path, "free_text", "x" * 21, code="PAYLOAD_TOO_LARGE")
    assert error.data["limit"] == 20 and error.data["actual"] == 21


def test_wrong_json_type_for_every_kind(tmp_path):
    _reject(tmp_path, "free_text", ["a"])
    _reject(tmp_path, "single_choice", 1)
    _reject(tmp_path, "multi_choice", "a")
    _reject(tmp_path, "confirm", 1)


def test_sender_side_validation_rejects_bad_questions(tmp_path):
    """§4.4 step 4: a malformed question is the model's error, not the user's."""
    config = InputConfig()

    def bad(**kwargs):
        with pytest.raises(VadeError) as excinfo:
            normalize_request(InputRequest(session_id="s", turn_id="t", **kwargs), config)
        return excinfo.value

    assert bad(kind="slider", prompt="x").data["field"] == "kind"
    assert bad(kind="free_text", prompt="   ").data["field"] == "prompt"
    assert bad(kind="free_text", prompt="x", options=[InputOption("a", "A")]).data["field"] == "options"
    assert bad(kind="single_choice", prompt="x", options=[InputOption("a", "A")]).data["field"] == "options"
    duplicate = [InputOption("a", "A"), InputOption("a", "B")]
    assert bad(kind="single_choice", prompt="x", options=duplicate).data["field"] == "options"
    assert bad(kind="free_text", prompt="x", timeout_s="soon").data["field"] == "timeout_s"


def test_out_of_range_timeout_is_clamped_not_refused(tmp_path):
    record = normalize_request(
        InputRequest(session_id="s", turn_id="t", kind="confirm", prompt="x", timeout_s=99999),
        InputConfig(max_timeout_s=600),
    )
    assert record["timeout_s"] == 600
    record = normalize_request(
        InputRequest(session_id="s", turn_id="t", kind="confirm", prompt="x", timeout_s=1),
        InputConfig(min_timeout_s=5),
    )
    assert record["timeout_s"] == 5
