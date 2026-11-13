"""SessionLog: the single source of truth for one core process.

Every outbound event goes through `emit()`, which (1) allocates the next seq
on the store, (2) persists the event and any messages in ONE transaction, then
(3) queues it for attached sinks. Persist-before-broadcast is not optional: a
reconnect replays from `events`, so broadcasting first means a crash leaves a
hole the client can never fill.

Sinks are decoupled through a bounded per-connection queue. A slow or dead UI
never blocks a turn — the queue fills, the connection is dropped, the turn goes
on.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import VadeError
from .models import Event, Message, Session, Turn, TurnState, new_id, now_ms
from .queue import TurnQueue
from .replay import ReplayBuffer
from .seq import SeqAllocator
from .store import SessionStore

__all__ = ["SessionLog", "SinkWorker", "AttachResult", "EventSink"]

EventSink = Any  # duck-typed: async send(dict) -> None


class SinkWorker:
    """Bounded outbound queue + writer task for one attached connection."""

    def __init__(self, sink: EventSink, *, maxsize: int = 256, on_drop: Callable[[str], None] | None = None) -> None:
        self.sink = sink
        self.sink_id = new_id()
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task[None] | None = None
        self._on_drop = on_drop
        self.dropped = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._writer(), name=f"sink-{self.sink_id[:8]}")

    def offer(self, event: dict[str, Any]) -> bool:
        if self.dropped:
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.drop()
            return False

    def drop(self) -> None:
        if self.dropped:
            return
        self.dropped = True
        if self._task is not None:
            self._task.cancel()
        if self._on_drop is not None:
            try:
                self._on_drop(self.sink_id)
            except Exception:  # pragma: no cover - never let a hook break the turn
                pass

    async def _writer(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            try:
                await self.sink.send(item)
            except Exception:
                self.drop()
                return

    async def stop(self) -> None:
        if self._task is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                self._task.cancel()
            self._task = None


@dataclass(frozen=True, slots=True)
class AttachResult:
    session_id: str
    from_seq: int
    snapshot: dict[str, Any] | None
    replay: tuple[Event, ...] = ()
    sink_id: str = ""


class SessionLog:
    def __init__(
        self,
        store: SessionStore,
        *,
        replay: ReplayBuffer | None = None,
        clock: Callable[[], float] = time.time,
        max_pending: int = 100,
        sink_queue: int = 256,
    ) -> None:
        self.store = store
        self.replay = replay or ReplayBuffer()
        self._clock = clock
        self._seq = SeqAllocator(store)
        self._queues: dict[str, TurnQueue] = {}
        self._sinks: dict[str, dict[str, SinkWorker]] = {}
        self._emit_locks: dict[str, asyncio.Lock] = {}
        self._executor: Callable[[Turn, tuple[dict[str, Any], ...]], Awaitable[Any]] | None = None
        self._cancel_hook: Callable[[str], None] | None = None
        self._max_pending = max_pending
        self._sink_queue = sink_queue
        self.approval_port: Any = None
        self._closed = False

    # ------------------------------------------------------------------ wiring
    def set_executor(self, fn: Callable[[Turn, tuple[dict[str, Any], ...]], Awaitable[Any]]) -> None:
        self._executor = fn

    def set_cancel_hook(self, fn: Callable[[str], None]) -> None:
        self._cancel_hook = fn

    # ------------------------------------------------------------------ sessions
    def create_session(self, *, session_id: str | None = None, title: str | None = None) -> Session:
        sid = session_id or new_id()
        session = Session(id=sid, created_at=now_ms(), title=title)
        self.store.create_session(session)
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self.store.load(session_id)

    def _require_session(self, session_id: str) -> Session:
        session = self.store.load(session_id)
        if session is None:
            raise VadeError("NOT_FOUND", f"session {session_id!r} does not exist")
        return session

    def _emit_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._emit_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._emit_locks[session_id] = lock
        return lock

    # ------------------------------------------------------------------ events
    async def emit(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        messages: Sequence[Message] = (),
    ) -> Event:
        """Allocate seq, persist (messages + event), replay-add, then broadcast."""
        async with self._emit_lock(session_id):
            seq = self._seq.next(session_id)
            event = Event(
                session_id=session_id, seq=seq, type=event_type,
                payload=dict(payload or {}), ts=int(self._clock()),
            )
            stamped = [replace(m, seq=seq) for m in messages]
            self.store.append(session_id, stamped, [event])
            self.replay.add(event)
            await self._broadcast(session_id, event)
            return event

    async def _broadcast(self, session_id: str, event: Event) -> None:
        for worker in list(self._sinks.get(session_id, {}).values()):
            worker.offer(event.to_wire())

    # ------------------------------------------------------------------ attach
    async def attach(self, session_id: str | None, last_seq: int | None, sink: EventSink) -> AttachResult:
        if session_id is None:
            session = self.create_session()
            session_id = session.id
        else:
            session = self._require_session(session_id)

        if last_seq is not None and last_seq >= 0 and self.replay.covers(session_id, last_seq):
            replay_events = tuple(self.store.read_since(session_id, last_seq))
            worker = self._register_sink(session_id, sink)
            # Replay first, on the same per-connection queue as live events, so
            # the sink can never observe `session.attached` before the backlog.
            for replayed in replay_events:
                worker.offer(replayed.to_wire())
            event = await self.emit(
                session_id,
                "session.attached",
                {
                    "session_id": session_id,
                    "from_seq": last_seq + 1,
                    "state": "open",
                    "snapshot": False,
                    "replay_count": len(replay_events),
                },
            )
            return AttachResult(session_id, event.seq, None, replay_events, worker.sink_id)

        # Not covered (or a fresh client): snapshot is the only honest answer.
        snapshot = self.snapshot(session_id)
        worker = self._register_sink(session_id, sink)
        event = await self.emit(
            session_id,
            "session.attached",
            {
                "session_id": session_id,
                "from_seq": snapshot["from_seq"],
                "state": snapshot["session"]["state"],
                "snapshot": True,
            },
        )
        # `from_seq` is the snapshot point (rpc.json): the client resumes live
        # from the attached event, which is seq+1, and dedupes by seq.
        return AttachResult(session_id, snapshot["from_seq"], snapshot, (), worker.sink_id)

    def _register_sink(self, session_id: str, sink: EventSink) -> SinkWorker:
        worker = SinkWorker(
            sink,
            maxsize=self._sink_queue,
            on_drop=lambda sink_id: self._sinks.get(session_id, {}).pop(sink_id, None),
        )
        worker.start()
        self._sinks.setdefault(session_id, {})[worker.sink_id] = worker
        return worker

    async def detach(self, session_id: str, sink_id: str | None = None) -> bool:
        workers = self._sinks.get(session_id, {})
        if sink_id is None:
            removed = list(workers.values())
            workers.clear()
        else:
            worker = workers.pop(sink_id, None)
            removed = [worker] if worker else []
        for worker in removed:
            await worker.stop()
        return bool(removed)

    def attached_count(self, session_id: str) -> int:
        return len(self._sinks.get(session_id, {}))

    # ------------------------------------------------------------------ snapshot
    def snapshot(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        queue = self._queues.get(session_id)
        pending: list[Any] = []
        if self.approval_port is not None and hasattr(self.approval_port, "pending"):
            try:
                pending = list(self.approval_port.pending(session_id))
            except Exception:  # pragma: no cover - approval port is C5a's
                pending = []
        return {
            "from_seq": self.store.last_seq(session_id),
            "session": {
                "id": session.id,
                "created_at": session.created_at,
                "title": session.title,
                "state": session.state,
                "seq_counter": session.seq_counter,
            },
            "messages": session.message_tree(),
            "queue": queue.state() if queue is not None else {"running": None, "pending": []},
            "pending_approvals": pending,
        }

    # ------------------------------------------------------------------ turns
    def _queue(self, session_id: str) -> TurnQueue:
        q = self._queues.get(session_id)
        if q is None:
            if self._executor is None:
                raise VadeError("UNAVAILABLE", "no turn executor installed")
            q = TurnQueue(
                session_id,
                executor=self._executor,
                emit=self.emit,
                save_turn=self.store.update_turn,
                cancel_hook=self._cancel_hook,
                max_pending=self._max_pending,
            )
            self._queues[session_id] = q
        return q

    async def submit(
        self,
        session_id: str,
        content: Sequence[Mapping[str, Any]],
        client_msg_id: str,
    ) -> tuple[Turn, int]:
        self._require_session(session_id)
        if not client_msg_id:
            raise VadeError("INVALID_ARGUMENT", "client_msg_id is required")
        blocks = tuple(dict(b) for b in content)
        user_text = _text_of(blocks)
        if not user_text.strip():
            raise VadeError("INVALID_ARGUMENT", "content has no text block")

        existing = self.store.find_turn_by_client_msg(session_id, client_msg_id)
        if existing is not None:
            # Idempotent replay of the same submit: same turn, no new queue entry.
            return existing, self._queue(session_id).position(existing.id)

        turn = Turn(
            id=new_id(), session_id=session_id, state=TurnState.QUEUED,
            client_msg_id=client_msg_id, user_text=user_text,
        )
        self.store.update_turn(session_id, turn)  # row exists before the event
        message = Message(
            id=new_id(), session_id=session_id, turn_id=turn.id, role="user", content=user_text,
        )
        await self.emit(
            session_id, "turn.queued",
            {"turn_id": turn.id, "state": turn.state.value, "client_msg_id": client_msg_id},
            messages=[message],
        )
        queue = self._queue(session_id)
        pos = queue.enqueue(turn, blocks)
        await self.emit(session_id, "queue.updated", queue.state_payload())
        queue.ensure_worker()
        return turn, pos

    async def interrupt(self, session_id: str, turn_id: str, *, by: str = "user") -> bool:
        self._require_session(session_id)
        return await self._queue(session_id).interrupt(turn_id, by=by)

    async def cancel(self, session_id: str, turn_id: str, *, by: str = "user") -> bool:
        self._require_session(session_id)
        return await self._queue(session_id).cancel(turn_id, by=by)

    def queue_state(self, session_id: str) -> dict[str, Any]:
        q = self._queues.get(session_id)
        return q.state() if q is not None else {"running": None, "pending": []}

    async def mark_state(self, session_id: str, turn_id: str, state: TurnState) -> None:
        q = self._queues.get(session_id)
        if q is not None:
            await q.mark_state(turn_id, state)

    async def wait_idle(self, session_id: str, timeout: float = 5.0) -> None:
        q = self._queues.get(session_id)
        if q is not None:
            await asyncio.wait_for(q.drain(), timeout)

    async def close(self) -> None:
        self._closed = True
        for q in list(self._queues.values()):
            await q.shutdown()
        for session_id in list(self._sinks):
            await self.detach(session_id)
        close = getattr(self.store, "close", None)
        if callable(close):
            close()


def _text_of(blocks: Sequence[Mapping[str, Any]]) -> str:
    parts = [str(b.get("text", "")) for b in blocks if b.get("type", "text") == "text"]
    return "\n".join(p for p in parts if p)
