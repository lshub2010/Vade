"""Credentials for `/ui/blob` (RFC-0004 §4.5, §8.1-§8.2).

Two principals, one rule each: a device bearer is *the* write credential, and
the browser cookie is only usable when all four §8.2 gates agree that the
request came from the real page. Losing any gate is 403, an unknown principal
is 401, and a revocation must be indistinguishable from a token that never was.

The whole point of the gate set is that the cookie is ambient: without the
Origin allowlist, `Sec-Fetch-Site`, and the double-submit CSRF pair, any page
could drive uploads with the user's session.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from .conftest import cookie_headers
from .images import png


def _payload() -> dict[str, object]:
    data = png()
    return {
        "kind": "image",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mime": "image/png",
        "name": "a.png",
        "width": 8,
        "height": 8,
    }


UPLOAD = "upl_" + "0" * 26
BLOB = "blb_" + "0" * 26


def test_without_a_credential_every_route_is_401(env):
    """Six routes, one answer: nothing about the caller is revealed."""
    body = json.dumps(_payload()).encode()
    requests = [
        ("POST", "/ui/blob", {"content-type": "application/json"}, body),
        ("PUT", f"/ui/blob/upload/{UPLOAD}?offset=0", {"content-type": "application/octet-stream"}, b"x"),
        ("HEAD", f"/ui/blob/upload/{UPLOAD}", {}, b""),
        ("POST", f"/ui/blob/upload/{UPLOAD}/complete", {"content-type": "application/json"}, b"{}"),
        ("DELETE", f"/ui/blob/upload/{UPLOAD}", {}, b""),
        ("GET", f"/ui/blob/{BLOB}", {}, b""),
    ]
    for method, path, headers, payload in requests:
        response = env.client.request(method, path, headers=headers, body=payload)
        assert response.status == 401, (method, path, response.json)
        assert response.json["code"] == "AUTH_FAILED"
        # No `data`: a field name would tell an attacker which part to fix.
        assert "data" not in response.json, (method, path, response.json)


def test_a_wrong_token_is_401_and_looks_exactly_like_no_token(env):
    body = json.dumps(_payload()).encode()
    wrong = env.client.post(
        "/ui/blob",
        headers={"authorization": "Bearer " + "A" * 43, "content-type": "application/json"},
        body=body,
    )
    missing = env.client.post("/ui/blob", headers={"content-type": "application/json"}, body=body)
    assert wrong.status == missing.status == 401
    assert wrong.json["code"] == missing.json["code"] == "AUTH_FAILED"
    assert wrong.json["message"] == missing.json["message"]

    # Revocation is the same answer: "was it ever valid" must not leak either.
    env.service.revoke_device("dev-1")
    revoked = env.client.post("/ui/blob", headers=env.json_headers(), body=body)
    assert revoked.status == 401
    assert revoked.json["message"] == wrong.json["message"]
    assert revoked.json.get("data") is None


@pytest.mark.parametrize(
    "gates, reason",
    [
        ({}, None),
        ({"origin": None}, "origin"),
        ({"fetch_site": "cross-site"}, "fetch_site"),
        ({"csrf": "a-different-value"}, "csrf"),
        ({"csrf": "a-different-value", "csrf_cookie": "a-different-value"}, "csrf_binding"),
    ],
)
def test_all_four_cookie_gates_together_and_each_one_alone(env, gates, reason):
    """The positive case is the control: an unconditional 403 would fail here."""
    body = json.dumps(_payload()).encode()
    response = env.client.post("/ui/blob", headers=cookie_headers(env, **gates), body=body)
    if reason is None:
        assert response.status == 201, response.json
        return
    assert response.status == 403, (gates, response.json)
    assert response.json["code"] == "ORIGIN_MISMATCH"
    assert response.json["data"]["reason"] == reason


@pytest.mark.parametrize(
    "content_type",
    [
        "multipart/form-data; boundary=x",
        "application/x-www-form-urlencoded",
        "text/plain",
    ],
)
def test_cors_simple_request_types_are_415_before_auth(env, content_type):
    """§8.2: these skip the preflight, so they must not reach the credential."""
    response = env.client.post("/ui/blob", headers={"content-type": content_type}, body=b"--x--")
    assert response.status == 415, response.json
    assert response.json["code"] == "UNSUPPORTED"
    assert response.json["data"]["field"] == "content-type"
    assert response.json["data"]["reason"] == "simple_request_forbidden"
