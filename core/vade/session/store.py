"""SessionStore interface (D36). Callers never contain SQL literals.

SQLite is only the default implementation; a Postgres store must be a drop-in
by adding `pg_store.py`. Keeping `append(msgs, events)` as a *batch* is what
makes that swap a transaction-boundary change and not a rewrite of the loop.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from .models import Event, Message, Session, SessionGroup, Turn
from .search import LIMIT_DEFAULT, SearchHit

__all__ = ["SessionStore", "UNSET"]

#: RFC-0010 §5: "parameter absent" for optional filters (`session/list`'s
#: `group_id`): omitted = every group, `None` = ungrouped only, a string = that
#: group. A plain `None` default cannot carry the first meaning.
UNSET: Any = object()


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

    # --- RFC-0010 §4/§5: session metadata (groups / icon / color / pin) -----
    def list_session_rows(
        self, *, limit: int = 50, offset: int = 0,
        group_id: Any = UNSET, archived: bool = False,
    ) -> list[Session]:
        """RFC-0010 §5: the LIGHT list page — metadata only, no messages/turns."""
        ...

    def update_session_meta(self, sess_id: str, fields: dict) -> None:
        """RFC-0010 §5: write a whitelist of metadata columns at once."""
        ...

    def delete_session(self, sess_id: str) -> None:
        """RFC-0010 §8.2: hard-delete the session and all of its rows."""
        ...

    def create_group(self, group: SessionGroup) -> None: ...

    def get_group(self, group_id: str) -> SessionGroup | None: ...

    def list_groups(self) -> list[SessionGroup]: ...

    def update_group_fields(self, group_id: str, fields: dict) -> None: ...

    def delete_group(self, group_id: str) -> int:
        """RFC-0010 §8.1: drop the group, move members to ungrouped; returns
        how many sessions were unassigned (never deletes a session)."""
        ...

    def group_session_counts(self) -> dict[str, int]: ...

    # --- RFC-0014 §5: session/search (titles + live bodies) -----------------
    def search_sessions(
        self, query: str, *, limit: int = LIMIT_DEFAULT, offset: int = 0,
        session_id: str | None = None,
    ) -> tuple[list[SearchHit], int]:
        """RFC-0014 §4: parameterized LIKE scan; returns (page, total).

        `query` is the already-validated literal (manager ran `validate_query`).
        Implementations MUST bind the escaped pattern through a placeholder —
        never concatenate query text into SQL (§4.10; A9 pins it with
        injection samples). `session_id=None` searches the whole workspace;
        a string pins the scan to one session. `total` counts DISTINCT
        matching sessions (a session counts once even when title and body
        both match).
        """
        ...
