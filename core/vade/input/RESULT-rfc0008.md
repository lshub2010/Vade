# RESULT · RFC-0008 ask-user (waiting_input / turn.input_* / turn/input)

Branch `tmp/rfc0008`. Baseline: 555 passing tests. After this batch: **611**.

## What landed

| Area | File | Change |
|---|---|---|
| State machine | `core/vade/session/models.py` | `TurnState.WAITING_INPUT` right after `WAITING_APPROVAL`; `TERMINAL_STATES` untouched |
| State machine | `core/vade/session/queue.py` | `state_of(turn_id)` (the only honest source for S2/S3/S6) and a `pre_terminal_hook` awaited at the top of `_finalize` (S5: dialog closes before the terminal event) |
| State machine | `core/vade/session/manager.py` | `input_port` slot, `turn_state()` (falls back to the persisted row, so a finished turn cannot be re-opened), `_input_pre_terminal`, `snapshot.pending_inputs[]`, pending inputs closed in `close()` before the sinks go away |
| Events | `schema/events.json` | **C-06 1.0.0 → 2.0.0**; `waiting_input` added to `turn_states` + `events["turn.state"].enum`; `turn.input_required` / `turn.input_resolved`; `x-vade.appends[]` block (`enum_widen`, `adds`, `rewrites: none`); one rule for "unknown event/state must be ignored/rendered as running" |
| Events | `core/vade/input/manager.py` | `PendingInput.required_payload()` / `ResolvedInput.payload()` are the only producers of the two events |
| RPC | `schema/rpc.json` | `turn/input`, `turn/input.cancel` under `ui_to_core` + `ui/attach` notes for `pending_inputs`; `x-vade.append_rfc0008` |
| RPC | `core/vade/session/rpc.py` | Both handlers, `_input_ids()` guard (ids + explicit read-only refusal), lazy `input_port` resolution |
| Controls | `core/vade/input/models.py` | `InputKind` (4), `InputOption`, `InputRequest`, `InputOutcome` (`.tool_result()`), `InputConfig`, `normalize_request()` (sender-side) and `validate_value()` (closed, core-side) |
| Manager | `core/vade/input/manager.py` | `InputManager`: single-slot gate, rate limit, no-UI/subagent short-circuit, shield+claim timeout, idempotency cache (LRU 256 / 24h), audit |
| Wiring | `core/vade/input/__init__.py` | `install(app)` (auto-discovered): `app.input_port`, `app.inputs`, `app.ask_user`; probes resolved through `app` at call time |
| Ports | `core/vade/loop/ports.py` | `InputPort` / `InputPrompt` / `InputAnswer` / `NullInputPort` (`CANCELLED{no_ui}`) |
| Config | `schema/config.schema.json` | `[ask_user]` with the six keys, all `apply: hot` |
| Gate | `tools/check_contracts.py` | both events in `STATE_CHANGE_EVENTS`, `turn/input*` in the method set, C-06 `2.0.0` + append-block assertions |
| Gate | `core/tests/events/test_schema_append.py` | 7 → 8 states, C-06 `2.0.0`, new required lists, append-block test |

## Behaviours verified by tests (`core/tests/input/`, 55 tests)

- **A1** `test_free_text`: `state:"waiting_input"` on the event *and* in the queue; `resolved` seq < `tool_result`; the value comes back verbatim.
- **A2** `test_value_encoding`: `single_choice` → id string, `multi_choice` → ordered array, `confirm` → JSON boolean (`"true"` refused).
- **A3** `test_validation`: unknown id / duplicate elements / over max_select / blank text / over max_length / wrong JSON type → `INVALID_ARGUMENT` or `PAYLOAD_TOO_LARGE` with `data.field="value"`, **no event, no state change, id still answerable**.
- **A4** `test_timeout`: `status:"timeout"`, `by:"system"`, `value:null`, model gets `CANCELLED`; `default:"SENTINEL"` is never applied (absent from the tool result).
- **A5** `test_cancel`: cancel closes one dialog, the turn keeps producing deltas; two cancels → one event.
- **A6** `test_idempotency`: rows 1/3/4/5/6 (duplicate, `already_resolved`, late submit after cancel/timeout, unknown id, other session → `NOT_FOUND`).
- **A8** `test_snapshot_pending`: `snapshot.pending_inputs[]` on the snapshot path, `turn.input_required` on the replay path, resolved ids absent.
- **A9** `test_state_machine`: `blocked_by_approval` (state unchanged, no event), `input_pending`, `running ↔ waiting_input`, terminal turn → `turn_terminal`.
- **A10** `test_interrupt_order`: `input_resolved{status:"cancelled", by:"system", reason:"turn_interrupted", state:"interrupted"}` strictly before `turn.interrupted`, nothing `turn.*` after it.
- **A11** `test_no_ui`: `ui_count==0` → `CANCELLED{no_ui}` in <0.5 s, neither event emitted, deadline not consumed; subagents off by default, on via config.
- **A12** `test_read_only`: `READ_ONLY_SESSION` for both methods on an explicit read-only session, request untouched, other methods unaffected.
- **A13** `test_audit`: `input_request` + `input_response` rows, `duration_ms>0`, `timed_out` explicit, `value_digest`/`value_chars` only — the raw answer is absent from the whole table.
- **A14** `test_event_compat`: an old 7-state client ignores both events, advances `last_seq`, still renders the turn; schema/enum pin.
- **Security** `test_security`: control chars stripped from prompt/labels, 500/120/240 caps, `$(rm -rf /)` travels as data, an answer never produces an approval event.

## Deviations (honest list)

1. **`ask_user` is not registered as an engine builtin tool.** `InputPort`/`NullInputPort` exist in `loop/ports.py`, but `LoopEngine` has no builtin-tool registry (tools come from the router snapshot), and adding one changes prompt rendering + `tools.py`. The channel, state machine, events, RPC and audit are complete and tested; wiring the tool schema is a follow-up slice.
2. **`events.json` uses `x-vade.appends[]`**, the shape RFC-0008 §10.2 specifies. There is no RFC-0007 block in this worktree (`x-vade.append_rfc0007` does not exist here), so no collision to avoid; the array absorbs a later append-0007 entry without a merge conflict on the key. `rpc.json` uses `x-vade.append_rfc0008` (its `x-vade.appends` is a string owned by RFC-0004).
3. **`[ask_user] audit_value` is declared but not honoured**: even when `true`, only `sha256(value)` + length reach the audit table. Storing the raw value is Q5's product decision with a UI disclosure; the schema note says so.
4. **`timeout_s` is clamped to `[5, 600]`** (config `min_timeout_s`/`max_timeout_s`), so RFC §11's A4 "`timeout_s=1`" is expressed in tests by shrinking `InputConfig.min_timeout_s`; the wire contract keeps the documented 5..600 range.
5. **`turn/input.cancel`'s free-form `reason`** never reaches the event (the payload's `reason` stays in the documented enum, `user_cancel`); it is recorded as `client_reason` on the audit row instead.

## Gates

```
pytest core/tests -q       611 passed
python tools/check_contracts.py   contracts: OK
python docs/check_docs.py         12 项，失败 0 项
```
