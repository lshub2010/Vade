"""Harness for the `/ui/blob` channel: a real ASGI call, no network.

Every test drives `BlobApp.__call__` with a hand-built scope, so the code under
test is exactly the object mounted on the data plane. `Clock` is injected into
the service so TTL and GC are deterministic instead of `sleep(900)`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from vade.blob.auth import CSRF_COOKIE, CSRF_HEADER, UI_COOKIE
from vade.blob.http import BlobApp
from vade.blob.service import BlobService
from vade.blob.settings import AttachSettings

ORIGIN = "https://ui.example"
CSRF_VALUE = "csrf-double-submit-value"


class Clock:
    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.body or b"{}")

    def header(self, name: str, default: str | None = None) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return default

    def all_headers(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]


class Client:
    """Minimal ASGI HTTP client: one request, collected response."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes = b"") -> Response:
        if "?" in path:
            path, _, query = path.partition("?")
        else:
            query = ""
        scope = {
            "type": "http",
            "method": method.upper(),
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [(str(k).lower().encode(), str(v).encode()) for k, v in (headers or {}).items()],
            "client": ("127.0.0.1", 54321),
            "server": ("127.0.0.1", 8443),
            "scheme": "https",
        }
        sent: list[dict[str, Any]] = []
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        status = 200
        out_headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []
        for message in sent:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                out_headers = [(k.decode(), v.decode()) for k, v in message.get("headers", [])]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body") or b"")
        return Response(status=status, headers=out_headers, body=b"".join(chunks))

    # convenience verbs -----------------------------------------------------
    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> Response:
        return self.request("HEAD", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self.request("DELETE", path, **kwargs)


@dataclass
class Env:
    service: BlobService
    app: BlobApp
    client: Client
    clock: Clock
    sessions: dict[str, Any]
    token: str
    device: str = "dev-1"

    # ------------------------------------------------------------------ auth
    def headers(self, **extra: str) -> dict[str, str]:
        base = {"authorization": f"Bearer {self.token}"}
        base.update(extra)
        return base

    def json_headers(self, **extra: str) -> dict[str, str]:
        return self.headers(**{"content-type": "application/json", **extra})

    def part_headers(self, **extra: str) -> dict[str, str]:
        return self.headers(**{"content-type": "application/octet-stream", **extra})

    def other_token(self, device: str = "dev-2") -> str:
        return str(self.service.issue_token(device)["token"])

    # -------------------------------------------------------------- protocol
    def begin(self, payload: dict[str, Any], *, token: str | None = None) -> Response:
        headers = self.json_headers()
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        return self.client.post("/ui/blob", headers=headers, body=json.dumps(payload).encode())

    def put_part(self, upload_id: str, offset: int, data: bytes, *, token: str | None = None, chunk_sha: str | None = None) -> Response:
        headers = self.part_headers()
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        if chunk_sha is not None:
            headers["x-vade-chunk-sha256"] = chunk_sha
        return self.client.put(
            f"/ui/blob/upload/{upload_id}?offset={offset}", headers=headers, body=data
        )

    def complete(self, upload_id: str, payload: dict[str, Any]) -> Response:
        return self.client.post(
            f"/ui/blob/upload/{upload_id}/complete",
            headers=self.json_headers(),
            body=json.dumps(payload).encode(),
        )

    def upload(self, data: bytes, *, mime: str = "image/png", width: int = 8, height: int = 8,
               kind: str = "image", name: str = "a.png", sha: str | None = None,
               part_size: int | None = None, token: str | None = None) -> Response:
        """begin + all parts + complete. Returns the final response."""
        digest = sha or hashlib.sha256(data).hexdigest()
        payload: dict[str, Any] = {
            "kind": kind,
            "sha256": digest,
            "size": len(data),
            "mime": mime,
            "name": name,
        }
        if kind == "image":
            payload.update({"width": width, "height": height})
        if part_size is not None:
            payload["part_size"] = part_size
        begun = self.begin(payload, token=token)
        if begun.status not in (200, 201):
            return begun
        body = begun.json
        if body.get("dedupe"):
            return begun
        upload_id = body["upload_id"]
        size = int(body["part_size"])
        for offset in range(0, len(data), size):
            part = self.put_part(upload_id, offset, data[offset : offset + size], token=token)
            if part.status != 200:
                return part
        return self.complete(upload_id, {"sha256": digest, "size": len(data)})


def cookie_headers(
    env: Env,
    *,
    token: str = "ui-session-token",
    device: str = "ui-1",
    read_only: bool = False,
    revoked: bool = False,
    origin: str | None = ORIGIN,
    fetch_site: str | None = "same-origin",
    csrf: str | None = CSRF_VALUE,
    csrf_cookie: str | None = CSRF_VALUE,
) -> dict[str, str]:
    """Seed a browser session and build its headers (gates included)."""
    env.sessions[token] = {
        "device_id": device,
        "csrf_hash": hashlib.sha256(CSRF_VALUE.encode("utf-8")).hexdigest(),
        "read_only": read_only,
        "revoked": revoked,
    }
    cookies = [f"{UI_COOKIE}={token}"]
    if csrf_cookie is not None:
        cookies.append(f"{CSRF_COOKIE}={csrf_cookie}")
    headers: dict[str, str] = {"cookie": "; ".join(cookies), "content-type": "application/json"}
    if origin is not None:
        headers["origin"] = origin
    if fetch_site is not None:
        headers["sec-fetch-site"] = fetch_site
    if csrf is not None:
        headers[CSRF_HEADER] = csrf
    return headers


@pytest.fixture()
def clock() -> Clock:
    return Clock()


#: Defaults for the fixture: a 1 KiB part size so a "multi-part" upload is
#: cheap, and the production ratio between quota and temp area (4:1).
BASE_SETTINGS: dict[str, Any] = {
    "part_size": 1024,
    "max_bytes": 8 * 1024 * 1024,
    "quota_bytes": 8 * 1024 * 1024,
    "image_max_bytes": 5 * 1024 * 1024,
    "image_max_edge": 2048,
    "image_max_pixels": 4_194_304,
    "gc_interval_s": 60,
    "upload_idle_ttl_min": 15,
    "ttl_hours": 1,
}


def build_env(data_dir, clock: Clock, **overrides: Any) -> Env:
    settings = AttachSettings(**{**BASE_SETTINGS, **overrides})
    service = BlobService(Path(data_dir), settings, wall=clock)
    sessions: dict[str, Any] = {}
    app = BlobApp(service, sessions=sessions, origins=(ORIGIN,))
    token = str(service.issue_token("dev-1")["token"])
    return Env(service=service, app=app, client=Client(app), clock=clock, sessions=sessions, token=token)


@pytest.fixture()
def make_env(tmp_path, clock: Clock):
    """Variant settings in the same clock: quota, TTL and GC tests need them."""
    services: list[BlobService] = []

    def factory(**overrides: Any) -> Env:
        environment = build_env(tmp_path / f"case{len(services)}", clock, **overrides)
        services.append(environment.service)
        return environment

    yield factory
    for service in services:
        service.close()


@pytest.fixture()
def env(tmp_path, clock: Clock) -> Any:
    environment = build_env(tmp_path / "data", clock)
    try:
        yield environment
    finally:
        environment.service.close()
