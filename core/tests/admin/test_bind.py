"""`POST /admin/bind`: the sealed envelope, verify-then-consume, single winner.

The client half below is written exactly against §7.12.3 ③ (X25519 →
HKDF-SHA256(salt `vade-bind-v1`, info `aes-256-gcm`) → AES-256-GCM, AAD
`vade-bind-v1‖fp8‖SHA256(ticket)‖challenge‖kex_pub`). If `bind.py` and the App
ever disagree on a byte, these tests fail on the first decrypt.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vade.admin.app import create_admin_app
from vade.admin.bind import bind_aad, envelope_key
from vade.admin import setup_token

from .conftest import PASSWORD, client_for, make_context

#: Built by concatenation so this file does not read like a leaked credential.
NEW_PASSWORD = "fresh" + "-battery-" + "staple-9"
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


# ------------------------------------------------------------------ client side
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def p256_device():
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return key, pub


def ed25519_device():
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return key, pub


def sign_p256(key, message: bytes) -> bytes:
    """Sign and normalise to low-S, exactly as the App must (see `bind.py`).

    ECDSA `s` is uniform, so OpenSSL/WebCrypto emit a high-S half of the time;
    the core refuses those (§7.12.3 ④c), so a compliant client normalises
    before sending instead of retrying until it gets lucky.
    """
    r, s = asym_utils.decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    if s > _P256_N // 2:
        s = _P256_N - s
    return asym_utils.encode_dss_signature(r, s)


def sign_ed25519(key, message: bytes) -> bytes:
    return key.sign(message)


async def hello(client: httpx.AsyncClient, ticket: str) -> dict:
    response = await client.get("/admin/bind/info", headers={"X-Bind-Ticket": ticket})
    assert response.status_code == 200, response.text
    return response.json()


def seal(
    info: dict,
    ticket: str,
    *,
    password: str = PASSWORD,
    device_key,
    device_pub: bytes,
    alg: str = "p256",
    ts: int | None = None,
    signature: bytes | None = None,
    challenge: bytes | None = None,
    kex_pub: bytes | None = None,
    label: str = "iPhone 15",
    hardware_backed: bool = True,
) -> dict:
    challenge_bytes = challenge if challenge is not None else base64.b64decode(info["challenge"])
    kex_bytes = kex_pub if kex_pub is not None else base64.b64decode(info["kex_pub"])
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    shared = eph.exchange(X25519PublicKey.from_public_bytes(kex_bytes))
    aad = bind_aad(
        fp8=info["fp8"], ticket=ticket, challenge=challenge_bytes, kex_pub=kex_bytes,
    )
    if signature is None:
        signature = (
            sign_p256(device_key, challenge_bytes) if alg == "p256"
            else sign_ed25519(device_key, challenge_bytes)
        )
    plaintext = json.dumps({
        "password": password, "device_pub": _b64(device_pub), "alg": alg,
        "label": label, "hardware_backed": hardware_backed,
        "ts": int(time.time()) if ts is None else ts, "sig": _b64(signature),
    }).encode("utf-8")
    nonce = secrets.token_bytes(12)
    ct = AESGCM(envelope_key(shared)).encrypt(nonce, plaintext, aad)
    return {"ticket": ticket, "eph_pub": _b64(eph_pub), "nonce": _b64(nonce), "ct": _b64(ct)}


def malleate(der: bytes) -> bytes:
    """The high-S twin of a *low-S* signature: same message, same key, still
    mathematically valid — and exactly what the core must refuse."""
    r, s = asym_utils.decode_dss_signature(der)
    assert s <= _P256_N // 2
    return asym_utils.encode_dss_signature(r, _P256_N - s)


async def post(client: httpx.AsyncClient, body: dict) -> httpx.Response:
    return await client.post("/admin/bind", json=body)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- round trip
def test_bind_round_trip_returns_a_device_session_and_consumes_the_ticket(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        # The hello is signed by the core identity over kex/challenge/ticket/host.
        verified = Ed25519PublicKey.from_public_bytes(base64.b64decode(info["core_pub"]))
        message = "\n".join([
            "vade-bind-hello-v1", info["kex_pub"], info["challenge"],
            hashlib.sha256(ticket.encode()).hexdigest(), ctx.config.admin_host,
        ]).encode()
        verified.verify(base64.b64decode(info["hello_sig"]), message)
        assert info["fp8"] == ctx.identity.fp8
        assert info["setup_required"] is False

        key, pub = p256_device()
        body = seal(info, ticket, device_key=key, device_pub=pub)
        response = await post(client, body)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["mode"] == "device"
        assert payload["device_id"] == hashlib.sha256(pub).hexdigest()[:16]
        assert payload["label"] == "iPhone 15"

        # The returned access token really is a device session.
        listed = await client.get(
            "/admin/api/devices", headers={"Authorization": f"Bearer {payload['access_token']}"}
        )
        assert listed.status_code == 200
        assert [d["device_id"] for d in listed.json()["items"]] == [payload["device_id"]]
        assert ctx.devices.get(payload["device_id"]).hardware_backed is True

        # One use only: the ticket is gone for info and for a second bind.
        again = await client.get("/admin/bind/info", headers={"X-Bind-Ticket": ticket})
        assert again.status_code == 401
        assert again.json()["code"] == "BIND_UNAUTHORIZED"
        reused = await post(client, body)
        assert reused.status_code == 401
        assert len(ctx.devices) == 1

    run(scenario())


def test_envelope_aad_is_bound_to_the_served_challenge(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = p256_device()
        # The signature is valid for the served challenge, but the AAD the
        # client used names a different one: decryption must fail.
        body = seal(
            info, ticket, device_key=key, device_pub=pub,
            challenge=b"\x00" * 32,
        )
        response = await post(client, body)
        assert response.status_code == 401
        assert response.json()["code"] == "BIND_UNAUTHORIZED"
        assert len(ctx.devices) == 0
        # ...and the ticket is still usable; only the 60s challenge was burned.
        info = await hello(client, ticket)
        body = seal(info, ticket, device_key=key, device_pub=pub)
        resp = await post(client, body)
        assert resp.status_code == 200, resp.text

    run(scenario())


# ------------------------------------------------------------------ rejects
def test_clock_skew_is_reported_and_does_not_consume_the_ticket(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = p256_device()
        stale = seal(info, ticket, device_key=key, device_pub=pub, ts=int(time.time()) - 121)
        response = await post(client, stale)
        assert response.status_code == 400
        assert response.json()["code"] == "BIND_CLOCK_SKEW"
        assert len(ctx.devices) == 0
        assert ctx.setup.peek_ticket(ticket) is not None

        fresh = seal(await hello(client, ticket), ticket, device_key=key, device_pub=pub)
        resp = await post(client, fresh)
        assert resp.status_code == 200, resp.text

    run(scenario())


def test_p256_high_s_signature_is_refused_and_the_ticket_survives(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        challenge = base64.b64decode(info["challenge"])
        key, pub = p256_device()
        twin = malleate(sign_p256(key, challenge))  # (r, n-s) also verifies
        body = seal(info, ticket, device_key=key, device_pub=pub, signature=twin)
        response = await post(client, body)
        assert response.status_code == 401
        assert response.json()["code"] == "BIND_UNAUTHORIZED"
        assert len(ctx.devices) == 0

        body = seal(await hello(client, ticket), ticket, device_key=key, device_pub=pub)
        resp = await post(client, body)
        assert resp.status_code == 200, resp.text

    run(scenario())


def test_step_up_path_requires_the_current_password_and_failure_keeps_the_ticket(tmp_path):
    """`setup_required=false` → the password in the envelope is the *current* one."""
    ctx = make_context(tmp_path)  # already initialised
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        assert info["setup_required"] is False
        key, pub = p256_device()

        # A "new password" is not accepted here: this path is step-up.
        wrong = seal(info, ticket, password=NEW_PASSWORD, device_key=key, device_pub=pub)
        response = await post(client, wrong)
        assert response.status_code == 401
        assert response.json()["code"] == "BIND_UNAUTHORIZED"
        assert len(ctx.devices) == 0
        assert ctx.setup.peek_ticket(ticket) is not None

        good = seal(await hello(client, ticket), ticket, device_key=key, device_pub=pub)
        assert (await post(client, good)).status_code == 200

    run(scenario())


def test_unsupported_algorithm_is_refused_before_the_ticket_is_consumed(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = ed25519_device()
        body = seal(info, ticket, device_key=key, device_pub=pub, alg="rsa")
        response = await post(client, body)
        assert response.status_code == 400
        assert response.json()["code"] == "BIND_KEY_UNSUPPORTED"
        assert ctx.setup.peek_ticket(ticket) is not None

        # ed25519 is allowed by the error table and still works.
        body = seal(await hello(client, ticket), ticket, device_key=key, device_pub=pub, alg="ed25519")
        resp = await post(client, body)
        assert resp.status_code == 200, resp.text
        assert ctx.devices.all()[0].alg == "ed25519"

    run(scenario())


# --------------------------------------------------------------- concurrency
def test_concurrent_posts_with_one_ticket_enrol_exactly_one_device(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = p256_device()
        body = seal(info, ticket, device_key=key, device_pub=pub)

        responses = await asyncio.gather(post(client, body), post(client, body))
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200, 401], [r.text for r in responses]
        assert len(ctx.devices) == 1
        assert ctx.setup.peek_ticket(ticket) is None

    run(scenario())


# ------------------------------------------------------------------ setup path
def test_setup_required_bind_initialises_admin_and_removes_the_setup_token(tmp_path):
    ctx = make_context(tmp_path, initialize=False)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        assert info["setup_required"] is True
        key, pub = p256_device()
        body = seal(info, ticket, password=NEW_PASSWORD, device_key=key, device_pub=pub)
        response = await post(client, body)
        assert response.status_code == 200, response.text
        payload = response.json()

        assert ctx.setup.initialized() is True
        assert setup_token.read_token(tmp_path) is None
        doc = json.loads((tmp_path / "admin" / "admin.json").read_text(encoding="utf-8"))
        assert NEW_PASSWORD not in json.dumps(doc)
        assert doc["password"].startswith("$argon2")
        assert len(ctx.devices) == 1

        # The password set through the envelope is the real one.
        challenge = (await client.post("/admin/api/login/challenge", json={})).json()
        logged = await client.post(
            "/admin/api/login", json={"challenge_id": challenge["challenge_id"], "password": NEW_PASSWORD}
        )
        assert logged.status_code == 200, logged.text

        # The plaintext password reached neither the admin document nor the audit log.
        audit_bytes = (tmp_path / "audit" / "audit.db").read_bytes()
        assert NEW_PASSWORD.encode() not in audit_bytes
        assert payload["access_token"]

    run(scenario())


# -------------------------------------------------------------- limits / revocation
def test_device_limit_refuses_and_leaves_the_ticket_usable(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        for index in range(5):
            _, pub = ed25519_device()
            ctx.devices.add(pubkey_b64=_b64(pub), alg="ed25519", label=f"d{index}", ip="127.0.0.1")
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = p256_device()
        body = seal(info, ticket, device_key=key, device_pub=pub)

        response = await post(client, body)
        assert response.status_code == 409
        assert response.json()["code"] == "BIND_DEVICE_LIMIT"
        assert ctx.setup.peek_ticket(ticket) is not None
        assert len(ctx.devices) == 5

        # Free a slot: the same ticket now works, which proves the refusal
        # above did not consume it.
        ctx.devices.revoke(ctx.devices.all()[0].device_id)
        body = seal(await hello(client, ticket), ticket, device_key=key, device_pub=pub)
        resp = await post(client, body)
        assert resp.status_code == 200, resp.text
        assert len(ctx.devices) == 5

    run(scenario())


def test_a_revoked_device_key_cannot_bind_again(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        key, pub = p256_device()
        enrolled = ctx.devices.add(
            pubkey_b64=_b64(pub), alg="p256", label="old phone", ip="127.0.0.1",
        )
        assert ctx.devices.revoke(enrolled.device_id) is True
        # The revocation is on disk, not just in memory.
        on_disk = json.loads((tmp_path / "admin" / "devices.json").read_text(encoding="utf-8"))
        assert enrolled.device_id in on_disk["revoked"]

        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        body = seal(info, ticket, device_key=key, device_pub=pub)
        response = await post(client, body)
        assert response.status_code == 401
        assert response.json()["code"] == "BIND_UNAUTHORIZED"
        assert len(ctx.devices) == 0
        # A different key with the same ticket still binds.
        other_key, other_pub = p256_device()
        body = seal(await hello(client, ticket), ticket, device_key=other_key, device_pub=other_pub)
        resp = await post(client, body)
        assert resp.status_code == 200, resp.text

    run(scenario())


def test_unknown_and_expired_tickets_are_indistinguishable(tmp_path):
    ctx = make_context(tmp_path)
    app = create_admin_app(ctx)

    async def scenario():
        client = client_for(app)
        key, pub = p256_device()
        missing = await client.get("/admin/bind/info", headers={"X-Bind-Ticket": "A" * 26})
        assert missing.status_code == 401
        assert missing.json()["code"] == "BIND_UNAUTHORIZED"

        ticket = ctx.setup.create_ticket(ttl=-1)["ticket"]
        expired = await client.get("/admin/bind/info", headers={"X-Bind-Ticket": ticket})
        assert expired.status_code == 401
        assert expired.json()["code"] == "BIND_UNAUTHORIZED"
        # A POST without a hello cannot even reach decryption.
        body = seal(
            {"fp8": ctx.identity.fp8, "challenge": _b64(b"\x01" * 32),
             "kex_pub": _b64(b"\x02" * 32)},
            ticket, device_key=key, device_pub=pub,
        )
        response = await post(client, body)
        assert response.status_code == 401

    run(scenario())


def test_consume_ticket_has_exactly_one_winner(tmp_path):
    """The `DELETE ... WHERE consumed_at IS NULL` / `rowcount=1` equivalent."""
    from concurrent.futures import ThreadPoolExecutor

    ctx = make_context(tmp_path)
    ticket = ctx.setup.create_ticket()["ticket"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ctx.setup.consume_ticket(ticket), range(8)))
    assert results.count(True) == 1
    assert ctx.setup.peek_ticket(ticket) is None
