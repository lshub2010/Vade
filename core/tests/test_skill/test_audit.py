"""Audit surface: secret scanning is warn-only, logs never carry a body."""
from __future__ import annotations

import json

from vade.skill.audit import body_ref, scan_secrets, trim_event


def test_openai_like_key_is_found() -> None:
    findings = scan_secrets("token: sk-" + "a" * 24)
    assert [(f.kind, f.count) for f in findings] == [("openai_key", 1)]


def test_private_key_header_is_found() -> None:
    # 运行时拼装：仓库不留 PEM 私钥头字面量，运行时值不变
    findings = scan_secrets("-----BEGIN " + "RSA " + "PRIVATE KEY-----\nMIIE...")
    assert [f.kind for f in findings] == ["private_key"]


def test_long_base64_blob_is_found_but_short_base64_is_not() -> None:
    assert [f.kind for f in scan_secrets("A" * 240)] == ["base64_blob"]
    assert scan_secrets("A" * 80) == ()


def test_clean_body_has_no_findings() -> None:
    assert scan_secrets("## Procedure\nRead the file, then answer.") == ()


def test_body_ref_contains_no_body_text() -> None:
    secret = "super secret instructions"
    ref = body_ref("/data/skills/x/SKILL.md", secret)
    assert ref == {"path": "/data/skills/x/SKILL.md", "body_sha8": body_ref("/x", secret)["body_sha8"]}
    assert secret not in json.dumps(ref)


def test_trim_event_is_names_only_and_sorted() -> None:
    event = trim_event(["b", "a"], ["c"])
    assert event == {"event": "skill_render_trimmed", "l0_dropped": ["a", "b"], "l1_dropped": ["c"]}
