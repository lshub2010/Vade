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
from .parts import (
    PartLimits,
    attachment_ids,
    check_limits,
    has_renderable,
    normalize,
    parts_text,
    require_shape,
    resolve_attachments,
)
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
        blobs: Any = None,
        limits: PartLimits | None = None,
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
        self._cancel_clear_hook: Callable[[str], None] | None = None
        self._max_pending = max_pending
        self._sink_queue = sink_queue
        #: RFC-0004 §6.4 step 4: `ui/submit` verifies `file_id`s against the live
        #: blob service. A callable is accepted so the session component can be
        #: installed before (or without) the upload channel; `None` means
        #: attachments are unavailable, not "skip the check".
        self._blobs = blobs
        self.limits = limits or PartLimits()
        self.approval_port: Any = None
        self._closed = False
        #: Bound on the first async call; `status.ModuleStatusPublisher` needs
        #: to know which loop to hop onto when the registry (sync) changes.
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    # ------------------------------------------------------------------ wiring
    def set_executor(self, fn: Callable[[Turn, tuple[dict[str, Any], ...]], Awaitable[Any]]) -> None:
        self._executor = fn

    def set_cancel_hook(self, fn: Callable[[str], None]) -> None:
        self._cancel_hook = fn

    def set_cancel_clear_hook(self, fn: Callable[[str], None]) -> None:
        """Paired with `set_cancel_hook`: forget a turn's cancel flag.

        The queue calls it after the turn's terminal event (or, for a turn
        that was only queued, immediately), so the router's per-turn set does
        not grow for the life of the process.
        """
        self._cancel_clear_hook = fn

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
        self._loop = asyncio.get_running_loop()
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

    def attached_sessions(self) -> tuple[str, ...]:
        """Sessions with at least one live sink (module.status fan-out)."""
        return tuple(session_id for session_id, workers in self._sinks.items() if workers)

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
                cancel_clear_hook=self._cancel_clear_hook,
                max_pending=self._max_pending,
            )
            self._queues[session_id] = q
        return q

    async def submit(
        self,
        session_id: str,
        content: Sequence[Mapping[str, Any]],
        client_msg_id: str,
        *,
        device_id: str | None = None,
    ) -> tuple[Turn, int]:
        """Store a user message and queue its turn (RFC-0004 §6.4, in order).

        The ladder is contractual: each step below is one numbered item of §6.4.
        Reordering two of them changes which `data.field` a client sees, and the
        UI keys its highlighting off that pointer — so this function is not the
        place for "while we are here" fixes.
        """
        # 0. The session must exist before anything else can be answered.
        self._require_session(session_id)

        # 1. Shape: a non-empty array of at most 20 parts, and a client id.
        if not isinstance(client_msg_id, str) or not client_msg_id.strip():
            raise VadeError(
                "INVALID_ARGUMENT", "client_msg_id is required", data={"field": "client_msg_id"}
            )
        blocks = require_shape(content)

        # 2. Idempotency BEFORE validation. A retry of the same submit must
        #    return the original turn even if a TTL expired or the effective
        #    model changed in between — otherwise the retry "fixes" itself into
        #    a different error and the UI shows a phantom failure.
        existing = self.store.find_turn_by_client_msg(session_id, client_msg_id)
        if existing is not None:
            # Idempotent replay of the same submit: same turn, no new queue entry.
            return existing, self._queue(session_id).position(existing.id)

        # 3. Part types, required fields, per-turn text budget and part count.
        parts = normalize(blocks, self.limits)

        # 4. Every file_id: shape, exists, visible, owned, unexpired, scanned.
        records = resolve_attachments(self._blob_service(), parts, device_id=device_id)

        # 5. Static `[attach]` ceilings on declared bytes and pixels. Per-model
        #    caps need RFC-0005 (§9) and are deliberately absent, not forgotten.
        check_limits(parts, self.limits)

        # 6. At least one part a renderer can draw (a pure-image turn passes).
        user_text = parts_text(parts)
        if not has_renderable(parts):
            raise VadeError(
                "INVALID_ARGUMENT",
                "content has no renderable part",
                data={"field": "content"},
            )

        # 7. Persist: turn row, message (+ its parts), pinned blobs, one emit.
        turn = Turn(
            id=new_id(), session_id=session_id, state=TurnState.QUEUED,
            client_msg_id=client_msg_id, user_text=user_text,
        )
        self.store.update_turn(session_id, turn)  # row exists before the event
        message = Message(
            id=new_id(), session_id=session_id, turn_id=turn.id, role="user",
            content=user_text, parts=parts,
        )
        pinned = self._pin(records, device_id)
        try:
            await self.emit(
                session_id, "turn.queued",
                {"turn_id": turn.id, "state": turn.state.value, "client_msg_id": client_msg_id},
                messages=[message],
            )
        except BaseException:
            # A message that never landed must not leave its blobs immortal:
            # pinned blobs are exempt from TTL GC (§4.7), so an orphan here is
            # storage that no future session can free.
            self._unpin(pinned)
            raise
        queue = self._queue(session_id)
        pos = queue.enqueue(turn, parts)
        await self.emit(session_id, "queue.updated", queue.state_payload())
        queue.ensure_worker()
        return turn, pos

    # ------------------------------------------------------------------ blobs
    def _blob_service(self) -> Any:
        """Resolve the blob service (or `None`): callable → `BlobApp` → service."""
        blobs = self._blobs
        if callable(blobs):
            blobs = blobs()
        if blobs is None:
            return None
        return getattr(blobs, "service", blobs)

    def _pin(self, records: Sequence[Any], device_id: str | None) -> list[str]:
        """§4.7: pin in the same breath as the message write."""
        service = self._blob_service()
        ids = attachment_ids(records)
        if service is None or not ids:
            return []
        pin = getattr(service, "pin", None)
        if not callable(pin):
            return []
        pinned: Any = pin(ids, device_id=device_id)
        return [str(item) for item in (pinned or [])]

    def _unpin(self, file_ids: Sequence[str]) -> None:
        service = self._blob_service()
        if service is None or not file_ids:
            return
        unpin = getattr(service, "unpin", None)
        if callable(unpin):
            unpin(list(file_ids))

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
