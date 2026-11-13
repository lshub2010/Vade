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

#: C-06 rule 4: `state` is required on exactly these, forbidden as a required
#: field on the high-frequency payload events.
STATE_CHANGE_EVENTS = {
    "turn.queued", "turn.started", "turn.completed", "turn.failed",
    "turn.interrupted", "turn.cancelled", "turn.approval_resolved",
}
PAYLOAD_ONLY_EVENTS = {"turn.delta", "turn.tool_call", "turn.tool_result"}

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

    methods = {
        "module/register", "module/capabilities.update", "module/heartbeat",
        "tools/call", "tools/cancel", "module/drain", "approval/decide", "ui/submit",
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
    missing_handlers = schema_events - emitted_event_names() - {"turn.approval_required"}
    check(not missing_handlers, f"events.json: declared events nobody emits: {sorted(missing_handlers)}")
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

    if FAILURES:
        print("contracts: FAIL")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 33 error codes, config metadata complete, event gate green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
