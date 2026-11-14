"""RFC-0008 A14: an old client survives both new events (§6.4).

The compatibility promise is not "the old client understands waiting_input" —
it is that the old client *keeps working*: it ignores the unknown event types,
advances `last_seq`, and keeps rendering the turn as `running` until the next
state it does know. This test implements that client.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .helpers import build, start_turn, wait_pending

SCHEMA = Path(__file__).resolve().parents[3] / "schema" / "events.json"

#: What a C-06 1.0.0 client knows: seven states, no `turn.input_*` events.
OLD_STATES = {"queued", "waiting_approval", "running", "completed", "failed",
              "interrupted", "cancelled"}
OLD_EVENTS = {
    "session.attached", "turn.queued", "turn.started", "turn.delta", "turn.tool_call",
    "turn.tool_result", "turn.approval_required", "turn.approval_resolved",
    "turn.completed", "turn.failed", "turn.interrupted", "turn.cancelled",
    "queue.updated", "module.status", "turn.state", "session.turn", "session.closed",
    "skill.proposed",
}


class OldClient:
    def __init__(self) -> None:
        self.last_seq = 0
        self.ignored: list[str] = []
        self.rendered = "running"
        self.rendered_history: list[str] = []

    def apply(self, event) -> None:
        assert event.seq > self.last_seq, "seq must advance by one per event"
        if event.type not in OLD_EVENTS:
            # RFC-0006 §9.1 row 6: unknown type -> ignore, but still advance.
            self.ignored.append(event.type)
            self.last_seq = event.seq
            return
        state = event.payload.get("state")
        if state is not None:
            if state in OLD_STATES:
                self.rendered = state
                self.rendered_history.append(state)
            else:
                # A 1.0.0 client that hard-validates drops the state, and the
                # turn keeps rendering as the last state it understood.
                self.ignored.append(f"state:{state}")
        self.last_seq = event.seq


def test_an_old_client_ignores_the_new_events_and_keeps_going(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="confirm", prompt="ok?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        await manager.submit(session_id=session.id, input_id=input_id, value=True)
        await log.wait_idle(session.id)

        events = list(log.store.read_since(session.id, 0))
        client = OldClient()
        for event in events:
            client.apply(event)

        assert client.last_seq == events[-1].seq == len(events)
        assert "turn.input_required" in client.ignored
        assert "turn.input_resolved" in client.ignored
        # It never rendered a state it did not know, and the turn still finished
        # visibly for it.
        assert client.rendered == "completed"
        assert not any(item.startswith("state:waiting_input") for item in client.ignored)

    asyncio.run(scenario())


def test_a_client_that_validates_states_still_never_disconnects(tmp_path):
    """The `state` is unknown, so the strict client drops that one state only."""

    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="confirm", prompt="ok?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        events = list(log.store.read_since(session.id, 0))
        client = OldClient()
        # A client that DOES understand the event type but not the state.
        for event in events:
            if event.type == "turn.input_required":
                state = event.payload["state"]
                assert state not in OLD_STATES
                assert state in json.loads(SCHEMA.read_text(encoding="utf-8"))["turn_states"]
                client.last_seq = event.seq
                continue
            client.apply(event)
        assert client.last_seq == events[-1].seq
        assert client.rendered == "running"
        await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1")
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_schema_and_enum_pin_the_widening():
    data = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert data["version"] == "2.0.0"
    assert "waiting_input" in data["turn_states"]
    assert set(data["events"]["turn.state"]["enum"]) == set(data["turn_states"])
    assert {"turn.input_required", "turn.input_resolved"} <= set(data["events"])
