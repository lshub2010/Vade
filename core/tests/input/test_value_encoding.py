"""RFC-0008 A2: the four controls encode their value with the right JSON type."""

from __future__ import annotations

import asyncio

import pytest

from vade.input import InputOption

from .helpers import build, of_type, start_turn, wait_pending

OPTIONS = [InputOption("a", "Option A"), InputOption("b", "Option B"),
           InputOption("c", "Option C", "the third one")]


def _run(tmp_path, kind, value, *, options=OPTIONS, ui=1):
    async def scenario():
        log, manager, executor = build(tmp_path, ui=ui)
        choice = kind in ("single_choice", "multi_choice")
        executor.ask(kind=kind, prompt="pick", options=options if choice else ())
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        result = await manager.submit(session_id=session.id, input_id=input_id, value=value)
        await log.wait_idle(session.id)
        resolved = of_type(log, session.id, "turn.input_resolved")[0]
        return result, resolved.payload, executor.outcomes[0]

    return asyncio.run(scenario())


def test_single_choice_value_is_the_option_id(tmp_path):
    result, payload, outcome = _run(tmp_path, "single_choice", "b")
    assert result["value"] == "b"
    assert payload["value"] == "b" and isinstance(payload["value"], str)
    assert outcome.value == "b"


def test_multi_choice_value_is_an_ordered_array(tmp_path):
    """Order is the submission order, not the option order (§4.2)."""
    result, payload, outcome = _run(tmp_path, "multi_choice", ["c", "a"])
    assert result["value"] == ["c", "a"]
    assert payload["value"] == ["c", "a"]
    assert isinstance(payload["value"], list)
    assert outcome.value == ["c", "a"]


def test_confirm_value_is_a_json_boolean(tmp_path):
    result, payload, outcome = _run(tmp_path, "confirm", False)
    assert result["value"] is False
    assert payload["value"] is False
    assert outcome.value is False


def test_confirm_refuses_the_string_spelling(tmp_path):
    """`"true"` is not a boolean; a client that sends it gets a field error."""
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="confirm", prompt="sure?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        from vade.errors import VadeError

        with pytest.raises(VadeError) as excinfo:
            await manager.submit(session_id=session.id, input_id=input_id, value="true")
        assert excinfo.value.code == "INVALID_ARGUMENT"
        assert excinfo.value.data["field"] == "value"
        # Nothing moved: still waiting, still one event, one pending record.
        assert manager.pending_count == 1
        assert of_type(log, session.id, "turn.input_resolved") == []
        await manager.submit(session_id=session.id, input_id=input_id, value=True)
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_option_shape_is_the_contract_shape(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="single_choice", prompt="pick", options=OPTIONS)
        session = log.create_session()
        await start_turn(log, session.id)
        await wait_pending(manager)
        payload = of_type(log, session.id, "turn.input_required")[0].payload
        assert payload["options"] == [
            {"id": "a", "label": "Option A", "description": None},
            {"id": "b", "label": "Option B", "description": None},
            {"id": "c", "label": "Option C", "description": "the third one"},
        ]
        assert payload["schema"] == {"type": "string", "enum": ["a", "b", "c"],
                                     "allow_other": False}

    asyncio.run(scenario())
