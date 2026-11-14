"""RFC-0008 A1: `free_text` end to end — event order, payload, tool result."""

from __future__ import annotations

import asyncio

from vade.input import InputConfig, InputManager

from .helpers import AskExecutor, build, events, of_type, start_turn, wait_pending


def test_free_text_round_trip(tmp_path):
    """required -> resolved -> tool_result, with the value preserved verbatim."""

    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="free_text", prompt="Which file should I overwrite?",
                     detail="relative to the repo root")
        session = log.create_session()
        await start_turn(log, session.id)

        input_id = await wait_pending(manager)
        required = of_type(log, session.id, "turn.input_required")[0]
        assert required.payload["state"] == "waiting_input"
        assert required.payload["kind"] == "free_text"
        assert required.payload["detail"] == "relative to the repo root"
        assert required.payload["options"] == []
        assert required.payload["schema"]["type"] == "string"
        assert required.payload["schema"]["multiline"] is True
        assert required.payload["timeout_s"] == 5
        assert required.payload["expires_at"] >= required.payload["created_at"]
        assert required.payload["default"] is None
        assert required.payload["source"]["depth"] == 0
        # The queue really is in waiting_input, not just the event says so.
        assert log.turn_state(session.id, required.payload["turn_id"]) == "waiting_input"

        answer = "config/settings.toml"
        result = await manager.submit(session_id=session.id, input_id=input_id,
                                      value=answer, by="dev-1")
        assert result == {"ok": True, "turn_id": required.payload["turn_id"],
                          "status": "submitted", "value": answer, "duplicate": False,
                          "input_id": input_id}

        await log.wait_idle(session.id)
        resolved = of_type(log, session.id, "turn.input_resolved")
        assert len(resolved) == 1
        assert resolved[0].payload["status"] == "submitted"
        assert resolved[0].payload["value"] == answer
        assert resolved[0].payload["by"] == "dev-1"
        assert resolved[0].payload["state"] == "running"
        # S5 / §5.3: the resolution precedes anything the turn writes afterwards.
        assert resolved[0].seq < max(e.seq for e in events(log, session.id))

        outcome = executor.outcomes[0]
        assert outcome.ok and outcome.value == answer
        # The turn left `waiting_input` and completed.
        loaded = log.store.load(session.id)
        assert [t.state.value for t in loaded.turns] == ["completed"]

    asyncio.run(scenario())


def test_free_text_keeps_newlines_and_does_not_trim_the_value(tmp_path):
    """§4.2: newlines survive; trimming is only used to decide "blank"."""

    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="free_text", prompt="steps?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        answer = "line one\nline two  "
        await manager.submit(session_id=session.id, input_id=input_id, value=answer)
        await log.wait_idle(session.id)
        assert executor.outcomes[0].value == answer
        assert of_type(log, session.id, "turn.input_resolved")[0].payload["value"] == answer

    asyncio.run(scenario())


def test_per_request_max_length_is_clamped_by_the_hard_ceiling(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path, config=InputConfig(max_free_text_chars=10))
        executor.ask(kind="free_text", prompt="short", max_length=99)
        session = log.create_session()
        await start_turn(log, session.id)
        await wait_pending(manager)
        required = of_type(log, session.id, "turn.input_required")[0]
        assert required.payload["schema"]["max_length"] == 10

    asyncio.run(scenario())


def test_unanswered_free_text_is_cancelled_not_empty(tmp_path):
    """§8.1: the model must never receive `""` as an answer."""

    async def scenario():
        log, manager, executor = build(tmp_path, config=InputConfig(
            min_timeout_s=1, default_timeout_s=1, max_timeout_s=1))
        executor.ask(kind="free_text", prompt="anything?")
        session = log.create_session()
        await start_turn(log, session.id)
        await wait_pending(manager)
        await log.wait_idle(session.id, )

        outcome = executor.outcomes[0]
        assert outcome.ok is False
        assert outcome.code == "CANCELLED"
        assert outcome.value is None and outcome.message
        assert outcome.status == "timeout"

    asyncio.run(scenario())
