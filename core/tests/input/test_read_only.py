"""RFC-0008 A12: both input methods are refused on an explicitly read-only session."""

from __future__ import annotations

import asyncio

from vade.session.rpc import SessionRPC

from .helpers import build, start_turn, wait_pending


class Conn:
    def __init__(self, *, read_only: bool = False, device_id: str = "dev-1") -> None:
        self.connection_id = f"conn-{device_id}"
        self.device_id = device_id
        self.read_only = read_only


def test_read_only_session_cannot_answer_or_cancel(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        rpc = SessionRPC(log, input_port=lambda: manager)
        executor.ask(kind="confirm", prompt="?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        readonly = Conn(read_only=True)
        denied = await rpc.dispatch(readonly, "turn/input",
                                    {"session_id": session.id, "input_id": input_id,
                                     "value": True})
        assert getattr(denied, "code", None) == "READ_ONLY_SESSION"
        denied = await rpc.dispatch(readonly, "turn/input.cancel",
                                    {"session_id": session.id, "input_id": input_id})
        assert getattr(denied, "code", None) == "READ_ONLY_SESSION"
        # Refused means untouched: the request is still open.
        assert manager.pending_count == 1

        # Every write is gated, including `ui/interrupt`: a read-only session
        # must not mutate a turn just because the id happens not to exist.
        other = await rpc.dispatch(readonly, "ui/interrupt",
                                   {"session_id": session.id, "turn_id": "nope"})
        assert getattr(other, "code", None) == "READ_ONLY_SESSION"

        # A writable connection answers normally through the same handler.
        result = await rpc.dispatch(Conn(), "turn/input",
                                    {"session_id": session.id, "input_id": input_id,
                                     "value": True})
        assert result["ok"] is True and result["status"] == "submitted"
        assert result["duplicate"] is False and result["value"] is True
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_cancel_is_reachable_and_idempotent_over_rpc(tmp_path):
    async def scenario():
        log, manager, executor = build(tmp_path)
        rpc = SessionRPC(log, input_port=lambda: manager)
        executor.ask(kind="confirm", prompt="?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)

        first = await rpc.dispatch(Conn(), "turn/input.cancel",
                                   {"session_id": session.id, "input_id": input_id,
                                    "reason": "user closed the dialog"})
        assert first == {"ok": True, "turn_id": first["turn_id"], "status": "cancelled"}
        second = await rpc.dispatch(Conn(), "turn/input.cancel",
                                    {"session_id": session.id, "input_id": input_id})
        assert second["status"] == "cancelled"
        await log.wait_idle(session.id)

    asyncio.run(scenario())


def test_missing_ids_and_unknown_id_are_reported(tmp_path):
    async def scenario():
        log, manager, _executor = build(tmp_path)
        rpc = SessionRPC(log, input_port=lambda: manager)
        session = log.create_session()
        bad = await rpc.dispatch(Conn(), "turn/input", {"session_id": session.id})
        assert getattr(bad, "code", None) == "INVALID_ARGUMENT"
        missing = await rpc.dispatch(Conn(), "turn/input",
                                     {"session_id": session.id, "input_id": "01JUNK",
                                      "value": "x"})
        assert getattr(missing, "code", None) == "NOT_FOUND"

    asyncio.run(scenario())
