"""RFC-0008 A11: with no UI attached the request is cancelled at once, not queued."""

from __future__ import annotations

import asyncio
import time

from .helpers import build, events, start_turn


def test_no_ui_cancels_immediately_and_emits_nothing(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path, ui=0)
        executor.ask(kind="free_text", prompt="anyone there?")
        session = log.create_session()
        started = time.monotonic()
        await start_turn(log, session.id)
        await log.wait_idle(session.id)
        elapsed = time.monotonic() - started

        outcome = executor.outcomes[0]
        assert outcome.ok is False
        assert outcome.status == "cancelled" and outcome.code == "CANCELLED"
        assert outcome.reason == "no_ui"
        # No dialog was ever shown, so neither event exists: a lone
        # `input_resolved` would close a dialog that never opened (§8.2).
        assert [e.type for e in events(log, session.id) if e.type.startswith("turn.input")] == []
        assert manager.pending_count == 0
        # It must not wait for the deadline (5s here) — the whole turn is instant.
        assert elapsed < 0.5

    asyncio.run(scenario())


def test_subagent_requests_are_refused_by_default(tmp_path):
    """§9.5: `allow_subagents=false` must not pop a dialog on the parent's screen."""
    async def scenario():
        log, manager, executor = build(tmp_path, ui=1)
        executor.ask(kind="confirm", prompt="child asks?", source={"depth": 1, "tool": "ask_user"})
        session = log.create_session()
        await start_turn(log, session.id)
        await log.wait_idle(session.id)

        outcome = executor.outcomes[0]
        assert outcome.ok is False and outcome.reason == "no_ui"
        assert manager.pending_count == 0
        assert [e.type for e in events(log, session.id) if e.type.startswith("turn.input")] == []

    asyncio.run(scenario())


def test_subagent_requests_can_be_enabled(tmp_path):
    from vade.input import InputConfig

    async def scenario():
        log, manager, executor = build(tmp_path, ui=1, config=InputConfig(allow_subagents=True))
        executor.ask(kind="confirm", prompt="child asks?", source={"depth": 1})
        session = log.create_session()
        await start_turn(log, session.id)
        assert await _wait(manager)
        [pending] = manager.pending_requests()
        await manager.submit(session_id=session.id, input_id=pending["input_id"], value=True)
        await log.wait_idle(session.id)
        assert executor.outcomes[0].ok is True

    asyncio.run(scenario())


async def _wait(manager, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if manager.pending_count:
            return True
        await asyncio.sleep(0.005)
    return False
