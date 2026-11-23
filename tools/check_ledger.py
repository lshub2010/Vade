#!/usr/bin/env python3
"""Ledger gate: docs/rfc/LEDGER.md must agree with the repo's real state.

Recomputes everything the ledger claims and diffs it. Four independent checks:

  1. Contract truth: version / method counts / event counts / error codes /
     append-block counts recomputed from `schema/*.json` and compared with the
     ledger's JSON facts block (§3). Per-RFC `adds` (methods, events, manifest
     fields) and append-level versions are re-derived from every
     `x-vade.appends` block and compared per RFC.
  2. Implementation paths: every path listed in an RFC row must exist in the
     repo. A row may have no paths only if its impl_state says so
     (未实施 / 客户端仓（未核实）); a row WITH contract appends must claim 已实现.
  3. Status: each row's status column must equal the RFC frontmatter `status:`
     verbatim; for the two pre-frontmatter RFCs (0001/0002) it must start with
     the body-table 状态 value.
  4. Acceptance tables: the per-RFC acceptance-row count is recomputed from
     the RFC file (rows whose first cell is an A-id) and compared; the checked
     count is recounted from [x] markers.

Contract parsing is a plain `json.load` — the heavy lifting in
tools/check_contracts.py (emit-site scans, handler registration) is a
different gate and is NOT duplicated here; this file only checks that the
LEDGER document does not drift from the contracts and the RFC texts.

Run:  D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe tools/check_ledger.py   (exit 0 = green)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "rfc" / "LEDGER.md"
SCHEMA = ROOT / "schema"
RFC_DIR = ROOT / "docs" / "rfc"

#: A ledger row without impl paths may only carry one of these impl_state
#: values; anything else with an empty path list is an unverified claim.
EMPTY_IMPL_STATES = {"未实施", "客户端仓（未核实）"}

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def load_ledger() -> tuple[dict, list[list[str]]]:
    """Return (json facts block, markdown table rows split on `|`)."""
    text = LEDGER.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    check(len(blocks) == 1, f"LEDGER.md: expected exactly one ```json facts block, found {len(blocks)}")
    facts = {}
    if blocks:
        try:
            facts = json.loads(blocks[0])
        except json.JSONDecodeError as exc:
            FAILURES.append(f"LEDGER.md: facts block is not valid JSON ({exc})")
    rows = []
    for line in text.splitlines():
        if re.match(r"^\|\s*RFC-\d{4}\s*\|", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(cells)
    return facts, rows


def rfc_key(doc: str) -> str | None:
    m = re.search(r"RFC-(\d{4})", str(doc))
    return f"RFC-{m.group(1)}" if m else None


def contract_truth() -> tuple[dict, dict]:
    """Recompute aggregates and the per-RFC append registration from the schemas."""
    rpc = json.loads((SCHEMA / "rpc.json").read_text(encoding="utf-8"))
    events = json.loads((SCHEMA / "events.json").read_text(encoding="utf-8"))
    manifest = json.loads((SCHEMA / "manifest.schema.json").read_text(encoding="utf-8"))
    errors = json.loads((SCHEMA / "errors.json").read_text(encoding="utf-8"))

    per_dir = {name: len(direction) for name, direction in (rpc.get("directions") or {}).items()}
    aggregate = {
        "rpc_version": rpc.get("version"),
        "rpc_methods_by_direction": per_dir,
        "rpc_methods_total": sum(per_dir.values()),
        "events_version": events.get("version"),
        "events_total_incl_turn_state": len(events.get("events") or {}),
        "events_total_excl_turn_state": len([k for k in (events.get("events") or {}) if k != "turn.state"]),
        "error_codes": len(errors.get("codes") or {}),
        "appends_rpc": len((rpc.get("x-vade") or {}).get("appends") or []),
        "appends_events": len((events.get("x-vade") or {}).get("appends") or []),
        "appends_manifest": len((manifest.get("x-vade") or {}).get("appends") or []),
    }

    per_rfc: dict[str, dict] = {}
    for block in (rpc.get("x-vade") or {}).get("appends") or []:
        key = rfc_key(block.get("doc") or "")
        if key:
            entry = per_rfc.setdefault(key, {"methods": [], "events": [], "manifest_fields": [], "append_versions": {}})
            entry["methods"].extend(block.get("adds") or [])
            entry["append_versions"]["rpc.json"] = block.get("version")
    for block in (events.get("x-vade") or {}).get("appends") or []:
        key = rfc_key(block.get("doc") or "")
        if key:
            entry = per_rfc.setdefault(key, {"methods": [], "events": [], "manifest_fields": [], "append_versions": {}})
            entry["events"].extend(block.get("adds") or [])
            entry["append_versions"]["events.json"] = block.get("version")
    for block in (manifest.get("x-vade") or {}).get("appends") or []:
        key = rfc_key(block.get("doc") or "")
        if key:
            entry = per_rfc.setdefault(key, {"methods": [], "events": [], "manifest_fields": [], "append_versions": {}})
            entry["manifest_fields"].extend(block.get("fields") or [])
            entry["append_versions"]["manifest.schema.json"] = block.get("version")
    return aggregate, per_rfc


def expected_status(ledger_status: str, rfc_path: Path) -> None:
    """Frontmatter `status:` wins; pre-frontmatter RFCs fall back to the body table."""
    text = rfc_path.read_text(encoding="utf-8")
    fm = re.match(r"^---\n(.*?)\n---", text, re.S)
    fm_status = None
    if fm:
        m = re.search(r"^status:\s*(.+)$", fm.group(1), re.M)
        if m:
            fm_status = m.group(1).strip()
    if fm_status is not None:
        check(
            ledger_status == fm_status,
            f"{rfc_path.name}: LEDGER status {ledger_status!r} != frontmatter status {fm_status!r}",
        )
        return
    body = re.search(r"^\|\s*状态\s*\|\s*(.+?)\s*\|", text, re.M)
    check(body is not None, f"{rfc_path.name}: no frontmatter status and no body 状态 table row")
    if body:
        token = body.group(1).replace("**", "").split("（")[0].strip()
        check(
            ledger_status.startswith(token),
            f"{rfc_path.name}: LEDGER status {ledger_status!r} does not start with body-table status {token!r}",
        )


def acceptance_truth(rfc_path: Path) -> tuple[int, int]:
    text = rfc_path.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if re.match(r"^\|\s*A\d", line)]
    checked = sum(1 for line in rows if re.search(r"\[\s*[xX]\s*\]", line))
    return len(rows), checked


def check_section_one_appends(aggregate: dict) -> None:
    """The §1 prose row must agree with the recomputed block counts.

    `check_ledger` used to compare only the trailing JSON facts block and the
    per-RFC rows, so §1 kept a stale "rpc.json 13 块 / 合计 25" through three
    green runs while §3/§4 said 12/24 (f15 review P1-2). A prose table that no
    gate reads is exactly where drift survives.
    """
    text = LEDGER.read_text(encoding="utf-8")
    line = next((ln for ln in text.splitlines() if ln.startswith("| append 登记块 |")), None)
    check(line is not None, "LEDGER.md §1: the `append 登记块` row is missing")
    if line is None:
        return
    seen = re.findall(r"\*\*(\d+)\*\*", line)
    check(len(seen) == 4, f"LEDGER.md §1 append row must carry four bold counts: {line!r}")
    if len(seen) != 4:
        return
    expected = [
        aggregate["appends_rpc"],
        aggregate["appends_events"],
        aggregate["appends_manifest"],
        aggregate["appends_rpc"] + aggregate["appends_events"] + aggregate["appends_manifest"],
    ]
    check([int(value) for value in seen] == expected,
          f"LEDGER.md §1 append counts {seen} != contracts {expected}")


def main() -> int:
    if not LEDGER.exists():
        print("ledger: FAIL (docs/rfc/LEDGER.md missing)")
        return 1
    facts, rows = load_ledger()

    # -- coverage: every RFC file exactly once --------------------------------
    rfc_files = sorted(RFC_DIR.glob("RFC-*.md"))
    row_keys = [cells[0] for cells in rows if cells]
    check(len(row_keys) == len(rfc_files),
          f"LEDGER.md: {len(row_keys)} table rows but {len(rfc_files)} RFC files exist")
    check(len(row_keys) == len(set(row_keys)), "LEDGER.md: duplicate RFC rows")
    for path in rfc_files:
        expected_key = f"RFC-{path.stem.split('-')[1]}"
        check(expected_key in row_keys, f"LEDGER.md: {expected_key} ({path.name}) has no row")

    # -- 1. aggregates --------------------------------------------------------
    aggregate, per_rfc = contract_truth()
    claimed = facts.get("aggregate") or {}
    for key, value in aggregate.items():
        check(claimed.get(key) == value,
              f"aggregate.{key}: LEDGER claims {claimed.get(key)!r}, contracts say {value!r}")
    check(claimed.get("rpc_methods_total") == sum((claimed.get("rpc_methods_by_direction") or {}).values()),
          "aggregate: rpc_methods_total != sum(rpc_methods_by_direction) inside the LEDGER itself")
    check(claimed.get("events_total_incl_turn_state") == claimed.get("events_total_excl_turn_state", -99) + 1,
          "aggregate: events_total_incl_turn_state != events_total_excl_turn_state + 1 inside the LEDGER itself")
    check_section_one_appends(aggregate)

    # -- 2/3/4. per-RFC rows --------------------------------------------------
    rfcs_facts = facts.get("rfcs") or {}
    for cells in rows:
        if not cells:
            continue
        key = cells[0]
        matches = sorted(RFC_DIR.glob(f"{key}-*.md"))
        if not matches:
            FAILURES.append(f"{key}: no RFC file matches {key}-*.md")
            continue
        rfc_path = matches[0]

        # status column (index 2), dropping the 〔…〕 source annotation.
        status_cell = re.sub(r"〔[^〕]*〕", "", cells[2]).strip()
        expected_status(status_cell, rfc_path)

        fact = rfcs_facts.get(key) or {}
        if not fact:
            FAILURES.append(f"{key}: missing from the LEDGER facts block")
            continue

        # impl_state sanity vs contract evidence.
        has_appends = key in per_rfc
        impl_state = fact.get("impl_state")
        if has_appends:
            check(impl_state == "已实现",
                  f"{key}: contracts register appends for it, so impl_state must be 已实现, got {impl_state!r}")
        else:
            check(impl_state in EMPTY_IMPL_STATES,
                  f"{key}: no contract appends exist, impl_state {impl_state!r} is not one of {sorted(EMPTY_IMPL_STATES)}")

        # contract adds, re-derived from the schema files.
        for field in ("methods", "events", "manifest_fields"):
            check(sorted(fact.get(field) or []) == sorted(per_rfc.get(key, {}).get(field, [])),
                  f"{key}.{field}: LEDGER {sorted(fact.get(field) or [])} != contracts {sorted(per_rfc.get(key, {}).get(field, []))}")
        check(fact.get("append_versions") == per_rfc.get(key, {}).get("append_versions", {}),
              f"{key}.append_versions: LEDGER {fact.get('append_versions')} != contracts {per_rfc.get(key, {}).get('append_versions', {})}")

        # implementation paths must exist; empty only when impl_state allows it.
        impl_paths = fact.get("impl") or []
        if not impl_paths:
            check(impl_state in EMPTY_IMPL_STATES,
                  f"{key}: empty impl list but impl_state is {impl_state!r}")
        for rel in impl_paths:
            check((ROOT / rel).exists(), f"{key}: implementation path does not exist: {rel}")

        # acceptance table recount.
        rows_n, checked_n = acceptance_truth(rfc_path)
        check(fact.get("acceptance_rows") == rows_n,
              f"{key}.acceptance_rows: LEDGER claims {fact.get('acceptance_rows')}, RFC file has {rows_n}")
        check(fact.get("acceptance_checked") == checked_n,
              f"{key}.acceptance_checked: LEDGER claims {fact.get('acceptance_checked')}, RFC file has {checked_n}")

    # markdown acceptance column must carry the same numbers as the facts block.
    for cells in rows:
        if not cells:
            continue
        key = cells[0]
        fact = (facts.get("rfcs") or {}).get(key) or {}
        m = re.search(r"(\d+)\s*/\s*(\d+)", cells[-1])
        check(m is not None, f"{key}: acceptance column {cells[-1]!r} lacks a trailing `rows/checked` count")
        if m and fact:
            check(int(m.group(1)) == fact.get("acceptance_rows"),
                  f"{key}: markdown acceptance rows {m.group(1)} != facts block {fact.get('acceptance_rows')}")
            check(int(m.group(2)) == fact.get("acceptance_checked"),
                  f"{key}: markdown acceptance checked {m.group(2)} != facts block {fact.get('acceptance_checked')}")

    if FAILURES:
        print("ledger: FAIL")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"ledger: OK ({len(rows)} RFC rows; rpc {aggregate['rpc_version']} "
          f"{aggregate['rpc_methods_total']} methods in {len(aggregate['rpc_methods_by_direction'])} directions, "
          f"events {aggregate['events_version']} {aggregate['events_total_incl_turn_state']} entries, "
          f"{aggregate['error_codes']} error codes, "
          f"appends {aggregate['appends_rpc']}+{aggregate['appends_events']}+{aggregate['appends_manifest']}; "
          f"impl paths verified, statuses verified, acceptance tables recounted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
