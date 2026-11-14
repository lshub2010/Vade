#!/usr/bin/env python3
"""Contract gate #2: schema files agree with docs/agents/01-contracts.md.

Checks (each one has broken at least once in review):
  1. `schema/*.json` parse and declare the right contract id.
  2. `errors.json` code set == the code block in 01-contracts §二 (33 codes).
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
    check(len(actual) == 33, f"errors.json must have 33 codes, has {len(actual)}")
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
    check_blob_routes()
    check_submit_parts(rpc)
    check_http_endpoints(rpc)

    methods = {
        "module/register", "module/capabilities.update", "module/heartbeat",
        "tools/call", "tools/cancel", "module/drain", "approval/decide", "ui/submit",
        # RFC-0008 §7.1/§7.2
        "turn/input", "turn/input.cancel",
    }
    declared = set()
    for direction in rpc.get("directions", {}).values():
        declared |= set(direction)
    check(methods <= declared, f"rpc.json missing methods: {sorted(methods - declared)}")
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
    print("contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 33 error codes, config metadata complete, event gate green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
