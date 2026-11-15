"""Redaction and digests: what may be stored, and what must never be."""

from __future__ import annotations

from vade.audit.redact import digest_args, redact_args, summarize_args

SECRETS = {
    "password": "hunter2",
    "api_token": "acs_abcdefghijklmnopqrstuvwxyz",
    "client_secret": "s3cr3t",
    # 运行时拼装：仓库不留 PEM 私钥头字面量，运行时值不变
    "private_key": "-----BEGIN " + "PRIVATE KEY-----",
    "credential": {"user": "admin", "pass": "x"},
    "psk": "00112233",
}


def test_credential_fields_are_replaced_not_truncated():
    redacted = redact_args({**SECRETS, "path": "/tmp/a", "content": "hello"})
    for key in ("password", "api_token", "client_secret", "private_key", "credential", "psk"):
        assert redacted[key] == "[redacted]", key
    assert redacted["path"] == "/tmp/a"
    assert "hunter2" not in str(redacted)


def test_nested_and_long_values_are_bounded():
    payload = {"options": {"nested": {"password": "x"}}, "blob": "a" * 1000,
               "items": list(range(100))}
    redacted = redact_args(payload)
    assert redacted["options"]["nested"]["password"] == "[redacted]"
    assert len(redacted["blob"]) < 260
    assert len(redacted["items"]) == 33  # 32 items + the "more" marker


def test_digest_is_stable_and_argument_sensitive():
    a = digest_args({"path": "/tmp/a", "content": "x"})
    b = digest_args({"content": "x", "path": "/tmp/a"})   # key order must not matter
    c = digest_args({"path": "/tmp/b", "content": "x"})
    assert a == b
    assert a != c
    assert a != digest_args({"path": "/tmp/a"})            # dropping an arg changes it


def test_summary_stays_small_for_the_list_page():
    summary = summarize_args({"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7, "h": 8, "i": 9})
    assert len(summary) == 8
    assert "i" not in summary
