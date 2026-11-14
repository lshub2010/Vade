"""Subagent run manager (RFC-0012 §5, §7): the LLM-loop sibling of tasks.

One manager per core process owns every subagent run of every session: the
five-state machine, the sha-256 result fingerprint and the parent-turn
terminal sweep. It is deliberately not a second event source — every event
goes through `SessionLog.emit()` on the PARENT session's seq stream, so
persist-before-broadcast, seq allocation and reconnect replay keep working
untouched (RFC-0012 §5.1: `session_id == parent_session_id`).

Three rules are load-bearing and easy to "fix" back into bugs:

* **The transition gate decides, not the caller** (§7.2): an illegal
  transition is `INVALID_ARGUMENT` and writes NOTHING — the record stays,
  the stream stays clean. A run never leaves a terminal state, and
  `spawned → completed/failed` is refused because a loop that never started
  has no result to report.
* **The result text never crosses** (§5.3): `subagent.result` carries
  `result_digest = sha256(full text)` and `result_chars`, nothing else. The
  full text lives only in the child session's message stream; `result_preview`
  (≤500 chars) is a read-time projection served by `subagent/get`, never
  persisted and never broadcast.
* **The parent turn sweeps before it closes** (§7.3): `cancel_for_turn` is
  called from the pre-terminal hook, so `subagent.result(cancelled)` rows land
  BEFORE `turn.completed` / `turn.failed` / `turn.interrupted` /
  `turn.cancelled` — a client never sees a finished parent with a running child.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Awaitable, Callable, Mapping

from ..errors import VadeError, is_known_code
from ..session.models import SubagentRun, SubagentState, SUBAGENT_TERMINAL_STATES, new_run_id, now_ms

__all__ = ["SubagentManager", "ALLOWED_TRANSITIONS", "LABEL_MAX", "RUN_ID_PATTERN"]

#: RFC-0012 §7.1: label is user-authored, 1..64 chars, refused not truncated.
LABEL_MAX = 64
#: RFC-0012 §7.1: `run_<32hex>`. Format errors and unknown ids answer
#: differently (INVALID_ARGUMENT vs NOT_FOUND) on purpose.
RUN_ID_PATTERN = re.compile(r"^run_[0-9a-f]{32}$")
#: RFC-0012 §5.2: the optional `step` ceiling (mirrors task action ≤120).
STEP_MAX = 120
#: RFC-0012 §6.2: the read-time result preview ceiling (FR-8.56's ≤500 字).
PREVIEW_MAX = 500
#: RFC-0012 §6.1: list page bounds.
LIST_LIMIT_MIN, LIST_LIMIT_MAX, LIST_LIMIT_DEFAULT = 1, 200, 50

#: RFC-0012 §7.2: the gate. `running → running` is a step/tool_name refresh,
#: not a transition; everything not listed here is refused.
ALLOWED_TRANSITIONS: dict[SubagentState, set[SubagentState]] = {
    SubagentState.SPAWNED: {SubagentState.RUNNING, SubagentState.CANCELLED},
    SubagentState.RUNNING: {
        SubagentState.RUNNING,
        SubagentState.COMPLETED,
        SubagentState.FAILED,
        SubagentState.CANCELLED,
    },
    SubagentState.COMPLETED: set(),
    SubagentState.FAILED: set(),
    SubagentState.CANCELLED: set(),
}

#: RFC-0012 §7.3: a swept run has no result text — its fingerprint is sha256
#: of the empty string, which keeps "every terminal run has a digest" true
#: with exactly one client-side rule (RFC-0012 §10 Q3).
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def result_digest_of(text: str | None) -> str:
    """RFC-0012 §5.3: the fingerprint. Full text never travels with it."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class SubagentManager:
    """Owns `subagent_runs` records and the three `subagent.*` events."""

    def __init__(
        self,
        log: Any = None,
        *,
        store: Any = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._log = log
        #: The store is the SQLiteStore behind the session log; taking it
        #: separately (instead of reaching through `log.store`) keeps unit
        #: tests able to wire an in-memory log with a file-backed store.
        self._store = store if store is not None else getattr(log, "store", None)
        self._clock = clock
        self._runs: dict[str, SubagentRun] = {}

    # ------------------------------------------------------------------ spawn
    async def spawn(
        self,
        *,
        parent_session_id: str,
        label: str,
        parent_turn_id: str | None = None,
        child_session_id: str | None = None,
    ) -> SubagentRun:
        """Create a run and emit `subagent.spawned` exactly once (§5.2)."""
        parent_session_id = self._require_str(parent_session_id, "parent_session_id")
        label = self._require_label(label)
        if parent_turn_id is not None and not isinstance(parent_turn_id, str):
            raise VadeError(
                "INVALID_ARGUMENT", "parent_turn_id must be a string or null",
                data={"field": "parent_turn_id"},
            )
        if child_session_id is not None and not isinstance(child_session_id, str):
            raise VadeError(
                "INVALID_ARGUMENT", "child_session_id must be a string or null",
                data={"field": "child_session_id"},
            )
        run = SubagentRun(
            run_id=new_run_id(),
            parent_session_id=parent_session_id,
            label=label,
            parent_turn_id=parent_turn_id,
            child_session_id=child_session_id,
            state=SubagentState.SPAWNED,
            created_at=self._clock(),
        )
        self._persist(run)
        await self._emit(
            parent_session_id,
            "subagent.spawned",
            {
                "run_id": run.run_id,
                "session_id": run.parent_session_id,
                "parent_session_id": run.parent_session_id,
                "parent_turn_id": run.parent_turn_id,
                "child_session_id": run.child_session_id,
                "label": run.label,
                "at": run.created_at,
            },
        )
        return run

    # ------------------------------------------------------------------ state
    async def set_running(
        self, run_id: str, *, step: str | None = None, tool_name: str | None = None,
    ) -> SubagentRun:
        """`spawned → running`: the loop started. Also serves as the running
        state's step/tool_name refresh (§7.2: running → running is legal)."""
        return await self._transition(
            run_id, SubagentState.RUNNING, step=step, tool_name=tool_name,
        )

    async def report(
        self, run_id: str, *, step: str | None = None, tool_name: str | None = None,
    ) -> SubagentRun:
        """Status refresh without a state change (§5.2: `subagent.status` with
        the current state). No-op on terminal runs — a late report is dropped,
        not resurrected."""
        run = self.require(run_id)
        if run.is_terminal:
            return run
        return await self._emit_status(run, step=step, tool_name=tool_name)

    async def finish(
        self,
        run_id: str,
        state: SubagentState | str,
        *,
        result_text: str | None = None,
        error_code: str | None = None,
    ) -> SubagentRun:
        """Terminal transition (§5.2): record first, then exactly one
        `subagent.result`. `result_digest` is the sha-256 fingerprint of
        `result_text` — the text itself is NOT persisted here and NOT emitted."""
        run = self.require(run_id)
        target = self._state_of(state)
        if run.is_terminal:
            if target is run.state:
                # Same-terminal retry (RFC-0007 §5.5 R1 precedent): the record
                # is the truth, a duplicate finish changes nothing and emits
                # nothing.
                return run
            # A DIFFERENT terminal target on a terminal run is an illegal
            # transition, not a retry: refused, nothing written, nothing emitted.
            raise VadeError(
                "INVALID_ARGUMENT",
                f"illegal subagent transition {run.state.value} -> {target.value} "
                "(RFC-0012 §7.2)",
                data={"field": "state", "from": run.state.value, "to": target.value},
            )
        self._check_transition(run, target)
        if target is SubagentState.FAILED and not error_code:
            raise VadeError(
                "INVALID_ARGUMENT",
                "a failed subagent run must carry a C-04 error_code",
                data={"field": "error_code"},
            )
        if error_code is not None and not is_known_code(error_code):
            # §7.1: modules and workers may not invent codes — the same rule
            # VadeError itself enforces (errors.py), applied to the payload.
            raise VadeError(
                "INVALID_ARGUMENT",
                f"error_code {error_code!r} is not a C-04 code",
                data={"field": "error_code"},
            )
        text = result_text if isinstance(result_text, str) else (result_text or "")
        digest = result_digest_of(text)
        final = run.with_changes(
            state=target,
            finished_at=self._clock(),
            result_digest=digest,
            result_chars=len(text),
            error_code=error_code if target is SubagentState.FAILED else None,
        )
        self._persist(final)
        payload: dict[str, Any] = {
            "run_id": final.run_id,
            "session_id": final.parent_session_id,
            "state": final.state.value,
            "result_digest": final.result_digest,
            "result_chars": final.result_chars,
            "finished_at": final.finished_at,
        }
        if final.error_code is not None:
            payload["error_code"] = final.error_code
        await self._emit(final.parent_session_id, "subagent.result", payload)
        return final

    async def cancel_for_turn(self, parent_session_id: str, parent_turn_id: str) -> list[str]:
        """RFC-0012 §7.3: the parent turn reached a terminal state; every
        non-terminal run it spawned is `cancelled`, each with exactly one
        `subagent.result`. Called from the pre-terminal hook — BEFORE the
        turn's terminal event is written."""
        if not isinstance(parent_session_id, str) or not isinstance(parent_turn_id, str):
            return []
        cancelled: list[str] = []
        for run in self._runs_of(parent_session_id):
            if run.parent_turn_id != parent_turn_id or run.is_terminal:
                continue
            await self.finish(run.run_id, SubagentState.CANCELLED)
            cancelled.append(run.run_id)
        return cancelled

    # ------------------------------------------------------------------ reads
    def get(self, run_id: str) -> SubagentRun | None:
        self._check_run_id(run_id)
        if run_id in self._runs:
            return self._runs[run_id]
        if self._store is None:
            return None
        return self._store.get_subagent_run(run_id)

    def require(self, run_id: str) -> SubagentRun:
        run = self.get(run_id)
        if run is None:
            raise VadeError("NOT_FOUND", f"subagent run {run_id!r} does not exist")
        return run

    def list_runs(
        self,
        *,
        session_id: str | None = None,
        states: list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """RFC-0012 §6.1: keyset pagination over `created_at, run_id`."""
        limit = self._check_limit(limit)
        state_filter = self._check_states(states)
        if cursor is not None:
            if not isinstance(cursor, str):
                raise VadeError(
                    "INVALID_ARGUMENT", "cursor must be a string or null",
                    data={"field": "cursor"},
                )
            marker = self._decode_cursor(cursor)
        else:
            marker = None
        rows = self._runs_of(session_id, states=state_filter)
        if marker is not None:
            rows = [r for r in rows if (r.created_at, r.run_id) > marker]
        page = rows[:limit]
        next_cursor = None
        if len(rows) > len(page) and page:
            last = page[-1]
            next_cursor = self._encode_cursor(last.created_at, last.run_id)
        return {
            "runs": [self.view(run) for run in page],
            "next_cursor": next_cursor,
        }

    def view(self, run: SubagentRun) -> dict[str, Any]:
        """`SubagentRunView` with the read-time `result_preview` (§6.2)."""
        return run.to_view(now=self._clock(), result_preview=self._preview_of(run))

    def _preview_of(self, run: SubagentRun) -> str | None:
        """RFC-0012 §5.3/§6.2: ≤500 chars of the child session's LAST assistant
        message. A read-time projection from the child's own message stream —
        never stored, never broadcast."""
        if run.state is not SubagentState.COMPLETED or not run.child_session_id:
            return None
        loader = getattr(self._store, "load", None)
        if not callable(loader):
            return None
        try:
            session: Any = loader(run.child_session_id)
        except Exception:  # pragma: no cover - an unreadable child is not a crash
            return None
        if session is None:
            return None
        for message in reversed(session.messages):
            if message.role == "assistant" and message.content:
                return message.content[:PREVIEW_MAX]
        return None

    # ------------------------------------------------------------- validation
    @staticmethod
    def _require_str(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise VadeError(
                "INVALID_ARGUMENT", f"{field} is required",
                data={"field": field},
            )
        return value

    @staticmethod
    def _require_label(label: Any) -> str:
        if not isinstance(label, str):
            raise VadeError(
                "INVALID_ARGUMENT", "label must be a string",
                data={"field": "label"},
            )
        if not 1 <= len(label) <= LABEL_MAX:
            raise VadeError(
                "INVALID_ARGUMENT",
                f"label must be 1..{LABEL_MAX} characters (never truncated)",
                data={"field": "label"},
            )
        return label

    @staticmethod
    def _check_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
            raise VadeError(
                "INVALID_ARGUMENT",
                "run_id must match run_<32 lowercase hex> (RFC-0012 §7.1)",
                data={"field": "run_id"},
            )

    @staticmethod
    def _state_of(state: SubagentState | str) -> SubagentState:
        try:
            return state if isinstance(state, SubagentState) else SubagentState(str(state))
        except ValueError as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "state is not a subagent.state",
                data={"field": "state"},
            ) from exc

    @staticmethod
    def _check_transition(run: SubagentRun, target: SubagentState) -> None:
        if target not in ALLOWED_TRANSITIONS.get(run.state, set()):
            raise VadeError(
                "INVALID_ARGUMENT",
                f"illegal subagent transition {run.state.value} -> {target.value} "
                "(RFC-0012 §7.2)",
                data={"field": "state", "from": run.state.value, "to": target.value},
            )

    def _check_limit(self, limit: int | None) -> int:
        if limit is None:
            return LIST_LIMIT_DEFAULT
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise VadeError(
                "INVALID_ARGUMENT", "limit must be an integer",
                data={"field": "limit"},
            )
        if not LIST_LIMIT_MIN <= limit <= LIST_LIMIT_MAX:
            raise VadeError(
                "INVALID_ARGUMENT",
                f"limit must be {LIST_LIMIT_MIN}..{LIST_LIMIT_MAX}",
                data={"field": "limit"},
            )
        return limit

    @staticmethod
    def _check_states(states: Any) -> list[str] | None:
        if states is None:
            return None
        if isinstance(states, (str, bytes)) or not isinstance(states, (list, tuple, set)):
            raise VadeError(
                "INVALID_ARGUMENT", "states must be an array of subagent.state",
                data={"field": "states"},
            )
        checked: list[str] = []
        for item in states:
            try:
                checked.append(SubagentState(str(item)).value)
            except ValueError as exc:
                raise VadeError(
                    "INVALID_ARGUMENT", f"states contains {item!r}",
                    data={"field": "states"},
                ) from exc
        return checked

    # ------------------------------------------------------------------ writes
    def _persist(self, run: SubagentRun) -> None:
        self._runs[run.run_id] = run
        if self._store is not None:
            self._store.upsert_subagent_run(run)

    def _runs_of(
        self, session_id: str | None, *, states: list[str] | None = None,
    ) -> list[SubagentRun]:
        if self._store is not None:
            rows = self._store.list_subagent_runs(session_id=session_id, states=states)
            for row in rows:  # memory is the hot cache; the store is the truth
                self._runs.setdefault(row.run_id, row)
            return rows
        rows = list(self._runs.values())
        if session_id is not None:
            rows = [r for r in rows if r.parent_session_id == session_id]
        if states:
            rows = [r for r in rows if r.state.value in states]
        rows.sort(key=lambda r: (r.created_at, r.run_id))
        return rows

    # ------------------------------------------------------------------ events
    async def _transition(
        self, run_id: str, target: SubagentState,
        *, step: str | None = None, tool_name: str | None = None,
    ) -> SubagentRun:
        run = self.require(run_id)
        if run.is_terminal:
            # Same refusal path as any illegal transition: a terminal run is
            # never resumed, and a late worker learns it from the error.
            raise VadeError(
                "INVALID_ARGUMENT",
                f"illegal subagent transition {run.state.value} -> {target.value} "
                "(RFC-0012 §7.2)",
                data={"field": "state", "from": run.state.value, "to": target.value},
            )
        self._check_transition(run, target)
        updated = run.with_changes(state=target)
        self._persist(updated)
        await self._emit_status(updated, step=step, tool_name=tool_name)
        return updated

    async def _emit_status(
        self, run: SubagentRun, *, step: str | None = None, tool_name: str | None = None,
    ) -> SubagentRun:
        payload: dict[str, Any] = {
            "run_id": run.run_id,
            "session_id": run.parent_session_id,
            "state": run.state.value,
            "at": self._clock(),
        }
        if step is not None:
            if not isinstance(step, str):
                raise VadeError(
                    "INVALID_ARGUMENT", "step must be a string",
                    data={"field": "step"},
                )
            payload["step"] = step[:STEP_MAX]
        if tool_name is not None:
            if not isinstance(tool_name, str) or not tool_name:
                raise VadeError(
                    "INVALID_ARGUMENT", "tool_name must be a non-empty string",
                    data={"field": "tool_name"},
                )
            payload["tool_name"] = tool_name
        await self._emit(run.parent_session_id, "subagent.status", payload)
        return run

    async def _emit(self, session_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._log is None:
            return
        await self._log.emit(session_id, event_type, dict(payload))

    # ----------------------------------------------------------------- cursors
    @staticmethod
    def _encode_cursor(created_at: int, run_id: str) -> str:
        raw = f"s1|{int(created_at)}|{run_id}".encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[int, str]:
        import binascii

        padded = cursor + "=" * (-len(cursor) % 4)
        try:
            raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "cursor is not a subagent/list cursor",
                data={"field": "cursor"},
            ) from exc
        parts = raw.split("|", 2)
        if len(parts) != 3 or parts[0] != "s1":
            raise VadeError(
                "INVALID_ARGUMENT", "cursor is not a subagent/list cursor",
                data={"field": "cursor"},
            )
        try:
            created_at = int(parts[1])
        except ValueError as exc:
            raise VadeError(
                "INVALID_ARGUMENT", "cursor is not a subagent/list cursor",
                data={"field": "cursor"},
            ) from exc
        if not parts[2]:
            raise VadeError(
                "INVALID_ARGUMENT", "cursor is not a subagent/list cursor",
                data={"field": "cursor"},
            )
        return created_at, parts[2]


def assert_gate_intact() -> None:
    """RFC-0012 §7.2: the gate is the contract; called by check_contracts to
    keep a refactor from silently widening it."""
    expected = {
        SubagentState.SPAWNED: {SubagentState.RUNNING, SubagentState.CANCELLED},
        SubagentState.RUNNING: {
            SubagentState.RUNNING,
            SubagentState.COMPLETED,
            SubagentState.FAILED,
            SubagentState.CANCELLED,
        },
        SubagentState.COMPLETED: set(),
        SubagentState.FAILED: set(),
        SubagentState.CANCELLED: set(),
    }
    if ALLOWED_TRANSITIONS != expected:
        raise VadeError(
            "INVALID_ARGUMENT", "ALLOWED_TRANSITIONS drifted from RFC-0012 §7.2"
        )


_ = SUBAGENT_TERMINAL_STATES  # re-exported for check_contracts' import path
