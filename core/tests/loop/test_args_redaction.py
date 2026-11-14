"""RFC-0006 §5.2 (A6): three risk levels, one rule — no secret in the preview.

The re-scan assertions are the contract: whatever `build_args_preview` produced
must not match any of the scrub patterns again, or the preview has become a
second leak path next to `turn.approval_required`.
"""

from __future__ import annotations

import json

import pytest

from vade.loop.toolcard import (
    MAX_REDACTED_PATHS, PREVIEW_MAX_BYTES, SECRET_PATTERNS, build_args_preview,
)


def _rescan(preview: str) -> list[str]:
    return [p.pattern for p in SECRET_PATTERNS if p.search(preview)]


def test_low_and_medium_use_the_redacted_mode():
    for risk in ("low", "medium"):
        preview = build_args_preview({"path": "a.txt", "password": "hunter2"}, risk)
        assert preview["args_preview_mode"] == "redacted"
        assert preview["args_preview_redacted"] == ["password"]
        body = json.loads(preview["args_preview"])
        assert body["path"] == "a.txt"
        assert body["***"] == "***"
        assert _rescan(preview["args_preview"]) == []


def test_high_uses_the_typed_mode_and_keeps_the_shape():
    preview = build_args_preview(
        {"path": "a/b.txt", "count": 2, "ok": False, "nothing": None, "nested": {"x": "y"}},
        "high",
    )
    assert preview["args_preview_mode"] == "typed"
    body = json.loads(preview["args_preview"])
    assert body["path"] == "<str:7>"
    assert body["count"] == 2
    assert body["ok"] is False
    assert body["nothing"] is None
    assert body["nested"]["x"] == "<str:1>"


def test_a_secret_key_name_is_masked_and_its_path_recorded():
    preview = build_args_preview({"Authorization": "Bearer abc", "x": 1}, "low")
    assert preview["args_preview_redacted"] == ["Authorization"]
    assert "Bearer" not in preview["args_preview"]


def test_whole_word_matching_keeps_author():
    """`author` must not match `auth`: the key is split on `_`/`-`/`.` first."""
    preview = build_args_preview({"author": "Ada", "authentic": "yes"}, "low")
    assert preview["args_preview_redacted"] == []
    assert json.loads(preview["args_preview"]) == {"author": "Ada", "authentic": "yes"}


def test_values_are_scrubbed_even_when_the_key_is_innocent():
    preview = build_args_preview(
        {"cmd": "curl -H 'Authorization: Bearer sk-ABCDEFGHIJKLMNOP'",
         "env": "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP"},
        "low",
    )
    assert preview["args_preview_redacted"] == ["cmd", "env"]
    assert _rescan(preview["args_preview"]) == []
    assert "sk-ABC" not in preview["args_preview"]


def test_a_large_payload_stays_under_512_bytes_and_is_flagged():
    # Long key names survive shape compression (only their count is capped), so
    # this is what actually reaches the 512-byte ceiling and trips step 5.
    args = {("k" * 120) + str(i): "v" for i in range(10)}
    preview = build_args_preview(args, "low")
    assert len(preview["args_preview"].encode("utf-8")) <= PREVIEW_MAX_BYTES
    assert preview["args_preview_truncated"] is True
    json.loads(preview["args_preview"])  # still parseable for the UI


def test_the_redacted_path_list_is_capped():
    args = {f"token_{i}": "x" for i in range(40)}
    preview = build_args_preview(args, "low")
    assert len(preview["args_preview_redacted"]) <= MAX_REDACTED_PATHS


def test_shape_compression_marks_what_it_dropped():
    preview = build_args_preview(
        {"text": "long sentence with spaces " * 10, "items": list(range(12))}, "low"
    )
    body = json.loads(preview["args_preview"])
    assert body["text"].endswith("…")
    assert body["items"][-1].startswith("…(+")


def test_non_dict_args_are_wrapped_not_dropped():
    preview = build_args_preview(["a", "b"], "low")
    assert json.loads(preview["args_preview"]) == {"value": ["a", "b"]}
    preview = build_args_preview("plain", "low")
    assert json.loads(preview["args_preview"]) == {"value": "plain"}
