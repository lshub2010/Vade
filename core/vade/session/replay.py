"""Replay buffer: most recent 1000 events per session or 24h, whichever first.

The buffer answers one question for `ui/attach(last_seq=N)`: can I still serve
N+1..now contiguously? If not (trimmed by length or age), the caller must send
a snapshot instead, otherwise the client silently loses events it can never
recover.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Iterable

from .models import Event

__all__ = ["ReplayBuffer"]

DEFAULT_MAXLEN = 1000
DEFAULT_TTL_S = 24 * 3600


class ReplayBuffer:
    def __init__(
        self,
        *,
        maxlen: int = DEFAULT_MAXLEN,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._maxlen = maxlen
        self._ttl_s = ttl_s
        self._clock = clock
        self._buf: dict[str, deque[tuple[int, float]]] = {}

    def add(self, event: Event) -> None:
        if getattr(event, "coalesce", False):
            # RFC-0007 §9.4: a coalescible event is not a continuity witness.
            # It stays in `events` (read_since still returns it), but counting
            # 1 200 `task.progress` rows against the 1 000-event window would
            # turn every reconnect into a snapshot (§5.4).
            return
        buf = self._buf.setdefault(event.session_id, deque(maxlen=self._maxlen))
        buf.append((event.seq, self._clock()))

    def extend(self, events: Iterable[Event]) -> None:
        for event in events:
            self.add(event)

    def _prune(self, session_id: str) -> deque[tuple[int, float]]:
        buf = self._buf.get(session_id)
        if not buf:
            return deque()
        cutoff = self._clock() - self._ttl_s
        while buf and buf[0][1] < cutoff:
            buf.popleft()
        return buf

    def covers(self, session_id: str, last_seq: int) -> bool:
        """True iff every seq in `last_seq+1 .. newest` is still retained.

        A brand-new session with `last_seq == 0` is covered (nothing to replay);
        any other empty buffer means "I cannot prove continuity" -> snapshot.
        """
        buf = self._prune(session_id)
        if not buf:
            return last_seq == 0
        oldest = buf[0][0]
        return oldest <= last_seq + 1

    def oldest(self, session_id: str) -> int | None:
        buf = self._prune(session_id)
        return buf[0][0] if buf else None

    def newest(self, session_id: str) -> int | None:
        buf = self._prune(session_id)
        return buf[-1][0] if buf else None

    def discard(self, session_id: str) -> None:
        self._buf.pop(session_id, None)
