"""The ask-user channel: one pending question per turn, idempotent answers.

Structure is lifted from `vade.policy.approval` on purpose — the blocking,
timeout, multi-UI and shield-the-future patterns are already debugged there —
but three things are deliberately **not** copied:

* **No grant cache.** An approval may answer the next identical question for
  the user (`scope:"session"`); an input may not, because the same question
  asked twice must produce two dialogs (RFC-0008 §2.1).
* **Timeout is a cancellation, not a default.** `default` is a UI prefill; a
  timeout hands the model `CANCELLED`, never the default value (§8.1).
* **Disconnect does not cancel.** Losing every UI is not "the user refused" —
  the request keeps its deadline (§8.4).

`turn.input_resolved` is emitted exactly once per `input_id`; every exit path
(submit, cancel, timeout, interrupt, session close) claims under the same lock
before writing, so a second event is impossible by construction (§5.3, §7.3).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from ..errors import VadeError
from .models import (
    InputConfig,
    InputOutcome,
    InputRequest,
    normalize_request,
    validate_value,
    value_chars,
    value_digest,
)

__all__ = ["InputManager", "PendingInput", "ResolvedInput", "new_input_id"]

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_LOCK = __import__("threading").Lock()
_ID_SEQ = 0

#: RFC-0008 §7.3 row 6: an id older than this is `NOT_FOUND`, not "already
#: answered". 24h is the contract's retention window (§14 `LRU 256 / 24h`).
RESOLVED_TTL_S = 24 * 3600
RESOLVED_MAX = 256

#: §9.5: a model stuck in "let me ask once more" must hit a wall.
MAX_PER_TURN = 10
MAX_PER_MINUTE = 3


def new_input_id() -> str:
    """ULID-shaped id (26 chars), sortable by creation — same alphabet as approvals."""
    global _ID_SEQ
    millis = int(time.time() * 1000)
    with _ID_LOCK:
        _ID_SEQ = (_ID_SEQ + 1) & 0xFFFF
        seq = _ID_SEQ
    import os

    value = (millis << 80) | ((int.from_bytes(os.urandom(8), "big") << 16 | seq) & ((1 << 80) - 1))
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


@dataclass(slots=True)
class PendingInput:
    input_id: str
    session_id: str
    turn_id: str
    kind: str
    prompt: str
    detail: str | None
    schema: Mapping[str, Any]
    options: tuple[dict[str, Any], ...]
    option_ids: tuple[str, ...]
    timeout_s: int
    default: Any
    source: Mapping[str, Any]
    created_at: int                 # wall clock ms, for the event payload
    started: float                  # monotonic
    deadline: float                 # monotonic
    future: "asyncio.Future[ResolvedInput]"
    emit: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None

    @property
    def expires_at(self) -> int:
        return int(self.created_at + self.timeout_s * 1000)

    def required_payload(self) -> dict[str, Any]:
        """The `turn.input_required` payload (§6.1); null keys stay present."""
        return {
            "input_id": self.input_id, "turn_id": self.turn_id,
            "session_id": self.session_id, "state": "waiting_input",
            "kind": self.kind, "prompt": self.prompt, "detail": self.detail,
            "schema": dict(self.schema), "options": [dict(o) for o in self.options],
            "timeout_s": self.timeout_s, "expires_at": self.expires_at,
            "default": self.default, "source": dict(self.source),
            "created_at": self.created_at,
        }

    def snapshot_row(self) -> dict[str, Any]:
        """`ui/attach` snapshot entry (§8.2): enough to rebuild the dialog."""
        return {
            "input_id": self.input_id, "turn_id": self.turn_id, "kind": self.kind,
            "prompt": self.prompt, "detail": self.detail, "schema": dict(self.schema),
            "options": [dict(o) for o in self.options], "timeout_s": self.timeout_s,
            "expires_at": self.expires_at, "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    input_id: str
    session_id: str
    turn_id: str
    kind: str
    status: str                     # submitted|timeout|cancelled
    value: Any
    state: str                      # turn state this resolution restores
    by: str
    reason: str
    duration_ms: float
    stored_at: float                # wall clock, for the retention prune

    def payload(self) -> dict[str, Any]:
        """The `turn.input_resolved` payload (§6.2): the ONLY dialog-closing signal."""
        return {
            "input_id": self.input_id, "turn_id": self.turn_id, "state": self.state,
            "status": self.status, "value": self.value, "by": self.by,
            "reason": self.reason, "duration_ms": self.duration_ms,
        }


class InputManager:
    def __init__(
        self,
        *,
        config: InputConfig | None = None,
        audit: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        ui_probe: Callable[[str], int] | None = None,
    ) -> None:
        self.config = config or InputConfig()
        self._audit = audit
        self._clock = clock
        self._wall = wall_clock
        self._ui_probe = ui_probe
        self._state_probe: Callable[[str, str], str | None] | None = None
        self._state_hook: Callable[[str, str, str], Any] | None = None
        self._pending: dict[str, PendingInput] = {}
        self._resolved: "OrderedDict[str, ResolvedInput]" = OrderedDict()
        self._lock = asyncio.Lock()
        self._ui_count = 0
        self._turn_hits: "OrderedDict[str, int]" = OrderedDict()
        self._session_hits: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------ wiring
    def set_ui_probe(self, probe: Callable[[str], int] | None) -> None:
        """Returns how many UIs are attached to `session_id` (§8.4)."""
        self._ui_probe = probe

    def set_state_probe(self, probe: Callable[[str, str], str | None] | None) -> None:
        """Returns the live turn state (`waiting_approval` gates S3)."""
        self._state_probe = probe

    def set_state_hook(self, hook: Callable[[str, str, str], Any] | None) -> None:
        """Writes the turn state to the queue (which persists it)."""
        self._state_hook = hook

    def note_ui_attached(self, delta: int = 1) -> None:
        self._ui_count = max(0, self._ui_count + delta)

    def ui_attached(self, session_id: str) -> int | None:
        """Public availability probe for the `ask_user` tool (§8.4).

        Returns the attached-UI count for `session_id`, or `None` when no
        probe is wired (availability unknown — the caller decides). A broken
        probe reads as 0, fail-closed: the tool must answer `UNAVAILABLE`
        instead of silently blocking a turn nobody can answer.
        """
        if self._ui_probe is None:
            return None
        try:
            return max(int(self._ui_probe(session_id)), 0)
        except Exception:
            return 0

    @property
    def ui_count(self) -> int:
        return self._ui_count

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending(self, session_id: str) -> list[dict[str, Any]]:
        """Snapshot rows for one session, oldest first (§8.2 `pending_inputs`)."""
        rows = [p.snapshot_row() for p in self._pending.values() if p.session_id == session_id]
        return sorted(rows, key=lambda row: (row["created_at"], row["input_id"]))

    def pending_requests(self) -> list[dict[str, Any]]:
        return [
            {"input_id": p.input_id, "session_id": p.session_id, "turn_id": p.turn_id,
             "kind": p.kind, "prompt": p.prompt, "timeout_s": p.timeout_s,
             "expires_at": p.expires_at}
            for p in self._pending.values()
        ]

    def resolved(self, input_id: str) -> ResolvedInput | None:
        return self._resolved.get(input_id)

    # ---------------------------------------------------------------- port
    async def ask(self, prompt: Any) -> Any:
        """`loop.ports.InputPort` implementation: one question, one answer.

        The engine's built-in `ask_user` tool arrives through this adapter —
        the loop package only knows the `InputPort` protocol, so this is where
        the shapes meet: `InputPrompt` becomes the core-owned `InputRequest`,
        and the `InputOutcome` maps to `InputAnswer` (`submitted` -> status
        `"answered"`; timeout/cancel keep their status with `value=None`).
        Imported lazily: `vade.input` must stay importable before `vade.loop`.
        """
        from ..loop.ports import InputAnswer
        from .models import InputRequest

        request = InputRequest(
            session_id=prompt.session_id, turn_id=prompt.turn_id,
            kind=prompt.kind, prompt=prompt.prompt, detail=prompt.detail,
            options=[dict(option) for option in (prompt.options or ())],
            timeout_s=prompt.timeout_s, default=prompt.default,
            max_length=prompt.max_length, source=dict(prompt.source or {}),
            emit=prompt.emit,
        )
        outcome = await self.request(request)
        if outcome.ok:
            return InputAnswer(ok=True, value=outcome.value, status="answered",
                               input_id=outcome.input_id)
        return InputAnswer(ok=False, value=None, status=outcome.status,
                           input_id=outcome.input_id,
                           code=outcome.code or "CANCELLED",
                           reason=outcome.reason, message=outcome.message)


    # ------------------------------------------------------------------ request
    async def request(self, req: InputRequest) -> InputOutcome:
        """Ask the user and block the turn until they answer (§5.3).

        Returns `submitted` with the value, or `cancelled`/`timeout` with
        `code="CANCELLED"` and no value. Raises `VadeError` when the QUESTION is
        invalid or the channel is not usable (nested request, rate limit): those
        are the model's mistake and the turn continues with a bad tool result.
        """
        record = normalize_request(req, self.config)
        source = record["source"]
        depth = int(source.get("depth") or 0)
        turn_id = req.turn_id
        session_id = req.session_id

        if depth > 0 and not self.config.allow_subagents:
            # §9.5: a background agent asking the user a question is a suspicious
            # interaction by default; the parent turn gets "nobody to ask".
            self._audit_request(session_id, turn_id, record, None, status="cancelled",
                                reason="no_ui", actor=_actor(source))
            return InputOutcome(ok=False, status="cancelled", code="CANCELLED",
                                reason="no_ui", message="subagent input requests are disabled ([ask_user] allow_subagents=false)")

        await self._enforce_rate(session_id, turn_id)
        await self._check_slot(session_id, turn_id)

        if self._ui_probe is not None and int(self._ui_probe(session_id) or 0) <= 0:
            # No UI to ask: cancel now instead of burning the timeout, exactly as
            # `approval.py` denies with `no_approver` (§8.4). No event is emitted:
            # nothing was ever shown, so a lone `input_resolved` would be an
            # orphan closing a dialog that never opened.
            self._audit_request(session_id, turn_id, record, None, status="cancelled",
                                reason="no_ui", actor=_actor(source))
            return InputOutcome(ok=False, status="cancelled", code="CANCELLED",
                                reason="no_ui", message="no UI is attached to answer this question")

        now = self._clock()
        input_id = new_input_id()
        pending = PendingInput(
            input_id=input_id, session_id=session_id, turn_id=turn_id,
            kind=record["kind"], prompt=record["prompt"], detail=record["detail"],
            schema=record["schema"], options=record["options"],
            option_ids=tuple(option["id"] for option in record["options"]),
            timeout_s=record["timeout_s"], default=record["default"],
            source=source, created_at=int(self._wall() * 1000), started=now,
            deadline=now + record["timeout_s"],
            future=asyncio.get_running_loop().create_future(), emit=req.emit,
        )
        async with self._lock:
            self._pending[input_id] = pending
        self._audit_request(session_id, turn_id, record, input_id, status="pending",
                            actor=_actor(source), reason="")
        # S2: entering `waiting_input` is a real state change, and
        # `turn.input_required` (which carries `state`) is its event — the
        # mistake §2.2 found in the approval path is not repeated here.
        await self._set_state(session_id, turn_id, "waiting_input")
        await self._emit(pending, "turn.input_required", pending.required_payload())

        resolved: ResolvedInput | None = None
        try:
            # `shield` is load-bearing (see `approval.py:161-170`): without it the
            # deadline cancels the very future an answer is about to resolve, and
            # the answer is lost in the same tick the timeout fires.
            record_out = await asyncio.wait_for(
                asyncio.shield(pending.future), record["timeout_s"]
            )
        except asyncio.TimeoutError:
            if pending.future.done() and not pending.future.cancelled():
                # A real answer beat the timeout ("real decision beats timeout").
                resolved = pending.future.result()
            else:
                claimed = await self._claim(
                    pending, status="timeout", value=None, by="system",
                    reason="timeout", state="running",
                )
                if claimed is None:
                    # Someone claimed between the deadline and the lock; report
                    # what they decided instead of inventing a timeout.
                    return self._outcome(pending, self._resolved.get(input_id))
                await self._set_state(session_id, turn_id, "running")
                await self._emit(pending, "turn.input_resolved", claimed.payload())
                self._audit_response(claimed)
                return InputOutcome(
                    ok=False, status="timeout", code="CANCELLED", input_id=input_id,
                    by="system", reason="timeout",
                    message=(f"input request timed out after {record['timeout_s']}s "
                             f"(input_id={input_id})"),
                )
        except asyncio.CancelledError:
            # The turn was interrupted (or the process is shutting down). Claim
            # before re-raising so the dialog closes ahead of `turn.interrupted`
            # (§5.3, S5).
            claimed = await self._claim(
                pending, status="cancelled", value=None, by="system",
                reason="turn_interrupted", state="interrupted",
            )
            if claimed is not None:
                await self._set_state(session_id, turn_id, "interrupted")
                await self._emit(pending, "turn.input_resolved", claimed.payload())
                self._audit_response(claimed)
            raise

        # Answered: submit/cancel already emitted `turn.input_resolved`.
        await self._set_state(session_id, turn_id, "running")
        return self._outcome(pending, resolved)

    def _outcome(self, pending: PendingInput, resolved: ResolvedInput | None) -> InputOutcome:
        if resolved is None:
            resolved = self._resolved.get(pending.input_id)
        if resolved is None:  # pragma: no cover - every path stores a record
            return InputOutcome(ok=False, status="cancelled", code="CANCELLED",
                                input_id=pending.input_id, reason="unknown",
                                message="input request disappeared")
        if resolved.status == "submitted":
            return InputOutcome(ok=True, status="submitted", value=resolved.value,
                                input_id=resolved.input_id, by=resolved.by,
                                reason=resolved.reason)
        return InputOutcome(ok=False, status=resolved.status, code="CANCELLED",
                            input_id=resolved.input_id, by=resolved.by,
                            reason=resolved.reason,
                            message=(f"input request was {resolved.status} "
                                     f"(input_id={resolved.input_id})"))

    # ------------------------------------------------------------------- submit
    async def submit(self, *, session_id: str, input_id: str, value: Any,
                     by: str = "user") -> dict[str, Any]:
        """`turn/input`: answer one question. Idempotency is keyed on `input_id`.

        Matrix (§7.3): same value -> `duplicate:true`, no second event; different
        value -> `invalid_argument{reason:already_resolved}`; already
        timed-out/cancelled -> `ok:true` with that status and `value:null`;
        unknown/expired -> `NOT_FOUND`.
        """
        if not isinstance(input_id, str) or not input_id:
            raise VadeError("INVALID_ARGUMENT", "input_id is required",
                            data={"field": "input_id"})

        async with self._lock:
            pending = self._pending.get(input_id)
            if pending is None:
                return self._submit_after_the_fact(session_id, input_id, value)
            if pending.session_id != session_id:
                # Never confirm that another session holds this id (§7.1).
                raise VadeError("NOT_FOUND", "unknown input request")

        # A terminal turn cannot be re-opened (S6): the answer arrives after the
        # only event that could have closed the dialog, so no event is emitted.
        live_state = self._state(session_id, pending.turn_id)
        if live_state is not None and live_state in ("completed", "failed", "interrupted", "cancelled"):
            async with self._lock:
                self._pending.pop(input_id, None)
                if not pending.future.done():
                    pending.future.cancel()
                self._remember(ResolvedInput(
                    input_id=input_id, session_id=pending.session_id,
                    turn_id=pending.turn_id, kind=pending.kind, status="cancelled",
                    value=None, state=str(live_state), by="system",
                    reason="turn_terminal", duration_ms=self._elapsed_ms(pending),
                    stored_at=self._wall(),
                ))
            raise VadeError(
                "INVALID_ARGUMENT",
                "the turn is already terminal; the input request cannot be answered",
                data={"field": "input_id", "reason": "turn_terminal", "status": live_state},
            )

        # Validate BEFORE claiming anything: a rejected value must leave the turn
        # in `waiting_input`, emit nothing and keep the id alive (§4.4 step 1).
        stored = validate_value(pending.kind, value, pending.schema, pending.option_ids,
                                max_free_text_chars=self.config.max_free_text_chars)

        async with self._lock:
            live = self._pending.get(input_id)
            if live is not pending:
                # Raced with a timeout/cancel between validation and claim.
                return self._submit_after_the_fact(session_id, input_id, stored)
            claimed = await self._claim_locked(
                pending, status="submitted", value=stored, by=by,
                reason="submitted", state="running",
            )
        await self._set_state(session_id, pending.turn_id, "running")
        await self._emit(pending, "turn.input_resolved", claimed.payload())
        self._audit_response(claimed)
        return {"ok": True, "turn_id": pending.turn_id, "status": "submitted",
                "value": stored, "duplicate": False, "input_id": input_id}

    def _submit_after_the_fact(self, session_id: str, input_id: str, value: Any) -> dict[str, Any]:
        """Rows 3–6 of §7.3, for an id that is no longer pending."""
        record = self._resolved.get(input_id)
        if record is None or record.session_id != session_id:
            raise VadeError("NOT_FOUND", "unknown input request")
        if record.status == "submitted":
            if _same_value(record.value, value):
                return {"ok": True, "turn_id": record.turn_id, "status": "submitted",
                        "value": record.value, "duplicate": True, "input_id": input_id}
            # Accepting the later value would leave two devices holding different
            # answers while exactly one `turn.input_resolved` exists (§7.3 row 4).
            raise VadeError(
                "INVALID_ARGUMENT",
                "this input request was already answered with a different value",
                data={"field": "value", "reason": "already_resolved",
                      "status": record.status},
            )
        return {"ok": True, "turn_id": record.turn_id, "status": record.status,
                "value": None, "duplicate": True, "input_id": input_id}

    # ------------------------------------------------------------------- cancel
    async def cancel(self, *, session_id: str, input_id: str, by: str = "user",
                     reason: str = "user_cancel") -> dict[str, Any]:
        """`turn/input.cancel`: close one dialog, keep the turn running (§7.2)."""
        if not isinstance(input_id, str) or not input_id:
            raise VadeError("INVALID_ARGUMENT", "input_id is required",
                            data={"field": "input_id"})
        async with self._lock:
            pending = self._pending.get(input_id)
            if pending is None:
                record = self._resolved.get(input_id)
                if record is None or record.session_id != session_id:
                    raise VadeError("NOT_FOUND", "unknown input request")
                # Already resolved: answer with the truth, emit nothing (§7.2).
                return {"ok": True, "turn_id": record.turn_id, "status": record.status,
                        "input_id": input_id}
            if pending.session_id != session_id:
                raise VadeError("NOT_FOUND", "unknown input request")
            claimed = await self._claim_locked(
                pending, status="cancelled", value=None, by=by,
                reason="user_cancel", state="running",
            )
        if claimed is None:  # pragma: no cover - still pending under this lock
            record = self._resolved.get(input_id)
            return {"ok": True, "turn_id": pending.turn_id,
                    "status": record.status if record else "cancelled", "input_id": input_id}
        await self._set_state(session_id, pending.turn_id, "running")
        await self._emit(pending, "turn.input_resolved", claimed.payload())
        self._audit_response(claimed, client_reason=reason)
        return {"ok": True, "turn_id": pending.turn_id, "status": "cancelled",
                "input_id": input_id}

    # ------------------------------------------------------- turn/session exits
    async def resolve_turn(self, turn_id: str, *, reason: str = "turn_interrupted",
                           state: str = "interrupted") -> int:
        """Close every pending question of a turn BEFORE its terminal event (S5).

        Called from `TurnQueue._finalize`, which is the only place a turn ends.
        Returns how many requests were closed (0 is the normal case).
        """
        claimed: list[tuple[PendingInput, ResolvedInput]] = []
        async with self._lock:
            for pending in [p for p in self._pending.values() if p.turn_id == turn_id]:
                record = await self._claim_locked(
                    pending, status="cancelled", value=None, by="system",
                    reason=reason, state=state,
                )
                if record is not None:
                    claimed.append((pending, record))
        for pending, record in claimed:
            await self._emit(pending, "turn.input_resolved", record.payload())
            self._audit_response(record)
        return len(claimed)

    async def on_session_end(self, session_id: str) -> int:
        """Resolve everything for a closing session (`reason:"session_closed"`)."""
        closed: list[tuple[PendingInput, ResolvedInput]] = []
        async with self._lock:
            for pending in [p for p in self._pending.values() if p.session_id == session_id]:
                record = await self._claim_locked(
                    pending, status="cancelled", value=None, by="system",
                    reason="session_closed", state="interrupted",
                )
                if record is not None:
                    closed.append((pending, record))
        for pending, record in closed:
            await self._emit(pending, "turn.input_resolved", record.payload())
            self._audit_response(record)
        return len(closed)

    async def close(self) -> None:
        for session_id in {p.session_id for p in self._pending.values()}:
            await self.on_session_end(session_id)

    # ------------------------------------------------------------------ claims
    async def _claim(self, pending: PendingInput, **kwargs: Any) -> ResolvedInput | None:
        async with self._lock:
            return await self._claim_locked(pending, **kwargs)

    async def _claim_locked(self, pending: PendingInput, *, status: str, value: Any,
                            by: str, reason: str, state: str) -> ResolvedInput | None:
        """The ONLY writer of a resolution. Exactly one caller wins per `input_id`."""
        if self._pending.get(pending.input_id) is not pending:
            return None
        record = ResolvedInput(
            input_id=pending.input_id, session_id=pending.session_id,
            turn_id=pending.turn_id, kind=pending.kind, status=status, value=value,
            state=state, by=by, reason=reason,
            duration_ms=self._elapsed_ms(pending), stored_at=self._wall(),
        )
        self._pending.pop(pending.input_id, None)
        self._remember(record)
        if not pending.future.done():
            pending.future.set_result(record)
        return record

    def _remember(self, record: ResolvedInput) -> None:
        self._resolved[record.input_id] = record
        cutoff = self._wall() - RESOLVED_TTL_S
        while self._resolved:
            oldest_key = next(iter(self._resolved))
            oldest = self._resolved[oldest_key]
            if len(self._resolved) <= RESOLVED_MAX and oldest.stored_at >= cutoff:
                break
            self._resolved.popitem(last=False)

    def _elapsed_ms(self, pending: PendingInput) -> float:
        # A fast answer can measure as exactly 0.0 on a coarse clock; the audit
        # column is read as "time the user took", and a zero there reads as a
        # missing measurement rather than a fast one.
        return max(round((self._clock() - pending.started) * 1000.0, 3), 0.001)

    # ------------------------------------------------------------------- gates
    async def _check_slot(self, session_id: str, turn_id: str) -> None:
        """S1/S2/S3/S4: at most ONE blocking wait per turn, and it must be ours."""
        async with self._lock:
            if any(p.turn_id == turn_id for p in self._pending.values()):
                raise VadeError(
                    "INVALID_ARGUMENT",
                    "this turn already has a pending input request",
                    data={"field": "turn_id", "reason": "input_pending"},
                )
        current = self._state(session_id, turn_id)
        if current is None:
            return
        if current == "waiting_approval":
            # Queuing here would decouple the dialog order from the seq order (§S3).
            raise VadeError(
                "INVALID_ARGUMENT",
                "the turn is waiting for an approval; answer that first",
                data={"field": "turn_id", "reason": "blocked_by_approval"},
            )
        if current in ("completed", "failed", "interrupted", "cancelled"):
            raise VadeError(
                "INVALID_ARGUMENT",
                "the turn is already terminal",
                data={"field": "turn_id", "reason": "turn_terminal", "status": current},
            )

    async def _enforce_rate(self, session_id: str, turn_id: str) -> None:
        now = self._clock()
        hits = self._session_hits.setdefault(session_id, deque())
        while hits and now - hits[0] > 60.0:
            hits.popleft()
        if len(hits) >= MAX_PER_MINUTE:
            retry_after = max(1, int(60.0 - (now - hits[0])))
            raise VadeError(
                "OVERLOADED",
                f"at most {MAX_PER_MINUTE} input requests per minute per session",
                data={"retry_after": retry_after},
            )
        count = self._turn_hits.get(turn_id, 0)
        if count >= MAX_PER_TURN:
            raise VadeError(
                "OVERLOADED",
                f"at most {MAX_PER_TURN} input requests per turn",
                data={"retry_after": 60},
            )
        hits.append(now)
        self._turn_hits[turn_id] = count + 1
        self._turn_hits.move_to_end(turn_id)
        while len(self._turn_hits) > 512:
            self._turn_hits.popitem(last=False)

    # ----------------------------------------------------------------- helpers
    def _state(self, session_id: str, turn_id: str) -> str | None:
        if self._state_probe is None:
            return None
        try:
            return self._state_probe(session_id, turn_id)
        except Exception:  # pragma: no cover - a probe must never break a turn
            return None

    async def _set_state(self, session_id: str, turn_id: str, state: str) -> None:
        if self._state_hook is None:
            return
        try:
            result = self._state_hook(session_id, turn_id, state)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        except Exception:  # pragma: no cover - state write is best effort
            return

    async def _emit(self, pending: PendingInput, event_type: str,
                    payload: Mapping[str, Any]) -> None:
        if pending.emit is None:
            return
        try:
            await pending.emit(event_type, payload)
        except Exception:
            # A broken UI sink must not abort the wait: the request is still
            # answerable through `turn/input` and the snapshot (§5.3).
            return

    # ------------------------------------------------------------------ auditing
    def _audit_request(self, session_id: str, turn_id: str, record: Mapping[str, Any],
                       input_id: str | None, *, status: str, reason: str,
                       actor: str) -> None:
        if self._audit is None:
            return
        options = record.get("options") or ()
        event = {
            "kind": "input_request", "status": status, "request_id": input_id,
            "session_id": session_id, "turn_id": turn_id, "actor": actor,
            "duration_ms": 0.0,
            "extra": {
                "input_kind": record.get("kind"),
                "timeout_s": record.get("timeout_s"),
                "prompt_chars": len(str(record.get("prompt") or "")),
                "option_count": len(options),
                "source_depth": int((record.get("source") or {}).get("depth") or 0),
                "reason": reason,
            },
        }
        try:
            self._audit.record(event)
        except Exception:  # pragma: no cover
            pass

    def _audit_response(self, record: ResolvedInput, *, client_reason: str | None = None) -> None:
        """One row per answer. `value` never appears: digest + length only (§9.4).

        `[ask_user] audit_value` is accepted by C-18 but NOT honoured in v1: a
        free-text answer routinely contains a secret, and the audit table is
        long-lived and exportable. Turning that on is a product decision with a
        UI disclosure attached (Q5), not a hidden config flag.
        """
        if self._audit is None:
            return
        extra: dict[str, Any] = {
            "input_kind": record.kind,
            "timed_out": record.status == "timeout",
            "reason": record.reason,
        }
        if record.status == "submitted" and record.value is not None:
            extra["value_digest"] = value_digest(record.value)
            extra["value_chars"] = value_chars(record.value)
        if client_reason:
            extra["client_reason"] = str(client_reason)[:200]
        event = {
            "kind": "input_response", "status": record.status,
            "request_id": record.input_id, "session_id": record.session_id,
            "turn_id": record.turn_id, "actor": record.by,
            "duration_ms": record.duration_ms, "extra": extra,
        }
        try:
            self._audit.record(event)
        except Exception:  # pragma: no cover
            pass


def _actor(source: Mapping[str, Any]) -> str:
    tool = str(source.get("tool") or "ask_user")
    module_id = source.get("module_id")
    return f"module:{module_id}" if module_id else f"tool:{tool}"


def _same_value(left: Any, right: Any) -> bool:
    """Strict equality: `True == 1` in Python, but not in this protocol."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if type(left) is not type(right) and not (isinstance(left, (list, tuple)) and isinstance(right, (list, tuple))):
        return False
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            isinstance(a, str) and isinstance(b, str) and a == b for a, b in zip(left, right)
        )
    return left == right
