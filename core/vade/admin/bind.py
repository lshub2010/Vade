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
* the expensive half (the Argon2 digest) runs *outside* the process lock, in a
  worker thread. Locking across it serialised every bind on ~100 ms of CPU, and
  doing it on the loop stalled every other admin request; `_BIND_LOCK` now
  covers only the in-memory reservation (re-check + `consume_ticket`).
* the disk writes (`admin.json` on first run, `devices.json`) run outside that
  lock too. Two commits for *different* tickets cannot corrupt each other:
  `devices.add` re-checks the limit under its own lock, and the only mutation
  overlapping between two first-run binds — creating `admin.json` — is refused
  with `BIND_SETUP_LOCKED` while another first-run commit is in flight.
* `setup_required=true` writes `admin.json` *before* the setup token is
  unlinked (a crash leaves a recoverable state, never "no password, no token").
* the commit is all-or-nothing: ticket, `admin.json`, device row and session.
  Any failure after the reservation rolls the finished steps back — the device
  row is discarded (not revoked), `admin.json` is restored from a snapshot and
  the ticket is released last — because an "initialised, no device, no ticket"
  plane can only be repaired with `vade admin reset`.
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
from .auth import run_argon2, verify_password_async, verify_signature

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

#: Guards the in-memory reservation only: the hello table, the re-check and
#: `consume_ticket`. Never held across a digest or a file write — that was the
#: whole point of the split (P0-3).
_BIND_LOCK = threading.RLock()

#: How many first-run commits are creating `admin.json` right now. Guarded by
#: `_BIND_LOCK`; only non-zero on an uninitialised plane. Two of them would
#: interleave `os.replace` on `admin.json` and one rollback could unlink the
#: other's finished document, so the second is refused with the spec'd
#: `BIND_SETUP_LOCKED` (423, retriable) instead.
_SETUP_COMMITS = 0

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
async def complete(ctx: Any, body: dict[str, Any], *, ip: str | None) -> dict[str, Any]:
    """Verify the envelope, then reserve the ticket and enrol the device.

    `async` because the Argon2 digest runs in a worker thread: a bind must not
    stop the loop for 100 ms, and it must not hold `_BIND_LOCK` while waiting
    for CPU either. Raises `VadeError`; every pre-consumption failure leaves
    the ticket usable.
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

    # ④e. The password is the only expensive step, and it no longer runs under
    # `_BIND_LOCK` nor on the event loop: one digest is 64 MiB / tens of ms, and
    # holding the process lock across it queued every other bind behind it.
    setup_required = not ctx.setup.initialized()
    if setup_required:
        _validate_new_password(password)
    elif not await verify_password_async(password, ctx.setup.password_hash()):
        raise _unauthorized()

    # ④f. Reservation — in-memory only, no `await`, no I/O beyond the cached
    # `stat` in `initialized()`. Everything that can change while the digest ran
    # is re-checked here, and the ticket is only consumed when the device can
    # actually be enrolled.
    global _SETUP_COMMITS
    with _BIND_LOCK:
        entry = ctx.setup.peek_ticket(ticket)
        if entry is None:
            raise _unauthorized()
        # The state may have flipped since the digest: the browser can finish
        # `POST /admin/setup` while a phone is scanning. That flips which
        # password had to be proven (a *new* one vs the stored one), so refuse
        # and let the App retry — the ticket is untouched.
        now_setup_required = not ctx.setup.initialized()
        if now_setup_required != setup_required:
            raise _unauthorized()
        if len(ctx.devices) >= ctx.devices.max_devices:
            raise VadeError("BIND_DEVICE_LIMIT", "at most 5 devices per account")
        if ctx.devices.is_revoked(raw_pubkey):
            raise _unauthorized()
        if setup_required:
            if _SETUP_COMMITS:
                # Two first-run commits would race on `admin.json`: the loser's
                # rollback would unlink the winner's finished document.
                raise VadeError("BIND_SETUP_LOCKED", "setup is in progress in another session")
            _SETUP_COMMITS += 1
        if not ctx.setup.consume_ticket(ticket):
            # rowcount=0 equivalent: another request won the race.
            if setup_required:
                _SETUP_COMMITS -= 1
            raise _unauthorized()

    # ④g. Commit, outside the lock: both remaining steps are file writes.
    # `devices.add` re-checks the limit under its own lock, so the reservation
    # above is an optimisation rather than the invariant; a bind that loses that
    # race rolls back and releases the ticket like any other commit failure.
    device: Any = None
    init_snapshot: Any = None
    try:
        if setup_required:
            # The first-run digest is 64 MiB too: keep it off the loop.
            init_snapshot = await run_argon2(
                ctx.setup.begin_initialize_bound, password=password, ip=ip,
            )
        device = ctx.devices.add(
            pubkey_b64=base64.b64encode(raw_pubkey).decode("ascii"), alg=alg,
            label=label, ip=ip, hardware_backed=hardware_backed,
        )
        # §7.12.3 ⑤: the token is minted inside the transaction. `issue` only
        # touches in-memory dicts, but the client cannot use a device it has
        # no session for, so a failure here is a failure of the whole bind.
        tokens = ctx.sessions.issue(mode="device", device_id=device.device_id, ip=ip)
    except BaseException:
        # The ticket is released last, unconditionally: it is the only step that
        # makes the QR retryable.
        _rollback(ctx, ticket, device=device, init_snapshot=init_snapshot)
        raise
    finally:
        if setup_required:
            with _BIND_LOCK:
                _SETUP_COMMITS -= 1

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
def _rollback(ctx: Any, ticket: str, *, device: Any, init_snapshot: Any) -> None:
    """Undo, newest first, everything the bind transaction already committed.

    Runs *outside* `_BIND_LOCK` (nothing else can observe this ticket as
    consumable: `consume_ticket` already returned True for exactly one caller),
    so the cost here is one `devices.json` rewrite plus one `admin.json`
    restore — the failure path of a request that would otherwise have written
    both anyway.

    The ticket is released **last and unconditionally**: it is the only step
    that makes the QR retryable, so a failing rollback step must not skip it.
    Each step is best-effort — the caller is about to re-raise the original
    exception, and replacing it with a bookkeeping error would hide the cause.
    A failed step is recorded in the audit log instead of being swallowed.
    """
    failed: list[str] = []
    if device is not None:
        try:
            ctx.devices.discard(device.device_id)
        except Exception:
            failed.append("device")
    if init_snapshot is not None:
        try:
            ctx.setup.rollback_initialize(init_snapshot)
        except Exception:
            failed.append("admin")
    ctx.setup.release_ticket(ticket)
    if failed:
        ctx.write_audit(
            kind="device.bound", status="denied", code="INTERNAL", actor=None,
            extra={"reason": "bind rollback incomplete", "steps": failed},
        )


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
