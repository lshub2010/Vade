"""`session/delete` vs. a concurrent submit: `turn_active` or a clean delete (P1-3).

Two connections — two `SessionRPC`s sharing one `SessionLog` — drive
`ui/submit` and `session/delete` for the same session.

The guard used to read `queue.state()`, which only reports turns that already
have a `QueueItem`. A `submit` that had taken a pending slot (`claim`) but had
not enqueued yet was therefore invisible: delete removed the session under it,
and the submit then allocated a seq against rows that no longer existed
(`next_seq` → `NOT_FOUND`) — or, on the other interleaving, announced
`turn.queued` for a session that was being deleted. The fix asks
`queue.is_idle()` (which counts claims), folds `enqueue` + `queue.updated` into
one guarded step, and deletes through `queue.shutdown()` so nothing can start a
turn into tables that are about to vanish.

There is no stub in this file: real queues, real SQLite, real asyncio
interleaving between two connections.
"""

from __future__ import annotations

import asyncio
from typing import Any

from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore
from vade.transport.connection import RpcError
from .helpers import GateExecutor, wait_until


class Conn:
    """A C2 connection stand-in: only the identity fields the RPC reads."""

    def __init__(self, connection_id: str, device_id: str) -> None:
        self.connection_id = connection_id
        self.device_id = device_id


class FinishingExecutor:
    """Completes after a few loop ticks, so a delete can land mid-turn."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    async def __call__(self, turn: Any, content: Any) -> Any:
        from vade.loop.engine import TurnOutcome

        self.current += 1
        self.peak = max(self.peak, self.current)
        try:
            for _ in range(3):
                await asyncio.sleep(0)
        finally:
            self.current -= 1
        return TurnOutcome(assistant_text="ok", stopped_reason="stop")


def _log(tmp_path, executor) -> SessionLog:
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    return log


def _row_counts(log: SessionLog, session_id: str) -> dict[str, int]:
    conn = log.store._conn  # type: ignore[attr-defined]
    counts: dict[str, int] = {}
    for table in ("sessions", "turns", "messages", "events"):
        key = "id" if table == "sessions" else "session_id"
        counts[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {key}=?", (session_id,),
        ).fetchone()[0]
    return counts


async def _call(rpc: SessionRPC, conn: Any, method: str, params: dict[str, Any]) -> Any:
    return await rpc.dispatch(conn, method, params)


def test_delete_refuses_while_a_submit_holds_a_slot_claim(tmp_path):
    """The window `state()` cannot see: a claimed slot with no `QueueItem` yet.

    This is the exact shape of the bug — the claim is what makes the capacity
    decision authoritative, and it was invisible to the delete guard.
    """

    async def scenario():
        log = _log(tmp_path, GateExecutor())
        rpc = SessionRPC(log)
        conn = Conn("conn-1", "dev-01")
        session = log.create_session()
        queue = log._queue(session.id)
        claim = queue.claim()
        assert claim is not None

        result = await _call(rpc, conn, "session/delete", {"session_id": session.id})
        assert isinstance(result, RpcError), result
        assert result.code == "INVALID_ARGUMENT"
        assert result.data and result.data["reason"] == "turn_active"
        assert log.store.load(session.id) is not None, "the guard must not delete first"
        assert queue._stopped is False

        # The claim goes back: the session is idle again and the delete is clean.
        assert queue.release(claim) is True
        assert await _call(rpc, conn, "session/delete", {"session_id": session.id}) == {"ok": True}
        assert log.store.load(session.id) is None
        assert _row_counts(log, session.id) == {
            "sessions": 0, "turns": 0, "messages": 0, "events": 0,
        }
        assert session.id not in log._queues
        assert queue._stopped is True, "delete must stop the queue, not just drop it"
        await log.close()

    asyncio.run(scenario())


def test_delete_refuses_a_queued_turn_and_succeeds_once_it_ends(tmp_path):
    """The ordinary guard still works, and it does not leak a false `turn_active`."""

    async def scenario():
        gate = GateExecutor()
        log = _log(tmp_path, gate)
        rpc = SessionRPC(log)
        conn = Conn("conn-1", "dev-01")
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id)

        result = await _call(rpc, conn, "session/delete", {"session_id": session.id})
        assert isinstance(result, RpcError) and result.data["reason"] == "turn_active"

        gate.gate.set()
        await log.wait_idle(session.id, timeout=1.0)
        assert await _call(rpc, conn, "session/delete", {"session_id": session.id}) == {"ok": True}
        assert _row_counts(log, session.id) == {
            "sessions": 0, "turns": 0, "messages": 0, "events": 0,
        }
        await log.close()

    asyncio.run(scenario())


async def _race_once(log: SessionLog, sub: SessionRPC, deleter: SessionRPC,
                     conn_a: Conn, conn_b: Conn, session_id: str, *,
                     submit_first: bool) -> tuple[Any, Any]:
    async def submit():
        return await _call(sub, conn_a, "ui/submit", {
            "session_id": session_id,
            "content": [{"type": "text", "text": "race"}],
            "client_msg_id": f"msg-{session_id}",
        })

    async def delete():
        return await _call(deleter, conn_b, "session/delete", {"session_id": session_id})

    first, second = (submit, delete) if submit_first else (delete, submit)
    lead = asyncio.create_task(first())
    await asyncio.sleep(0)  # let the leading call reach its first suspension
    trail = asyncio.create_task(second())
    left, right = await asyncio.gather(lead, trail)
    # `gather` preserves the argument order, not the role: hand back
    # (submit, delete) whichever ran first.
    return (left, right) if submit_first else (right, left)


def test_concurrent_submit_and_delete_only_yield_turn_active_or_a_clean_delete(tmp_path):
    """Both orders, on two connections, with the turn finishing underneath.

    Every round must end in one of exactly two states:

      * refused: `turn_active`, the session and its rows untouched, the turn
        then finishing normally;
      * or deleted: `{"ok": true}`, and not one row left in any of the four
        tables (a residual turn row for a deleted session is the failure this
        test exists for).

    And nothing may ever surface as `INTERNAL`, on either connection.
    """

    async def scenario():
        probe = FinishingExecutor()
        log = _log(tmp_path, probe)
        submit_rpc = SessionRPC(log)
        delete_rpc = SessionRPC(log)  # a second connection, its own sink map
        conn_a = Conn("conn-a", "dev-01")
        conn_b = Conn("conn-b", "dev-02")
        loop = asyncio.get_running_loop()
        problems: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, ctx: problems.append(dict(ctx)))

        outcomes = {"deleted": 0, "refused": 0}
        for round_index in range(12):
            session = log.create_session()
            queue = log._queue(session.id)
            submit_result, delete_result = await _race_once(
                log, submit_rpc, delete_rpc, conn_a, conn_b, session.id,
                submit_first=bool(round_index % 2),
            )

            # ---- the delete: exactly one of the two allowed answers
            if isinstance(delete_result, RpcError):
                assert delete_result.code == "INVALID_ARGUMENT", delete_result.code
                assert delete_result.data and delete_result.data["reason"] == "turn_active"
                outcomes["refused"] += 1
                assert log.store.load(session.id) is not None
                assert _row_counts(log, session.id)["sessions"] == 1
            else:
                assert delete_result == {"ok": True}, delete_result
                outcomes["deleted"] += 1
                assert log.store.load(session.id) is None
                assert _row_counts(log, session.id) == {
                    "sessions": 0, "turns": 0, "messages": 0, "events": 0,
                }, _row_counts(log, session.id)
                assert session.id not in log._queues
                assert queue._stopped is True

            # ---- the submit: landed, or refused because the session is gone
            if isinstance(submit_result, RpcError):
                assert submit_result.code == "NOT_FOUND", submit_result.code
                assert log.store.load(session.id) is None, "a NOT_FOUND submit deleted nothing"
            else:
                assert submit_result.get("turn_id"), submit_result

            # The client is never told `internal` on either path.
            for result in (submit_result, delete_result):
                assert not (isinstance(result, RpcError) and result.code == "INTERNAL")

            # Clean up the surviving session so the next round starts empty.
            if log.store.load(session.id) is not None:
                await log.wait_idle(session.id, timeout=1.0)
                assert await _call(delete_rpc, conn_b, "session/delete",
                                   {"session_id": session.id}) == {"ok": True}
                assert _row_counts(log, session.id)["turns"] == 0

        # Both halves of the contract were really exercised.
        assert outcomes == {"deleted": 6, "refused": 6}, outcomes
        assert probe.peak == 1
        assert problems == []
        await log.close()

    asyncio.run(scenario())
