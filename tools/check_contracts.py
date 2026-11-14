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
}
PAYLOAD_ONLY_EVENTS = {"turn.delta", "turn.tool_call", "turn.tool_result"}

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
        names |= set(pattern.findall(path.read_text(encoding="utf-8")))
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

    methods = {
        "module/register", "module/capabilities.update", "module/heartbeat",
        "tools/call", "tools/cancel", "module/drain", "approval/decide", "ui/submit",
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

    if FAILURES:
        print("contracts: FAIL")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete, "
          "RFC-0005 append green, event gate green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
