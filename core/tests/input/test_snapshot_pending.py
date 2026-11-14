"""RFC-0008 A8: a reconnecting client finds the dialog on BOTH paths (§8.2)."""

from __future__ import annotations

import asyncio

from .helpers import build, start_turn, wait_pending


class Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send(self, event: dict) -> None:
        self.events.append(dict(event))


def test_snapshot_carries_pending_inputs(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="single_choice", prompt="which env?",
                     options=[{"id": "dev", "label": "dev"}, {"id": "prod", "label": "prod"}])
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        snapshot = log.snapshot(session.id)
        [row] = snapshot["pending_inputs"]
        assert row["input_id"] == input_id
        assert row["kind"] == "single_choice"
        assert row["prompt"] == "which env?"
        assert [o["id"] for o in row["options"]] == ["dev", "prod"]
        assert row["expires_at"] > row["created_at"]
        # Same row shape the event carries (minus source/default/detail scope).
        event_payload = [e for e in log.store.read_since(session.id, 0)
                         if e.type == "turn.input_required"][0].payload
        for key in ("input_id", "turn_id", "kind", "prompt", "schema", "options",
                    "timeout_s", "expires_at", "created_at"):
            assert row[key] == event_payload[key], key

        # A fresh attach (last_seq=None) is the snapshot path.
        sink = Sink()
        result = await log.attach(session.id, None, sink)
        assert result.snapshot is not None
        assert [r["input_id"] for r in result.snapshot["pending_inputs"]] == [input_id]

        [pending] = manager.pending_requests()
        await manager.submit(session_id=session.id, input_id=pending["input_id"], value="dev")
        await log.wait_idle(session.id)
        # An answered request must not resurrect from the snapshot.
        assert log.snapshot(session.id)["pending_inputs"] == []

    asyncio.run(scenario())


def test_replay_rebuilds_the_same_dialog(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="confirm", prompt="continue?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        sink = Sink()
        result = await log.attach(session.id, 0, sink)  # replay path
        assert result.snapshot is None
        replayed = [e for e in result.replay if e.type == "turn.input_required"]
        assert [e.payload["input_id"] for e in replayed] == [input_id]
        await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1")
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_resolved_requests_are_not_in_a_later_snapshot(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        executor.ask(kind="confirm", prompt="?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1")
        assert log.snapshot(session.id)["pending_inputs"] == []
        assert manager.resolved(input_id).status == "cancelled"
        await log.wait_idle(session.id)

    asyncio.run(scenario())
