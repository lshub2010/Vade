"""RFC-0016 §10 A9 (unbind leg) + §5.6-17: revoking a whitelist device kills
its blob tokens in the same stroke.

The wiring under test is the production chain, not a re-implementation:
`Whitelist.revoke(fingerprint)` deletes the entries, then invokes the
`on_revoke` callback with `(fingerprint, removed_ids)`; the App hook closes the
live connections (registry) and purges `blob_tokens` for every removed
`module_id#instance_id`. Before RFC-0016 the tokens of an unbound device lived
on until their TTL ceiling (900 s) — a revoked pairing could keep uploading.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from vade.app import App
from vade.blob.http import BlobApp
from vade.blob.service import BlobService
from vade.blob.settings import AttachSettings
from vade.identity.whitelist import Whitelist

MODULE_ID = "com.example.fs"
INSTANCE_ID = "fs-01"
DEVICE = f"{MODULE_ID}#{INSTANCE_ID}"
FP = "ab" * 16  # the whitelist stores fingerprints as opaque hex strings


def _wired_app(tmp_path: Path) -> tuple[App, Whitelist, BlobService]:
    """A real `App` hook + real whitelist + real blob service, wired exactly
    the two production sites wire them (`app.startup()` and the
    `identity.install()` fallback both build `Whitelist(...,
    on_revoke=self._revoke_connection)`)."""
    app = App({"core.heartbeat_interval_s": 10})  # type: ignore[arg-type]
    service = BlobService(tmp_path / "blobs", AttachSettings(part_size=1024))
    app.blob = BlobApp(service, origins=())
    whitelist = Whitelist(tmp_path / "whitelist.json", on_revoke=app._revoke_connection)
    app.whitelist = whitelist
    return app, whitelist, service


def test_unbinding_a_device_purges_its_minted_tokens(tmp_path) -> None:
    app, whitelist, service = _wired_app(tmp_path)
    try:
        whitelist.add(MODULE_ID, INSTANCE_ID, FP)
        issued = service.issue_token(DEVICE, ttl_s=900)
        token = str(issued["token"])

        # The token authenticates before the unbind (the exact HTTP gate).
        from vade.blob.auth import Authenticator

        auth = Authenticator(tokens=service, sessions={}, origins=(), wall=time.time)
        assert auth.authenticate({"authorization": f"Bearer {token}"}).device_id == DEVICE

        # Unbind = whitelist.revoke: entries deleted, callback fires.
        assert whitelist.revoke(FP) == (DEVICE,)
        assert whitelist.allows(FP) is False

        # The token is dead the moment the unbind returns (A9).
        try:
            auth.authenticate({"authorization": f"Bearer {token}"})
            raise AssertionError("the unbound device's token must be 401 AUTH_FAILED")
        except Exception as exc:
            from vade.errors import VadeError

            assert isinstance(exc, VadeError), exc
            assert exc.code == "AUTH_FAILED", exc
        assert service.get_token(__import__("hashlib").sha256(token.encode()).hexdigest(), time.time()) is None
    finally:
        service.close()


def test_revoke_callback_receives_the_removed_device_ids(tmp_path) -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []
    whitelist = Whitelist(tmp_path / "whitelist.json", on_revoke=lambda fp, ids: seen.append((fp, ids)))
    whitelist.add(MODULE_ID, INSTANCE_ID, FP)
    whitelist.add(MODULE_ID, "fs-02", FP)  # one key, two instances
    removed = whitelist.revoke(FP)
    assert removed == (DEVICE, f"{MODULE_ID}#fs-02")
    assert seen == [(FP, removed)]
    assert whitelist.revoke(FP) == () and seen[-1] == (FP, ()), (
        "a revoke with nothing to remove fires the callback with the empty tuple"
    )


def test_unbind_purges_tokens_for_every_instance_of_the_key(tmp_path) -> None:
    """One fingerprint may be paired under several instances; the unbind must
    kill tokens minted against ALL of the removed `module#instance` names."""
    app, whitelist, service = _wired_app(tmp_path)
    try:
        whitelist.add(MODULE_ID, INSTANCE_ID, FP)
        whitelist.add(MODULE_ID, "fs-02", FP)
        tokens = [
            str(service.issue_token(DEVICE, ttl_s=900)["token"]),
            str(service.issue_token(f"{MODULE_ID}#fs-02", ttl_s=900)["token"]),
            str(service.issue_token("other#device", ttl_s=900)["token"]),  # untouched
        ]
        now = time.time()
        assert all(service.get_token(_sha(t), now) is not None for t in tokens)
        whitelist.revoke(FP)
        now = time.time()
        assert service.get_token(_sha(tokens[0]), now) is None
        assert service.get_token(_sha(tokens[1]), now) is None
        assert service.get_token(_sha(tokens[2]), now) is not None, (
            "only the unbound devices' tokens die, never a bystander's"
        )
    finally:
        service.close()


def test_unbind_without_a_blob_component_still_closes_connections(tmp_path) -> None:
    """`app.blob` is None (attachments disabled): the callback must not blow
    up — connection closing still happens, token purging is a no-op."""
    app = App({"core.heartbeat_interval_s": 10})  # type: ignore[arg-type]
    whitelist = Whitelist(tmp_path / "whitelist.json", on_revoke=app._revoke_connection)
    whitelist.add(MODULE_ID, INSTANCE_ID, FP)
    assert whitelist.revoke(FP) == (DEVICE,)  # no exception, registry None-safe


def _sha(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()
