"""Shared fixtures for the admin plane tests (no network, no real TLS)."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vade.admin.app import AdminConfig, AdminContext, create_admin_app, login_message
from vade.admin.auth import ChallengeStore, SessionStore
from vade.admin.devices import DeviceStore
from vade.admin.identity import load_or_create
from vade.admin import setup_token
from vade.admin.setup import SetupService
from vade.audit.writer import AuditWriter
from vade.identity.pairing import PairingStore

ADMIN_HOST = "127.0.0.1:8444"
ORIGIN = f"https://{ADMIN_HOST}"
PASSWORD = "correct-horse-battery"


def make_context(tmp_path: Path, *, initialize: bool = True) -> AdminContext:
    config = AdminConfig(data_dir=tmp_path, admin_host=ADMIN_HOST)
    identity = load_or_create(tmp_path)
    writer = AuditWriter(tmp_path / "audit" / "audit.db", sync=True)
    ctx = AdminContext(
        config=config,
        identity=identity,
        setup=SetupService(tmp_path, ADMIN_HOST, identity.fp8),
        devices=DeviceStore(tmp_path / "admin" / "devices.json"),
        sessions=SessionStore(access_ttl_s=900),
        challenges=ChallengeStore(ttl_s=60),
        audit=writer,
    )
    # Same wiring as `AdminContext.from_app`: invites need the C2 pairing store.
    ctx.pairing = PairingStore(tmp_path / "pairing.sqlite3")
    if initialize:
        ctx.setup.initialize(password=PASSWORD, loopback=True, ip="127.0.0.1")
    else:
        # Production runs `ensure_setup_token()` on every start; without it the
        # gate test would have no token to unlock with.
        setup_token.ensure_setup_token(tmp_path, env={})
    return ctx


@pytest.fixture
def ctx(tmp_path):
    context = make_context(tmp_path)
    yield context
    if context.audit is not None:
        context.audit.close()


@pytest.fixture
def app(ctx):
    return create_admin_app(ctx)


def client_for(app, ip: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 40000)),
        base_url=ORIGIN,
    )


def new_device(ctx: AdminContext, *, label: str = "phone", hardware_backed: bool = True):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    device = ctx.devices.add(
        pubkey_b64=base64.b64encode(raw).decode(), alg="ed25519", label=label,
        ip="127.0.0.1", hardware_backed=hardware_backed,
    )
    return device, key


async def login_with_device(client: httpx.AsyncClient, ctx: AdminContext, device, key,
                            *, origin: str = ORIGIN):
    response = await client.post("/admin/api/login/challenge", json={})
    assert response.status_code == 200, response.text
    challenge = response.json()
    signature = base64.b64encode(key.sign(login_message(challenge["challenge"], origin))).decode()
    return await client.post(
        "/admin/api/login",
        json={
            "challenge_id": challenge["challenge_id"], "password": PASSWORD,
            "device_id": device.device_id, "signature": signature,
        },
        headers={"Origin": origin},
    )


async def login_browser(client: httpx.AsyncClient, ctx: AdminContext, *, password: str = PASSWORD):
    response = await client.post("/admin/api/login/challenge", json={})
    challenge = response.json()
    return await client.post(
        "/admin/api/login",
        json={"challenge_id": challenge["challenge_id"], "password": password},
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
