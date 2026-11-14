"""RFC-0008 A4: a timeout is a cancellation — the default value is NEVER applied."""

from __future__ import annotations

import asyncio

from vade.input import InputConfig

from .helpers import build, of_type, start_turn, wait_pending


def test_timeout_yields_cancelled_and_not_the_default(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path, config=InputConfig(
            min_timeout_s=1, default_timeout_s=1, max_timeout_s=1))
        # The default is a UI prefill. If any code path "helpfully" answered with
        # it on timeout, this sentinel would show up in the tool result.
        executor.ask(kind="free_text", prompt="filename?", default="SENTINEL")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        required = of_type(log, session.id, "turn.input_required")[0]
        assert required.payload["default"] == "SENTINEL"

        await log.wait_idle(session.id)

        resolved = of_type(log, session.id, "turn.input_resolved")
        assert len(resolved) == 1
        payload = resolved[0].payload
        assert payload["status"] == "timeout"
        assert payload["by"] == "system"
        assert payload["value"] is None
        assert payload["state"] == "running"
        assert payload["duration_ms"] >= 900  # ~the 1s deadline, monotonic

        outcome = executor.outcomes[0]
        assert outcome.ok is False
        assert outcome.code == "CANCELLED"
        assert outcome.value is None
        assert "SENTINEL" not in str(outcome.tool_result())
        assert input_id in outcome.message
        # The turn continued and completed after the CANCELLED tool result.
        assert [t.state.value for t in log.store.load(session.id).turns] == ["completed"]

    asyncio.run(scenario())


def test_a_commit_at_the_deadline_beats_the_timeout(tmp_path):
    """`approval.py`'s shield: a real answer in the deadline tick must win."""

    async def scenario():
        log, manager, executor = build(tmp_path, config=InputConfig(
            min_timeout_s=1, default_timeout_s=1, max_timeout_s=1))
        executor.ask(kind="confirm", prompt="continue?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        # Answer with no delay: the manager is inside `wait_for`, and a naive
        # implementation cancels the future here and reports a timeout instead.
        result = await manager.submit(session_id=session.id, input_id=input_id, value=True)
        assert result["status"] == "submitted"
        await log.wait_idle(session.id)
        assert executor.outcomes[0].ok is True
        assert of_type(log, session.id, "turn.input_resolved")[0].payload["status"] == "submitted"

    asyncio.run(scenario())
