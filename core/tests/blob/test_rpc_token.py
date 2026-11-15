"""RFC-0016 §10 acceptance: `ui/blob.token` minting, binding, TTL, revocation.

Every test below cites the A-number it carries. The harness reuses the blob
conftest (`Env`: a real ASGI `BlobApp` + `Client`) and wires `BlobTokenRPC` on
top of it exactly the way `install()` does, so what is under test is the same
object graph a live core builds. A12 (real device) and the ws-level protocol
repro live in `core/tests/e2e/test_blob_token_ws.py`; the client-side degrade
table (§7.2) is the vade-client's job and is only asserted here at the
dispatch-shape level (A11).

Security posture under test (拍板 2026-11-15, §11.1):
* Q1 — native connections only: a cookie session is PERMISSION_DENIED.
* Q2 — TTL default 600, window 60..900, values from `TOKEN_TTL_*`.
* Q3 — replayable inside the TTL (not one-shot).
* Q7 — a device bearer reads only its own device's blobs, independent of
  `read_scope` (the known cost: cross-device history images do not render
  through a token; cookie reads are unchanged).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import types
from pathlib import Path
from typing import Any

import pytest

from vade.app import make_rpc_handler
from vade.blob import install as blob_install
from vade.blob.auth import Authenticator, hash_token
from vade.blob.rpc import BLOB_TOKEN_ENDPOINTS, BlobTokenRPC
from vade.blob.service import TOKEN_TTL_DEFAULT, TOKEN_TTL_MAX, TOKEN_TTL_MIN, BlobService
from vade.blob.settings import AttachSettings
from vade.errors import VadeError
from vade.session.parts import resolve_attachments
from vade.transport.connection import RpcError

from .conftest import cookie_headers

SCHEMA_RPC = Path(__file__).resolve().parents[3] / "schema" / "rpc.json"
TOKEN_ALPHABET = re.compile(r"^[A-Za-z0-9_-]{43}$")


# --------------------------------------------------------------------- helpers
def native(device: str = "dev-1", *, read_only: bool = False) -> Any:
    """A Noise-paired connection the way the transport hands one over."""
    return types.SimpleNamespace(
        device_id=device, browser_session=False, read_only=read_only, principal=None
    )


def cookie_conn(device: str = "ui-abc123") -> Any:
    return types.SimpleNamespace(
        device_id=device, browser_session=True, read_only=False, principal=None
    )


def anonymous() -> Any:
    """A Noise connection whose key is not on the whitelist: fail-closed None."""
    return types.SimpleNamespace(
        device_id=None, browser_session=False, read_only=False, principal=None
    )


def rpc_for(env: Any) -> BlobTokenRPC:
    return BlobTokenRPC(types.SimpleNamespace(blob=env.app))


def token_rows(service: BlobService) -> list[str]:
    return [r["token_hash"] for r in service.store._db.execute(
        "SELECT token_hash FROM blob_tokens ORDER BY token_hash").fetchall()]


def mint(env: Any, connection: Any = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return asyncio.run(rpc_for(env).mint(connection or native(env.device), params))


# --------------------------------------------------------------------- A1/A2
def test_a1_contract_shape_matches_the_schema_and_the_code(env) -> None:
    data = json.loads(SCHEMA_RPC.read_text(encoding="utf-8"))
    entry = data["directions"]["ui_to_core"]["ui/blob.token"]
    assert set(entry["params"]) == {"ttl_s"}
    assert set(entry["result"]) == {"token", "expires_at", "max_bytes", "endpoints"}
    assert set(entry["result"]["endpoints"]) == set(BLOB_TOKEN_ENDPOINTS)
    # The endpoints table is an alias of http_endpoints, not a second contract:
    # method+path must match the six declared rows verbatim (§4.4).
    declared = {
        f'{row["method"]} {row["path"]}' for row in data["http_endpoints"]["endpoints"]
    }
    assert set(entry["result"]["endpoints"].values()) == declared
    assert set(BLOB_TOKEN_ENDPOINTS.values()) == declared
    assert set(entry["errors"]) == {
        "INVALID_ARGUMENT", "PERMISSION_DENIED", "READ_ONLY_SESSION", "UNSUPPORTED"
    }
    errors = json.loads((SCHEMA_RPC.parent / "errors.json").read_text(encoding="utf-8"))
    assert set(entry["errors"]) <= set(errors["codes"]), "no new C-04 code"
    assert "notes" in entry and "RFC-0016" in entry["notes"]


def test_a1_handler_is_registered_through_both_install_paths(tmp_path, clock) -> None:
    # Provider path (the one a real core drains in session.install): the app
    # has no `session_handlers` yet when `blob` installs, so the provider list
    # is what carries the method into the dispatcher.
    app = types.SimpleNamespace(config={"core.data_dir": str(tmp_path), "server.ui_origins": []})
    blob_install(app)
    assert app.blob is not None
    assert len(app.handler_providers) == 1
    assert "ui/blob.token" in app.handler_providers[0]()
    # Idempotent: a second install neither duplicates providers nor rebuilds
    # the data plane.
    first_blob = app.blob
    blob_install(app)
    assert app.blob is first_blob
    assert len(app.handler_providers) == 1
    # Setdefault path: when `session_handlers` already exists (tests, bare
    # apps), the name is published straight into it.
    app2 = types.SimpleNamespace(
        config={"core.data_dir": str(tmp_path / "two"), "server.ui_origins": []},
        session_handlers={},
        handler_providers=[],
    )
    blob_install(app2)
    assert "ui/blob.token" in app2.session_handlers
    assert "ui/blob.token" in app2.handler_providers[0]()


def test_a2_mint_then_full_upload_lifecycle_over_the_real_asgi_app(env) -> None:
    result = mint(env, native(env.device), {})
    token = result["token"]
    assert TOKEN_ALPHABET.match(token)
    headers = {"authorization": f"Bearer {token}"}

    # --- begin + PUT (>= 2 parts) + complete on a 2560-byte body ------------
    body = b"vade-blob-token-e2e-body-" * 100  # ASCII: the validator sniffs mime
    digest = hashlib.sha256(body).hexdigest()
    begun = env.client.post("/ui/blob", headers={
        **headers, "content-type": "application/json",
    }, body=json.dumps({
        "kind": "file", "sha256": digest, "size": len(body),
        "mime": "text/plain", "name": "x.txt",
    }).encode())
    assert begun.status in (200, 201), begun.body
    upload_id, part_size = begun.json["upload_id"], int(begun.json["part_size"])
    parts = 0
    for offset in range(0, len(body), part_size):
        chunk = body[offset:offset + part_size]
        put = env.client.put(
            f"/ui/blob/upload/{upload_id}?offset={offset}",
            headers={**headers, "content-type": "application/octet-stream"}, body=chunk,
        )
        assert put.status == 200, put.body
        parts += 1
    assert parts >= 2
    done = env.client.post(
        f"/ui/blob/upload/{upload_id}/complete",
        headers={**headers, "content-type": "application/json"},
        body=json.dumps({"sha256": digest, "size": len(body)}).encode(),
    )
    assert done.status in (200, 201), done.body
    file_id = done.json["file_id"]
    blob = env.service.store.get_blob(file_id)
    assert blob is not None and blob.device_id == env.device  # A3 binding

    # --- replayability (拍板 Q3): the same token keeps working --------------
    got = env.client.get(f"/ui/blob/{file_id}", headers=headers)
    assert got.status == 200 and got.body == body
    ranged = env.client.get(
        f"/ui/blob/{file_id}", headers={**headers, "range": "bytes=0-99"}
    )
    assert ranged.status == 206 and ranged.body == body[:100]

    # --- HEAD resume anchor + DELETE abort on a second upload ---------------
    # (different bytes: an identical (device, sha256, size) would dedupe to
    # the committed blob and never open an upload session — §4.2 idempotency)
    body2 = b"second-upload-payload-" * 117
    digest2 = hashlib.sha256(body2).hexdigest()
    begun2 = env.client.post("/ui/blob", headers={
        **headers, "content-type": "application/json",
    }, body=json.dumps({
        "kind": "file", "sha256": digest2, "size": len(body2),
        "mime": "text/plain", "name": "y.txt",
    }).encode())
    assert begun2.status in (200, 201), begun2.body
    upload2 = begun2.json["upload_id"]
    put = env.client.put(
        f"/ui/blob/upload/{upload2}?offset=0",
        headers={**headers, "content-type": "application/octet-stream"}, body=body2[:part_size],
    )
    assert put.status == 200
    head = env.client.head(f"/ui/blob/upload/{upload2}", headers=headers)
    assert head.status == 200 and head.json["size"] == len(body2)
    abort = env.client.delete(f"/ui/blob/upload/{upload2}", headers=headers)
    assert abort.status == 204
    assert env.client.head(f"/ui/blob/upload/{upload2}", headers=headers).status == 404


def test_a3_token_binds_the_calling_connection_and_the_submit_gate_agrees(env) -> None:
    result = mint(env, native(env.device), {})
    token = result["token"]
    done = env.upload(b"binding-check", kind="file", mime="text/plain", token=token)
    assert done.status in (200, 201), done.body
    file_id = done.json["file_id"]
    blob = env.service.store.get_blob(file_id)
    assert blob.device_id == env.device

    part = [{"type": "image", "file_id": file_id}]
    mine = resolve_attachments(env.service, part, device_id=env.device)
    assert [r.file_id for r in mine] == [file_id]
    with pytest.raises(VadeError) as excinfo:
        resolve_attachments(env.service, part, device_id="dev-2")
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.message == "file belongs to another device"


# ----------------------------------------------------------------------- A4/A5
def test_a4_ttl_absent_means_the_default_600(env) -> None:
    before = token_rows(env.service)
    result = mint(env, native(env.device), None)
    assert result["expires_at"] - int(env.clock() * 1000) == TOKEN_TTL_DEFAULT * 1000
    assert len(token_rows(env.service)) == len(before) + 1


@pytest.mark.parametrize("value", [60, 900])
def test_a4_ttl_boundaries_are_accepted(env, value: int) -> None:
    before = token_rows(env.service)
    result = mint(env, native(env.device), {"ttl_s": value})
    assert result["expires_at"] - int(env.clock() * 1000) == value * 1000
    assert len(token_rows(env.service)) == len(before) + 1


@pytest.mark.parametrize("value", [59, 901, 1.5, True, "600", None])
def test_a4_out_of_range_or_non_integer_ttl_is_refused_without_a_row(env, value) -> None:
    before = token_rows(env.service)
    with pytest.raises(VadeError) as excinfo:
        mint(env, native(env.device), {"ttl_s": value})
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert excinfo.value.data == {"field": "ttl_s"}
    assert token_rows(env.service) == before, "a refused mint writes NO row"


def test_a4_rejects_instead_of_clamping_and_the_constants_are_the_ones_pinned() -> None:
    assert (TOKEN_TTL_DEFAULT, TOKEN_TTL_MIN, TOKEN_TTL_MAX) == (600, 60, 900)


def test_a5_token_shape_and_expiry_window(env) -> None:
    ttl = 123
    result = mint(env, native(env.device), {"ttl_s": ttl})
    assert TOKEN_ALPHABET.match(result["token"])
    now_ms = int(env.clock() * 1000)
    assert now_ms + (ttl - 2) * 1000 <= result["expires_at"] <= now_ms + (ttl + 2) * 1000
    assert isinstance(result["expires_at"], int)  # unix MILLISECONDS
    assert isinstance(result["max_bytes"], int) and result["max_bytes"] > 0
    assert set(result["endpoints"]) == {"begin", "part", "complete", "abort", "resume", "fetch"}


# ----------------------------------------------------------------------- A6/A7
def _auth_failed_response(env: Any, bearer: str) -> Any:
    return env.client.get(
        f"/ui/blob/{'blb_' + '0' * 24}", headers={"authorization": f"Bearer {bearer}"}
    )


def test_a6_expired_revoked_and_never_issued_are_indistinguishable(env) -> None:
    minted = mint(env, native(env.device), {"ttl_s": 60})["token"]
    never = "a" * 43  # same alphabet, never minted
    # Expired: same token, after its TTL.
    env.clock.advance(61)
    expired_bytes = _auth_failed_response(env, minted).body
    expired_status = _auth_failed_response(env, minted).status
    # Revoked: mint another, then drop the whole device set.
    revoked = mint(env, native(env.device), {"ttl_s": 60})["token"]
    assert env.service.revoke_device(env.device) >= 1
    revoked_bytes = _auth_failed_response(env, revoked).body
    # Never issued.
    never_bytes = _auth_failed_response(env, never).body
    never_status = _auth_failed_response(env, never).status
    assert expired_status == never_status == 401
    assert expired_bytes == revoked_bytes == never_bytes, "no oracle (auth.py:161-165)"


def test_a7_connection_without_identity_is_refused_and_writes_no_row(env) -> None:
    before = token_rows(env.service)
    with pytest.raises(VadeError) as excinfo:
        mint(env, anonymous(), {})
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.data == {"reason": "device_unknown"}
    assert token_rows(env.service) == before


# -------------------------------------------------------------------------- A8
@pytest.mark.parametrize("key,value", [
    ("device_id", "module#victim"),
    ("read_only", True),
    ("session_id", "s1"),
])
def test_a8_unknown_parameter_keys_are_refused_by_name(env, key: str, value: Any) -> None:
    before = token_rows(env.service)
    with pytest.raises(VadeError) as excinfo:
        mint(env, native(env.device), {"ttl_s": 600, key: value})
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert excinfo.value.data == {"field": key}
    assert token_rows(env.service) == before, "no row, and none for the named device"


def test_a8_non_object_params_are_refused(env) -> None:
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(rpc_for(env).mint(native(env.device), ["ttl_s"]))  # type: ignore[arg-type]
    assert excinfo.value.code == "INVALID_ARGUMENT"


# ------------------------------------------------------------------- 拍板 Q1/Q7
def test_q1_cookie_session_is_refused(env) -> None:
    before = token_rows(env.service)
    with pytest.raises(VadeError) as excinfo:
        mint(env, cookie_conn(), {})
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.data == {"reason": "cookie_session"}
    assert token_rows(env.service) == before


def test_q1_explicitly_read_only_connection_is_refused(env) -> None:
    with pytest.raises(VadeError) as excinfo:
        mint(env, native(env.device, read_only=True), {})
    assert excinfo.value.code == "READ_ONLY_SESSION"
    assert not TOKEN_ALPHABET.match(str(excinfo.value))


def test_q1_disabled_or_missing_component_is_unsupported(tmp_path, clock) -> None:
    # attach.enabled=false: install() must still publish the handler.
    app = types.SimpleNamespace(config={
        "core.data_dir": str(tmp_path / "off"), "server.ui_origins": [],
        "attach.enabled": False,
    })
    blob_install(app)
    assert app.blob is None
    assert "ui/blob.token" in BlobTokenRPC(app).handlers()
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(BlobTokenRPC(app).mint(native("dev-1"), {}))
    assert excinfo.value.code == "UNSUPPORTED"
    assert excinfo.value.data == {"reason": "attachments_disabled"}
    # Component missing entirely (bare app) — same answer, not INTERNAL.
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(BlobTokenRPC(types.SimpleNamespace()).mint(native("dev-1"), {}))
    assert excinfo.value.code == "UNSUPPORTED"


def test_q7_token_reads_are_device_scoped_regardless_of_read_scope(env) -> None:
    """拍板 Q7: a bearer may only read its own device's blob — even though the
    default `read_scope` is "core" (the operator switch must not reopen this)."""
    assert env.service.settings.read_scope == "core"
    done = env.upload(b"owned-by-dev-1", kind="file", mime="text/plain")
    assert done.status in (200, 201), done.body
    file_id = done.json["file_id"]

    # dev-1's own token: allowed.
    ok = env.client.get(f"/ui/blob/{file_id}", headers=env.headers())
    assert ok.status == 200
    # dev-2's token: 403, fail-closed.
    foreign = env.client.get(
        f"/ui/blob/{file_id}", headers={"authorization": f"Bearer {env.other_token('dev-2')}"}
    )
    assert foreign.status == 403
    assert foreign.json["code"] == "PERMISSION_DENIED"
    # The service-level gate agrees (stat is the shared read path).
    principal = types.SimpleNamespace(device_id="dev-2", kind="device")
    with pytest.raises(VadeError) as excinfo:
        env.service.stat(principal, file_id)
    assert excinfo.value.code == "PERMISSION_DENIED"


def test_q7_cookie_reads_keep_the_history_semantics(env) -> None:
    """Cookie principals are NOT tightened (§11.1 names the cost for tokens
    only; the browser renders history through its cookie session). Under the
    default read_scope="core" a cookie session may read another device's
    uploaded blob — exactly the semantics the 拍板 left untouched."""
    done = env.upload(b"cookie-visible", kind="file", mime="text/plain")
    assert done.status in (200, 201), done.body
    file_id = done.json["file_id"]
    blob = env.service.store.get_blob(file_id)
    assert blob is not None and blob.device_id != "ui-other"
    got = env.client.get(f"/ui/blob/{file_id}", headers=cookie_headers(env, device="ui-other"))
    assert got.status == 200, got.body


def test_q3_token_is_replayable_within_the_ttl(env) -> None:
    """拍板 Q3: shard retries reuse the same token — `get_token` does not
    consume the row (store.py:479-492)."""
    token = mint(env, native(env.device), {"ttl_s": 60})["token"]
    for _ in range(3):
        record = env.service.get_token(hash_token(token), env.clock())
        assert record is not None and record.device_id == env.device
    # ...and the HTTP gate accepts it repeatedly.
    for _ in range(2):
        begun = env.client.post("/ui/blob", headers={
            "authorization": f"Bearer {token}", "content-type": "application/json",
        }, body=json.dumps({
            "kind": "file", "sha256": "0" * 64, "size": 4,
            "mime": "text/plain", "name": "r.bin",
        }).encode())
        assert begun.status in (200, 201), begun.body


# -------------------------------------------------------------------------- A9
def test_a9_token_survives_a_core_restart(env, tmp_path) -> None:
    """The row lives in sqlite (only the hash), so a fresh service on the same
    data dir accepts the same bearer (§5.6-15)."""
    token = mint(env, native(env.device), {"ttl_s": 60})["token"]
    restarted = BlobService(env.service.data_dir, env.service.settings, wall=env.clock)
    try:
        principal = Authenticator(
            tokens=restarted, sessions={}, origins=(), wall=env.clock
        ).authenticate({"authorization": f"Bearer {token}"})
        assert principal.device_id == env.device and principal.kind == "device"
    finally:
        restarted.close()


def test_a9_revoke_device_kills_the_token_immediately(env) -> None:
    token = mint(env, native(env.device), {"ttl_s": 900})["token"]
    assert env.client.get(
        f"/ui/blob/{'blb_' + '0' * 24}", headers={"authorization": f"Bearer {token}"}
    ).status == 404  # authenticated; the file just does not exist
    env.service.revoke_device(env.device)
    dead = env.client.get(
        f"/ui/blob/{'blb_' + '0' * 24}", headers={"authorization": f"Bearer {token}"}
    )
    assert dead.status == 401 and dead.json["code"] == "AUTH_FAILED"


# ------------------------------------------------------------------------- A10
def test_a10_audit_records_the_mint_and_never_the_token(tmp_path, clock) -> None:
    records: list[dict[str, Any]] = []
    settings = AttachSettings(part_size=1024)
    service = BlobService(  # type: ignore[arg-type]
        tmp_path / "audit", settings, wall=clock, audit=records.append
    )
    try:
        rpc = BlobTokenRPC(types.SimpleNamespace(blob=types.SimpleNamespace(service=service)))
        result = asyncio.run(rpc.mint(native("dev-9"), {}))
        [record] = [r for r in records if r.get("kind") == "blob.token"]
        assert record["status"] == "ok"
        assert record["extra"]["device_id"] == "dev-9"
        assert record["extra"]["expires_at"] == result["expires_at"]
        # The literal token (and any hash prefix) never reaches the audit.
        dump = json.dumps(records)
        assert result["token"] not in dump
        assert hash_token(result["token"]) not in dump
        assert hash_token(result["token"])[:16] not in dump
        # A refused mint writes no success row.
        count = len([r for r in records if r.get("kind") == "blob.token"])
        with pytest.raises(VadeError):
            asyncio.run(rpc.mint(native("dev-9"), {"ttl_s": 5}))
        assert len([r for r in records if r.get("kind") == "blob.token"]) == count
    finally:
        service.close()


# ------------------------------------------------------------------------- A11
def test_a11_unregistered_method_dispatches_as_not_found_unknown_method() -> None:
    """Old-core simulation: without the handler published, the dispatcher
    answers the STRING code NOT_FOUND + "unknown method …" — never -32601
    (RFC-0016 §7.1). The client predicate must be the disjunction."""
    bare = types.SimpleNamespace(session_handlers={})
    handler = make_rpc_handler(bare)
    reply = asyncio.run(handler(types.SimpleNamespace(), {
        "jsonrpc": "2.0", "id": 1, "method": "ui/blob.token", "params": {},
    }))
    assert isinstance(reply, RpcError)
    assert reply.code == "NOT_FOUND"
    assert reply.message.startswith("unknown method")
    assert "ui/blob.token" in reply.message
    # The documented client predicate (§7.1): both shapes mean CoreTooOld.
    def method_absent(err: Any) -> bool:
        return err.code == -32601 or (
            err.code == "NOT_FOUND" and err.message.startswith("unknown method")
        )

    assert method_absent(reply)
    assert method_absent(types.SimpleNamespace(code=-32601, message="Method not found"))
    assert not method_absent(types.SimpleNamespace(code="UNSUPPORTED", message="x"))


def test_a11_published_handler_answers_over_the_dispatcher(env) -> None:
    app = types.SimpleNamespace(blob=env.app, session_handlers={})
    blob_rpc = BlobTokenRPC(app)
    for name, fn in blob_rpc.handlers().items():
        app.session_handlers.setdefault(name, fn)
    dispatcher = make_rpc_handler(app)
    reply = asyncio.run(dispatcher(native(env.device), {
        "jsonrpc": "2.0", "id": 7, "method": "ui/blob.token", "params": {},
    }))
    assert not isinstance(reply, RpcError)
    assert TOKEN_ALPHABET.match(reply["token"])


def test_a11_a_refused_mint_is_an_error_envelope_not_a_closed_socket(env) -> None:
    """The e2e ws test found this: a VadeError raised by a provider-published
    handler used to escape `make_rpc_handler` and close the connection
    (`connection.py:170`), so a refused mint looked like a dropped socket."""
    app = types.SimpleNamespace(blob=None, session_handlers={})
    for name, fn in BlobTokenRPC(app).handlers().items():
        app.session_handlers.setdefault(name, fn)
    dispatcher = make_rpc_handler(app)
    reply = asyncio.run(dispatcher(native(env.device), {
        "jsonrpc": "2.0", "id": 9, "method": "ui/blob.token", "params": {},
    }))
    assert isinstance(reply, RpcError)
    assert reply.code == "UNSUPPORTED"
    assert reply.data == {"reason": "attachments_disabled"}
