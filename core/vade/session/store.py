"""SessionStore interface (D36). Callers never contain SQL literals.

SQLite is only the default implementation; a Postgres store must be a drop-in
by adding `pg_store.py`. Keeping `append(msgs, events)` as a *batch* is what
makes that swap a transaction-boundary change and not a rewrite of the loop.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .models import Event, Message, Session, Turn

__all__ = ["SessionStore"]


@runtime_checkable
class SessionStore(Protocol):
    """Persistence port. Implementations must be safe for one event loop.

    `next_seq` must allocate atomically on the storage side (SQLite:
    `UPDATE ... RETURNING`, Postgres: `UPDATE ... RETURNING` as well) — never
    read+1+write from the caller.
    """

    def create_session(self, session: Session) -> None: ...

    def append(self, sess_id: str, msgs: Sequence[Message], events: Sequence[Event]) -> None:
        """Persist one batch (messages + events) atomically, before broadcast."""

    def read_since(self, sess_id: str, seq: int) -> list[Event]: ...

    def update_turn(self, sess_id: str, turn: Turn) -> None: ...

    def next_seq(self, sess_id: str) -> int: ...

    def load(self, sess_id: str) -> Session | None: ...

    # --- extensions used by the queue/idempotency and attach paths ----------
    def find_turn_by_client_msg(self, sess_id: str, client_msg_id: str) -> Turn | None: ...

    def list_sessions(self, limit: int = 50) -> list[Session]: ...

    def last_seq(self, sess_id: str) -> int: ...

    def set_session_model(self, sess_id: str, model_id: str | None) -> None:
        """RFC-0005 §5.3: persist or clear the session-level model override."""
        ...

    def close_session(self, sess_id: str) -> None: ...
