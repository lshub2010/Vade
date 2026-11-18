"""SSE stream: one seq, `Last-Event-ID` replay, `: ping` every 15s.

The seq is shared with C3's replay logic — the doc is explicit that the admin
plane must not invent a second numbering (31-C5a §T4). `EventStream` accepts a
`seq_source` so the caller can hand it C3's counter; without one it keeps its
own monotonic integer.

Delivery model (review P0-1). Every subscriber owns a cursor: the id of the last
event it was actually handed. A wake only delivers `id > cursor`, so a burst is
neither truncated (the old code sent `[-50:]`) nor re-sent on the next wake (the
old code replayed the tail on *every* wake: 3 publishes produced 8 frames).

The ring buffer is finite, so a slow subscriber can fall behind far enough that
the events it needs are gone. That is a hole, and a silent one is worse than a
loud one: the stream emits an explicit `type=resync` frame (no `id:` line, so
`Last-Event-ID` is not moved past events the client never saw) and then replays
the whole current buffer, letting the client rebuild instead of keeping the gap.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from typing import Any, AsyncIterator, Callable, Mapping

__all__ = ["EventStream", "RESYNC"]

#: Event type of the "your cursor fell out of the ring" frame.
RESYNC = "resync"

ALLOWED_EVENTS = frozenset({"node.upsert", "edge.state", "node.remove", "approval.pending"})


def _as_id(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class EventStream:
    def __init__(self, *, buffer: int = 500, ping_s: float = 15.0,
                 seq_source: Callable[[], int] | None = None) -> None:
        self.ping_s = ping_s
        self._seq_source = seq_source
        self._seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=buffer)
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        #: Highest id that has been *evicted* from the ring. A cursor below this
        #: cannot be served by replay without a hole (see `_snapshot`).
        self._evicted_through = 0

    # ------------------------------------------------------------------ public
    def publish(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._seq_source is not None:
                seq = int(self._seq_source())
            else:
                self._seq += 1
                seq = self._seq
            event = {"id": seq, "type": event_type, "data": dict(payload)}
            if self._events.maxlen is not None and len(self._events) >= self._events.maxlen:
                # About to drop the oldest event: remember how far the hole
                # reaches so a lagging subscriber gets a resync, not a gap.
                self._evicted_through = max(self._evicted_through, _as_id(self._events[0]["id"]))
            self._events.append(event)
            subscribers = list(self._subscribers)
        for loop, wake in subscribers:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:  # loop already closed
                pass
        return event

    def replay(self, last_event_id: str | None) -> list[dict[str, Any]]:
        if not last_event_id:
            return []
        try:
            last = int(last_event_id)
        except (TypeError, ValueError):
            return []
        with self._lock:
            return [e for e in self._events if _as_id(e["id"], -1) > last]

    def format(self, event: Mapping[str, Any]) -> str:
        """One SSE frame. `id` is optional: the resync frame has none on purpose."""
        payload = json.dumps({"type": event["type"], **event["data"]}, ensure_ascii=False)
        ident = event.get("id")
        head = f"id: {ident}\n" if ident is not None else ""
        return f"{head}data: {payload}\n\n"

    async def events(self, last_event_id: str | None = None) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        entry = (loop, wake)
        explicit = self._parse_cursor(last_event_id)
        with self._lock:
            self._subscribers.append(entry)
            # No `Last-Event-ID`: start at the current head. A fresh subscriber
            # must not be handed history it never asked for — the old code sent
            # `[-50:]` on the first wake, so a reconnect saw events twice.
            cursor = explicit if explicit is not None else self._head_locked()
        try:
            while True:
                gap, pending = self._snapshot(cursor)
                if gap:
                    yield self.format({"type": RESYNC, "data": {"reason": "gap", "cursor": cursor}})
                for event in pending:
                    cursor = max(cursor, _as_id(event["id"], cursor))
                    yield self.format(event)
                try:
                    await asyncio.wait_for(wake.wait(), self.ping_s)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                # Clear *before* the next snapshot: a publish that lands between
                # the clear and the read is seen by the read; one that lands
                # after it re-sets the flag and wakes us immediately.
                wake.clear()
        finally:
            with self._lock:
                try:
                    self._subscribers.remove(entry)
                except ValueError:
                    pass

    # ----------------------------------------------------------------- internal
    @staticmethod
    def _parse_cursor(last_event_id: str | None) -> int | None:
        if not last_event_id:
            return None
        try:
            return int(last_event_id)
        except (TypeError, ValueError):
            return None

    def _head_locked(self) -> int:
        return _as_id(self._events[-1]["id"]) if self._events else 0

    def _snapshot(self, cursor: int) -> tuple[bool, list[dict[str, Any]]]:
        """(needs_resync, events_after_cursor) — one atomic read of the ring."""
        with self._lock:
            if not self._events:
                return False, []
            if cursor < self._evicted_through:
                # Events between `cursor` and the ring head were dropped while
                # this subscriber was not reading; replay the whole buffer.
                return True, list(self._events)
            return False, [e for e in self._events if _as_id(e["id"], -1) > cursor]
