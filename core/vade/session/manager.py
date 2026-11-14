"""SessionLog: the single source of truth for one core process.

Every outbound event goes through `emit()` (or `emit_batch()`), which (1)
allocates the next seq on the store, (2) persists the event, its messages and —
since RFC-0006 — the turn rows it describes in ONE transaction, then (3) queues
it for attached sinks. Persist-before-broadcast is not optional: a reconnect
replays from `events`, so broadcasting first means a crash leaves a hole the
client can never fill.

`emit_batch` exists because two RFC-0006 flows must commit several rows and
several events atomically: an interrupt (turn row + partial message +
`turn.interrupted`) and a regenerate/edit-resubmit (old pointer + new turn/new
message + `turn.superseded`/`message.edited` + `turn.queued`).

Sinks are decoupled through a bounded per-connection queue. A slow or dead UI
never blocks a turn — the queue fills, the connection is dropped, the turn goes
on.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import VadeError
from .limits import check_content_limits, content_text
from .models import (
    TERMINAL_STATES, Event, Message, Session, Turn, TurnState, new_id, now_ms,
)
from .parts import (
    PartLimits,
    apply_attachment_metadata,
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

#: C-06 rule 4: payload-only events. After a terminal event for a turn, these
#: are dropped on arrival (RFC-0006 §4.2 tombstone) instead of being appended
#: after `turn.interrupted` — a late delta must not reopen a closed turn.
PAYLOAD_ONLY_EVENTS = frozenset({"turn.delta", "turn.tool_call", "turn.tool_result"})
TERMINAL_EVENT_TYPES = frozenset(
    {"turn.completed", "turn.failed", "turn.interrupted", "turn.cancelled"}
)
#: Terminal turns kept for the tombstone check. Bounded so the set cannot grow
#: one id per turn for the life of the process.
_TOMBSTONE_LIMIT = 1024

#: RFC-0006 §6.4 reasons carried on `turn.queued` / `turn.superseded`.
REASON_SUBMIT = "submit"
REASON_REGENERATE = "regenerate"
REASON_EDIT = "edit_resubmit"


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
        #: RFC-0008: the ask-user channel, resolved lazily because the `input`
        #: component installs before `session` (alphabetical scan). `None` means
        #: no channel: the snapshot simply has no `pending_inputs` rows.
        self.input_port: Any = None
        #: RFC-0007: `TaskRegistry`, installed by `vade.tasks.install()`. Two
        #: things read it — `snapshot()` (tasks are reconnect truth) and
        #: `interrupt()` (a stopped turn cancels its subagents). `None` means
        #: the tasks component is absent, and `snapshot` then omits the key
        #: entirely so a client can tell "no tasks" from "core predates tasks".
        self.tasks: Any = None
        self._closed = False
        #: RFC-0006 §4.2: the text already streamed for a live turn. The queue
        #: reads it through `partial_text()` when it finalizes an interrupt, so
        #: the partial message is the *same* text the client was rendered.
        self._partial: dict[str, list[str]] = {}
        #: Turns whose terminal event has been persisted: late `turn.delta` /
        #: `turn.tool_*` writes are dropped instead of appended after it.
        self._terminal: OrderedDict[str, None] = OrderedDict()
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
        turns: Sequence[Turn] = (),
        message_updates: Sequence[tuple[str, str | None]] = (),
        coalesce: bool = False,
    ) -> Event | None:
        """Allocate seq, persist (turns + messages + event), replay, broadcast.

        `coalesce=True` is RFC-0007 §5.5 R5: the row is persisted and broadcast
        like any other, but it does not count as a continuity witness for
        `ui/attach(last_seq=...)` — only droppable, absolute-value updates
        (`task.progress`) may pass it.
        """
        events = await self.emit_batch(
            session_id, [(event_type, payload)], messages=messages, turns=turns,
            message_updates=message_updates, coalesce=coalesce,
        )
        return events[0] if events else None

    async def emit_batch(
        self,
        session_id: str,
        events: Sequence[tuple[str, Mapping[str, Any] | None]],
        *,
        messages: Sequence[Message] = (),
        turns: Sequence[Turn] = (),
        message_updates: Sequence[tuple[str, str | None]] = (),
        coalesce: bool = False,
    ) -> list[Event]:
        """Persist several events (strict seq order) in the SAME transaction.

        RFC-0006 §6.4: an edit must land `message.edited` → `turn.superseded` →
        `turn.queued` before any of them is broadcast, otherwise an attached UI
        can observe the new turn before it knows the old one was replaced.
        `coalesce` stamps every event of the batch (RFC-0007 §5.5 R5); it is
        only meaningful for a lone droppable update, which is why
        `task.progress` never batches with anything else.
        """
        self._loop = asyncio.get_running_loop()
        kept: list[tuple[str, dict[str, Any]]] = []
        for event_type, payload in events:
            body = dict(payload or {})
            if event_type in PAYLOAD_ONLY_EVENTS and body.get("turn_id") in self._terminal:
                continue  # tombstone: the turn is closed, drop the late write
            kept.append((event_type, body))
        if not kept:
            # RFC-0006 §4.2: the tombstone drops the *write*, not just the
            # event. A late `turn.tool_call`/`turn.delta` may carry the messages
            # it meant to persist; appending them with no event to anchor them
            # wrote a `seq=0` assistant bubble into a turn that had already
            # closed. Nothing left to announce means nothing left to write.
            return []

        async with self._emit_lock(session_id):
            stamped: list[Event] = []
            for event_type, body in kept:
                stamped.append(Event(
                    session_id=session_id, seq=self._seq.next(session_id),
                    type=event_type, payload=body, ts=int(self._clock()),
                    coalesce=bool(coalesce),
                ))
            anchor = stamped[0].seq if stamped else 0
            stamped_messages = [replace(m, seq=anchor) for m in messages]
            self.store.append(
                session_id, stamped_messages, stamped, turns=tuple(turns),
                message_updates=tuple(message_updates),
            )
            for event in stamped:
                self.replay.add(event)
            self._track(stamped)
            for event in stamped:
                await self._broadcast(session_id, event)
            return stamped

    def _track(self, events: Sequence[Event]) -> None:
        """Maintain the partial accumulator and the terminal tombstone set."""
        for event in events:
            turn_id = event.payload.get("turn_id")
            if not isinstance(turn_id, str):
                continue
            if event.type == "turn.delta":
                text = event.payload.get("text")
                if text:
                    self._partial.setdefault(turn_id, []).append(str(text))
            elif event.type in TERMINAL_EVENT_TYPES:
                self._partial.pop(turn_id, None)
                self._terminal[turn_id] = None
                self._terminal.move_to_end(turn_id)
                while len(self._terminal) > _TOMBSTONE_LIMIT:
                    self._terminal.popitem(last=False)

    def partial_text(self, turn_id: str) -> str:
        """Text already streamed for `turn_id` (RFC-0006 §4.2 partial provider)."""
        return "".join(self._partial.get(turn_id, ()))

    def is_terminal(self, turn_id: str) -> bool:
        return turn_id in self._terminal

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
            assert event is not None
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
        # RFC-0008 §8.2: a reconnecting client that fell outside the replay
        # window only gets this snapshot, so a pending question MUST be here —
        # otherwise the dialog exists on the phone and not on the desktop.
        pending_inputs: list[dict[str, Any]] = []
        if self.input_port is not None and hasattr(self.input_port, "pending"):
            try:
                pending_inputs = list(self.input_port.pending(session_id))
            except Exception:  # pragma: no cover - input port is RFC-0008's
                pending_inputs = []
        snapshot: dict[str, Any] = {
            "from_seq": self.store.last_seq(session_id),
            "session": {
                "id": session.id,
                "created_at": session.created_at,
                "title": session.title,
                "state": session.state,
                "seq_counter": session.seq_counter,
            },
            "messages": session.message_tree(),
            # RFC-0006 §6.1: the version chain is stored, not a client-side
            # "greyed out" set — a refresh has to show the same thing.
            "turns": [turn.to_dict() for turn in session.turns],
            "queue": queue.state() if queue is not None else {"running": None, "pending": []},
            "pending_approvals": pending,
            "pending_inputs": pending_inputs,
        }
        if self.tasks is not None:
            # RFC-0007 §5.6: `attach()` sends a snapshot *instead of* the events
            # that led here, so a task missing from this dict is a task that
            # vanishes on reconnect (FR-8.59). Omitted when the component is
            # absent — a missing key is the documented "not implemented" signal.
            tasks, truncated = self.tasks.snapshot(session_id)
            snapshot["tasks"] = tasks
            snapshot["tasks_truncated"] = bool(truncated)
        return snapshot

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
                pre_terminal_hook=self._input_pre_terminal,
                partial_provider=self.partial_text,
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

        # Text budget: RFC-0006 §8 caps one message at 32,000 code points
        # (unit `codepoints`, not UTF-16 units — 16,500 non-BMP emoji pass).
        # Checked before `normalize` so the refusal carries the exact
        # field-level payload C-05 documents; `normalize` re-applies the same
        # ceiling per part as a second gate.
        check_content_limits(blocks)

        # 3. Part types, required fields, per-turn text budget and part count.
        parts = normalize(blocks, self.limits)

        # 4. Every file_id: shape, exists, visible, owned, unexpired, scanned.
        records = resolve_attachments(self._blob_service(), parts, device_id=device_id)

        # 4b. The blob store owns the metadata: replace the client's declared
        #     mime/name/size/width/height/sha256 with the measured record before
        #     step 5 judges them — otherwise "check the size" checks a number the
        #     client chose. The part type is re-checked against the record here.
        parts = apply_attachment_metadata(parts, records)

        # 5. Static `[attach]` ceilings on the authoritative bytes and pixels.
        #    Per-model caps need RFC-0005 (§9) and are deliberately absent, not
        #    forgotten.
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
        turn_id = new_id()
        message_id = new_id()
        turn = Turn(
            id=turn_id, session_id=session_id, state=TurnState.QUEUED,
            client_msg_id=client_msg_id, user_text=user_text,
            root_turn_id=turn_id, parent_turn_id=None, version=1,
            user_message_id=message_id, user_parts=_parts_of(blocks),
        )
        message = Message(
            id=message_id, session_id=session_id, turn_id=turn.id, role="user",
            content=user_text, parts=parts,
        )
        pinned = self._pin(records, device_id)
        try:
            await self.emit(
                session_id, "turn.queued",
                {
                    "turn_id": turn.id, "state": turn.state.value,
                    "client_msg_id": client_msg_id,
                    "root_turn_id": turn.root_id, "version": turn.version,
                    "parent_turn_id": turn.parent_turn_id,
                    "user_message_id": message_id, "reason": REASON_SUBMIT,
                },
                messages=[message],
                # Turn row + user message + event in one transaction: a crash
                # after `turn.queued` must never lose the message it announced.
                turns=[turn],
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
        stopped = await self._queue(session_id).interrupt(turn_id, by=by)
        if stopped and self.tasks is not None:
            # RFC-0007 §5.7: stopping a turn cancels its subagents, never its
            # jobs. "Stop thinking for me" is not "cancel that download".
            await self.tasks.cancel_turn(session_id, turn_id, by="parent")
        return stopped

    async def cancel(self, session_id: str, turn_id: str, *, by: str = "user") -> bool:
        self._require_session(session_id)
        return await self._queue(session_id).cancel(turn_id, by=by)

    # ------------------------------------------------------------------ versions
    async def regenerate(
        self,
        session_id: str,
        turn_id: str,
        client_msg_id: str,
    ) -> dict[str, Any]:
        """RFC-0006 §6.2: a NEW turn for the same user message, old one superseded."""
        session = self._require_session(session_id)
        _require_client_msg_id(client_msg_id)
        old = self._find_turn(session, turn_id)

        existing = self.store.find_turn_by_client_msg(session_id, client_msg_id)
        if existing is not None:
            if existing.parent_turn_id != turn_id:
                raise VadeError(
                    "INVALID_ARGUMENT",
                    "client_msg_id is already used by another turn",
                    data={"reason": "client_msg_id_conflict"},
                )
            return self._version_result(session_id, existing)

        if old.state not in TERMINAL_STATES:
            raise VadeError("INVALID_ARGUMENT", "turn is still active",
                            data={"reason": "turn_active", "turn_id": turn_id})
        head = self._head_of(session, old)
        if old.superseded_by is not None:
            raise VadeError("INVALID_ARGUMENT", "turn has already been superseded",
                            data={"reason": "not_head", "head_turn_id": head.id if head else None})
        tail = session.turns[-1] if session.turns else None
        if tail is None or tail.id != old.id:
            raise VadeError("INVALID_ARGUMENT", "only the tail of the session can be regenerated",
                            data={"reason": "not_tail",
                                  "head_turn_id": tail.id if tail is not None else None})

        user_message = self._user_message_of(session, old)
        if user_message is None:
            raise VadeError("INVALID_ARGUMENT", "turn has no user message to rebuild from")
        if old.user_parts:
            # RFC-0006 §6.2: RFC-0004 parts are not landed, so an attachment
            # reference cannot be rebuilt (`text` is all a turn stores). The
            # old code rebuilt the text and silently dropped the rest; refuse
            # before any row/event is written instead.
            raise VadeError(
                "UNSUPPORTED",
                "turn has non-text content parts that cannot be rebuilt yet",
                data={"capability": "parts", "turn_id": old.id,
                      "parts": list(old.user_parts)},
            )

        new_turn_id = new_id()
        new_turn = Turn(
            id=new_turn_id, session_id=session_id, state=TurnState.QUEUED,
            client_msg_id=client_msg_id, user_text=user_message.content,
            root_turn_id=old.root_id, parent_turn_id=old.id,
            version=(old.version or 1) + 1, user_message_id=user_message.id,
        )
        superseded = replace(old, superseded_by=new_turn_id,
                             superseded_reason=REASON_REGENERATE)
        await self.emit_batch(
            session_id,
            [
                ("turn.superseded", {
                    "turn_id": old.id, "superseded_by": new_turn_id,
                    "reason": REASON_REGENERATE, "root_turn_id": old.root_id,
                    "version": new_turn.version,
                }),
                ("turn.queued", self._queued_payload(new_turn, REASON_REGENERATE)),
            ],
            turns=[superseded, new_turn],
        )
        queue = self._queue(session_id)
        pos = queue.enqueue(new_turn, [{"type": "text", "text": user_message.content}])
        await self.emit(session_id, "queue.updated", queue.state_payload())
        queue.ensure_worker()
        return {
            "turn_id": new_turn_id, "root_turn_id": old.root_id,
            "version": new_turn.version, "supersedes_turn_id": old.id,
            "queue_pos": pos,
        }

    async def edit_resubmit(
        self,
        session_id: str,
        message_id: str,
        content: Sequence[Mapping[str, Any]],
        client_msg_id: str,
    ) -> dict[str, Any]:
        """RFC-0006 §6.3: a NEW user message; the old one is pointed at, never edited."""
        session = self._require_session(session_id)
        _require_client_msg_id(client_msg_id)
        if not isinstance(content, (list, tuple)):
            raise VadeError("INVALID_ARGUMENT", "content must be an array of blocks")
        blocks = tuple(dict(block) for block in content)
        check_content_limits(blocks)
        new_text = _text_of(blocks)
        if not new_text.strip():
            raise VadeError("INVALID_ARGUMENT", "content has no text block")

        message = next((m for m in session.messages if m.id == message_id), None)
        if message is None:
            raise VadeError("NOT_FOUND", f"message {message_id!r} does not exist")
        if message.role != "user":
            raise VadeError("INVALID_ARGUMENT", "only user messages can be edited")
        born_in = self._find_turn(session, message.turn_id) if message.turn_id else None
        if born_in is None:
            raise VadeError("NOT_FOUND", "message does not belong to a turn")
        # RFC-0006 §6.3 + §6.2: `regenerate` reuses the user message but leaves
        # its `turn_id` pointing at v1, so the turn an edit actually replaces is
        # the head of that message's version chain. Reading `message.turn_id`
        # directly made every edit after a regenerate fail `not_head` and the
        # edit capability dead on a version chain.
        old_turn = self._head_of(session, born_in)
        if old_turn is None:  # pragma: no cover - a chain always has one head
            raise VadeError("INVALID_ARGUMENT", "turn has no live head",
                            data={"reason": "not_head", "head_turn_id": None})

        existing = self.store.find_turn_by_client_msg(session_id, client_msg_id)
        if existing is not None:
            # Idempotency keys on the user-message chain (one root per message,
            # unchanged by regenerate/edit), not on the head row: after a
            # successful edit the head IS the turn this id created, and a retry
            # must return it instead of `client_msg_id_conflict`.
            if existing.root_id != born_in.root_id:
                raise VadeError(
                    "INVALID_ARGUMENT",
                    "client_msg_id is already used by another turn",
                    data={"reason": "client_msg_id_conflict"},
                )
            return self._version_result(session_id, existing)

        if message.superseded_by is not None:
            raise VadeError("INVALID_ARGUMENT", "message has already been superseded",
                            data={"reason": "already_superseded",
                                  "superseded_by": message.superseded_by})
        users = [m for m in session.messages if m.role == "user"]
        if not users or users[-1].id != message_id:
            raise VadeError("INVALID_ARGUMENT", "only the last user message can be edited",
                            data={"reason": "not_tail",
                                  "head_turn_id": old_turn.id})
        if old_turn.state not in TERMINAL_STATES:
            raise VadeError("INVALID_ARGUMENT", "turn is still active",
                            data={"reason": "turn_active", "turn_id": old_turn.id})
        if old_turn.superseded_by is not None:
            raise VadeError("INVALID_ARGUMENT", "turn has already been superseded",
                            data={"reason": "not_head", "head_turn_id": old_turn.superseded_by})

        new_message_id = new_id()
        new_turn_id = new_id()
        edition = Message(
            id=new_message_id, session_id=session_id, turn_id=new_turn_id,
            role="user", content=new_text,
        )
        new_turn = Turn(
            id=new_turn_id, session_id=session_id, state=TurnState.QUEUED,
            client_msg_id=client_msg_id, user_text=new_text,
            root_turn_id=old_turn.root_id, parent_turn_id=old_turn.id,
            version=(old_turn.version or 1) + 1, user_message_id=new_message_id,
            user_parts=_parts_of(blocks),
        )
        superseded = replace(old_turn, superseded_by=new_turn_id,
                             superseded_reason=REASON_EDIT)
        await self.emit_batch(
            session_id,
            [
                ("message.edited", {
                    "message_id": message_id, "session_id": session_id,
                    "superseded_by_message_id": new_message_id,
                    "new_turn_id": new_turn_id, "text": new_text,
                    "edited_at": now_ms(),
                }),
                ("turn.superseded", {
                    "turn_id": old_turn.id, "superseded_by": new_turn_id,
                    "reason": REASON_EDIT, "root_turn_id": old_turn.root_id,
                    "version": new_turn.version,
                }),
                ("turn.queued", self._queued_payload(new_turn, REASON_EDIT)),
            ],
            messages=[edition],
            turns=[superseded, new_turn],
            message_updates=[(message_id, new_message_id)],
        )
        queue = self._queue(session_id)
        pos = queue.enqueue(new_turn, blocks)
        await self.emit(session_id, "queue.updated", queue.state_payload())
        queue.ensure_worker()
        return {
            "turn_id": new_turn_id, "message_id": new_message_id,
            "supersedes_turn_id": old_turn.id, "root_turn_id": old_turn.root_id,
            "version": new_turn.version, "queue_pos": pos,
        }

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _queued_payload(turn: Turn, reason: str) -> dict[str, Any]:
        return {
            "turn_id": turn.id, "state": turn.state.value,
            "client_msg_id": turn.client_msg_id,
            "root_turn_id": turn.root_id, "version": turn.version,
            "parent_turn_id": turn.parent_turn_id,
            "user_message_id": turn.user_message_id, "reason": reason,
        }

    def _version_result(self, session_id: str, turn: Turn) -> dict[str, Any]:
        return {
            "turn_id": turn.id, "root_turn_id": turn.root_id,
            "version": turn.version or 1, "supersedes_turn_id": turn.parent_turn_id,
            "queue_pos": self._queue(session_id).position(turn.id),
        }

    @staticmethod
    def _find_turn(session: Session, turn_id: Any) -> Turn:
        if not isinstance(turn_id, str) or not turn_id:
            raise VadeError("INVALID_ARGUMENT", "turn_id is required")
        for turn in session.turns:
            if turn.id == turn_id:
                return turn
        raise VadeError("NOT_FOUND", f"turn {turn_id!r} does not exist")

    @staticmethod
    def _head_of(session: Session, turn: Turn) -> Turn | None:
        for candidate in session.turns:
            if candidate.root_id == turn.root_id and candidate.superseded_by is None:
                return candidate
        return None

    @staticmethod
    def _user_message_of(session: Session, turn: Turn) -> Message | None:
        if turn.user_message_id:
            for message in session.messages:
                if message.id == turn.user_message_id:
                    return message
        for message in session.messages:
            if message.role == "user" and message.turn_id == turn.id:
                return message
        return None

    # ------------------------------------------------------------------ misc
    def queue_state(self, session_id: str) -> dict[str, Any]:
        q = self._queues.get(session_id)
        return q.state() if q is not None else {"running": None, "pending": []}

    async def mark_state(self, session_id: str, turn_id: str, state: TurnState) -> None:
        q = self._queues.get(session_id)
        if q is not None:
            await q.mark_state(turn_id, state)

    def turn_state(self, session_id: str, turn_id: str) -> str | None:
        """Live state of one turn for RFC-0008's single-slot gate (S2/S3/S6).

        The queue knows running and queued turns; a finished turn is no longer in
        it, so the persisted row answers "this turn is terminal" (S6) instead of
        the probe going silent and letting a late request reopen a closed turn.
        """
        q = self._queues.get(session_id)
        if q is not None:
            state = q.state_of(turn_id)
            if state is not None:
                return state
        session = self.store.load(session_id)
        if session is not None:
            for turn in session.turns:
                if turn.id == turn_id:
                    return turn.state.value
        return None

    async def _input_pre_terminal(self, turn_id: str, state: TurnState) -> None:
        """RFC-0008 S5: close pending inputs before the terminal event is written.

        `TurnQueue._finalize` awaits this first, so a dialog is never left open
        behind a `turn.interrupted` (after which C-06 forbids any turn event).
        """
        port: Any = self.input_port
        resolver: Any = getattr(port, "resolve_turn", None)
        if not callable(resolver):
            return
        if state is TurnState.INTERRUPTED:
            reason, target = "turn_interrupted", "interrupted"
        elif state is TurnState.CANCELLED:
            reason, target = "session_closed", "interrupted"
        else:
            # A turn cannot legitimately finish while a question is open; if it
            # does (a crashed executor path), close the dialog rather than strand it.
            reason, target = "superseded", state.value
        await resolver(turn_id, reason=reason, state=target)

    async def wait_idle(self, session_id: str, timeout: float = 5.0) -> None:
        q = self._queues.get(session_id)
        if q is not None:
            await asyncio.wait_for(q.drain(), timeout)

    async def close(self) -> None:
        self._closed = True
        # RFC-0008 §5.3 "等待中会话关闭": close the dialogs while the sinks are
        # still attached, so `turn.input_resolved{session_closed}` reaches them
        # before `session.closed`/the socket goes away.
        closer: Any = getattr(self.input_port, "on_session_end", None)
        if callable(closer):
            for session_id in list(self._queues):
                try:
                    await closer(session_id)
                except Exception:  # pragma: no cover - closing must not raise
                    pass
        for q in list(self._queues.values()):
            await q.shutdown()
        for session_id in list(self._sinks):
            await self.detach(session_id)
        close = getattr(self.store, "close", None)
        if callable(close):
            close()


def _require_client_msg_id(client_msg_id: Any) -> None:
    if not isinstance(client_msg_id, str) or not client_msg_id:
        raise VadeError("INVALID_ARGUMENT", "client_msg_id is required")


def _text_of(blocks: Sequence[Mapping[str, Any]]) -> str:
    return content_text(blocks)


def _parts_of(blocks: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Non-text content part types on a user message (RFC-0006 §6.2).

    Only `text` becomes a stored `Message`, so the *types* of everything else
    are remembered on the turn row: `regenerate` must refuse a turn whose
    attachments it cannot rebuild rather than silently dropping them.
    """
    seen: list[str] = []
    for block in blocks:
        kind = str(block.get("type", "text"))
        if kind != "text" and kind not in seen:
            seen.append(kind)
    return tuple(seen)
