#!/usr/bin/env python3
"""Contract gate #2: schema files agree with docs/agents/01-contracts.md.

Checks (each one has broken at least once in review):
  1. `schema/*.json` parse and declare the right contract id.
  2. `errors.json` code set == the code block in 01-contracts §二 (34 codes).
  3. Every Rust `ErrorCode` variant exists in errors.json (no invented codes).
  4. `config.schema.json` covers the required sections and every leaf carries
     x-vade.apply (hot|restart) and x-vade.owner.
  5. `manifest.schema.json` is forward compatible (additionalProperties true)
     and its limits agree with C-01's 65519-byte ceiling.
  6. `rpc.json` declares the RPC methods C3/C4 consume.
  7. `events.json` (C-06) agrees with the code that emits it: every statically
     emitted event name exists in the schema, payload `required` fields are a
     subset of the declared payload, `state` is present exactly on the
     state-change events (rule 4), and `turn_states` matches `TurnState`.
  8. C-14's push topics are checked at their own emit site: `EventBus._dispatch`
     is the only producer of `events/deliver` notifications, so it is scanned
     instead of being exempted. Every entry the schema marks
     `x-vade.push: events/deliver` must be pushed, every pushed topic must be
     declared, and the marked set must equal the topic set the bus serves.
  9. RFC-0004: `[attach]` carries exactly the 17 keys of §7.4 with their
     documented defaults/apply semantics, and `core/vade/blob/http.py` declares
     the six `/ui/blob*` routes the RFC froze.
 10. RFC-0005: `[usage]` carries exactly the six keys of §6.4 with their
     documented defaults (all hot), `[llm]` carries the four keys this RFC
     appends to C-18, `capabilities.models` keeps its `maxItems: 16` / required
     `name` shape AND is still parsed into `Manifest.models` (a schema field
     core drops is worse than no field), `usage.budget` is declared in C-06 with
     its six required keys, and the four new methods live in `ui_to_core`.
 11. RFC-0010: the ten `session/*` metadata methods are declared (C-05 1.3.0)
     and registered in `SessionRPC.handlers()`, the §7 validation constants
     (icon whitelist + pattern, `#RRGGBB`, name limits) live in
     `core/vade/session/manager.py`, and the two C-06 metadata events exist
     with their frozen `required` arrays and no `state` / push pollution.
 12. RFC-0012: the three `subagent.*` events are declared (C-06, top-level
     `subagent_states` == `SubagentState`) with their frozen `required`
     arrays, `subagent.spawned` is payload-only / the other two are
     state-change events, no result-text key exists on any of them, the two
     read-only methods are declared (C-05 1.4.0) and registered in
     `SubagentRPC.handlers()`, and `ALLOWED_TRANSITIONS` (the §7.2 gate)
     refuses exactly the twelve transitions the RFC says it must.
 13. RFC-0014: `session/search` is declared (C-05 1.5.0) and registered in
     `SessionRPC.handlers()`, the §4 constants (query 200 / limit 1..100 /
     default 20 / snippet 120) live in `core/vade/session/search.py`,
     `escape_like` touches only the three LIKE metacharacters (a quote and
     `--` stay literal — parameterization's job), the store binds the pattern
     (`LIKE ? ESCAPE`) and never interpolates it, and the `x-vade.appends`
     block carries `adds=["session/search"]` / `rewrites: none` / 1.5.0.
 14. RFC-0016: `ui/blob.token` is declared (C-05 1.7.0) with the §4 shape
     (params exactly {ttl_s}, result token/expires_at/max_bytes/endpoints{6},
     the four reused C-04 codes), registered in `BlobTokenRPC.handlers()`,
     the `TOKEN_TTL_*` constants are 600/60/900, the `endpoints` aliases match
     the `http_endpoints` rows verbatim, `http_endpoints.auth.device` names
     the minting method, and the append block carries
     `adds=["ui/blob.token"]` / `rewrites: none` / 1.7.0.

Run:  python3 tools/check_contracts.py      (exit 0 = green)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema"
CONTRACTS = ROOT / "docs" / "agents" / "01-contracts.md"
ERROR_RS = ROOT / "protocol" / "core" / "src" / "error.rs"
CORE = ROOT / "core" / "vade"

#: Events emitted through a variable (`queue.py` maps a TurnState to a name);
#: the static scan cannot see them, so they are listed and checked explicitly.
DYNAMIC_EVENT_TYPES = {"turn.completed", "turn.failed", "turn.interrupted", "turn.cancelled"}

#: C-14 push topics: emitted as `events/deliver` notifications, not as
#: `SessionLog.emit()` events. The marker lives on the event entry itself, so a
#: new push topic cannot be exempted by editing this file.
PUSH_TRANSPORT = "events/deliver"

#: C-06 rule 4: `state` is required on exactly these, forbidden as a required
#: field on the high-frequency payload events.
STATE_CHANGE_EVENTS = {
    "turn.queued", "turn.started", "turn.completed", "turn.failed",
    "turn.interrupted", "turn.cancelled", "turn.approval_resolved",
    # RFC-0008 §10.1 row 4: both carry `state` and are therefore state-change
    # events. Entering `waiting_input` IS `turn.input_required`; leaving it is
    # `turn.input_resolved` (state=running|interrupted). Not registering them
    # would fail the C-06 rule-4 scan below.
    "turn.input_required", "turn.input_resolved",
    # RFC-0007 §5.2/§5.3: the task family. `task.started` is a state-change
    # event too (state ∈ {queued, running}); `task.progress` is the opposite.
    "task.started", "task.status", "task.finished",
    # RFC-0012 §5.2: the subagent family. `subagent.status` / `subagent.result`
    # carry `state`; `subagent.spawned` does not (creation IS the event name).
    "subagent.status", "subagent.result",
}
PAYLOAD_ONLY_EVENTS = {
    "turn.delta", "turn.tool_call", "turn.tool_result",
    "task.progress",
    "subagent.spawned",
}

#: RFC-0012 §4.3: the frozen `subagent_states` set and its terminal three.
#: Kept here (not derived from the schema) so a silent enum change goes red.
SUBAGENT_STATES = {"spawned", "running", "completed", "failed", "cancelled"}
SUBAGENT_TERMINAL_STATES = {"completed", "failed", "cancelled"}

#: RFC-0012 §5.2: the three `required` arrays must be byte-identical —
#: required-with-null is the self-describing rule, and dropping a key silently
#: breaks client tree-building (RFC-0007 §9.2 precedent).
RFC0012_REQUIRED = {
    "subagent.spawned": [
        "run_id", "session_id", "parent_session_id", "parent_turn_id",
        "child_session_id", "label", "at",
    ],
    "subagent.status": ["run_id", "session_id", "state", "at"],
    "subagent.result": [
        "run_id", "session_id", "state", "result_digest", "result_chars",
        "finished_at",
    ],
}

#: RFC-0012 §5.3: no subagent.* event may carry the result text under any name.
RFC0012_FORBIDDEN_PAYLOAD_KEYS = {"result_text", "summary", "output", "text"}

#: RFC-0007 §9.4: the frozen `[tasks]` key set, defaults and apply semantics.
#: Kept here (not derived from the schema) so a silent default change goes red.
TASKS_DEFAULTS: dict[str, object] = {
    "tasks.max_concurrent": 8,
    "tasks.max_depth": 4,
    "tasks.ui_fold_depth": 3,
    "tasks.history_limit": 50,
    "tasks.snapshot_finished": 20,
    "tasks.log_lines": 200,
    "tasks.log_bytes": 65536,
    "tasks.log_ring_lines": 2000,
    "tasks.log_line_chars": 4096,
    "tasks.progress_hz": 2,
    "tasks.event_burst": 20,
    "tasks.cancel_grace_ms": 2000,
    "tasks.subagent_message": False,
}
TASKS_HOT = {
    "tasks.max_concurrent",
    "tasks.ui_fold_depth",
    "tasks.history_limit",
    "tasks.snapshot_finished",
    "tasks.log_lines",
    "tasks.log_bytes",
    "tasks.log_line_chars",
    "tasks.progress_hz",
    "tasks.event_burst",
    "tasks.cancel_grace_ms",
}

#: RFC-0007 §5.2: `task_states`, terminal three, and the two hard ceilings.
TASK_TERMINAL_STATES = {"completed", "failed", "cancelled"}

#: RFC-0006 §6.4: names emitted inside an `emit_batch([("name", {...}), ...])`.
_BATCH_EVENT_LITERAL = re.compile(r"\(\s*[\"']([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)[\"']\s*,")

#: RFC-0006 §9.1: the `required` arrays must be byte-identical to 1.0.0 —
#: every field this RFC adds is optional. Hardcoded so a "helpful" promotion of
#: a new field to required goes red instead of shipping a 1.1 break.
RFC0006_REQUIRED = {
    "turn.delta": ["turn_id", "text"],
    "turn.tool_call": ["turn_id", "call_id", "tool", "args_digest"],
    "turn.tool_result": ["turn_id", "call_id", "ok", "summary"],
    "turn.interrupted": ["turn_id", "state", "by"],
    "turn.queued": ["turn_id", "state"],
}

#: RFC-0006 §5.1/§7.1/§6.1: appended optional fields per event.
RFC0006_FIELDS = {
    "turn.delta": {"part_index", "kind"},
    "turn.tool_call": {"args_preview", "args_preview_mode", "args_preview_redacted",
                       "args_preview_truncated", "started_at", "batch_id"},
    "turn.tool_result": {"duration_ms", "truncated", "result_ref"},
    "turn.interrupted": {"partial_message_id", "partial_chars"},
    "turn.queued": {"root_turn_id", "version", "parent_turn_id", "user_message_id", "reason"},
}

#: RFC-0004 §7.4: the frozen `[attach]` key set, defaults and apply semantics.
#: Kept here (not derived from the schema) so a silent default change goes red.
ATTACH_DEFAULTS: dict[str, object] = {
    "attach.enabled": True,
    "attach.max_bytes": 104857600,
    "attach.image.max_bytes": 5242880,
    "attach.image.max_edge": 2048,
    "attach.image.max_pixels": 4194304,
    "attach.part_size": 10485760,
    "attach.part_max_concurrent": 3,
    "attach.max_per_turn": 10,
    "attach.text_max_chars": 32000,
    "attach.ttl_hours": 1,
    "attach.upload_idle_ttl_min": 15,
    "attach.quota_bytes": 2147483648,
    "attach.gc_interval_s": 60,
    "attach.thumb_px": 320,
    "attach.read_scope": "core",
    "attach.scan.enabled": False,
    "attach.scan.command": "",
}
ATTACH_HOT = {
    "attach.part_max_concurrent",
    "attach.max_per_turn",
    "attach.text_max_chars",
    "attach.upload_idle_ttl_min",
    "attach.quota_bytes",
    "attach.gc_interval_s",
    "attach.thumb_px",
    "attach.read_scope",
}

#: RFC-0004 §4.1 route table; `GET|HEAD` is one row in the RFC.
BLOB_ROUTES = {
    ("POST", "/ui/blob"),
    ("PUT", "/ui/blob/upload/{upload_id}"),
    ("HEAD", "/ui/blob/upload/{upload_id}"),
    ("POST", "/ui/blob/upload/{upload_id}/complete"),
    ("DELETE", "/ui/blob/upload/{upload_id}"),
    ("GET|HEAD", "/ui/blob/{file_id}"),
}

#: RFC-0005 §6.4: the frozen `[usage]` key set with the contract's defaults.
#: Kept here (not derived from the schema) so a silent default change goes red.
#: All six are `hot`: a budget that needs a restart to raise is a budget nobody
#: raises, and `enforce` flipping to `block` must not need one either.
USAGE_DEFAULTS: dict[str, object] = {
    "usage.enabled": True,
    "usage.session_tokens": 0,
    "usage.daily_tokens": 0,
    "usage.warn_ratio": 0.8,
    "usage.include_aux": True,
    "usage.enforce": "block",
}

#: RFC-0005 §7.5: the four `[llm]` keys this RFC appends to C-18. Listed
#: explicitly because the root schema is `additionalProperties: false`: a key
#: that is missing here is an `UNKNOWN_CONFIG_KEY` at startup, i.e. the feature
#: has no configuration entry at all.
LLM_APPEND_KEYS: dict[str, str] = {
    "llm.model_module": "restart",
    "llm.fallback_to_builtin": "restart",
    "llm.default_model": "hot",
    "llm.capabilities": "restart",
}

#: RFC-0005 §7.4: `usage.budget` payload — exactly these six are required.
USAGE_BUDGET_REQUIRED = {"scope", "state", "used", "limit", "ratio", "degraded"}

#: RFC-0005 §7.1: new methods, all on the browser-session channel. Adding a
#: third direction would break the `for direction in directions.values()`
#: consumers this file already relies on, so the direction is pinned too.
MODEL_METHODS = {"model/list", "session/model.get", "session/model.set", "session/usage.get"}

#: RFC-0005 §4.3: a manifest must not become a second model directory.
MAX_MANIFEST_MODELS = 16

#: RFC-0009 §7.3: the frozen `[ui]` key set, defaults and apply semantics.
UI_DEFAULTS: dict[str, object] = {
    "ui.state_cache_s": 5,
    "ui.page_cache_bytes": 65536,
    "ui.module_cache_bytes": 524288,
    "ui.pending_calls": 256,
}
UI_HOT = {
    "ui.state_cache_s",
    "ui.page_cache_bytes",
    "ui.module_cache_bytes",
    "ui.pending_calls",
}

#: RFC-0009 §4.5 rev 1: the seven component names, and the four host components
#: that must never appear on a page (impersonation, not styling).
UI_COMPONENTS = {
    "Button", "TextField", "Card", "ListItem", "Switch", "ToolCallCard", "AttachmentChip",
}
UI_HOST_COMPONENTS = {"MessageBubble", "InputBar", "ApprovalDialog", "BottomBar"}
#: RFC-0009 §4.3: the six node types the inline tree may use.
UI_NODE_TYPES = {"column", "row", "tabs", "list", "text", "component"}
#: RFC §6's three kinds plus the `open_url` required by the C-19 implementation
#: brief (http/https only; it opens the user's browser, never loads a page).
UI_ACTION_KINDS = {"navigate", "tools/call", "ui/submit", "open_url"}
#: §7.3 / §4.2: page counts and byte budgets.
UI_LIMITS = {
    "pages_per_module": 8,
    "ui_block_max_bytes": 49152,
    "ui_inline_max_bytes": 8192,
    "ui_ref_max_bytes": 65536,
    "ui_nodes_per_page": 256,
    "ui_depth_max": 8,
}

#: §10.1: the module-page render path may not contain these tokens at all.
UI_FORBIDDEN_CODE = ("eval(", "Function(", "exec(", "WebView(", "loadUrl(", "<script")

#: RFC-0010 §5: the ten session-metadata methods, all on `ui_to_core` (C-05 1.3.0).
SESSION_META_METHODS = {
    "session/list", "session/rename", "session/pin", "session/archive", "session/delete",
    "session/group.list", "session/group.create", "session/group.update",
    "session/group.delete", "session/group.assign",
}
#: RFC-0010 §6: the two C-06 events, with their exact `required` arrays.
SESSION_META_EVENTS = {
    "session.updated": ["session_id", "by"],
    "session.list_changed": ["session_id", "change", "by"],
}

#: RFC-0014 §5: the one search method, on `ui_to_core` (C-05 1.5.0).
SESSION_SEARCH_METHODS = {"session/search"}
#: RFC-0014 §5: the hit-row shape — must match `search.SearchHit.to_wire()`.
SESSION_SEARCH_HIT_KEYS = {
    "session_id", "title", "updated_at", "matched_in",
    "message_id", "snippet", "snippet_offset", "archived",
}

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def load(name: str) -> dict:
    path = SCHEMA / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        FAILURES.append(f"{name}: missing")
    except json.JSONDecodeError as exc:
        FAILURES.append(f"{name}: invalid JSON ({exc})")
    return {}


def declared_codes() -> set[str]:
    text = CONTRACTS.read_text(encoding="utf-8")
    block = re.search(r"## 二、错误码.*?```json\n(.*?)```", text, re.S)
    if not block:
        FAILURES.append("01-contracts.md: cannot locate the error code block")
        return set()
    return set(json.loads(block.group(1)).keys())


def rust_codes() -> set[str]:
    if not ERROR_RS.exists():
        return set()
    text = ERROR_RS.read_text(encoding="utf-8")
    return set(re.findall(r"^\s{4}([A-Z][A-Za-z]+)\s*(?:=|{)", text, re.M))


def emitted_event_names() -> set[str]:
    """Event names the core actually emits (`emit(<session>, "domain.action")`)."""
    pattern = re.compile(r"emit\(\s*[^,()]+,\s*[\"']([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)[\"']")
    names = set(DYNAMIC_EVENT_TYPES)
    for path in sorted(CORE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        names |= set(pattern.findall(source))
        # Multi-event batches carry their names as tuple literals inside
        # `emit_batch(...)` (RFC-0006 §6.4 needs several events in ONE
        # transaction, so they cannot each be an `emit(` call).
        for call in re.finditer(r"emit_batch\(", source):
            chunk = source[call.end(): call.end() + 800]
            names |= set(_BATCH_EVENT_LITERAL.findall(chunk))
    return names


def push_declared(events: dict) -> set[str]:
    """Event names whose schema entry says `x-vade.push: events/deliver`."""
    out: set[str] = set()
    for name, spec in (events.get("events") or {}).items():
        if name == "turn.state":
            continue
        if (spec.get("x-vade") or {}).get("push") == PUSH_TRANSPORT:
            out.add(name)
    return out


def pushed_event_names() -> set[str]:
    """Topics the events package pushes: `_dispatch("<domain.action>", ...)`.

    C-14 events never reach `SessionLog.emit()`: they are assembled by
    `deliver.py` and pushed as `events/deliver` notifications. The C-06 scan
    above therefore cannot see them, and a plain allowlist would make the gate
    vacuous. Scanning the actual call site keeps both directions honest: drop a
    topic from the schema and `pushed - schema_events` goes red.
    """
    pattern = re.compile(r"_dispatch\(\s*[\"']([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)[\"']")
    names: set[str] = set()
    for path in sorted((CORE / "events").rglob("*.py")):
        names |= set(pattern.findall(path.read_text(encoding="utf-8")))
    return names


def code_topics() -> set[str]:
    """The topic set the bus actually serves (`subscribe.EVOLVE_TOPICS | ADMIN_TOPICS`)."""
    text = (CORE / "events" / "subscribe.py").read_text(encoding="utf-8")
    out: set[str] = set()
    for const in ("EVOLVE_TOPICS", "ADMIN_TOPICS"):
        block = re.search(rf"{const}: frozenset\[str\] = frozenset\((.*?)\)", text, re.S)
        if block is None:
            FAILURES.append(f"core/vade/events/subscribe.py: cannot locate {const}")
            continue
        out |= set(re.findall(r"\"([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)\"", block.group(1)))
    return out


def code_turn_states() -> set[str]:
    text = (CORE / "session" / "models.py").read_text(encoding="utf-8")
    block = re.search(r"class TurnState\(str, Enum\):(.*?)(?=\nclass |\Z)", text, re.S)
    if not block:
        FAILURES.append("core/vade/session/models.py: cannot locate TurnState")
        return set()
    return set(re.findall(r"=\s*\"([a-z_]+)\"", block.group(1)))


def code_task_states() -> set[str]:
    """RFC-0007 §5.2: the schema's `task_states` and `TaskState` are one enum."""
    text = (CORE / "tasks" / "models.py").read_text(encoding="utf-8")
    block = re.search(r"class TaskState\(str, Enum\):(.*?)(?=\nclass |\Z)", text, re.S)
    if not block:
        FAILURES.append("core/vade/tasks/models.py: cannot locate TaskState")
        return set()
    return set(re.findall(r"=\s*\"([a-z_]+)\"", block.group(1)))


def code_terminal_task_states() -> set[str]:
    text = (CORE / "tasks" / "models.py").read_text(encoding="utf-8")
    block = re.search(r"TERMINAL_TASK_STATES\s*=\s*frozenset\((.*?)\)", text, re.S)
    if not block:
        FAILURES.append("core/vade/tasks/models.py: cannot locate TERMINAL_TASK_STATES")
        return set()
    return {name.lower() for name in re.findall(r"TaskState\.([A-Z_]+)", block.group(1))}


def code_subagent_states() -> set[str]:
    """RFC-0012 §4.3: the schema's `subagent_states` and `SubagentState` are one enum."""
    text = (CORE / "session" / "models.py").read_text(encoding="utf-8")
    block = re.search(r"class SubagentState\(str, Enum\):(.*?)(?=\nclass |\n@dataclass|\Z)", text, re.S)
    if not block:
        FAILURES.append("core/vade/session/models.py: cannot locate SubagentState")
        return set()
    return set(re.findall(r"=\s*\"([a-z_]+)\"", block.group(1)))


def code_terminal_subagent_states() -> set[str]:
    text = (CORE / "session" / "models.py").read_text(encoding="utf-8")
    block = re.search(r"SUBAGENT_TERMINAL_STATES\s*=\s*frozenset\((.*?)\)", text, re.S)
    if not block:
        FAILURES.append("core/vade/session/models.py: cannot locate SUBAGENT_TERMINAL_STATES")
        return set()
    return {name.lower() for name in re.findall(r"SubagentState\.([A-Z_]+)", block.group(1))}


def walk_leaves(node: dict, prefix: str = ""):
    for key, sub in (node.get("properties") or {}).items():
        dotted = f"{prefix}.{key}" if prefix else key
        if "properties" in sub:
            yield from walk_leaves(sub, dotted)
        else:
            yield dotted, sub


def check_attach(config: dict) -> None:
    """RFC-0004 §7.4: exactly 17 keys, documented defaults, hot|restart."""
    attach = (config.get("properties") or {}).get("attach")
    if not isinstance(attach, dict):
        FAILURES.append("config.schema.json: missing [attach] section (RFC-0004 §7.4)")
        return
    check(
        attach.get("additionalProperties") is False,
        "config.schema.json: [attach].additionalProperties must be false",
    )
    leaves = dict(walk_leaves(attach, "attach"))
    missing = sorted(set(ATTACH_DEFAULTS) - set(leaves))
    extra = sorted(set(leaves) - set(ATTACH_DEFAULTS))
    check(not missing and not extra, f"config.schema.json [attach] key drift: missing={missing} extra={extra}")
    for dotted, expected in ATTACH_DEFAULTS.items():
        leaf = leaves.get(dotted) or {}
        check(
            leaf.get("default") == expected,
            f"config.schema.json {dotted}: default {leaf.get('default')!r} != {expected!r} (RFC-0004 §7.4)",
        )
        wanted = "hot" if dotted in ATTACH_HOT else "restart"
        check(
            (leaf.get("x-vade") or {}).get("apply") == wanted,
            f"config.schema.json {dotted}: apply must be {wanted} (RFC-0004 §7.4)",
        )


def check_blob_routes() -> None:
    """RFC-0004 §4.1: the six routes are a contract, not an implementation detail."""
    if str(ROOT / "core") not in sys.path:
        sys.path.insert(0, str(ROOT / "core"))
    try:
        from vade.blob.http import ROUTES
    except Exception as exc:  # pragma: no cover - import failure is the finding
        FAILURES.append(f"core/vade/blob/http.py: cannot import ROUTES ({type(exc).__name__}: {exc})")
        return
    declared = {(method, path) for method, path in ROUTES}
    check(
        declared == BLOB_ROUTES,
        "blob route drift vs RFC-0004 §4.1: "
        f"missing={sorted(BLOB_ROUTES - declared)} extra={sorted(declared - BLOB_ROUTES)}",
    )


def check_usage(config: dict) -> None:
    """RFC-0005 §6.4: exactly six `[usage]` keys, documented defaults, all hot."""
    usage = (config.get("properties") or {}).get("usage")
    if not isinstance(usage, dict):
        FAILURES.append("config.schema.json: missing [usage] section (RFC-0005 §6.4)")
        return
    check(
        usage.get("additionalProperties") is False,
        "config.schema.json: [usage].additionalProperties must be false",
    )
    leaves = dict(walk_leaves(usage, "usage"))
    missing = sorted(set(USAGE_DEFAULTS) - set(leaves))
    extra = sorted(set(leaves) - set(USAGE_DEFAULTS))
    check(not missing and not extra, f"config.schema.json [usage] key drift: missing={missing} extra={extra}")
    for dotted, expected in USAGE_DEFAULTS.items():
        leaf = leaves.get(dotted) or {}
        check(
            leaf.get("default") == expected,
            f"config.schema.json {dotted}: default {leaf.get('default')!r} != {expected!r} (RFC-0005 §6.4)",
        )
        check(
            (leaf.get("x-vade") or {}).get("apply") == "hot",
            f"config.schema.json {dotted}: apply must be hot (RFC-0005 §6.4)",
        )
    enforce = leaves.get("usage.enforce") or {}
    check(
        enforce.get("enum") == ["block", "warn"],
        f"config.schema.json usage.enforce: enum must be ['block', 'warn'], got {enforce.get('enum')!r}",
    )
    warn = leaves.get("usage.warn_ratio") or {}
    check(
        warn.get("minimum") == 0.1 and warn.get("maximum") == 1,
        "config.schema.json usage.warn_ratio: bounds must be 0.1..1 (a 0 ratio would warn on every turn)",
    )


def check_llm_append(config: dict) -> None:
    """RFC-0005 §7.5: the four `[llm]` keys, present with their apply semantics.

    The config root is `additionalProperties: false`, so an unlisted key is a
    startup `UNKNOWN_CONFIG_KEY`. Each of the four is therefore load-bearing:
    without `model_module` the module filter is unreachable, without
    `default_model` there is no global layer, without `capabilities` the builtin
    always reports UNKNOWN, and without `fallback_to_builtin` a module target
    that is offline has no recovery path.
    """
    llm = (config.get("properties") or {}).get("llm")
    if not isinstance(llm, dict):
        FAILURES.append("config.schema.json: missing [llm] section (RFC-0005 §7.5)")
        return
    leaves = dict(walk_leaves(llm, "llm"))
    missing = sorted(set(LLM_APPEND_KEYS) - set(leaves))
    check(not missing, f"config.schema.json missing RFC-0005 [llm] keys: {missing}")
    for dotted, wanted in LLM_APPEND_KEYS.items():
        leaf = leaves.get(dotted) or {}
        check(dotted in leaves and "default" in leaf, f"config.schema.json {dotted}: default missing")
        check(
            (leaf.get("x-vade") or {}).get("apply") == wanted,
            f"config.schema.json {dotted}: apply must be {wanted} (RFC-0005 §7.5)",
        )
    check(
        "capabilities" in leaves or "llm.capabilities" in leaves,
        "config.schema.json: [llm.capabilities] is a nested object, not a leaf; it must still be declared",
    )
    caps = (llm.get("properties") or {}).get("capabilities")
    check(isinstance(caps, dict), "config.schema.json: [llm.capabilities] missing")
    if isinstance(caps, dict):
        check(
            caps.get("additionalProperties") is True,
            "config.schema.json [llm.capabilities]: additionalProperties must be true "
            "(capability keys are validated field-by-field at read time, not by aborting startup)",
        )


def check_manifest_models(manifest: dict) -> None:
    """RFC-0005 §4.3: `capabilities.models` shape, and that core still parses it."""
    capabilities = (manifest.get("properties") or {}).get("capabilities") or {}
    check(
        list(capabilities.get("required") or []) == ["tools"],
        "manifest.schema.json: capabilities.required must stay ['tools'] (RFC-0005 append is additive)",
    )
    models = (capabilities.get("properties") or {}).get("models")
    if not isinstance(models, dict):
        FAILURES.append("manifest.schema.json: capabilities.models missing (RFC-0005 §4.3)")
        return
    check(
        models.get("type") == "array",
        f"manifest.schema.json capabilities.models: type must be array, got {models.get('type')!r}",
    )
    check(
        models.get("maxItems") == MAX_MANIFEST_MODELS,
        f"manifest.schema.json capabilities.models: maxItems must be {MAX_MANIFEST_MODELS}, "
        f"got {models.get('maxItems')!r}",
    )
    check(
        models.get("items", {}).get("$ref") == "#/$defs/model",
        "manifest.schema.json capabilities.models: items must $ref #/$defs/model",
    )
    entry = (manifest.get("$defs") or {}).get("model") or {}
    check(
        list(entry.get("required") or []) == ["name"],
        f"manifest.schema.json $defs.model: required must be exactly ['name'], got {entry.get('required')!r}",
    )
    entry_props = entry.get("properties") or {}
    expected = {
        "name", "display_name", "input_modalities", "max_image_bytes", "max_image_px",
        "max_images_per_turn", "context_window", "supports_tools",
    }
    check(
        expected <= set(entry_props),
        f"manifest.schema.json $defs.model: missing capability fields {sorted(expected - set(entry_props))}",
    )
    check(
        entry_props.get("input_modalities", {}).get("items", {}).get("enum") == ["text", "image", "audio", "file"],
        "manifest.schema.json $defs.model.input_modalities: enum must be text|image|audio|file",
    )
    # The C-03 lesson (see the RFC-0001 append row): a schema field core drops
    # is a registration silently losing data, so pin the parse target too.
    if str(ROOT / "core") not in sys.path:
        sys.path.insert(0, str(ROOT / "core"))
    try:
        from vade.registry.manifest import Manifest
    except Exception as exc:  # pragma: no cover - import failure is the finding
        FAILURES.append(f"core/vade/registry/manifest.py: cannot import Manifest ({type(exc).__name__}: {exc})")
        return
    check(
        "models" in getattr(Manifest, "__dataclass_fields__", {}),
        "core/vade/registry/manifest.Manifest: no `models` field; capabilities.models would be dropped on parse",
    )


def check_model_methods(rpc: dict) -> None:
    """RFC-0005 §7.1: the four methods, on `ui_to_core`, with a declared surface."""
    directions = rpc.get("directions") or {}
    ui_to_core = directions.get("ui_to_core") or {}
    missing = sorted(MODEL_METHODS - set(ui_to_core))
    check(not missing, f"rpc.json ui_to_core missing RFC-0005 methods: {missing}")
    elsewhere = sorted(MODEL_METHODS & set(directions.get("module_to_core") or {}))
    check(not elsewhere, f"rpc.json: RFC-0005 methods must not be module-facing: {elsewhere}")
    codes = set(load("errors.json").get("codes") or {})
    for name in sorted(MODEL_METHODS & set(ui_to_core)):
        entry = ui_to_core.get(name) or {}
        for field in ("params", "result", "errors"):
            check(field in entry, f"rpc.json {name}: missing {field}")
        unknown = sorted(set(entry.get("errors") or []) - codes)
        check(not unknown, f"rpc.json {name}: errors not declared in C-04: {unknown}")


def check_usage_budget_event(events: dict) -> None:
    """RFC-0005 §7.4: `usage.budget` exists with its six required keys."""
    declared = dict(events.get("events") or {})
    spec = declared.get("usage.budget")
    if not isinstance(spec, dict):
        FAILURES.append("events.json: usage.budget missing (RFC-0005 §7.4)")
        return
    payload = spec.get("payload") or {}
    required = set(spec.get("required") or [])
    check(
        required == USAGE_BUDGET_REQUIRED,
        f"events.json usage.budget: required must be exactly {sorted(USAGE_BUDGET_REQUIRED)}, "
        f"got {sorted(required)}",
    )
    undeclared = sorted(required - set(payload))
    check(not undeclared, f"events.json usage.budget: required fields not in payload: {undeclared}")
    check(
        (spec.get("x-vade") or {}).get("push") is None,
        "events.json usage.budget: must not be a C-14 push topic (it travels as a session event)",
    )
    scope = payload.get("scope") or ""
    state = payload.get("state") or ""
    check("session" in scope and "day" in scope,
          f"events.json usage.budget.scope: must be session|day, got {scope!r}")
    check(state.startswith("warn") and "exceeded" in state,
          f"events.json usage.budget.state: must be warn|exceeded, got {state!r}")
def check_rfc0006(events: dict, rpc: dict, config: dict) -> None:
    """RFC-0006: append-only additions, frozen `required` arrays, two methods."""
    appended = (events.get("x-vade") or {}).get("appends")
    check(isinstance(appended, list) and bool(appended),
          "events.json: x-vade.appends is missing (RFC-0006 §9.2)")
    block = next((a for a in (appended or []) if a.get("contract") == "C-06"), {})
    check(block.get("rewrites") == "none",
          "events.json x-vade.appends[0]: rewrites must be 'none' (append-only)")
    check(block.get("version") == "1.1.0",
          "events.json x-vade.appends[0]: version must be 1.1.0")
    check(set(block.get("adds") or []) == {"turn.superseded", "message.edited"},
          "events.json x-vade.appends[0].adds: must be exactly the two RFC-0006 events")
    declared_fields = {name: set(fields) for name, fields in (block.get("fields") or {}).items()}
    check(declared_fields == RFC0006_FIELDS,
          "events.json x-vade.appends[0].fields drift vs RFC-0006 §5.1/§6.1/§7.1")

    declared = dict(events.get("events") or {})
    for name, required in RFC0006_REQUIRED.items():
        spec = declared.get(name) or {}
        check(list(spec.get("required") or []) == required,
              f"events.json {name}: required changed (RFC-0006 keeps 1.0.0 arrays byte-identical)")
        missing = RFC0006_FIELDS[name] - set(spec.get("payload") or {})
        check(not missing, f"events.json {name}: appended fields missing from payload: {sorted(missing)}")

    for name, required in (
        ("turn.superseded", ["turn_id", "superseded_by", "reason"]),
        ("message.edited", ["message_id", "session_id", "superseded_by_message_id",
                            "new_turn_id", "text", "edited_at"]),
    ):
        spec = declared.get(name)
        check(isinstance(spec, dict), f"events.json: {name} is missing (RFC-0006 §6.4)")
        if isinstance(spec, dict):
            check(list(spec.get("required") or []) == required,
                  f"events.json {name}: required must be {required}")
            check("state" not in (spec.get("required") or []),
                  f"events.json {name}: must not require `state` (it is not a state change)")

    # §6.4 row 14: `superseded` is NOT a state.
    check("superseded" not in (events.get("turn_states") or []),
          "events.json turn_states: RFC-0006 adds no state value")

    methods = set()
    for direction in (rpc.get("directions") or {}).values():
        methods |= set(direction)
    for name in ("turn/regenerate", "message/edit_resubmit"):
        check(name in methods, f"rpc.json: {name} is missing from ui_to_core (RFC-0006 §9.1)")
    check("session/fork" not in methods,
          "rpc.json: session/fork must NOT be registered in v1 (RFC-0006 §6.5)")
    check(((rpc.get("x-vade") or {}).get("appends") or [{}])[0].get("rewrites") == "none",
          "rpc.json x-vade.appends: rewrites must be 'none'")

    # §8: the protocol constant and RFC-0004's configurable ceiling must agree.
    try:
        from vade.session.limits import TEXT_LIMIT
    except Exception as exc:  # pragma: no cover - import failure is the finding
        FAILURES.append(f"core/vade/session/limits.py: cannot import TEXT_LIMIT ({exc})")
        return
    attach_default = ((config.get("properties") or {}).get("attach") or {}).get("properties", {})
    configured = (attach_default.get("text_max_chars") or {}).get("default")
    check(TEXT_LIMIT == configured == 32000,
          f"RFC-0006 §8: text limit drift (core={TEXT_LIMIT}, attach.text_max_chars={configured})")

PART_TITLES = {"text", "image", "file", "audio"}
PART_REQUIRED = {
    "text": {"type", "text"},
    "image": {"type", "file_id", "mime", "name", "size", "width", "height", "sha256"},
    "file": {"type", "file_id", "mime", "name", "size"},
    "audio": {"type", "file_id", "mime", "name", "size"},
}


def check_submit_parts(rpc: dict) -> None:
    """RFC-0004 §6.1/§7.2: `ui/submit.content` is a typed part union, not `[object]`.

    This is the one breaking change in the batch: a client that sends a block
    nobody defined used to be silently ignored. The schema is the only place a
    client author can see that, so its shape is asserted rather than trusted.
    """
    submit = ((rpc.get("directions") or {}).get("ui_to_core") or {}).get("ui/submit") or {}
    content = ((submit.get("params") or {}).get("content")) or {}
    if not isinstance(content, dict):
        FAILURES.append("rpc.json ui/submit.content is not a typed array (RFC-0004 §7.2)")
        return
    check(content.get("type") == "array", "rpc.json ui/submit.content: type must be array")
    check(content.get("minItems") == 1, "rpc.json ui/submit.content: minItems must be 1")
    check(content.get("maxItems") == 20, "rpc.json ui/submit.content: maxItems must be 20 (hard cap)")
    branches = list(((content.get("items") or {}).get("oneOf")) or [])
    titles = [str((branch or {}).get("title")) for branch in branches]
    check(
        set(titles) == PART_TITLES,
        f"rpc.json ui/submit.content.oneOf: part types {sorted(titles)} != {sorted(PART_TITLES)}",
    )
    for branch in branches:
        title = str((branch or {}).get("title"))
        props = (branch or {}).get("properties") or {}
        const = (props.get("type") or {}).get("const")
        check(const == title, f"rpc.json ui/submit.content[{title}]: type const {const!r} != {title!r}")
        required = set((branch or {}).get("required") or [])
        expected = PART_REQUIRED.get(title, set())
        check(
            required == expected,
            f"rpc.json ui/submit.content[{title}]: required {sorted(required)} != {sorted(expected)}",
        )
        for name in expected:
            check(name in props, f"rpc.json ui/submit.content[{title}]: {name} not declared")
    image = next((b for b in branches if (b or {}).get("title") == "image"), {}) or {}
    mimes = set(((image.get("properties") or {}).get("mime") or {}).get("enum") or [])
    check(
        mimes == {"image/jpeg", "image/png", "image/webp"},
        f"rpc.json ui/submit.content[image].mime: {sorted(mimes)} is not the FR-8.38 whitelist",
    )


def check_http_endpoints(rpc: dict) -> None:
    """RFC-0004 §7.3: `/ui/blob*` is declared next to the RPC table it is not part of."""
    endpoints = rpc.get("http_endpoints")
    if not isinstance(endpoints, dict):
        FAILURES.append("rpc.json: missing http_endpoints (RFC-0004 §7.3)")
        return
    rows = endpoints.get("endpoints") or []
    # The RFC's §4.1 table and this list must describe the same six routes; the
    # published rows carry decorators (`?offset=N`, `[?v=thumb]`), so compare the
    # bare path or `POST /ui/blob` would "match" everything under /ui/blob/upload.
    declared = {
        (str(row.get("method")), str(row.get("path") or "").split("?")[0].split("[")[0])
        for row in rows
        if isinstance(row, dict)
    }
    check(len(rows) == len(BLOB_ROUTES), f"rpc.json http_endpoints: {len(rows)} routes, RFC §4.1 has {len(BLOB_ROUTES)}")
    check(
        declared == BLOB_ROUTES,
        "rpc.json http_endpoints drift vs RFC-0004 §4.1: "
        f"missing={sorted(BLOB_ROUTES - declared)} extra={sorted(declared - BLOB_ROUTES)}",
    )
    check("listener" in endpoints, "rpc.json http_endpoints: listener missing")
    check("auth" in endpoints, "rpc.json http_endpoints: auth missing")
    check("body_max" in endpoints, "rpc.json http_endpoints: body_max missing")
    check(
        (endpoints.get("x-vade") or {}).get("appends") == "RFC-0004",
        "rpc.json http_endpoints: x-vade.appends must name RFC-0004",
    )


def check_tasks(config: dict) -> None:
    """RFC-0007 §9.4: exactly 13 keys, documented defaults, hot|restart."""
    tasks = (config.get("properties") or {}).get("tasks")
    if not isinstance(tasks, dict):
        FAILURES.append("config.schema.json: missing [tasks] section (RFC-0007 §9.4)")
        return
    check(
        tasks.get("additionalProperties") is False,
        "config.schema.json: [tasks].additionalProperties must be false",
    )
    leaves = dict(walk_leaves(tasks, "tasks"))
    missing = sorted(set(TASKS_DEFAULTS) - set(leaves))
    extra = sorted(set(leaves) - set(TASKS_DEFAULTS))
    check(not missing and not extra, f"config.schema.json [tasks] key drift: missing={missing} extra={extra}")
    for dotted, expected in TASKS_DEFAULTS.items():
        leaf = leaves.get(dotted) or {}
        check(
            leaf.get("default") == expected,
            f"config.schema.json {dotted}: default {leaf.get('default')!r} != {expected!r} (RFC-0007 §9.4)",
        )
        wanted = "hot" if dotted in TASKS_HOT else "restart"
        check(
            (leaf.get("x-vade") or {}).get("apply") == wanted,
            f"config.schema.json {dotted}: apply must be {wanted} (RFC-0007 §9.4)",
        )
    # §6.3: the cancel deadline is a ceiling, not a preference.
    grace = leaves.get("tasks.cancel_grace_ms") or {}
    check(
        grace.get("maximum") == 3000,
        "config.schema.json tasks.cancel_grace_ms: maximum must be 3000 (RFC-0007 §6.3)",
    )


def check_prompt_layers(skill: dict, config: dict, rpc: dict, events: dict, declared_events: dict) -> None:
    """RFC-0003 §4.4/§5: C-11 order table, C-18 keys, C-05 reserved names, C-06 appends."""
    # ---------------------------------------------------------- C-11: orders
    injection = (skill.get("x-vade") or {}).get("injection")
    if not isinstance(injection, dict):
        FAILURES.append("skill.schema.json: missing x-vade.injection (RFC-0003 §5.2)")
        return
    orders = injection.get("orders") or {}
    check(bool(orders), "C-11: injection.orders missing")
    check(injection.get("schema_version") == 2,
          "C-11: injection.schema_version must be 2 (RFC-0003 §5.2)")
    check(len(set(orders.values())) == len(orders), "C-11: SECTION_ORDERS has a duplicate number")
    stable = list(injection.get("stable") or [])
    context = list(injection.get("context") or [])
    check(set(stable) | set(context) <= set(orders), "C-11: stable/context sections missing from the order table")
    # Sections outside stable/context are the non-system channels; every order
    # table entry must be exactly one of the three (the RFC's §4.4 sketch
    # compared stable∪context to orders directly, which its own §5.2 table
    # — orders also lists the history.user / tool_result channels — contradicts).
    non_system = {name for name, channel in (injection.get("channels") or {}).items()
                  if channel not in ("system.stable", "system.context")}
    check(set(stable) | set(context) | non_system == set(orders) and not (non_system & (set(stable) | set(context))),
          "C-11: section list and order table disagree")
    if stable and context:
        check(max(orders[s] for s in stable) < min(orders[s] for s in context),
              "C-11: every context section must sort after every stable section")
    channels = injection.get("channels") or {}
    check(set(channels) == set(orders),
          "C-11: injection.channels must cover exactly the order-table sections")
    LEGACY = ["persona", "tool_block", "skill_catalog", "skill_bodies", "session_memory", "context"]
    ALIAS = {"context": "runtime_facts"}
    missing = [s for s in LEGACY if ALIAS.get(s, s) not in orders]
    check(not missing, f"C-11: legacy sections missing from the order table: {missing}")
    # Ascending by ORDER NUMBER, not by name: the legacy projection must never
    # need a re-read of history to make sense (RFC-0003 §4.4 design point 1).
    projected = [int(orders[ALIAS.get(s, s)]) for s in LEGACY]
    check(projected == sorted(projected),
          "C-11: the six legacy sections must stay ascending in the new order table")
    check(list((skill.get("x-vade") or {}).get("injection_order") or []) == LEGACY,
          "C-11: injection_order must stay byte-identical (legacy projection)")
    check(orders.get("skill_catalog") == 300 and orders.get("skill_bodies") == 400,
          "C-11: skill_catalog/skill_bodies keep their legacy order slots (300/400)")
    for name, channel in (("skill_catalog", "history.user"), ("skill_bodies", "tool_result")):
        check(channels.get(name) == channel,
              f"C-11: {name} channel must be {channel} (RFC-0003 moves it out of system)")

    # ---------------------------------------------------------- C-18: keys
    leaves = dict(walk_leaves(config, ""))
    for dotted, default, wanted in (
        ("prompt.injection", "legacy", "restart"),
        ("tools.hot", [], "restart"),
        ("tools.hot_max", 12, "restart"),
    ):
        leaf = leaves.get(dotted) or {}
        check(leaf.get("default") == default,
              f"config.schema.json {dotted}: default {leaf.get('default')!r} != {default!r} (RFC-0003 §7)")
        check((leaf.get("x-vade") or {}).get("apply") == wanted,
              f"config.schema.json {dotted}: apply must be {wanted} (RFC-0003 §7: no hot switch)")
    check((leaves.get("prompt.injection") or {}).get("enum") == ["legacy", "layered"],
          "config.schema.json prompt.injection: enum must be ['legacy', 'layered']")

    # ---------------------------------------------------------- C-05: reserved
    reserved = (rpc.get("x-vade") or {}).get("core_reserved_tools") or {}
    names = set(reserved.get("reserved") or [])
    check({"skill_load", "tools_search", "tools/search"} <= names,
          "rpc.json x-vade.core_reserved_tools: must register skill_load/tools_search/tools/search (RFC-0003 §5.4)")
    check(reserved.get("doc") == "docs/rfc/RFC-0003-prompt-injection-layers.md",
          "rpc.json core_reserved_tools: doc must point at RFC-0003")
    for direction in (rpc.get("directions") or {}).values():
        clash = names & set(direction or {})
        check(not clash,
              f"rpc.json: core-reserved tool names must not be registered as methods: {sorted(clash)}")

    # ---------------------------------------------------------- C-06: appends
    appends = (events.get("x-vade") or {}).get("appends") or []
    block = next((b for b in appends if isinstance(b, dict)
                  and "RFC-0003" in str(b.get("doc") or "")), None)
    check(block is not None, "events.json: missing the x-vade.appends block for RFC-0003")
    if block is not None:
        check(set(block.get("adds") or []) == {"turn.skill_catalog", "turn.tool_discovered"},
              "events.json: the RFC-0003 append block must add exactly the two RFC-0003 events")
        check(block.get("rewrites") == "none",
              "events.json: the RFC-0003 append block must not rewrite anything")
    for name, required in (
        ("turn.skill_catalog", ["turn_id", "digest"]),
        ("turn.tool_discovered", ["turn_id", "tool", "tool_id"]),
    ):
        spec = declared_events.get(name) or {}
        check(bool(spec), f"events.json: {name} is not declared (RFC-0003 §5.5)")
        if isinstance(spec, dict):
            check(list(spec.get("required") or []) == required,
                  f"events.json {name}: required must be {required}")
            check("state" not in (spec.get("required") or []),
                  f"events.json {name}: payload-only event must not require `state`")
    check(events.get("version") == "2.0.0",
          "events.json: version must stay 2.0.0 (RFC-0003 appends no state)")


def check_task_events(events: dict, declared_events: dict, schema_events: set[str]) -> None:
    """RFC-0007 §9.2: enum drift, the `task.state` trap, and the coalesce marker."""
    states = list(events.get("task_states") or [])
    check(set(states) == code_task_states(), "events.json task_states drift vs core/vade/tasks/models.TaskState")
    check(
        code_terminal_task_states() == TASK_TERMINAL_STATES,
        "core/vade/tasks/models.TERMINAL_TASK_STATES must be exactly "
        f"{sorted(TASK_TERMINAL_STATES)}, has {sorted(code_terminal_task_states())}",
    )
    # §9.2: a `task.state` entry under `events` would be "declared but nobody
    # emits" on day one — only `turn.state` is exempt from that gate.
    check(
        "task.state" not in declared_events,
        "events.json: task_states is a TOP-LEVEL key; a `task.state` events entry is not allowed (RFC-0007 §9.2)",
    )
    check(
        {"task.started", "task.status", "task.progress", "task.finished"} <= schema_events,
        "events.json: the four RFC-0007 task.* events are missing",
    )
    progress = declared_events.get("task.progress") or {}
    check(
        (progress.get("x-vade") or {}).get("coalesce") is True,
        "events.json task.progress: x-vade.coalesce must be true (RFC-0007 §5.5 R5 / §9.2)",
    )
    check(
        "state" not in (progress.get("payload") or {}),
        "events.json task.progress: must not declare `state` (payload-only class)",
    )
    # §5.3: these nullable keys are required-with-null so every event is
    # self-describing; dropping one silently breaks client tree-building.
    expected_required = {
        "task.started": {
            "task_id", "kind", "state", "session_id", "turn_id", "parent_id",
            "depth", "name", "started_at", "retryable",
        },
        "task.status": {"task_id", "kind", "parent_id", "depth", "state", "elapsed_ms"},
        "task.progress": {"task_id", "pct", "elapsed_ms"},
        "task.finished": {
            "task_id", "kind", "parent_id", "depth", "state", "duration_ms", "summary",
        },
    }
    for name, wanted in expected_required.items():
        actual = set((declared_events.get(name) or {}).get("required") or [])
        check(actual == wanted, f"events.json {name}.required drift: {sorted(actual)} != {sorted(wanted)}")

    # C-06 append block (§9.1/§9.2 asked for one; it landed retroactively —
    # events.json was still 1.0.0 with a string-shaped `appends` at 404c3f3).
    # Without a block the four events and `task_states` are declared but never
    # registered, which is the gap this pin exists to keep shut.
    appends = (events.get("x-vade") or {}).get("appends") or []
    eblock = next((b for b in appends if isinstance(b, dict)
                   and "RFC-0007" in str(b.get("doc") or "")), None)
    check(eblock is not None, "events.json: missing the x-vade.appends block for RFC-0007")
    if eblock is not None:
        check(eblock.get("contract") == "C-06",
              "events.json: the RFC-0007 append block must target C-06")
        check(eblock.get("version") == "1.1.0",
              "events.json: the RFC-0007 append block must carry version 1.1.0 "
              "(the version rpc.json's RFC-0007 block records)")
        check(
            set(eblock.get("adds") or []) == {"task.started", "task.status", "task.progress", "task.finished"},
            "events.json: the RFC-0007 append block must add exactly the four task.* events",
        )
        check(str(eblock.get("rewrites") or "").startswith("none"),
              "events.json: the RFC-0007 append block must declare rewrites 'none' (append-only)")


def check_approval_attribution(declared_events: dict) -> None:
    """RFC-0007 §7.1: four OPTIONAL keys, and `required` must not have moved."""
    for name in ("turn.approval_required", "turn.approval_resolved"):
        spec = declared_events.get(name) or {}
        payload = spec.get("payload") or {}
        for key in ("session_id", "task_id", "task_name", "depth"):
            check(key in payload, f"events.json {name}: RFC-0007 §7.1 field {key} is not declared")
        required = set(spec.get("required") or [])
        check(
            not ({"session_id", "task_id", "task_name", "depth"} & required),
            f"events.json {name}: RFC-0007 attribution fields must stay OPTIONAL (required is frozen)",
        )


def check_subagent_events(events: dict, declared_events: dict, schema_events: set[str], rpc: dict) -> None:
    """RFC-0012: the three-event family, the enum, the gate, and C-05 1.4.0.

    Things a review can miss by reading:

    * `subagent.spawned` is payload-only while the other two are state-change
      events — getting either backwards breaks the C-06 rule-4 scan;
    * the result text must NEVER cross the event boundary (only its sha-256
      fingerprint and length do) — a "helpful" `summary` key is a leak;
    * the transition gate in `core/vade/subagents/manager.py` is the contract:
      a widened gate lets a run leave a terminal state and nobody would notice.
    """
    # §4.3: the enum is a TOP-LEVEL key (task_states precedent) and agrees
    # with the code enum; the terminal three agree on both sides.
    states = list(events.get("subagent_states") or [])
    check(set(states) == SUBAGENT_STATES,
          "events.json subagent_states drift vs the frozen RFC-0012 set")
    check(set(states) == code_subagent_states(),
          "events.json subagent_states drift vs core/vade/session/models.SubagentState")
    check(code_terminal_subagent_states() == SUBAGENT_TERMINAL_STATES,
          "core/vade/session/models.SUBAGENT_TERMINAL_STATES must be exactly "
          f"{sorted(SUBAGENT_TERMINAL_STATES)}, has {sorted(code_terminal_subagent_states())}")
    # §4.3: a `subagent.state` entry under `events` would be "declared but
    # nobody emits" on day one (RFC-0007 §9.2's same trap).
    check("subagent.state" not in declared_events,
          "events.json: subagent_states is a TOP-LEVEL key; a `subagent.state` events entry is not allowed (RFC-0012 §4.3)")
    # §5.2: the three events exist with their frozen required arrays.
    for name, required in RFC0012_REQUIRED.items():
        spec = declared_events.get(name)
        check(isinstance(spec, dict), f"events.json: {name} is missing (RFC-0012 §5.2)")
        if isinstance(spec, dict):
            actual = list(spec.get("required") or [])
            check(actual == required,
                  f"events.json {name}.required drift: {actual} != {required} (RFC-0012 §5.2)")
            payload = spec.get("payload") or {}
            undeclared = [field for field in required if field not in payload]
            check(not undeclared,
                  f"events.json {name}: required fields not declared in payload: {undeclared}")
            forbidden = RFC0012_FORBIDDEN_PAYLOAD_KEYS & set(payload)
            check(not forbidden,
                  f"events.json {name}: result-text keys {sorted(forbidden)} are forbidden "
                  "(RFC-0012 §5.3: digest, not the text)")
            check((spec.get("x-vade") or {}).get("push") is None,
                  f"events.json {name}: must not be a C-14 push topic (session event)")
    # §5.3: the digest is a fingerprint — the schema says so, in these words.
    result_payload = (declared_events.get("subagent.result") or {}).get("payload") or {}
    digest_decl = str(result_payload.get("result_digest") or "")
    check("sha-256" in digest_decl,
          "events.json subagent.result: result_digest must declare the sha-256 fingerprint rule")
    # §5.1: the stream key rule is pinned in the spawned payload.
    spawned_payload = (declared_events.get("subagent.spawned") or {}).get("payload") or {}
    check("parent_session_id" in spawned_payload and "session_id" in spawned_payload,
          "events.json subagent.spawned: both session_id (stream key) and parent_session_id must be declared")

    # C-06 append block.
    appends = (events.get("x-vade") or {}).get("appends") or []
    eblock = next((b for b in appends if "RFC-0012" in str(b.get("doc") or "")), None)
    check(eblock is not None, "events.json: missing the x-vade.appends block for RFC-0012")
    if eblock is not None:
        check(set(eblock.get("adds") or []) == set(RFC0012_REQUIRED),
              "events.json: the RFC-0012 append block must add exactly the three subagent.* events")
        check("none" in str(eblock.get("rewrites") or ""),
              "events.json: the RFC-0012 append must be rewrite-free")
        check(eblock.get("version") == "2.0.0",
              "events.json: the RFC-0012 append must NOT claim a version bump (top-level stays 2.0.0)")
        check("subagent_states" in str(eblock.get("overrides") or ""),
              "events.json: the RFC-0012 append must mention the new top-level subagent_states key")

    # C-05: two read-only methods, version 1.4.0.
    directions = rpc.get("directions") or {}
    ui_to_core = directions.get("ui_to_core") or {}
    for name in ("subagent/list", "subagent/get"):
        check(name in ui_to_core, f"rpc.json ui_to_core missing {name} (RFC-0012 §6)")
        entry = ui_to_core.get(name) or {}
        for field in ("params", "result", "errors"):
            check(field in entry, f"rpc.json {name}: missing {field}")
        unknown = sorted(set(entry.get("errors") or []) - set(load("errors.json").get("codes") or {}))
        check(not unknown, f"rpc.json {name}: errors not declared in C-04: {unknown}")
    elsewhere = sorted(
        {"subagent/list", "subagent/get"}
        & (set(directions.get("module_to_core") or {}) | set(directions.get("core_to_module") or {}))
    )
    check(not elsewhere, f"rpc.json: RFC-0012 methods must not be module-facing: {elsewhere}")
    check(rpc.get("version") in ("1.4.0", "1.5.0", "1.6.0", "1.7.0"),
          f"rpc.json: version must be 1.4.0 (RFC-0012), 1.5.0 (RFC-0014), 1.6.0 (RFC-0015) "
          f"or 1.7.0 (RFC-0016, the current head) (got {rpc.get('version')!r})")
    rpc_appends = (rpc.get("x-vade") or {}).get("appends") or []
    rblock = next((b for b in rpc_appends if "RFC-0012" in str(b.get("doc") or "")), None)
    check(rblock is not None, "rpc.json: missing the x-vade.appends block for RFC-0012")
    if rblock is not None:
        check(set(rblock.get("adds") or []) == {"subagent/list", "subagent/get"},
              "rpc.json: the RFC-0012 append block must add exactly the two subagent/* methods")
        check(rblock.get("version") == "1.4.0",
              "rpc.json: the RFC-0012 append block must carry version 1.4.0")
        check("none" in str(rblock.get("rewrites") or ""),
              "rpc.json: the RFC-0012 append must be rewrite-free")

    # The handlers must actually be registered, and the gate must be intact.
    if str(ROOT / "core") not in sys.path:
        sys.path.insert(0, str(ROOT / "core"))
    try:
        from vade.session.models import new_run_id
        from vade.subagents.manager import (
            ALLOWED_TRANSITIONS, LABEL_MAX, RUN_ID_PATTERN, SubagentManager,
        )
        from vade.subagents.rpc import SubagentRPC
    except Exception as exc:  # pragma: no cover - import failure is the finding
        FAILURES.append(f"core/vade/subagents: cannot import for RFC-0012 checks "
                        f"({type(exc).__name__}: {exc})")
        return
    registered = set(SubagentRPC(None).handlers())
    absent = sorted({"subagent/list", "subagent/get"} - registered)
    check(not absent, f"SubagentRPC.handlers() missing RFC-0012 methods: {absent}")
    check(LABEL_MAX == 64, f"subagents.manager.LABEL_MAX must be 64, is {LABEL_MAX}")
    check(str(RUN_ID_PATTERN.pattern) == r"^run_[0-9a-f]{32}$",
          "manager.RUN_ID_PATTERN drifted from RFC-0012 §7.1")
    check(bool(RUN_ID_PATTERN.match(new_run_id())),
          "manager.RUN_ID_PATTERN must match a freshly minted run_id")
    expected_gate = {
        "spawned": {"running", "cancelled"},
        "running": {"running", "completed", "failed", "cancelled"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    }
    actual_gate = {
        state.value: {target.value for target in targets}
        for state, targets in ALLOWED_TRANSITIONS.items()
    }
    check(actual_gate == expected_gate,
          f"subagents.manager.ALLOWED_TRANSITIONS drifted from RFC-0012 §7.2: {actual_gate}")
    for probe, expect_ok in (
        (("spawned", "running"), True),
        (("spawned", "cancelled"), True),
        (("running", "completed"), True),
        (("running", "failed"), True),
        (("running", "cancelled"), True),
        (("running", "running"), True),
        (("spawned", "completed"), False),
        (("spawned", "failed"), False),
        (("running", "spawned"), False),
        (("completed", "running"), False),
        (("failed", "cancelled"), False),
        (("cancelled", "completed"), False),
    ):
        frm, to = probe
        allowed = to in actual_gate.get(frm, set())
        check(allowed == expect_ok,
              f"gate {frm} -> {to} must be {'legal' if expect_ok else 'refused'} (§7.2)")
    # The digest helper is really a sha-256 fingerprint of the text.
    check(hasattr(SubagentManager, "finish") and hasattr(SubagentManager, "cancel_for_turn"),
          "SubagentManager must expose finish() and cancel_for_turn() (RFC-0012 §5.2/§7.3)")


def check_ui(config: dict, manifest: dict, rpc: dict) -> None:
    """RFC-0009: the `[ui]` keys, the C-03 append, the C-05 methods, §10.1.

    Three things are asserted that a review can miss by reading:

    * the page block is the only `additionalProperties:false` subtree in C-03
      (otherwise `on_click` would be forward-compatible, i.e. a script hook);
    * the component whitelist and the action kinds in code are exactly the
      contract's (a silently added 8th component is how phishing gets in);
    * the module-page package contains no evaluator/browser token (§10.1).
    """
    section = (config.get("properties") or {}).get("ui")
    if not isinstance(section, dict):
        FAILURES.append("config.schema.json: missing [ui] section (RFC-0009 §7.3)")
    else:
        check(section.get("additionalProperties") is False, "config.schema.json: [ui].additionalProperties must be false")
        leaves = dict(walk_leaves(section, "ui"))
        missing = sorted(set(UI_DEFAULTS) - set(leaves))
        extra = sorted(set(leaves) - set(UI_DEFAULTS))
        check(not missing and not extra, f"config.schema.json [ui] key drift: missing={missing} extra={extra}")
        for dotted, expected in UI_DEFAULTS.items():
            leaf = leaves.get(dotted) or {}
            check(leaf.get("default") == expected,
                  f"config.schema.json {dotted}: default {leaf.get('default')!r} != {expected!r} (RFC-0009 §7.3)")
            wanted = "hot" if dotted in UI_HOT else "restart"
            check((leaf.get("x-vade") or {}).get("apply") == wanted,
                  f"config.schema.json {dotted}: apply must be {wanted} (RFC-0009 §7.3)")

    ui = (manifest.get("properties") or {}).get("ui") or {}
    check(ui.get("additionalProperties") is False,
          "manifest.schema.json: ui.additionalProperties must be false (C-19 is the strict subtree)")
    check("^x-" in (ui.get("patternProperties") or {}),
          "manifest.schema.json: ui needs the ^x- escape hatch and nothing else")
    pages = (ui.get("properties") or {}).get("pages") or {}
    check(pages.get("maxItems") == UI_LIMITS["pages_per_module"],
          f"manifest.schema.json ui.pages.maxItems must be {UI_LIMITS['pages_per_module']}")
    page = ((manifest.get("$defs") or {}).get("page")) or {}
    check(page.get("additionalProperties") is False,
          "manifest.schema.json $defs.page.additionalProperties must be false")
    check(set(page.get("required") or []) == {"id", "title", "version", "schema"},
          "manifest.schema.json $defs.page.required must be id/title/version/schema")
    # S5 (C-19 review): the node tree must be closed IN THE FILE, not only in
    # `uipage.py`. A consumer that validates nothing but this schema (the SDK,
    # another language's client) had zero protection against `onClick`, an
    # unknown prop or a host component — every node-level guarantee lived in
    # Python. These five assertions are what keep that from regressing.
    defs = manifest.get("$defs") or {}
    node = defs.get("node") or {}
    check(node.get("additionalProperties") is False,
          "manifest.schema.json $defs.node.additionalProperties must be false (a node key is a script hook)")
    check(set(node.get("properties") or {}) ==
          {"type", "component", "id", "visible", "data_ref", "props", "children", "on"},
          f"manifest.schema.json $defs.node.properties drifted: {sorted((node.get('properties') or {}))}")
    node_props = node.get("properties") or {}
    check(set((node_props.get("type") or {}).get("enum") or []) == UI_NODE_TYPES,
          "manifest.schema.json $defs.node.type enum must be the six C-19 node types")
    check(set((node_props.get("component") or {}).get("enum") or []) == set(UI_COMPONENTS),
          "manifest.schema.json $defs.node.component enum must be exactly the seven page components "
          "(a host component in the enum is phishing)")
    check((node_props.get("props") or {}).get("$ref") == "#/$defs/uiProps",
          "manifest.schema.json $defs.node.props must point at the closed $defs/uiProps")
    check((node_props.get("on") or {}).get("$ref") == "#/$defs/uiOn",
          "manifest.schema.json $defs.node.on must point at the closed $defs/uiOn")
    ui_props = defs.get("uiProps") or {}
    check(ui_props.get("additionalProperties") is False,
          "manifest.schema.json $defs/uiProps.additionalProperties must be false (props are a closed set)")
    ui_on = defs.get("uiOn") or {}
    check(ui_on.get("additionalProperties") is False,
          "manifest.schema.json $defs/uiOn.additionalProperties must be false (`onClick` is not an event)")
    check(set(ui_on.get("properties") or {}) == {"click", "change"},
          "manifest.schema.json $defs/uiOn.properties must be exactly click/change")
    ui_action = defs.get("uiAction") or {}
    check(ui_action.get("additionalProperties") is False,
          "manifest.schema.json $defs/uiAction.additionalProperties must be false")
    check(set((ui_action.get("properties") or {}).get("kind", {}).get("enum") or []) == UI_ACTION_KINDS,
          "manifest.schema.json $defs/uiAction.kind enum must be the four C-19 action kinds")
    inline = (((page.get("properties") or {}).get("schema") or {}).get("oneOf") or [{}])[0]
    check(inline.get("$ref") == "#/$defs/node",
          "manifest.schema.json page.schema's inline branch must $ref $defs/node "
          "(otherwise the closed node tree is not reachable from a real manifest)")
    props = page.get("properties") or {}
    check(set((props.get("icon") or {}).get("enum") or []) ==
          {"list", "chart", "settings", "tool", "file", "none"},
          "manifest.schema.json page.icon enum drifted from C-19 §4.2")
    check(set((props.get("entry") or {}).get("enum") or []) == {"auto", "panel", "capabilities"},
          "manifest.schema.json page.entry enum drifted from C-19 §4.2")
    branches = list(((props.get("schema") or {}).get("oneOf")) or [])
    check(len(branches) == 2,
          "manifest.schema.json page.schema must be oneOf[inline tree, ref] — two disjoint branches")
    limits = (manifest.get("x-vade") or {}).get("limits") or {}
    for key, expected in UI_LIMITS.items():
        check(limits.get(key) == expected,
              f"manifest.schema.json x-vade.limits.{key} must be {expected} (RFC-0009 §7.3)")

    if str(ROOT / "core") not in sys.path:
        sys.path.insert(0, str(ROOT / "core"))
    try:
        from vade.registry import uipage
    except Exception as exc:  # pragma: no cover - import failure is the finding
        FAILURES.append(f"core/vade/registry/uipage.py: cannot import ({type(exc).__name__}: {exc})")
    else:
        check(set(uipage.COMPONENTS) == UI_COMPONENTS,
              f"uipage.COMPONENTS {sorted(uipage.COMPONENTS)} != C-19 rev 1 {sorted(UI_COMPONENTS)}")
        check(set(uipage.HOST_COMPONENTS) == UI_HOST_COMPONENTS,
              f"uipage.HOST_COMPONENTS {sorted(uipage.HOST_COMPONENTS)} != {sorted(UI_HOST_COMPONENTS)}")
        check(set(uipage.ACTION_KINDS) == UI_ACTION_KINDS,
              f"uipage.ACTION_KINDS {sorted(uipage.ACTION_KINDS)} != {sorted(UI_ACTION_KINDS)}")
        code_limits = {
            "pages_per_module": uipage.MAX_PAGES,
            "ui_block_max_bytes": uipage.MAX_UI_BLOCK_BYTES,
            "ui_inline_max_bytes": uipage.MAX_INLINE_BYTES,
            "ui_ref_max_bytes": uipage.MAX_REF_BYTES,
            "ui_nodes_per_page": uipage.MAX_NODES,
            "ui_depth_max": uipage.MAX_DEPTH,
        }
        check(code_limits == UI_LIMITS,
              f"uipage limits {code_limits} != C-19 {UI_LIMITS}")
        check(set(uipage.BINDING_ROOTS) == {"module.status", "page.state", "item"},
              "uipage.BINDING_ROOTS must stay the three read-only roots (§5.1)")

    page_dir = CORE / "modulepage"
    if not page_dir.is_dir():
        FAILURES.append("core/vade/modulepage: the C-19 serving package is missing")
    else:
        for path in sorted(page_dir.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in UI_FORBIDDEN_CODE:
                check(token not in text,
                      f"{path.relative_to(ROOT)}: contains {token!r} — §10.1 forbids any evaluator/browser on the page path")

    ui_to_core = ((rpc.get("directions") or {}).get("ui_to_core")) or {}
    core_to_module = ((rpc.get("directions") or {}).get("core_to_module")) or {}
    for name in ("ui/page.get", "ui/state.get", "ui/tool.call"):
        check(name in ui_to_core, f"rpc.json ui_to_core missing {name} (RFC-0009 §9.3)")
    for name in ("state/get", "page/get"):
        check(name in core_to_module, f"rpc.json core_to_module missing {name} (RFC-0009 §9.3)")
    tool_call = ui_to_core.get("ui/tool.call") or {}
    required = set((tool_call.get("params") or {}).keys())
    check({"module_id", "instance_id", "page_id", "tool", "args", "client_call_id"} <= required,
          f"rpc.json ui/tool.call params missing {sorted({'module_id','instance_id','page_id','tool','args','client_call_id'} - required)}")
    result = tool_call.get("result") or {}
    statuses = str(result.get("status") or "")
    check(all(word in statuses for word in ("approval_required", "denied", "ok", "failed")),
          "rpc.json ui/tool.call.result.status must name all four C-19 states")
    page_get = ui_to_core.get("ui/page.get") or {}
    check(set((page_get.get("result") or {}).keys()) == {"schema", "sha256"},
          "rpc.json ui/page.get.result must be {schema, sha256}")
    state_get = ui_to_core.get("ui/state.get") or {}
    # B5: `unchanged` is part of the result — the client must be able to tell a
    # stale reply (cached value returned) from a fresh one.
    check(set((state_get.get("result") or {}).keys()) == {"rev", "state", "fetched_at", "unchanged"},
          "rpc.json ui/state.get.result must be {rev, state, fetched_at, unchanged}")

    # C-06 is untouched by RFC-0009 (§9.1 #5): a page approval resolves through
    # `approval/decide`, not through a new event family.
    events_path = SCHEMA / "events.json"
    if events_path.exists():
        names = set((json.loads(events_path.read_text(encoding="utf-8")).get("events") or {}))
        check(not {name for name in names if name.startswith("ui.")},
              "events.json: RFC-0009 adds no ui.* event (page approvals are RPC-only, §9.4)")


def check_session_meta(rpc: dict, events: dict) -> None:
    """RFC-0010: the ten `session/*` methods, the §7 validation constants, and the two events.

    Three things a review can miss by reading:

    * the icon whitelist is the GATE, not just the pattern — a shape-valid
      unknown name must be refused (otherwise "whitelist" is decoration);
    * the events must not be state-change events (they are metadata edges) and
      must not be C-14 push topics;
    * the handlers must actually be registered in `SessionRPC.handlers()` —
      a schema entry without a handler answers -32601 on a real socket.
    """
    directions = rpc.get("directions") or {}
    ui_to_core = directions.get("ui_to_core") or {}
    missing = sorted(SESSION_META_METHODS - set(ui_to_core))
    check(not missing, f"rpc.json ui_to_core missing RFC-0010 methods: {missing}")
    elsewhere = sorted(
        SESSION_META_METHODS
        & (set(directions.get("module_to_core") or {}) | set(directions.get("core_to_module") or {}))
    )
    check(not elsewhere, f"rpc.json: RFC-0010 methods must not be module-facing: {elsewhere}")
    codes = set(load("errors.json").get("codes") or {})
    for name in sorted(SESSION_META_METHODS & set(ui_to_core)):
        entry = ui_to_core.get(name) or {}
        for field in ("params", "result", "errors"):
            check(field in entry, f"rpc.json {name}: missing {field}")
        unknown = sorted(set(entry.get("errors") or []) - codes)
        check(not unknown, f"rpc.json {name}: errors not declared in C-04: {unknown}")
    check(rpc.get("version") in ("1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"),
          f"rpc.json: version must be 1.3.0 (RFC-0010), 1.4.0 (RFC-0012), 1.5.0 (RFC-0014), "
          f"1.6.0 (RFC-0015) or 1.7.0 (RFC-0016, the current head) (got {rpc.get('version')!r})")
    rpc_appends = (rpc.get("x-vade") or {}).get("appends") or []
    rblock = next((b for b in rpc_appends if "RFC-0010" in str(b.get("doc") or "")), None)
    check(rblock is not None, "rpc.json: missing the x-vade.appends block for RFC-0010")
    if rblock is not None:
        check(set(rblock.get("adds") or []) == SESSION_META_METHODS,
              "rpc.json: the RFC-0010 append block must add exactly the ten session/* methods")
        check("none" in str(rblock.get("rewrites") or ""),
              "rpc.json: the RFC-0010 append must be rewrite-free")

    # §7: the validation rules live in code; the constants are the contract.
    if str(ROOT / "core") not in sys.path:
        sys.path.insert(0, str(ROOT / "core"))
    try:
        from vade.errors import VadeError
        from vade.session import manager as session_manager
        from vade.session.rpc import SessionRPC
    except Exception as exc:  # pragma: no cover - import failure is the finding
        FAILURES.append(f"core/vade/session: cannot import for RFC-0010 checks "
                        f"({type(exc).__name__}: {exc})")
        return
    check(str(session_manager.ICON_PATTERN.pattern) == r"^[a-z][a-z0-9-]*$",
          "manager.ICON_PATTERN drifted from RFC-0010 §7.1")
    check(session_manager.ICON_NAME_MAX == 64,
          f"manager.ICON_NAME_MAX must be 64, is {session_manager.ICON_NAME_MAX}")
    check(session_manager.GROUP_NAME_MAX == 64,
          f"manager.GROUP_NAME_MAX must be 64, is {session_manager.GROUP_NAME_MAX}")
    check(str(session_manager.COLOR_PATTERN.pattern) == r"^#[0-9a-fA-F]{6}$",
          "manager.COLOR_PATTERN drifted from RFC-0010 §7.2 (#RRGGBB only)")
    check(len(session_manager.ICON_NAMES) >= 16,
          f"manager.ICON_NAMES whitelist is implausibly small ({len(session_manager.ICON_NAMES)})")
    for icon in session_manager.ICON_NAMES:
        check(bool(session_manager.ICON_PATTERN.match(icon)) and len(icon) <= session_manager.ICON_NAME_MAX,
              f"manager.ICON_NAMES member {icon!r} violates its own §7.1 shape rule")
    for probe, expect_ok in (("chat", True), ("zz-not-an-icon", False), ("1bad", False), ("Chat", False)):
        try:
            ok = session_manager.validate_icon(probe) == probe
        except VadeError:
            ok = False
        check(ok == expect_ok,
              f"manager.validate_icon({probe!r}) -> {ok!r}, expected {expect_ok!r} (§7.1 whitelist gate)")
    for probe in ("#fff", "#GGHHII", "red", "4a90d9", "#4a90d90"):
        try:
            session_manager.validate_color(probe)
            rejected = False
        except VadeError:
            rejected = True
        check(rejected, f"manager.validate_color({probe!r}) must be refused (§7.2)")
    try:
        session_manager.validate_color("#4A90D9")
        accepted = True
    except VadeError:
        accepted = False
    check(accepted, "manager.validate_color('#4A90D9') must be accepted (§7.2)")

    registered = set(SessionRPC(None).handlers())
    absent = sorted(SESSION_META_METHODS - registered)
    check(not absent, f"SessionRPC.handlers() missing RFC-0010 methods: {absent}")

    # §6: the two events, their exact `required`, and no state/push pollution.
    declared = dict(events.get("events") or {})
    for name, required in SESSION_META_EVENTS.items():
        spec = declared.get(name)
        check(isinstance(spec, dict), f"events.json: {name} is missing (RFC-0010 §6)")
        if isinstance(spec, dict):
            check(set(spec.get("required") or []) == set(required),
                  f"events.json {name}: required must be exactly {sorted(required)}")
            undeclared = [f for f in required if f not in (spec.get("payload") or {})]
            check(not undeclared, f"events.json {name}: required fields not declared in payload: {undeclared}")
            check("state" not in (spec.get("required") or []),
                  f"events.json {name}: a metadata edge must not require `state`")
            check((spec.get("x-vade") or {}).get("push") is None,
                  f"events.json {name}: must not be a C-14 push topic (session event)")
            check("state" not in (spec.get("payload") or {}),
                  f"events.json {name}: metadata edge must not carry `state` (§6)")
    appends = (events.get("x-vade") or {}).get("appends") or []
    eblock = next((b for b in appends if "RFC-0010" in str(b.get("doc") or "")), None)
    check(eblock is not None, "events.json: missing the x-vade.appends block for RFC-0010")
    if eblock is not None:
        check(set(eblock.get("adds") or []) == set(SESSION_META_EVENTS),
              "events.json: the RFC-0010 append block must add exactly the two session.* events")
        check("none" in str(eblock.get("rewrites") or ""),
              "events.json: the RFC-0010 append must be rewrite-free")
        check(eblock.get("version") == "2.0.0",
              "events.json: the RFC-0010 append must NOT claim an enum bump (top-level stays 2.0.0)")


def check_session_search(rpc: dict) -> None:
    """RFC-0014: `session/search` — declared, registered, and injection-safe.

    The two things a review can miss by reading:

    * the limit cap must live in exactly ONE place (the handler) — a second
      copy would let mutation ② (§8) delete one and keep the tests green;
    * `escape_like` must touch ONLY the three LIKE metacharacters. Escaping
      the quote or `--` would corrupt honest queries; parameterization is
      what keeps those literal, so the gate asserts the quote survives
      `escape_like` untouched.
    """
    directions = rpc.get("directions") or {}
    ui_to_core = directions.get("ui_to_core") or {}
    entry = ui_to_core.get("session/search")
    check(entry is not None, "rpc.json: session/search is missing from ui_to_core (RFC-0014 §5)")
    elsewhere = sorted(
        SESSION_SEARCH_METHODS
        & (set(directions.get("module_to_core") or {}) | set(directions.get("core_to_module") or {}))
    )
    check(not elsewhere, f"rpc.json: RFC-0014 methods must not be module-facing: {elsewhere}")
    if isinstance(entry, dict):
        for field in ("params", "result", "errors"):
            check(field in entry, f"rpc.json session/search: missing {field}")
        unknown = sorted(set(entry.get("errors") or []) - set(load("errors.json").get("codes") or {}))
        check(not unknown, f"rpc.json session/search: errors not declared in C-04: {unknown}")
        check(set(entry.get("params") or {}) == {"query", "limit", "offset", "session_id"},
              "rpc.json session/search.params must be exactly {query, limit, offset, session_id}")
        check(set(entry.get("result") or {}) == {"results", "total"},
              "rpc.json session/search.result must be exactly {results, total}")
        rows = (entry.get("result") or {}).get("results")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            check(set(rows[0]) == SESSION_SEARCH_HIT_KEYS,
                  f"rpc.json session/search hit-row shape drifted: {sorted(set(rows[0]) ^ SESSION_SEARCH_HIT_KEYS)}")
        else:
            check(False, "rpc.json session/search.result.results: must declare the hit-row shape (one object)")
    check(rpc.get("version") in ("1.5.0", "1.6.0", "1.7.0"),
          f"rpc.json: version must be 1.5.0 (RFC-0014), 1.6.0 (RFC-0015) or "
          f"1.7.0 (RFC-0016, the current head) after the RFC-0014 append (got {rpc.get('version')!r})")

    if str(ROOT / "core") not in sys.path:
        sys.path.insert(0, str(ROOT / "core"))
    try:
        from vade.session import search as search_mod
        from vade.session.rpc import SessionRPC
    except Exception as exc:  # pragma: no cover - import failure is the finding
        FAILURES.append(f"core/vade/session: cannot import for RFC-0014 checks "
                        f"({type(exc).__name__}: {exc})")
        return
    check("session/search" in SessionRPC(None).handlers(),
          "SessionRPC.handlers(): session/search is not registered (RFC-0014 §5)")
    # §4 constants are the contract; they are echoed in schema/rpc.json strings.
    check((search_mod.QUERY_MAX, search_mod.LIMIT_MAX, search_mod.LIMIT_DEFAULT, search_mod.SNIPPET_MAX)
          == (200, 100, 20, 120),
          f"search constants drifted from RFC-0014 §4: "
          f"QUERY_MAX={search_mod.QUERY_MAX} LIMIT_MAX={search_mod.LIMIT_MAX} "
          f"LIMIT_DEFAULT={search_mod.LIMIT_DEFAULT} SNIPPET_MAX={search_mod.SNIPPET_MAX}")
    # §4.10: the escape is the injection gate — metacharacters only.
    for probe, expect in (("%", "\\%"), ("_", "\\_"), ("\\", "\\\\"), ("'", "'"), ("--", "--"), (";", ";")):
        got = search_mod.escape_like(probe)
        check(got == expect,
              f"escape_like({probe!r}) -> {got!r}, expected {expect!r} "
              f"(metacharacters only; quote/-- are parameterization's job)")
    # §4.10: the store binds the pattern — the source may not interpolate it.
    store_text = (CORE / "session" / "sqlite_store.py").read_text(encoding="utf-8")
    check("LIKE ? ESCAPE" in store_text,
          "sqlite_store.search_sessions: the LIKE pattern must be a bound parameter (LIKE ? ESCAPE)")
    check("LIKE '%" not in store_text,
          "sqlite_store.search_sessions: found a literal-quote LIKE — the pattern must never enter SQL text")

    rpc_appends = (rpc.get("x-vade") or {}).get("appends") or []
    block = next((b for b in rpc_appends if isinstance(b, dict)
                  and "RFC-0014" in str(b.get("doc") or "")), None)
    check(block is not None, "rpc.json: missing the x-vade.appends block for RFC-0014")
    if block is not None:
        check(set(block.get("adds") or []) == SESSION_SEARCH_METHODS,
              "rpc.json: the RFC-0014 append block must add exactly session/search")
        check(block.get("version") == "1.5.0",
              "rpc.json: the RFC-0014 append block must carry version 1.5.0")
        check("none" in str(block.get("rewrites") or ""),
              "rpc.json: the RFC-0014 append must be rewrite-free")


#: RFC-0016 §4.1: the minting method and its frozen shape.
BLOB_TOKEN_METHOD = "ui/blob.token"
BLOB_TOKEN_PARAMS = {"ttl_s"}
BLOB_TOKEN_RESULT = {"token", "expires_at", "max_bytes", "endpoints"}
BLOB_TOKEN_ENDPOINT_KEYS = {"begin", "part", "complete", "abort", "resume", "fetch"}
BLOB_TOKEN_ERRORS = {"INVALID_ARGUMENT", "PERMISSION_DENIED", "READ_ONLY_SESSION", "UNSUPPORTED"}


def check_blob_token(rpc: dict) -> None:
    """RFC-0016 §10 A1: `ui/blob.token` is declared, shaped, and registered.

    Three things a review can miss by reading:

    * the `endpoints` table is an ALIAS of `http_endpoints.endpoints[]` — a
      second copy that drifted would hand clients routes the listener no
      longer serves, so the check compares the two tables verbatim;
    * the handler must be registered in `BlobTokenRPC.handlers()` — a schema
      entry without a handler answers NOT_FOUND on a real socket;
    * the `TOKEN_TTL_*` constants ARE the contract (拍板 Q2: 600 s default,
      60..900 window) — a constant drift silently changes credential
      lifetimes for every client that pinned the RFC value.
    """
    directions = rpc.get("directions") or {}
    ui_to_core = directions.get("ui_to_core") or {}
    entry = ui_to_core.get(BLOB_TOKEN_METHOD)
    check(entry is not None, f"rpc.json: {BLOB_TOKEN_METHOD} is missing from ui_to_core (RFC-0016 §4)")
    elsewhere = sorted(
        {BLOB_TOKEN_METHOD}
        & (set(directions.get("module_to_core") or {}) | set(directions.get("core_to_module") or {}))
    )
    check(not elsewhere, f"rpc.json: RFC-0016 methods must not be module-facing: {elsewhere}")
    if isinstance(entry, dict):
        for field in ("params", "result", "errors", "notes"):
            check(field in entry, f"rpc.json {BLOB_TOKEN_METHOD}: missing {field}")
        codes = set(load("errors.json").get("codes") or {})
        unknown = sorted(set(entry.get("errors") or []) - codes)
        check(not unknown, f"rpc.json {BLOB_TOKEN_METHOD}: errors not declared in C-04: {unknown}")
        check(set(entry.get("params") or {}) == BLOB_TOKEN_PARAMS,
              "rpc.json ui/blob.token.params must be exactly {ttl_s}")
        check(set(entry.get("result") or {}) == BLOB_TOKEN_RESULT,
              "rpc.json ui/blob.token.result must be exactly {token, expires_at, max_bytes, endpoints}")
        endpoints = (entry.get("result") or {}).get("endpoints")
        if isinstance(endpoints, dict):
            check(set(endpoints) == BLOB_TOKEN_ENDPOINT_KEYS,
                  "rpc.json ui/blob.token.endpoints must have exactly the six keys")
            declared_rows = {
                f'{str(row.get("method"))} {str(row.get("path"))}'
                for row in ((rpc.get("http_endpoints") or {}).get("endpoints") or [])
                if isinstance(row, dict)
            }
            check(set(endpoints.values()) == declared_rows,
                  "rpc.json ui/blob.token.endpoints drift vs http_endpoints.endpoints[] "
                  f"(they are aliases, not a second contract): {sorted(set(endpoints.values()) ^ declared_rows)}")
        else:
            check(False, "rpc.json ui/blob.token.result.endpoints must be the six-key object")
        check(set(entry.get("errors") or []) == BLOB_TOKEN_ERRORS,
              "rpc.json ui/blob.token.errors must be exactly the four reused C-04 codes (no new code)")

    check(rpc.get("version") == "1.7.0",
          f"rpc.json: version must be 1.7.0 (RFC-0016 owns the C-05 head) (got {rpc.get('version')!r})")

    if str(ROOT / "core") not in sys.path:
        sys.path.insert(0, str(ROOT / "core"))
    try:
        from vade.blob.rpc import BLOB_TOKEN_ENDPOINTS, BlobTokenRPC
        from vade.blob.service import TOKEN_TTL_DEFAULT, TOKEN_TTL_MAX, TOKEN_TTL_MIN
    except Exception as exc:  # pragma: no cover - import failure is the finding
        FAILURES.append(f"core/vade/blob: cannot import for RFC-0016 checks "
                        f"({type(exc).__name__}: {exc})")
        return
    check(BLOB_TOKEN_METHOD in BlobTokenRPC(None).handlers(),
          "BlobTokenRPC.handlers(): ui/blob.token is not registered (RFC-0016 §5)")
    check((TOKEN_TTL_DEFAULT, TOKEN_TTL_MIN, TOKEN_TTL_MAX) == (600, 60, 900),
          f"blob.service TOKEN_TTL_* drifted from RFC-0016 §5.2: "
          f"default={TOKEN_TTL_DEFAULT} min={TOKEN_TTL_MIN} max={TOKEN_TTL_MAX}")
    # The code-side alias table and the schema say the same six routes.
    check(set(BLOB_TOKEN_ENDPOINTS) == BLOB_TOKEN_ENDPOINT_KEYS,
          "vade.blob.rpc.BLOB_TOKEN_ENDPOINTS keys drifted from RFC-0016 §4.3")
    schema_endpoints = set((((entry or {}).get("result") or {}).get("endpoints") or {}).values())
    check(set(BLOB_TOKEN_ENDPOINTS.values()) == schema_endpoints,
          "BLOB_TOKEN_ENDPOINTS values drift vs schema/rpc.json ui/blob.token.endpoints")
    # auth.device must name the minting method (§9.3): it is the only
    # client-visible sentence about where a bearer comes from.
    auth_device = str(((rpc.get("http_endpoints") or {}).get("auth") or {}).get("device") or "")
    check(BLOB_TOKEN_METHOD in auth_device,
          "rpc.json http_endpoints.auth.device must name ui/blob.token (RFC-0016 §9.3)")

    rpc_appends = (rpc.get("x-vade") or {}).get("appends") or []
    block = next((b for b in rpc_appends if isinstance(b, dict)
                  and "RFC-0016" in str(b.get("doc") or "")), None)
    check(block is not None, "rpc.json: missing the x-vade.appends block for RFC-0016")
    if block is not None:
        check(set(block.get("adds") or []) == {BLOB_TOKEN_METHOD},
              "rpc.json: the RFC-0016 append block must add exactly ui/blob.token")
        check(block.get("version") == "1.7.0",
              "rpc.json: the RFC-0016 append block must carry version 1.7.0")
        check("none" in str(block.get("rewrites") or ""),
              "rpc.json: the RFC-0016 append must be rewrite-free")
        check(block.get("contract") == "C-05" and block.get("owner") == "C2",
              "rpc.json: the RFC-0016 append block must carry contract C-05 / owner C2")


def main() -> int:
    manifest = load("manifest.schema.json")
    errors = load("errors.json")
    rpc = load("rpc.json")
    config = load("config.schema.json")

    check(manifest.get("x-vade", {}).get("contract") == "C-03", "manifest.schema.json: contract id != C-03")
    check(errors.get("id") == "C-04", "errors.json: id != C-04")
    check(rpc.get("id") == "C-05", "rpc.json: id != C-05")
    check(config.get("x-vade", {}).get("contract") == "C-18", "config.schema.json: contract id != C-18")

    expected = declared_codes()
    actual = set(errors.get("codes", {}))
    check(actual == expected, f"errors.json drift: missing={sorted(expected - actual)} extra={sorted(actual - expected)}")
    check(len(actual) == 34, f"errors.json must have 34 codes, has {len(actual)}")
    check(errors.get("count") == len(actual), "errors.json: count field disagrees with codes")

    invented = {c for c in rust_codes() if c and c[0].isupper() and c not in actual}
    # Rust enum variants are PascalCase; map them to SCREAMING_SNAKE.
    def to_snake(name: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()

    invented = {to_snake(c) for c in invented} - actual
    check(not invented, f"protocol/core error.rs declares codes absent from C-04: {sorted(invented)}")

    check(manifest.get("additionalProperties") is True, "manifest.schema.json: additionalProperties must be true")
    limits = manifest.get("x-vade", {}).get("limits", {})
    check(limits.get("max_payload_ceiling") == 65519, "manifest.schema.json: frame ceiling must be 65519 (C-01 §7.5)")
    tool_schema = manifest.get("$defs", {}).get("tool", {})
    for field in ("name", "description", "input_schema", "risk", "timeout_ms", "idempotent"):
        check(field in tool_schema.get("required", []), f"manifest tool: {field} must be required")

    required_sections = {
        "core", "tls", "policy", "audit", "server", "dev", "memory", "evolve", "compaction", "skills",
        "tasks",
    }
    present = set(config.get("properties", {}))
    check(required_sections <= present, f"config.schema.json missing sections: {sorted(required_sections - present)}")
    check("tool_risk" in config["properties"]["policy"]["properties"], "config.schema.json: [policy.tool_risk] missing")
    check(
        config.get("additionalProperties") is False,
        "config.schema.json: root additionalProperties must be false (unknown keys abort startup)",
    )
    for dotted, leaf in walk_leaves(config):
        meta = leaf.get("x-vade")
        check(bool(meta), f"config key {dotted}: missing x-vade metadata")
        if meta:
            check(meta.get("apply") in ("hot", "restart"), f"config key {dotted}: apply must be hot|restart")
            check(bool(meta.get("owner")), f"config key {dotted}: owner missing")
            check("default" in leaf, f"config key {dotted}: default missing")

    check_attach(config)
    check_usage(config)
    check_llm_append(config)
    check_blob_routes()
    check_manifest_models(manifest)
    check_tasks(config)
    check_ui(config, manifest, rpc)
    check_submit_parts(rpc)
    check_http_endpoints(rpc)

    methods = {
        "module/register", "module/capabilities.update", "module/heartbeat",
        "tools/call", "tools/cancel", "module/drain", "approval/decide", "ui/submit",
        # RFC-0008 §7.1/§7.2
        "turn/input", "turn/input.cancel",
        # RFC-0006 §6.2/§6.3
        "turn/regenerate", "message/edit_resubmit",
        # RFC-0007 §6.1-§6.4
        "task/list", "task/log_tail", "task/cancel", "subagent/message",
        # RFC-0012 §6.1/§6.2
        "subagent/list", "subagent/get",
        "ui/page.get", "ui/state.get", "ui/tool.call", "state/get", "page/get",
    }
    declared = set()
    for direction in rpc.get("directions", {}).values():
        declared |= set(direction)
    check(methods <= declared, f"rpc.json missing methods: {sorted(methods - declared)}")
    check_model_methods(rpc)
    check(errors.get("codes", {}).get("MANIFEST_INVALID", {}).get("http") == 400, "MANIFEST_INVALID http must be 400")

    # ---------------------------------------------------------------- C-06 events
    events = load("events.json")
    check(events.get("id") == "C-06", "events.json: id != C-06")
    check(events.get("status") == "frozen", "events.json: status must be frozen")
    declared_events = dict(events.get("events") or {})
    check("turn.state" in declared_events, "events.json: turn.state enum is missing")
    schema_events = {name for name in declared_events if name != "turn.state"}
    unknown = emitted_event_names() - schema_events
    check(not unknown, f"events.json: code emits event names absent from C-06: {sorted(unknown)}")
    pushed = pushed_event_names()
    marked = push_declared(events)
    missing_handlers = schema_events - emitted_event_names() - pushed - {"turn.approval_required"}
    check(not missing_handlers, f"events.json: declared events nobody emits: {sorted(missing_handlers)}")
    # C-14: push topics are not exempt, they are scanned at `_dispatch()`.
    unknown_push = pushed - schema_events
    check(not unknown_push, f"events.json: events/deliver pushes topics absent from C-06: {sorted(unknown_push)}")
    unmarked_push = pushed - marked
    check(not unmarked_push, f"events.json: pushed topics missing `x-vade.push: {PUSH_TRANSPORT}`: {sorted(unmarked_push)}")
    unserved = marked - code_topics()
    check(not unserved, f"events.json: declared push topics the bus does not serve: {sorted(unserved)}")
    unmarked = code_topics() - marked
    check(not unmarked, f"core/vade/events/subscribe.py serves topics not declared as push in C-06: {sorted(unmarked)}")
    for name, spec in declared_events.items():
        if name == "turn.state":
            continue
        payload = spec.get("payload") or {}
        required = list(spec.get("required") or [])
        undeclared = [field for field in required if field not in payload]
        check(not undeclared, f"events.json {name}: required fields not declared in payload: {undeclared}")
        if name in STATE_CHANGE_EVENTS:
            check("state" in payload and "state" in required,
                  f"events.json {name}: a state-change event must declare AND require `state` (C-06 rule 4)")
        if name in PAYLOAD_ONLY_EVENTS:
            check("state" not in required,
                  f"events.json {name}: payload-only event must not require `state` (C-06 rule 4)")
    check(STATE_CHANGE_EVENTS <= schema_events,
          f"events.json missing state-change events: {sorted(STATE_CHANGE_EVENTS - schema_events)}")
    states = list(events.get("turn_states") or [])
    check(set(states) == code_turn_states(), "events.json turn_states drift vs core/vade/session/models.TurnState")
    check(set(declared_events.get("turn.state", {}).get("enum") or []) == set(states),
          "events.json: turn.state enum disagrees with turn_states")
    check_usage_budget_event(events)
    check_rfc0006(events, rpc, config)
    check_task_events(events, declared_events, schema_events)
    check_approval_attribution(declared_events)
    check_session_meta(rpc, events)
    check_session_search(rpc)
    check_blob_token(rpc)
    check_subagent_events(events, declared_events, schema_events, rpc)

    # ---------------------------------------------------------------- RFC-0003
    check_prompt_layers(load("skill.schema.json"), config, rpc, events, declared_events)

    # ---------------------------------------------------------------- RFC-0008
    # C-06 went 1.0.0 -> 2.0.0 because `turn_states` was WIDENED: an exhaustive
    # validator on the old side would silently drop the new state's events, so
    # the version is the one place that can warn it. A future RFC that only
    # appends fields must not quietly reuse 2.0.0 — this pins today's value.
    check(events.get("version") == "2.0.0",
          f"events.json: version must be 2.0.0 after the RFC-0008 enum widening (got {events.get('version')!r})")
    appends = (events.get("x-vade") or {}).get("appends") or []
    rfc0008 = next((block for block in appends if isinstance(block, dict)
                    and "RFC-0008" in str(block.get("doc") or "")), None)
    check(rfc0008 is not None, "events.json: missing the x-vade.appends block for RFC-0008")
    if rfc0008 is not None:
        check(rfc0008.get("version") == "2.0.0",
              "events.json: the RFC-0008 append block must carry version 2.0.0")
        widen = (rfc0008.get("enum_widen") or {}).get("turn_states") or []
        check("waiting_input" in widen,
              "events.json: the RFC-0008 append block must declare enum_widen.turn_states=['waiting_input']")
        check(set(rfc0008.get("adds") or []) == {"turn.input_required", "turn.input_resolved"},
              "events.json: the RFC-0008 append block must add exactly the two turn.input_* events")
    for name in ("turn.input_required", "turn.input_resolved"):
        spec = declared_events.get(name) or {}
        check(bool(spec), f"events.json: {name} is not declared (RFC-0008 §6)")
        check("state" in (spec.get("required") or []),
              f"events.json {name}: a state-change event must require `state` (C-06 rule 4)")
    check("waiting_input" in states,
          "events.json: turn_states must contain `waiting_input` (RFC-0008 §6.3)")

    if FAILURES:
        print("contracts: FAIL")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete, "
          "RFC-0005 append green, RFC-0007 task family green, RFC-0010 session metadata green, "
          "RFC-0012 subagent family green, RFC-0014 session search green, RFC-0016 blob token green, "
          "RFC-0003 layers green, event gate green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
