"""Per-session sequence allocation.

`seq` is core-owned and strictly +1 per session (design §2.6). No client may
supply one; no event may skip a number. All allocation goes through the store,
because "read counter, add one, write it back" from two coroutines is how you
get duplicate seq under load.
"""

from __future__ import annotations

from .store import SessionStore

__all__ = ["SeqAllocator"]


class SeqAllocator:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def next(self, session_id: str) -> int:
        return self._store.next_seq(session_id)

    def last(self, session_id: str) -> int:
        return self._store.last_seq(session_id)
