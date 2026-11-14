"""RFC-0008 §9: the value is data, the prompt is untrusted content."""

from __future__ import annotations

import asyncio

from vade.input import InputRequest, InputConfig, normalize_request

from .helpers import build, of_type, start_turn, wait_pending


def test_prompt_control_characters_are_stripped_and_length_is_capped():
    record = normalize_request(
        InputRequest(session_id="s", turn_id="t", kind="free_text",
                     prompt="\x1b[31mred\x1b[0m <script>alert(1)</script>\x00"),
        InputConfig(),
    )
    # Escape sequences and NUL are removed; the markup stays as text for the
    # client to render as plain text (never as HTML/markdown).
    assert record["prompt"] == "[31mred[0m <script>alert(1)</script>"
    assert "\x1b" not in record["prompt"] and "\x00" not in record["prompt"]


def test_a_prompt_over_500_characters_is_refused_not_truncated():
    from vade.errors import VadeError
    import pytest

    with pytest.raises(VadeError) as excinfo:
        normalize_request(
            InputRequest(session_id="s", turn_id="t", kind="free_text", prompt="x" * 501),
            InputConfig(),
        )
    assert excinfo.value.data["field"] == "prompt"


def test_option_labels_are_bounded():
    record = normalize_request(
        InputRequest(session_id="s", turn_id="t", kind="single_choice", prompt="q",
                     options=[{"id": "a", "label": "L" * 500},
                              {"id": "b", "label": "B", "description": "D" * 500}]),
        InputConfig(),
    )
    assert len(record["options"][0]["label"]) == 120
    assert len(record["options"][1]["description"]) == 240


def test_the_value_comes_back_as_data_never_as_an_executed_argument(tmp_path):
    """A shell-looking answer is a string, not something core runs (§9.1)."""

    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="free_text", prompt="which path?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        answer = "$(rm -rf /) && echo pwned; `id`"
        await manager.submit(session_id=session.id, input_id=input_id, value=answer)
        await log.wait_idle(session.id)

        outcome = executor.outcomes[0]
        assert outcome.value == answer
        result = outcome.tool_result()
        assert result["ok"] is True
        assert result["value"] == answer
        # The payload is the value inside a JSON-ish result, not an argv list and
        # not a command string: there is no place a shell could see it.
        assert result["status"] == "submitted"
        assert of_type(log, session.id, "turn.input_resolved")[0].payload["value"] == answer

    asyncio.run(scenario())


def test_the_answer_does_not_satisfy_an_approval(tmp_path):
    """§9.3: answering a question is not approving anything."""

    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="free_text", prompt="type 'yes' to delete?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        await manager.submit(session_id=session.id, input_id=input_id, value="yes, delete it")
        await log.wait_idle(session.id)

        # Nothing in the approval channel moved: no approval event, and the
        # input manager never touches the approval port.
        types = [e.type for e in log.store.read_since(session.id, 0)]
        assert not any(t.startswith("turn.approval") for t in types)
        assert executor.outcomes[0].value == "yes, delete it"

    asyncio.run(scenario())
