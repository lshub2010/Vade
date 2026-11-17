"""`ui/attach` re-entry: one sink per `(connection, session)`, no double delivery.

The bug this file pins down: a second `ui/attach` for the same connection and
session registered a SECOND `SinkWorker` and overwrote the sink id in
`SessionRPC._sinks`. The first worker stayed in `SessionLog._sinks` for the
life of the process, so

  * every event was written to the same socket twice (`session.attached` of the
    re-attach itself was the cheapest witness), and
  * `ui/detach` only knew about the newest id, leaving the old worker running
    — a per-connection leak that outlived the attachment.

Three invariants, one test each (plus the cross-scope guard):
  ① a re-attach retires the previous worker and leaves exactly one registered;
  ② no seq reaches a connection twice, including the re-attach's own event;
  ③ `ui/detach` takes that session's sink count to zero — and only that pair is
     touched, never another connection or another session on the same one.

Conventions follow `test_module_status.py`: sync test functions wrap
`asyncio.run(scenario())` because `SessionLog.emit` needs a running loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore
from vade.transport.connection import RpcError
from .helpers import wait_until


class RecordingConnection:
    """A C2 connection stand-in: records the event frames written to it."""

    def __init__(self, connection_id: str = "conn-1", device_id: str = "dev-01") -> None:
        self.connection_id = connection_id
        self.device_id = device_id
        self.frames: list[dict[str, Any]] = []

    async def send_event(self, frame: dict[str, Any]) -> None:
        self.frames.append(dict(frame))

    def events(self, type_: str | None = None) -> list[dict[str, Any]]:
        payloads = [frame["params"] for frame in self.frames]
        if type_ is None:
            return payloads
        return [event for event in payloads if event.get("type") == type_]

    def seqs(self) -> list[int]:
        return [int(event["seq"]) for event in self.events()]


class HoldingConnection(RecordingConnection):
    """Records frames but holds the FIRST send until the test releases it.

    That pins a frame in a `SinkWorker`'s outbound queue: the writer is busy,
    so a later event stacks up behind it. It is what distinguishes "drop the
    stale worker" from "stop it": `stop()` flushes the backlog, `drop()`
    discards it and lets the re-attach's replay be the only copy.
    """

    def __init__(self, connection_id: str = "conn-1") -> None:
        super().__init__(connection_id)
        self.gate = asyncio.Event()
        self.blocked = asyncio.Event()
        self._hold_next = 1

    async def send_event(self, frame: dict[str, Any]) -> None:
        self.frames.append(dict(frame))
        if self._hold_next:
            self._hold_next -= 1
            self.blocked.set()
            await self.gate.wait()


class RegisterSpy:
    """Counts `SessionLog._register_sink` calls and keeps the workers it made."""

    def __init__(self, log: SessionLog) -> None:
        self.workers: list[Any] = []
        self._real = log._register_sink
        log._register_sink = self._spy  # type: ignore[method-assign]

    def _spy(self, session_id: str, sink: Any) -> Any:
        worker = self._real(session_id, sink)
        self.workers.append(worker)
        return worker


def _log(tmp_path) -> SessionLog:
    return SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))


async def _attach(rpc: SessionRPC, conn: Any, session_id: str | None,
                  last_seq: int | None = None) -> dict[str, Any]:
    result = await rpc.dispatch(conn, "ui/attach",
                                {"session_id": session_id, "last_seq": last_seq})
    assert not isinstance(result, RpcError), f"{result.code}: {result.message}"
    return result


def test_reattach_retires_the_previous_worker(tmp_path):
    """① One attachment, one worker — the older one must be stopped, not orphaned."""

    async def scenario():
        log = _log(tmp_path)
        rpc = SessionRPC(log)
        conn = RecordingConnection()
        session = log.create_session()
        spy = RegisterSpy(log)

        await _attach(rpc, conn, session.id)
        assert log.attached_count(session.id) == 1
        assert len(spy.workers) == 1
        stale = spy.workers[0]
        assert stale.dropped is False

        second = await _attach(rpc, conn, session.id)

        # The re-attach registers exactly ONE replacement ...
        assert len(spy.workers) == 2
        assert second["session_id"] == session.id
        # ... and it is the only one left: the stale worker unregistered itself
        # and its writer task is gone, so nothing can deliver twice.
        assert log.attached_count(session.id) == 1
        assert list(log._sinks[session.id]) == [spy.workers[1].sink_id]
        assert stale.sink_id not in log._sinks.get(session.id, {})
        assert stale.dropped is True
        assert await wait_until(lambda: stale._task is None or stale._task.done(), 1.0)

        await log.close()

    asyncio.run(scenario())


def test_reattach_delivers_each_seq_once(tmp_path):
    """② A leaked worker shows up as a duplicated `session.attached` seq."""

    async def scenario():
        log = _log(tmp_path)
        rpc = SessionRPC(log)
        conn = RecordingConnection()
        session = log.create_session()

        await _attach(rpc, conn, session.id)
        assert await wait_until(lambda: len(conn.events("session.attached")) == 1, 1.0)
        # Live traffic reaches the first worker; the client records its last seq
        # (what a reconnecting client passes as `last_seq`).
        await log.rename_session(session.id, "renamed", by="test")
        assert await wait_until(lambda: len(conn.frames) == 2, 1.0)
        last_seen = conn.seqs()[-1]

        second = await _attach(rpc, conn, session.id, last_seq=last_seen)
        # Give a leaked worker every chance to write its copy of the event.
        await asyncio.sleep(0.05)

        seqs = conn.seqs()
        assert seqs == sorted(seqs), seqs
        assert len(seqs) == len(set(seqs)), f"duplicate seq on the wire: {seqs}"
        attached = conn.events("session.attached")
        assert len(attached) == 2  # one per attach, never one per worker
        assert attached[-1]["payload"] == {
            "session_id": session.id,
            "from_seq": second["from_seq"],
            "state": "open",
            "snapshot": False,
            "replay_count": 0,
        }
        # Resuming from the last seq the client saw is a pure replay attach:
        # its `from_seq` is exactly the next seq, so ② and ③ share one numbering.
        assert second["from_seq"] == last_seen + 1

        await log.close()

    asyncio.run(scenario())


def test_reattach_does_not_flush_the_stale_workers_backlog(tmp_path):
    """②b A queued frame must come from the replay, not from the dying worker.

    This is the difference between dropping and stopping the stale worker: a
    graceful stop first writes everything already queued, and the re-attach
    writes those same seqs again. `last_seq` is what the client actually saw,
    so exactly one copy may follow it.
    """

    async def scenario():
        log = _log(tmp_path)
        rpc = SessionRPC(log)
        conn = HoldingConnection()
        session = log.create_session()

        await _attach(rpc, conn, session.id)
        assert await wait_until(conn.blocked.is_set, 1.0)  # frame 1 in flight
        assert conn.seqs() == [1]
        # Writer is still busy, so this event parks in the worker's queue and
        # the client has NOT seen it: its own last_seq stays 1.
        await log.rename_session(session.id, "parked", by="test")

        second = await _attach(rpc, conn, session.id, last_seq=1)
        conn.gate.set()  # let a merely-stopped worker drain, if that is what it is
        assert await wait_until(lambda: len(conn.events("session.attached")) == 2, 1.0)
        await asyncio.sleep(0.05)

        seqs = conn.seqs()
        assert seqs == [1, 2, 3], seqs  # replay(2) then attached(3), each once
        assert second["from_seq"] == 3  # the resume point is the attached event's own seq
        assert conn.events("session.updated")[0]["payload"]["title"] == "parked"

        await log.close()

    asyncio.run(scenario())


def test_detach_zeroes_the_sink_count(tmp_path):
    """③ After `ui/detach` the session has no sink and receives nothing more."""

    async def scenario():
        log = _log(tmp_path)
        rpc = SessionRPC(log)
        conn = RecordingConnection()
        session = log.create_session()

        await _attach(rpc, conn, session.id)
        await _attach(rpc, conn, session.id)  # re-entry, then detach: still one to stop
        assert log.attached_count(session.id) == 1

        result = await rpc.dispatch(conn, "ui/detach", {"session_id": session.id})
        assert result == {"ok": True}
        assert log.attached_count(session.id) == 0

        # `ui/detach` is graceful: whatever the writer already had queued is
        # flushed before it stops, so drain that first and only then watch for
        # NEW traffic. One re-entry above must leave exactly one worker to stop.
        await asyncio.sleep(0.05)
        before = len(conn.frames)
        await log.rename_session(session.id, "after detach", by="test")
        await asyncio.sleep(0.05)
        assert len(conn.frames) == before, "a detached session's event still reached the socket"

        # The second detach is the same honest NOT_FOUND as before (§C-05):
        # re-entry must not leave a phantom attachment behind to detach again.
        again = await rpc.dispatch(conn, "ui/detach", {"session_id": session.id})
        assert isinstance(again, RpcError) and again.code == "NOT_FOUND"

        await log.close()

    asyncio.run(scenario())


def test_reattach_only_touches_its_own_connection_and_session(tmp_path):
    """Dedupe is per `(connection, session)` — other viewers keep their sinks."""

    async def scenario():
        log = _log(tmp_path)
        rpc = SessionRPC(log)
        watcher_a = RecordingConnection("conn-a")
        watcher_b = RecordingConnection("conn-b")
        shared = log.create_session()
        side = log.create_session()

        await _attach(rpc, watcher_a, shared.id)
        await _attach(rpc, watcher_b, shared.id)
        await _attach(rpc, watcher_a, side.id)
        assert log.attached_count(shared.id) == 2
        assert log.attached_count(side.id) == 1

        # A re-attaches to the shared session: B's sink and A's OTHER session
        # sink are untouched.
        await _attach(rpc, watcher_a, shared.id)
        assert log.attached_count(shared.id) == 2
        assert log.attached_count(side.id) == 1
        assert await wait_until(lambda: len(watcher_b.events("session.attached")) == 2, 1.0)

        # A failed attach (unknown session) must not disturb what is attached.
        missing = await rpc.dispatch(watcher_a, "ui/attach",
                                     {"session_id": "nope", "last_seq": None})
        assert isinstance(missing, RpcError) and missing.code == "NOT_FOUND"
        assert log.attached_count(shared.id) == 2
        assert log.attached_count(side.id) == 1

        # Each of A's streams still gets exactly its own session's traffic,
        # once: one live event on `side` must not reach the `shared` stream.
        side_before = len(watcher_a.events("session.updated"))
        await log.rename_session(side.id, "side only", by="test")
        assert await wait_until(
            lambda: len(watcher_a.events("session.updated")) == side_before + 1, 1.0)
        await asyncio.sleep(0.05)
        assert len(watcher_a.events("session.updated")) == side_before + 1
        assert len(watcher_b.events("session.updated")) == 0

        await log.close()

    asyncio.run(scenario())
