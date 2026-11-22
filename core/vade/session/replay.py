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

    def covers(self, session_id: str, last_seq: int, *, durable_last_seq: int | None = None) -> bool:
        """True iff `last_seq+1 .. newest` is retained and `last_seq` is real.

        Both ends are checked.

        * Lower bound: `oldest <= last_seq + 1` — the classic trim test.
        * Upper bound: `last_seq <= newest`. A cursor AHEAD of the newest event
          is not "caught up", it is unverifiable: it is either a cursor carried
          over from another session (one client, one variable) or one that was
          incremented twice. Answering "covered" there returned `snapshot=null`
          and zero events, so the history the client did not have was silently
          declared present (`from_seq=last_seq+1` made it permanent).

        An empty buffer proves nothing: it is either a brand-new session or a
        process that restarted after the events were written, and from here the
        two are indistinguishable. Treating them alike made `attach(last_seq=0)`
        after a restart replay "ok" with `snapshot=null` while every state item
        that is persisted WITHOUT an event (`session/model.set` is the proof)
        never reached the client. `durable_last_seq == 0` does not rescue the
        fresh case either: `create_session()` + `set_session_model()` writes a
        session with state and zero events, so "no event was ever written" is
        not "there is nothing to send". Only a non-empty window can prove
        continuity; everything else gets a snapshot, which is always correct
        and, on an empty session, nearly free.

        `durable_last_seq` is the store's `last_seq` (callers that have a store
        pass it). It is the ceiling for the upper bound: the replay window only
        holds non-coalescible events, so a client whose cursor sits on a
        `task.progress` (RFC-0007 §9.4) is still covered by the store even
        though the buffer's own newest is older.
        """
        buf = self._prune(session_id)
        if not buf:
            return False
        oldest = buf[0][0]
        newest = buf[-1][0] if durable_last_seq is None else durable_last_seq
        return oldest <= last_seq + 1 and last_seq <= newest

    def oldest(self, session_id: str) -> int | None:
        buf = self._prune(session_id)
        return buf[0][0] if buf else None

    def newest(self, session_id: str) -> int | None:
        buf = self._prune(session_id)
        return buf[-1][0] if buf else None

    def discard(self, session_id: str) -> None:
        self._buf.pop(session_id, None)
