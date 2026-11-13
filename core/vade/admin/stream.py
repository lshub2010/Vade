"""SSE stream: one seq, `Last-Event-ID` replay, `: ping` every 15s.

The seq is shared with C3's replay logic — the doc is explicit that the admin
plane must not invent a second numbering (31-C5a §T4). `EventStream` accepts a
`seq_source` so the caller can hand it C3's counter; without one it keeps its
own monotonic integer.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from typing import Any, AsyncIterator, Callable, Mapping

__all__ = ["EventStream"]

ALLOWED_EVENTS = frozenset({"node.upsert", "edge.state", "node.remove", "approval.pending"})


class EventStream:
    def __init__(self, *, buffer: int = 500, ping_s: float = 15.0,
                 seq_source: Callable[[], int] | None = None) -> None:
        self.ping_s = ping_s
        self._seq_source = seq_source
        self._seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=buffer)
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    def publish(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._seq_source is not None:
                seq = int(self._seq_source())
            else:
                self._seq += 1
                seq = self._seq
            event = {"id": seq, "type": event_type, "data": dict(payload)}
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
            return [e for e in self._events if int(e["id"]) > last]

    def format(self, event: Mapping[str, Any]) -> str:
        return f"id: {event['id']}\ndata: {json.dumps({'type': event['type'], **event['data']}, ensure_ascii=False)}\n\n"

    async def events(self, last_event_id: str | None = None) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        entry = (loop, wake)
        with self._lock:
            self._subscribers.append(entry)
        try:
            for event in self.replay(last_event_id):
                yield self.format(event)
            while True:
                try:
                    await asyncio.wait_for(wake.wait(), self.ping_s)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                wake.clear()
                # Drain whatever accumulated between wakes.
                with self._lock:
                    pending = list(self._events)[-50:]
                for event in pending:
                    yield self.format(event)
        finally:
            with self._lock:
                try:
                    self._subscribers.remove(entry)
                except ValueError:
                    pass
