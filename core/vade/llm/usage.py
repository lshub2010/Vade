"""Usage views + budget gate (RFC-0005 §6).

One dataset, two views, both reconciled against `turns.usage` and the audit
`llm.call` rows (D102):

* `session/usage.get` — per-session cumulative (`chat`), per-model split
  (`by_model`), auxiliary calls (`aux`, read from the audit trail because it is
  the only place they exist), and the day aggregate;
* the budget gate used by `ui/submit` — session and day limits, `ok|warn|
  exceeded|off`, and the §6.4 rule that a hard block degrades to a warning when
  the books are not trustworthy (`source="none"`).

Nothing here estimates tokens: a number that was never reported is 0 and the
turn is counted in `partial_turns` instead.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ..errors import VadeError
from ..session.models import Session, Turn, TurnState

__all__ = ["BudgetConfig", "UsageAccounting", "TERMINAL_MEASURED"]

#: Terminal states whose usage is part of the session total. `cancelled` is a
#: turn that never reached the model, so it has nothing to account for.
TERMINAL_MEASURED = (TurnState.COMPLETED, TurnState.FAILED, TurnState.INTERRUPTED)

_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    """`[usage]` (C-18 append). Defaults are the contract's, not this file's."""

    enabled: bool = True
    session_tokens: int = 0
    daily_tokens: int = 0
    warn_ratio: float = 0.8
    include_aux: bool = True
    enforce: str = "block"

    @classmethod
    def from_config(cls, config: Any) -> "BudgetConfig":
        def get(key: str, default: Any) -> Any:
            value = config.get(f"usage.{key}", default)
            return default if value is None else value

        warn = float(get("warn_ratio", 0.8))
        return cls(
            enabled=bool(get("enabled", True)),
            session_tokens=int(get("session_tokens", 0) or 0),
            daily_tokens=int(get("daily_tokens", 0) or 0),
            warn_ratio=min(max(warn, 0.1), 1.0),
            include_aux=bool(get("include_aux", True)),
            enforce=str(get("enforce", "block") or "block"),
        )


def budget_state(used: int, limit: int, warn_ratio: float) -> str:
    """`off` (no limit) -> `ok` -> `warn` -> `exceeded` (§6.4)."""
    if limit <= 0:
        return "off"
    if used >= limit:
        return "exceeded"
    if used >= limit * warn_ratio:
        return "warn"
    return "ok"


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


class UsageAccounting:
    """Read-side accounting for `session/usage.get` + the submit-time gate."""

    def __init__(
        self,
        *,
        config: Any,
        store: Any,
        audit_reader: Any = None,
        clock: Callable[[], float] = time.time,
        emit: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.audit_reader = audit_reader
        self._clock = clock
        self._emit = emit
        self._sent: set[tuple[str, str, str]] = set()
        self.budget = BudgetConfig.from_config(config)

    # ------------------------------------------------------------------ audit
    def _llm_calls(self, *, session_id: str | None = None, from_ts: float | None = None) -> list[dict[str, Any]]:
        """Every `llm.call` row, paged past the reader's 500-row page cap.

        `audit/reader.py` clamps `limit` to 500 on purpose (interactive paging);
        the day total must not be truncated by that, so this walks `next_cursor`
        until the page is exhausted. The evolve budget in `admin/app.py` uses
        the same walk for the same reason (§6.5 rule 4).
        """
        if self.audit_reader is None:
            return []
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.audit_reader.query(kind="llm.call", from_ts=from_ts, limit=500, cursor=cursor)
            items = page.get("items") or []
            rows.extend(items)
            cursor = page.get("next_cursor")
            if not cursor:
                break
        if session_id is not None:
            rows = [row for row in rows if row.get("session_id") == session_id]
        return rows

    @staticmethod
    def _purpose(row: Mapping[str, Any]) -> str:
        extra = row.get("extra") or {}
        return str(extra.get("purpose") or "chat")

    @staticmethod
    def _row_tokens(row: Mapping[str, Any]) -> int:
        extra = row.get("extra") or {}
        return _int(extra.get("total_tokens", extra.get("tokens", 0)))

    def _day_start(self) -> tuple[str, float]:
        now = self._clock()
        stamp = time.gmtime(now)
        day = time.strftime("%Y-%m-%d", stamp)
        # The day boundary is UTC, aligned with `audit/writer.day_key()` and
        # the admin's今日 view (§6.3 / Q3). `time.mktime` is local time and
        # would move "today" for every non-UTC host.
        start = calendar.timegm((stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 0, 0, 0, 0, 0, 0))
        return day, float(start)

    # ------------------------------------------------------------------ report
    def report(self, session: Session) -> dict[str, Any]:
        measured = [t for t in session.turns if t.state in TERMINAL_MEASURED]
        chat = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "turns": len(measured), "partial_turns": 0}
        by_model: dict[str | None, dict[str, Any]] = {}
        for turn in measured:
            usage = dict(turn.usage or {})
            if not usage or usage.get("source") == "none":
                # §6.3: a turn whose provider never reported is honestly
                # "unmeasured", not zero tokens that were really free.
                chat["partial_turns"] += 1
            for key in _TOKEN_KEYS:
                chat[key] += _int(usage.get(key, 0))
            model_id = usage.get("model_id")
            bucket = by_model.setdefault(model_id, {"model_id": model_id, "total_tokens": 0, "turns": 0})
            bucket["total_tokens"] += _int(usage.get("total_tokens", 0))
            bucket["turns"] += 1

        day, day_start = self._day_start()
        session_rows = self._llm_calls(session_id=session.id)
        aux_rows = [row for row in session_rows if self._purpose(row) != "chat"]
        aux = {"total_tokens": sum(self._row_tokens(row) for row in aux_rows), "calls": len(aux_rows)}

        today_rows = self._llm_calls(from_ts=day_start)
        core_today = sum(self._row_tokens(row) for row in today_rows)
        session_today = sum(self._row_tokens(row) for row in today_rows if row.get("session_id") == session.id)

        total = chat["total_tokens"] + aux["total_tokens"]
        budget_used = total if self.budget.include_aux else chat["total_tokens"]
        session_budget = self._budget_block("session", budget_used, self.budget.session_tokens)
        day_state = budget_state(core_today, self.budget.daily_tokens, self.budget.warn_ratio)

        return {
            "session_id": session.id,
            "chat": chat,
            "aux": aux,
            "total_tokens": total,
            "by_model": [
                {"model_id": bucket["model_id"], "total_tokens": bucket["total_tokens"],
                 "turns": bucket["turns"]}
                for bucket in sorted(by_model.values(),
                                     key=lambda b: (-b["total_tokens"], str(b["model_id"] or "")))
            ],
            "day": {
                "day": day,
                "core_total_tokens": core_today,
                "core_limit": self.budget.daily_tokens,
                "session_tokens": session_today,
                "state": day_state,
            },
            "budget": session_budget,
        }

    def _budget_block(self, scope: str, used: int, limit: int) -> dict[str, Any]:
        state = budget_state(used, limit, self.budget.warn_ratio)
        return {
            "scope": scope,
            "limit": limit,
            "used": used,
            "ratio": (used / limit) if limit > 0 else None,
            "state": state,
            # `enforced` must agree with `gate()`: a disabled budget keeps
            # accounting (and still reports `exceeded`) but refuses nothing, so
            # a UI that showed "you are blocked" would be lying.
            "enforced": state == "exceeded" and self.budget.enforce == "block" and self.budget.enabled,
        }

    # ------------------------------------------------------------------ gate
    def _trusted(self, session: Session) -> bool:
        """Is this session's ledger trustworthy? (§6.4)

        Trust is a property of the *provider*, so only turns that could have
        carried a measurement count as evidence. Walk the terminal turns from
        newest to oldest:

        * a turn with a real `source` (anything but `none`) proves the provider
          reports usage -> trustworthy, even if a later turn failed before it
          could be measured (a network error is not "this provider never
          reports"; letting one failure poison the ledger would silently turn
          the hard block off for the rest of the session);
        * a *completed* turn whose `source` is missing or `none` is evidence
          that the provider did not report -> untrustworthy. A pre-RFC-0005 row
          (no `source` at all) is just as unmeasured: "unknown" must never be
          read as "trustworthy";
        * a failed/interrupted turn with no measurement says nothing about the
          provider — skip it and keep looking;
        * no evidence at all (an empty session) is trusted: there is nothing to
          block and nothing to invent.
        """
        measured = [t for t in session.turns if t.state in TERMINAL_MEASURED]
        if not measured:
            return True
        for turn in reversed(measured):
            usage = dict(turn.usage or {})
            source = usage.get("source")
            if source is not None and source != "none":
                return True
            if turn.state is TurnState.COMPLETED:
                return False
        return False

    @staticmethod
    def _candidate_rank(candidate: tuple[str, str, int, int]) -> tuple[int, float, int]:
        """Order gate candidates: severity, then how far past the limit, then day.

        `max()` alone returns the first candidate on a tie, i.e. it always
        reported `session` when both scopes were `exceeded` — a user starting a
        new session while the day budget was the binding limit got told the
        wrong scope. The overrun ratio is the closest honest answer to "which
        limit is actually holding the turn back"; an exact tie goes to `day`
        because a daily limit refuses every session, not just this one.
        """
        scope, state, used, limit = candidate
        severity = {"off": 0, "ok": 0, "warn": 1, "exceeded": 2}
        ratio = (used / limit) if limit > 0 else 0.0
        return (severity.get(state, 0), ratio, 1 if scope == "day" else 0)

    async def gate(self, session: Session) -> dict[str, Any]:
        """Submit-time budget decision (§6.4).

        Returns `{"allowed": bool, "code": str|None, "data": {...}}`. Emits
        `usage.budget` once per `(scope, state, period)` — dedupe lives here,
        not in the UI, because the event is a notification and `usage.get` is
        the truth.
        """
        report = self.report(session)
        session_budget = report["budget"]
        day = report["day"]
        candidates = [
            ("session", session_budget["state"], session_budget["used"], session_budget["limit"]),
            ("day", day["state"], day["core_total_tokens"], day["core_limit"]),
        ]
        scope, state, used, limit = max(candidates, key=self._candidate_rank)
        if state == "warn":
            # `warn` never blocks and is never `degraded` (§7.4): nothing was
            # suppressed, so there is nothing to disclose.
            if self.budget.enabled:
                await self._notify(session, scope, state, used, limit, degraded=False)
            return {"allowed": True, "code": None, "data": None}

        if state != "exceeded":
            return {"allowed": True, "code": None, "data": None}

        trusted = self._trusted(session)
        blocked = trusted and self.budget.enforce == "block" and self.budget.enabled
        if self.budget.enabled:
            # `degraded=true` means exactly one thing: the turn should have
            # been refused but the ledger is untrustworthy (§6.4).
            await self._notify(session, scope, state, used, limit, degraded=not trusted)
        if blocked:
            return {
                "allowed": False,
                "code": "BUDGET_EXCEEDED",
                "data": {"state": "exceeded", "scope": scope, "used": used, "limit": limit},
            }
        return {"allowed": True, "code": None, "data": None}

    async def _notify(
        self,
        session: Session,
        scope: str,
        state: str,
        used: int,
        limit: int,
        *,
        degraded: bool,
    ) -> None:
        period = session.id if scope == "session" else self._day_start()[0]
        key = (scope, state, period)
        if key in self._sent:
            return
        self._sent.add(key)
        if self._emit is None:
            return
        payload = {
            "scope": scope,
            "state": state,
            "used": int(used),
            "limit": int(limit),
            "ratio": (used / limit) if limit > 0 else 0.0,
            "degraded": bool(degraded),
            "session_id": session.id,
        }
        await self._emit(session.id, "usage.budget", payload)


def require_session(store: Any, session_id: str) -> Session:
    """Small helper shared by the RPC handlers (NOT_FOUND, never a KeyError)."""
    session = store.load(session_id)
    if session is None:
        raise VadeError("NOT_FOUND", f"session {session_id!r} does not exist")
    return session


def model_provider(model_id: str | None) -> str | None:
    """`builtin` or a `module_id` — the first `/` splits the id (§4.2)."""
    if not model_id:
        return None
    return model_id.split("/", 1)[0]


def aggregate(rows: Sequence[Mapping[str, Any]]) -> int:
    """Sum `extra.total_tokens` over audit rows (used by the admin view too)."""
    return sum(UsageAccounting._row_tokens(row) for row in rows)
