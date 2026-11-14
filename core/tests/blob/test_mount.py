"""The component hook and the data-plane mount (RFC-0004 §4.1, §7.1).

`install(app)` is what core's component scan calls; `CoreApp` is what uvicorn
serves. Both halves have to agree, otherwise the six routes exist only in unit
tests: the mount hands over the *live* session table, and a disabled feature
must not leave a 404-shaped oracle that differs from any other unknown path.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from vade.blob import install
from vade.blob.http import ROUTES
from vade.config import load_config
from vade.transport.server import CoreApp

from .conftest import ORIGIN, Client
from .images import png


def _config(tmp_path, **overrides):
    return load_config(
        cli={
            "core.data_dir": str(tmp_path),
            "server.ui_origins": [ORIGIN],
            # The origin's host must be one of the listener's names, otherwise
            # `load_config` refuses to start (SameSite=Strict would break login).
            "server.listen_hosts": ["127.0.0.1", "localhost", "ui.example"],
            **overrides,
        }
    )


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _json_bearer(token: str) -> dict[str, str]:
    return {**_bearer(token), "content-type": "application/json"}


def _begin_payload(data: bytes, width: int = 8, height: int = 8) -> dict[str, object]:
    return {
        "kind": "image",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mime": "image/png",
        "name": "a.png",
        "width": width,
        "height": height,
    }


def test_install_then_all_six_routes_are_reachable_through_coreapp(tmp_path):
    config = _config(tmp_path)
    holder = SimpleNamespace(config=config)
    install(holder)
    assert holder.blob is not None
    assert len(ROUTES) == 6  # the RFC's route table, nothing more

    core = CoreApp(config=config, blob=holder.blob)
    client = Client(core)
    token = str(holder.blob.service.issue_token("dev-1")["token"])
    try:
        data = png()
        digest = hashlib.sha256(data).hexdigest()

        begun = client.post(
            "/ui/blob", headers=_json_bearer(token), body=json.dumps(_begin_payload(data)).encode()
        )
        assert begun.status == 201, begun.json
        upload_id = begun.json["upload_id"]

        part = client.put(
            f"/ui/blob/upload/{upload_id}?offset=0",
            headers={**_bearer(token), "content-type": "application/octet-stream"},
            body=data,
        )
        assert part.status == 200, part.json

        head = client.head(f"/ui/blob/upload/{upload_id}", headers=_bearer(token))
        assert head.status == 200, head.json
        assert head.json["confirmed_bytes"] == len(data)

        done = client.post(
            f"/ui/blob/upload/{upload_id}/complete",
            headers=_json_bearer(token),
            body=json.dumps({"sha256": digest, "size": len(data)}).encode(),
        )
        assert done.status == 201, done.json

        fetched = client.get(f"/ui/blob/{done.json['file_id']}", headers=_bearer(token))
        assert fetched.status == 200
        assert fetched.body == data

        # `complete` consumed the session, so abort needs its own.
        other = png(width=4, height=4)
        second = client.post(
            "/ui/blob",
            headers=_json_bearer(token),
            body=json.dumps(_begin_payload(other, 4, 4)).encode(),
        )
        assert second.status == 201, second.json
        aborted = client.delete(f"/ui/blob/upload/{second.json['upload_id']}", headers=_bearer(token))
        assert aborted.status == 204
    finally:
        holder.blob.close()


def test_disabled_attach_means_the_routes_do_not_exist(tmp_path):
    config = _config(tmp_path, **{"attach.enabled": False})
    holder = SimpleNamespace(config=config)
    install(holder)
    assert holder.blob is None  # not "installed but refusing"

    client = Client(CoreApp(config=config, blob=getattr(holder, "blob", None)))
    upload = "upl_" + "0" * 26
    blob = "blb_" + "0" * 26
    for method, path in [
        ("POST", "/ui/blob"),
        ("PUT", f"/ui/blob/upload/{upload}?offset=0"),
        ("HEAD", f"/ui/blob/upload/{upload}"),
        ("POST", f"/ui/blob/upload/{upload}/complete"),
        ("DELETE", f"/ui/blob/upload/{upload}"),
        ("GET", f"/ui/blob/{blob}"),
    ]:
        response = client.request(
            method, path, headers={"content-type": "application/json"}, body=b"{}"
        )
        assert response.status == 404, (method, path, response.json)
        assert response.json["code"] == "NOT_FOUND"


def test_the_cookie_gate_reads_the_live_session_table(tmp_path):
    """`/ui/session` fills this dict; the mounted app must see it by reference."""
    config = _config(tmp_path)
    holder = SimpleNamespace(config=config)
    install(holder)
    core = CoreApp(config=config, blob=holder.blob)
    try:
        session, csrf = "session-token", "csrf-value"
        core._sessions[session] = {
            "device_id": "ui-1",
            "revoked": False,
            "read_only": False,
            "csrf_hash": hashlib.sha256(csrf.encode("utf-8")).hexdigest(),
        }
        client = Client(core)
        headers = {
            "cookie": f"__Host-ac_ui={session}; __Host-ac_csrf={csrf}",
            "content-type": "application/json",
            "origin": ORIGIN,
            "sec-fetch-site": "same-origin",
            "x-vade-csrf": csrf,
        }
        data = png()
        response = client.post(
            "/ui/blob", headers=headers, body=json.dumps(_begin_payload(data)).encode()
        )
        assert response.status == 201, response.json
        assert response.json["upload_id"].startswith("upl_")
    finally:
        holder.blob.close()
