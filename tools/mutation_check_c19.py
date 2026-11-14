#!/usr/bin/env python3
"""Mutation self-check for the C-19 review fixes.

For each finding: apply the *pre-fix* behaviour as a one-line mutation, run the
tests that are supposed to catch it, require them to FAIL, then restore the file
from the backup and require the same tests to PASS again.

Prints one row per mutation: name, applied?, red count, green count.
Exit code 0 only when every mutation turned its tests red and the restore
turned them green.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

AUTH = "core/vade/modulepage/auth.py"
SERVICE = "core/vade/modulepage/service.py"
UIPAGE = "core/vade/registry/uipage.py"
SESSION = "core/vade/session/rpc.py"
SCHEMA = "schema/manifest.schema.json"

#: (label, file, old, new, tests)
MUTATIONS: list[tuple[str, str, str, str, list[str]]] = [
    (
        "B1 remove caller authentication",
        AUTH,
        "    caller = require_caller(connection)\n"
        "    if caller.is_module and (caller.module_id, caller.instance_id) != (module_id, instance_id):",
        "    caller = caller_of(connection)\n"
        "    if False and caller.is_module and (caller.module_id, caller.instance_id) != (module_id, instance_id):",
        [
            "core/tests/uipage/test_review_c19.py::test_b1_module_a_may_not_read_module_b_state",
            "core/tests/uipage/test_review_c19.py::test_b1_module_a_may_not_read_module_b_page",
            "core/tests/uipage/test_review_c19.py::test_b1_module_a_may_not_call_module_b_tool",
            "core/tests/uipage/test_review_c19.py::test_b1_module_a_may_not_route_module_b_action",
            "core/tests/uipage/test_review_c19.py::test_b1_anonymous_connection_is_refused_everywhere",
            "core/tests/uipage/test_review_c19.py::test_b1_identity_comes_from_the_connection_not_the_params",
        ],
    ),
    (
        "B1 browser may read a disabled module",
        SERVICE,
        "        caller = authorize_target(connection, module_id, instance_id)\n"
        "        record = self._record(module_id, instance_id)\n"
        "        require_visible(caller, record)\n"
        "        page = self._page(record, page_id)\n"
        "        if page.is_ref:",
        "        caller = authorize_target(connection, module_id, instance_id)\n"
        "        record = self._record(module_id, instance_id)\n"
        "        page = self._page(record, page_id)\n"
        "        if page.is_ref:",
        [
            "core/tests/uipage/test_review_c19.py::test_b1_browser_session_may_not_read_a_disabled_module_page",
        ],
    ),
    (
        "B2 drop the session binding",
        SERVICE,
        "        if session_id != pending_call.session_id:",
        "        if False and session_id != pending_call.session_id:",
        [
            "core/tests/uipage/test_review_c19.py::test_b2_approval_minted_in_one_session_cannot_be_redeemed_in_another",
            "core/tests/uipage/test_review_c19.py::test_b2_a_refused_redemption_does_not_burn_the_approval",
        ],
    ),
    (
        "B2 drop the approver-device binding",
        SERVICE,
        "        if device_of(connection) != pending_call.approver_device_id:",
        "        if False and device_of(connection) != pending_call.approver_device_id:",
        [
            "core/tests/uipage/test_review_c19.py::test_b2_approval_minted_by_one_device_cannot_be_redeemed_by_another",
        ],
    ),
    (
        "B3 drop the read-only gate on ui/tool.call",
        SERVICE,
        "        if is_read_only(connection):\n"
        "            raise VadeError(\n"
        '                "READ_ONLY_SESSION",\n'
        '                "this session is read-only; a page may not call a tool from it",\n'
        "            )",
        "        if False:\n"
        "            raise VadeError(\n"
        '                "READ_ONLY_SESSION",\n'
        '                "this session is read-only; a page may not call a tool from it",\n'
        "            )",
        [
            "core/tests/uipage/test_review_c19.py::test_b3_read_only_session_may_not_call_a_low_risk_tool",
            "core/tests/uipage/test_review_c19.py::test_b3_read_only_session_may_not_route_a_tool_action_either",
        ],
    ),
    (
        "B3 drop the read-only gate on ui/submit",
        SESSION,
        "        if self._read_only(connection):\n"
        "            # B3: `rpc.json` has declared READ_ONLY_SESSION for `ui/submit` since",
        "        if False:\n"
        "            # B3: `rpc.json` has declared READ_ONLY_SESSION for `ui/submit` since",
        [
            "core/tests/uipage/test_review_c19.py::test_b3_read_only_session_may_not_ui_submit",
        ],
    ),
    (
        "B4 apply the 8 KiB inline ceiling to ref bodies again",
        SERVICE,
        "                inline_max_bytes=MAX_REF_BYTES,",
        "                inline_max_bytes=8 * 1024,",
        [
            "core/tests/uipage/test_review_c19.py::test_b4_ref_body_above_the_inline_ceiling_is_served",
        ],
    ),
    (
        "B5 restore the rev inflation and the blind write",
        SERVICE,
        "            previous = self._state_cache.get(key)\n"
        "            if previous is not None and rev <= previous.rev:",
        "            previous = self._state_cache.get(key)\n"
        "            if previous is not None:\n"
        "                rev = max(rev, previous.rev + 1)\n"
        "            if False:",
        [
            "core/tests/uipage/test_review_c19.py::test_b5_a_backwards_rev_is_never_relabelled_upwards",
            "core/tests/uipage/test_review_c19.py::test_b5_a_slow_stale_answer_never_overwrites_a_newer_one",
        ],
    ),
    (
        "B5 remove the lock AND add an await point (pre-fix interleaving)",
        SERVICE,
        "        async with lock:\n"
        "            previous = self._state_cache.get(key)",
        "        if True:\n"
        "            previous = self._state_cache.get(key)\n"
        "            await asyncio.sleep(0)",
        [
            "core/tests/uipage/test_review_c19.py::test_b5_the_lock_makes_the_compare_and_swap_atomic",
        ],
    ),
    (
        # Complementary control for the row above: the SAME await point, but the
        # lock kept. Must stay GREEN — that is what shows the lock (not some
        # other guard) is what closes the interleaving.
        "B5 add an await point but KEEP the lock (control, expect green)",
        SERVICE,
        "            previous = self._state_cache.get(key)",
        "            previous = self._state_cache.get(key)\n"
        "            await asyncio.sleep(0)",
        [
            "core/tests/uipage/test_review_c19.py::test_b5_the_lock_makes_the_compare_and_swap_atomic",
        ],
        False,  # control: must stay GREEN
    ),
    (
        "B5 overwrite the cache when rev is equal",
        SERVICE,
        "            if previous is not None and rev <= previous.rev:",
        "            if previous is not None and rev < previous.rev:",
        [
            "core/tests/uipage/test_review_c19.py::test_b5_an_equal_rev_does_not_rewrite_the_cache",
        ],
    ),
    (
        "S3 key the ref cache without the digest",
        SERVICE,
        "        key = (record.module_id, record.instance_id, page.page_id, declared)",
        "        key = (record.module_id, record.instance_id, page.page_id, \"\")",
        [
            "core/tests/uipage/test_review_c19.py::test_s3_a_capabilities_update_invalidates_the_ref_cache",
        ],
    ),
    (
        "S4 drop the userinfo check",
        UIPAGE,
        '    if "@" in parts.netloc:',
        '    if False and "@" in parts.netloc:',
        [
            "core/tests/uipage/test_review_c19.py::test_s4_open_url_refuses_a_host_the_user_would_not_see",
        ],
    ),
    (
        "S4 drop the private-host check",
        UIPAGE,
        "    _check_open_url_host(host, pointer=pointer)",
        "    pass  # _check_open_url_host(host, pointer=pointer)",
        [
            "core/tests/uipage/test_review_c19.py::test_s4_open_url_refuses_a_host_the_user_would_not_see",
        ],
    ),
    (
        "S4 drop the encoded-control-character check",
        UIPAGE,
        '    for encoded in ("%0a", "%0d", "%09", "%00"):',
        '    for encoded in ():',
        [
            "core/tests/uipage/test_review_c19.py::test_s4_open_url_refuses_a_host_the_user_would_not_see",
        ],
    ),
    (
        "S5 reopen the node definition",
        SCHEMA,
        '"node": {\n      "type": "object",\n      "required": [\n        "type"\n      ],\n      "additionalProperties": false,',
        '"node": {\n      "type": "object",\n      "required": [\n        "type"\n      ],\n      "additionalProperties": true,',
        [
            "core/tests/uipage/test_review_c19.py::test_s5_the_schema_alone_refuses_a_node_level_onclick",
            "core/tests/uipage/test_review_c19.py::test_s5_the_schema_alone_refuses_an_unknown_node_key",
        ],
    ),
    (
        "S5 reopen the props definition",
        SCHEMA,
        '      "additionalProperties": false,\n      "properties": {\n        "gap": {',
        '      "additionalProperties": true,\n      "properties": {\n        "gap": {',
        [
            "core/tests/uipage/test_review_c19.py::test_s5_the_schema_alone_refuses_an_unknown_prop",
        ],
    ),
]


def run(tests: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [PY, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *tests],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    rows: list[tuple[str, bool, str]] = []
    ok = True
    for entry in MUTATIONS:
        label, relative, old, new, tests = entry[:5]
        expect_red = entry[5] if len(entry) > 5 else True
        path = ROOT / relative
        backup = path.with_suffix(path.suffix + ".mutbak")
        shutil.copyfile(path, backup)
        try:
            text = path.read_text(encoding="utf-8")
            if old not in text:
                rows.append((label, False, "MUTATION NOT APPLICABLE (anchor not found)"))
                ok = False
                continue
            path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\r\n")
            mutated_code, mutated_out = run(tests)
            path.write_text(text, encoding="utf-8", newline="\r\n")
            green_code, green_out = run(tests)
            went_red = mutated_code != 0
            restored = green_code == 0
            verdict = "ok" if (went_red == expect_red and restored) else "MISS"
            ok = ok and verdict == "ok"
            rows.append((
                label, True,
                f"expected={'red' if expect_red else 'green'} "
                f"got={'red' if went_red else 'green'} "
                f"restore={'green' if restored else 'RED'}  [{verdict}]",
            ))
            if verdict != "ok":
                print(f"--- {label} ---")
                print((mutated_out if went_red != expect_red else green_out)[-1200:])
        finally:
            path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8", newline="\r\n")
            backup.unlink()

    width = max(len(label) for label, _a, _s in rows)
    print()
    for label, applied, status in rows:
        print(f"{label.ljust(width)}  applied={applied!s:5}  {status}")
    print()
    print("ALL MUTATIONS LOAD-BEARING" if ok else "SOME MUTATIONS DID NOT TURN RED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
