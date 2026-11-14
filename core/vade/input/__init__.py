"""Ask-user component (RFC-0008): `InputManager` behind `app.input_port`.

`install(app)` is discovered by C2's module scan like every other component. It
must be safe on a bare app: the session log may not exist yet (component install
order is alphabetical, `input` < `session`), so every seam is resolved lazily
through `app` at *call* time rather than captured at install time.

The three seams are deliberately small:

* `state_probe` -> the live turn state, so a request while `waiting_approval`
  is refused (`blocked_by_approval`, S3) and a terminal turn cannot be re-opened;
* `state_hook`  -> writes `waiting_input` / `running` into the queue, which
  persists it (the event itself carries `state`, so there is exactly one
  state-change event per transition);
* `ui_probe`    -> attached UI count; 0 means "cancel now" instead of burning
  the timeout (§8.4).
"""

from __future__ import annotations

from typing import Any

from .manager import InputManager, PendingInput, ResolvedInput, new_input_id
from .models import (
    DETAIL_MAX,
    FREE_TEXT_DEFAULT_MAX,
    FREE_TEXT_HARD_MAX,
    KINDS,
    LABEL_MAX,
    OPTIONS_MAX,
    OPTIONS_MIN,
    PROMPT_MAX,
    InputConfig,
    InputKind,
    InputOption,
    InputOutcome,
    InputRequest,
    normalize_request,
    schema_for,
    validate_value,
    value_chars,
    value_digest,
)

__all__ = [
    "install", "InputManager", "InputConfig", "InputKind", "InputOption",
    "InputOutcome", "InputRequest", "PendingInput", "ResolvedInput", "new_input_id",
    "normalize_request", "validate_value", "schema_for", "value_digest", "value_chars",
    "PROMPT_MAX", "DETAIL_MAX", "LABEL_MAX", "OPTIONS_MIN", "OPTIONS_MAX",
    "FREE_TEXT_DEFAULT_MAX", "FREE_TEXT_HARD_MAX", "KINDS",
]


def install(app: Any) -> None:
    if getattr(app, "input_port", None) is not None:
        return
    config = InputConfig.from_config(getattr(app, "config", None))
    manager = InputManager(config=config, audit=getattr(app, "audit", None))

    manager.set_ui_probe(lambda session_id: _ui_count(app, session_id))
    manager.set_state_probe(lambda session_id, turn_id: _turn_state(app, session_id, turn_id))
    manager.set_state_hook(lambda session_id, turn_id, state: _mark_state(
        app, session_id, turn_id, state
    ))
    app.input_port = manager
    app.inputs = manager
    # `ask_user` is the tool-facing name; a caller that has no engine wiring can
    # still reach the channel through the same object.
    app.ask_user = manager


def _ui_count(app: Any, session_id: str) -> int:
    log: Any = getattr(app, "session_log", None)
    attached: Any = getattr(log, "attached_count", None)
    if callable(attached):
        try:
            return int(attached(session_id))
        except Exception:  # pragma: no cover - a broken probe must not raise
            return 0
    return 0


def _turn_state(app: Any, session_id: str, turn_id: str) -> str | None:
    log: Any = getattr(app, "session_log", None)
    probe: Any = getattr(log, "turn_state", None)
    if callable(probe):
        try:
            state = probe(session_id, turn_id)
        except Exception:  # pragma: no cover
            return None
        return str(state) if state is not None else None
    return None


async def _mark_state(app: Any, session_id: str, turn_id: str, state: str) -> None:
    log: Any = getattr(app, "session_log", None)
    mark: Any = getattr(log, "mark_state", None)
    if not callable(mark):
        return
    from ..session.models import TurnState

    try:
        await mark(session_id, turn_id, TurnState(state))
    except Exception:  # pragma: no cover - an unknown state must not break the wait
        return
