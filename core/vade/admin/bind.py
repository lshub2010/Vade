"""`POST /admin/bind` — the encrypted device-binding envelope (§7.12.3 ③–⑤).

Byte-for-byte contract with the App (WebCrypto on the other side):

    shared = X25519(eph_priv, kex_pub)
    key    = HKDF-SHA256(shared, salt=b"vade-bind-v1", info=b"aes-256-gcm")  # 32B
    aad    = b"vade-bind-v1" || fp8.ascii() || SHA256(ticket) || challenge || kex_pub
    ct     = AES-256-GCM(key, nonce(12B), plaintext, aad)  # ciphertext || tag

`fp8`, `challenge` and `kex_pub` are exactly the values served by
`GET /admin/bind/info`; `SHA256(ticket)` is the raw 32-byte digest. The AAD is
computable *before* decryption only because the ticket travels in the clear
(v1.16 fixed v1.15's circular dependency).

Ordering rules — the reasons this lives in its own module:

* verify everything first, consume the ticket last. A bad ciphertext, a bad
  signature or a full device list must not burn a ticket: that would be a
  zero-cost onboarding DoS (§7.12.3 ④).
* ticket reservation and device enrolment happen together under one lock;
  `SetupService.consume_ticket` is the single point that can succeed once, so
  concurrent POSTs with the same ticket enrol exactly one device.
* `setup_required=true` writes `admin.json` *before* the setup token is
  unlinked (a crash leaves a recoverable state, never "no password, no token").
* the plaintext password never leaves this function: it is hashed by
  `hash_password`, never audited and never logged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import secrets
import threading
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..errors import VadeError
from .auth import verify_password, verify_signature

__all__ = [
    "BIND_PREFIX", "HELLO_TTL_S", "MAX_SKEW_S", "begin_hello", "complete",
    "bind_aad", "envelope_key",
]

BIND_PREFIX = b"vade-bind-v1"
KDF_SALT = b"vade-bind-v1"
KDF_INFO = b"aes-256-gcm"
NONCE_LEN = 12
TICKET_LEN = 26
#: §7.12.3 ①: "challenge 60s 一次性".
HELLO_TTL_S = 60
#: §7.12.3 ④b / §7.12.5 `BIND_CLOCK_SKEW`.
MAX_SKEW_S = 120

#: One lock for every bind attempt in this process. It spans "reserve the
#: ticket → write admin.json → add the device", so a rollback never exposes a
#: half-applied state to a second request.
_BIND_LOCK = threading.RLock()

_TICKET_ALPHABET = frozenset("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")


def bind_aad(*, fp8: str, ticket: str, challenge: bytes, kex_pub: bytes) -> bytes:
    """`"vade-bind-v1" || fp8 || SHA256(ticket) || challenge || kex_pub`."""
    return (
        BIND_PREFIX
        + fp8.encode("ascii")
        + hashlib.sha256(ticket.encode("utf-8")).digest()
        + challenge
        + kex_pub
    )


def envelope_key(shared_secret: bytes) -> bytes:
    """HKDF-SHA256 → AES-256-GCM key, fixed salt/info (`vade-bind-v1`)."""
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=KDF_SALT, info=KDF_INFO,
    ).derive(shared_secret)


# --------------------------------------------------------------------- hello
def begin_hello(ctx: Any, ticket: str, entry: dict[str, Any], *, ip: str | None) -> dict[str, Any]:
    """Mint `kex_pub`/`challenge` for one ticket and remember them for step ③.

    The challenge is bound to `(ticket_hash, 来源 IP /24)` and dies after 60s
    (§7.12.3 ①), so a hello captured on one network cannot be completed from
    another one.
    """
    from cryptography.hazmat.primitives import serialization

    key = X25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    now = time.time()
    session = {
        "kex_public": public,
        "challenge": secrets.token_bytes(32),
        "private": private,
        "ip_net": _net(ip),
        "expires_at": min(float(entry["expires_at"]), now + HELLO_TTL_S),
    }
    with _BIND_LOCK:
        ctx._bind_sessions[ticket] = session
    return session


def _take_hello(ctx: Any, ticket: str, ip: str | None) -> dict[str, Any]:
    """One-time: the challenge is burned even when later checks fail.

    Burning the *challenge* is not burning the *ticket* — the App can ask for a
    fresh hello with the same ticket, and a bad ciphertext still cannot make the
    QR unusable (§7.12.3 ④).
    """
    with _BIND_LOCK:
        session = ctx._bind_sessions.pop(ticket, None)
    if session is None:
        raise _unauthorized()
    if session["expires_at"] <= time.time():
        raise _unauthorized()
    if session["ip_net"] != _net(ip):
        raise _unauthorized()
    return session


# -------------------------------------------------------------------- complete
def complete(ctx: Any, body: dict[str, Any], *, ip: str | None) -> dict[str, Any]:
    """Verify the envelope, then reserve the ticket and enrol the device.

    Raises `VadeError`; every pre-consumption failure leaves the ticket usable.
    """
    ticket = body.get("ticket")
    if not isinstance(ticket, str) or len(ticket) != TICKET_LEN or not set(ticket) <= _TICKET_ALPHABET:
        raise _unauthorized()
    eph_pub = _raw_field(body, "eph_pub", 32)
    nonce = _raw_field(body, "nonce", NONCE_LEN)
    ciphertext = _raw_field(body, "ct", None)
    if len(ciphertext) < 16:  # GCM tag is the last 16 bytes
        raise _unauthorized()

    # ④a. ticket exists / unexpired / unconsumed — SELECT only, never delete.
    entry = ctx.setup.peek_ticket(ticket)
    if entry is None:
        raise _unauthorized()
    session = _take_hello(ctx, ticket, ip)

    plaintext = _open_envelope(
        ctx, ticket=ticket, session=session,
        eph_pub=eph_pub, nonce=nonce, ciphertext=ciphertext,
    )
    payload = _payload(plaintext)

    password = payload.get("password")
    if not isinstance(password, str) or not password or len(password) > 256:
        raise _unauthorized()
    device_pub = payload.get("device_pub")
    if not isinstance(device_pub, str):
        raise _unauthorized()
    raw_pubkey, alg = _device_key(device_pub, payload.get("alg"))
    signature = _b64(payload.get("sig"), required=True)

    # ④b. clock skew first: it has its own code and must not look like a
    # generic failure, or the user cannot fix it.
    ts = payload.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        raise _unauthorized()
    if abs(time.time() - float(ts)) > MAX_SKEW_S:
        raise VadeError("BIND_CLOCK_SKEW", "the request timestamp is more than 120s off")

    # ④c. the signature proves the sender holds the private half of the key
    # being enrolled. P-256 high-S twins are refused inside `verify_signature`.
    if not verify_signature(alg, raw_pubkey, session["challenge"], signature):
        raise _unauthorized()

    label = str(payload.get("label") or "device")[:64]
    # Self-reported and unverifiable (§7.12.3 ③): only a boolean we can render
    # as "device says so", never an authorisation input.
    hardware_backed = payload.get("hardware_backed") is True

    # ④d. a key that was explicitly revoked never comes back, even with a
    # fresh, valid ticket.
    if ctx.devices.is_revoked(raw_pubkey):
        raise _unauthorized()

    # ④e + ④f. From here on the ticket is the scarce resource: everything is
    # re-checked under the lock, and the ticket is only consumed when the
    # device can actually be enrolled.
    with _BIND_LOCK:
        entry = ctx.setup.peek_ticket(ticket)
        if entry is None:
            raise _unauthorized()
        # The state may have flipped since the hello: the browser can finish
        # `POST /admin/setup` while a phone is scanning. Re-read it here.
        setup_required = not ctx.setup.initialized()
        if setup_required:
            _validate_new_password(password)
        elif not verify_password(password, ctx.setup.password_hash()):
            raise _unauthorized()
        if len(ctx.devices) >= ctx.devices.max_devices:
            raise VadeError("BIND_DEVICE_LIMIT", "at most 5 devices per account")
        if ctx.devices.is_revoked(raw_pubkey):
            raise _unauthorized()

        if not ctx.setup.consume_ticket(ticket):
            # rowcount=0 equivalent: another request won the race.
            raise _unauthorized()
        try:
            if setup_required:
                ctx.setup.initialize_bound(password=password, ip=ip)
            device = ctx.devices.add(
                pubkey_b64=base64.b64encode(raw_pubkey).decode("ascii"), alg=alg,
                label=label, ip=ip, hardware_backed=hardware_backed,
            )
        except BaseException:
            # Failure must not burn the ticket (§7.12.3 ④). The rollback runs
            # under the same lock, so no other request can observe it.
            ctx.setup.release_ticket(ticket)
            raise

    tokens = ctx.sessions.issue(mode="device", device_id=device.device_id, ip=ip)
    ctx.write_audit(
        kind="device.bound", status="ok", code=None, actor=device.device_id,
        extra={"device_id": device.device_id, "alg": alg, "label": device.label,
               "setup_required": setup_required, "ip": ip},
    )
    return {
        "device_id": device.device_id,
        "label": device.label,
        **tokens,
    }


# ---------------------------------------------------------------------- crypto
def _open_envelope(
    ctx: Any, *, ticket: str, session: dict[str, Any], eph_pub: bytes,
    nonce: bytes, ciphertext: bytes,
) -> bytes:
    try:
        private = X25519PrivateKey.from_private_bytes(session["private"])
        peer = X25519PublicKey.from_public_bytes(eph_pub)
        shared = private.exchange(peer)
    except (ValueError, TypeError):
        raise _unauthorized() from None
    aad = bind_aad(
        fp8=ctx.identity.fp8, ticket=ticket,
        challenge=session["challenge"], kex_pub=session["kex_public"],
    )
    try:
        return AESGCM(envelope_key(shared)).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        # Wrong key, wrong AAD (fp8/challenge/kex_pub changed) or tampered ct —
        # indistinguishable on purpose.
        raise _unauthorized() from None


def _payload(plaintext: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(plaintext)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _unauthorized() from None
    if not isinstance(payload, dict):
        raise _unauthorized()
    return payload


def _device_key(device_pub: str, alg: Any) -> tuple[bytes, str]:
    """`alg` ∈ {p256, ed25519}; the key encoding is checked here, not later."""
    name = str(alg or "p256").lower()
    if name == "es256":
        name = "p256"
    if name == "ecdsa-p256":
        name = "p256"
    if name not in {"p256", "ed25519"}:
        raise VadeError("BIND_KEY_UNSUPPORTED", "alg must be p256 or ed25519")
    raw = _b64(device_pub, required=True)
    if name == "p256" and not (len(raw) == 65 and raw[:1] == b"\x04"):
        raise VadeError("BIND_KEY_UNSUPPORTED", "p256 public key must be a 65-byte uncompressed point")
    if name == "ed25519" and len(raw) != 32:
        raise VadeError("BIND_KEY_UNSUPPORTED", "ed25519 public key must be 32 raw bytes")
    return raw, name


def _validate_new_password(password: str) -> None:
    from .auth import PasswordPolicyError, validate_password

    try:
        validate_password(password)
    except PasswordPolicyError as exc:
        raise VadeError("INVALID_ARGUMENT", str(exc)) from None


# --------------------------------------------------------------------- helpers
def _unauthorized() -> VadeError:
    """§7.12.3 ④: every verification failure is one indistinguishable 401."""
    return VadeError("BIND_UNAUTHORIZED", "bind ticket is unknown, expired or already used")


def _b64(value: Any, *, required: bool = False) -> bytes:
    if not isinstance(value, str) or not value:
        if required:
            raise _unauthorized()
        return b""
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise _unauthorized() from None


def _raw_field(body: dict[str, Any], key: str, length: int | None) -> bytes:
    raw = _b64(body.get(key), required=True)
    if length is not None and len(raw) != length:
        raise _unauthorized()
    return raw


def _net(ip: str | None) -> str:
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    if addr.version == 4:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))
