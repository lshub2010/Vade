"""Noise gateway: wraps protocol-core's Handshake/Transport. No crypto here.

State machine: NEW -> HANDSHAKING -> ESTABLISHED -> DRAINING -> CLOSED.

Rules encoded below (21-C2 T3):
  * NEW accepts only `type=0x01`; an rpc/event before the handshake closes.
  * IK failure NEVER falls back to XX (there is simply no fallback branch).
  * `remote_static` is compared against the whitelist *inside* `read_handshake`
    before the reply is handed to the caller — frames.md §3.5 requires that the
    responder not even write msg2 to an unlisted key.
  * AEAD failure is fatal: the session is unusable and must be dropped. snow
    0.10 leaves the nonce unadvanced on failure (frames.md §7.6), but "the
    ciphertext did not authenticate" is indistinguishable from an active
    attacker, so continuing is not an option.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Callable, Iterable, Protocol

from ..errors import VadeError
from ..transport.framing import DEFAULT_MAX_PAYLOAD, FrameHeader, FrameType, ReplayWindow, open_frame as open_wire, seal as seal_wire
from .errors import from_ffi

__all__ = ["CryptoSession", "CandidateKeySession", "GatewayState", "MODES", "fingerprint_of"]


def fingerprint_of(public_key: bytes) -> str:
    """fp8 = SHA-256(raw X25519 public key)[:16] lowercase hex (01-contracts 裁决表)."""
    return hashlib.sha256(bytes(public_key)).hexdigest()[:16]


class GatewayState(str, Enum):
    NEW = "new"
    HANDSHAKING = "handshaking"
    ESTABLISHED = "established"
    DRAINING = "draining"
    CLOSED = "closed"


MODES = ("ik", "xxpsk3")
CONNECTIONS = ("core_initiated", "module_initiated")


class _Whitelist(Protocol):
    def allows(self, fingerprint: str) -> bool: ...


class CryptoSession:
    def __init__(
        self,
        local_identity: Any,
        *,
        mode: str = "ik",
        role: str = "responder",
        connection: str = "module_initiated",
        remote_pub: bytes | None = None,
        psk: bytes | None = None,
        whitelist: _Whitelist | None = None,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        protocol_module: Any | None = None,
    ) -> None:
        if mode not in MODES:
            raise VadeError("INVALID_ARGUMENT", f"mode must be one of {MODES}, got {mode!r}")
        if connection not in CONNECTIONS:
            raise VadeError("INVALID_ARGUMENT", f"connection must be one of {CONNECTIONS}, got {connection!r}")
        if mode == "ik" and role == "initiator" and remote_pub is None:
            # IK needs the responder's static key; silently switching to XX here
            # is exactly the downgrade C-01 forbids.
            raise VadeError("INVALID_ARGUMENT", "IK initiator requires remote_pub (no XX fallback)")
        if mode == "xxpsk3" and psk is None:
            raise VadeError("INVALID_ARGUMENT", "xxpsk3 requires a pair-code PSK")

        self._proto = protocol_module or _load_protocol()
        self._mode = mode
        self._role = role
        self._connection = connection
        self._whitelist = whitelist
        self._max_payload = max_payload
        self._state = GatewayState.NEW
        self._handshake: Any | None = None
        self._transport: Any | None = None
        self._replay = ReplayWindow()
        self._remote_static: bytes | None = None
        self._remote_fp: str | None = None
        try:
            self._handshake = self._proto.FfiHandshake(
                mode=mode,
                role=role,
                connection=connection,
                local=local_identity,
                remote_pub=remote_pub,
                psk=psk,
            )
        except Exception as exc:  # FfiError
            raise from_ffi(exc) from None
        self._state = GatewayState.HANDSHAKING

    # ------------------------------------------------------------------ props
    @property
    def state(self) -> GatewayState:
        return self._state

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def remote_static(self) -> bytes | None:
        return self._remote_static

    @property
    def remote_fingerprint(self) -> str | None:
        return self._remote_fp

    @property
    def transport(self) -> Any:
        if self._state is not GatewayState.ESTABLISHED or self._transport is None:
            raise VadeError("INVALID_ARGUMENT", f"transport not available in state {self._state.value}")
        return self._transport

    # ------------------------------------------------------------------ handshake
    def start(self) -> bytes | None:
        """Initiator: produce msg1. Responder: nothing to send yet."""
        self._require(GatewayState.HANDSHAKING)
        if self._role != "initiator":
            return None
        try:
            return bytes(self._handshake.write(b""))
        except Exception as exc:
            self._fail()
            raise from_ffi(exc) from None

    def read_handshake(self, message: bytes) -> bytes | None:
        """Consume one handshake message; return the reply to send (or None)."""
        self._require(GatewayState.HANDSHAKING)
        if len(message) > 65535:
            raise VadeError("PAYLOAD_TOO_LARGE", f"handshake message {len(message)} > 65535", fatal=True)
        try:
            payload = bytes(self._handshake.read(bytes(message)))
            # `read` returns the decrypted handshake PAYLOAD; the next outbound
            # message is produced by `write`. IK responder: read(msg1) then
            # write() -> msg2. XXpsk3 initiator: read(msg2) then write() -> msg3.
            reply = b""
            if not self._handshake.is_finished():
                reply = bytes(self._handshake.write(b""))
        except Exception as exc:
            self._fail()
            raise from_ffi(exc) from None
        _ = payload

        # Whitelist check IMMEDIATELY, before the reply can be transmitted.
        remote = self._handshake.remote_static()
        if remote is not None:
            self._remote_static = bytes(remote)
            self._remote_fp = fingerprint_of(self._remote_static)
            if self._whitelist is not None and not self._whitelist.allows(self._remote_fp):
                self._fail()
                raise VadeError(
                    "PERMISSION_DENIED",
                    f"peer static key {self._remote_fp} is not whitelisted",
                    fatal=True,
                )
        return reply or None

    def is_finished(self) -> bool:
        return bool(self._handshake.is_finished()) if self._handshake is not None else False

    def establish(self) -> None:
        """Enter transport mode. For a responder this must happen AFTER msg2."""
        self._require(GatewayState.HANDSHAKING)
        if not self.is_finished():
            raise VadeError("INVALID_ARGUMENT", "into_transport() before the handshake finished")
        try:
            self._transport = self._handshake.into_transport()
        except Exception as exc:
            self._fail()
            raise from_ffi(exc) from None
        # Defence in depth: compare again, the way C-01 §3.5 demands.
        if self._remote_fp and self._whitelist is not None and not self._whitelist.allows(self._remote_fp):
            self._fail()
            raise VadeError("PERMISSION_DENIED", "peer key revoked during handshake", fatal=True)
        self._handshake = None
        # Receive direction starts on generation 1 (the transport's first key).
        self._replay.reset(1)
        self._state = GatewayState.ESTABLISHED

    # ------------------------------------------------------------------ records
    def seal(self, frame_type: FrameType, plaintext: bytes) -> bytes:
        self._require(GatewayState.ESTABLISHED)
        try:
            return seal_wire(self.transport, frame_type, plaintext)
        except VadeError:
            raise
        except Exception as exc:
            raise from_ffi(exc) from None

    def open(self, wire: bytes) -> tuple[FrameHeader, bytes]:
        self._require(GatewayState.ESTABLISHED)
        try:
            header, plaintext = open_wire(self.transport, wire, replay=self._replay, max_payload=self._max_payload)
        except VadeError as exc:
            if exc.fatal:
                self._fail()
            raise
        except Exception as exc:
            err = from_ffi(exc)
            if err.fatal:
                self._fail()
            raise err from None
        if header.type is FrameType.REKEY:
            # Rust already advanced the receive generation; nothing to do here
            # except let the caller know (it is a transport-internal frame).
            pass
        return header, plaintext

    def maybe_rekey(self) -> bytes | None:
        """Sender-side rekey. Call when any threshold trips (24h / 1GiB / 2^20)."""
        self._require(GatewayState.ESTABLISHED)
        try:
            return bytes(self.transport.begin_rekey())
        except Exception as exc:
            raise from_ffi(exc) from None

    def send_generation(self) -> int:
        self._require(GatewayState.ESTABLISHED)
        return int(self.transport.send_generation())

    def recv_generation(self) -> int:
        self._require(GatewayState.ESTABLISHED)
        return int(self.transport.recv_generation())

    # ------------------------------------------------------------------ lifecycle
    def drain(self) -> None:
        if self._state is GatewayState.ESTABLISHED:
            self._state = GatewayState.DRAINING

    def close(self) -> None:
        self._state = GatewayState.CLOSED
        self._transport = None
        self._handshake = None

    def _fail(self) -> None:
        self._state = GatewayState.CLOSED
        self._transport = None
        self._handshake = None

    def _require(self, expected: GatewayState) -> None:
        if self._state is not expected:
            raise VadeError(
                "INVALID_ARGUMENT",
                f"operation requires state {expected.value}, session is {self._state.value}",
            )


class CandidateKeySession:
    """IK responder that picks the peer's static key from a whitelist pool.

    Why this exists: protocol-core refuses `IK` without `remote_pub` for BOTH
    roles (no XX fallback), while an inbound connection only reveals the
    initiator's static key inside msg1. A single `CryptoSession` therefore
    cannot be built before the first frame arrives. This wrapper keeps a fresh
    `CryptoSession` per candidate and re-reads msg1 with the next key when the
    AEAD check fails.

    Trying a wrong candidate is indistinguishable from talking to a stranger, so
    a failure is only a hint to try the next key. The authoritative decision is
    still made by `CryptoSession.read_handshake` -> `whitelist.allows()`: a
    candidate that decrypts msg1 but is not stored is rejected with
    `PERMISSION_DENIED` and never produces msg2 on the wire.

    Not wired: XXpsk3 pairing (first contact, PSK from the pair code). That
    needs the pairing RPC surface owned by C3/C5a; `mode` is explicit rather
    than guessed so no downgrade can hide here.
    """

    def __init__(
        self,
        local_identity: Any,
        candidates: Iterable[bytes],
        *,
        connection: str = "module_initiated",
        whitelist: _Whitelist | None = None,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        protocol_module: Any | None = None,
    ) -> None:
        if connection not in CONNECTIONS:
            raise VadeError("INVALID_ARGUMENT", f"connection must be one of {CONNECTIONS}, got {connection!r}")
        self._identity = local_identity
        self._candidates = tuple(bytes(c) for c in candidates)
        self._connection = connection
        self._whitelist = whitelist
        self._max_payload = max_payload
        self._protocol = protocol_module
        self._next = 0
        self._session: CryptoSession | None = None
        self._last_error: BaseException | None = None
        self._closed = False
        if not self._candidates:
            # Fail closed and loudly: a core with an empty whitelist must not
            # accept a module, and silently waiting for a handshake that can
            # never verify is worse than telling the operator why.
            raise VadeError(
                "PERMISSION_DENIED",
                "no whitelisted module public key is stored; pair the module first",
                fatal=True,
            )
        self._session = self._make(self._candidates[0])

    def _make(self, remote_pub: bytes) -> CryptoSession:
        return CryptoSession(
            self._identity,
            mode="ik",
            role="responder",
            connection=self._connection,
            remote_pub=remote_pub,
            whitelist=self._whitelist,
            max_payload=self._max_payload,
            protocol_module=self._protocol,
        )

    # ------------------------------------------------------------------ props
    @property
    def state(self) -> GatewayState:
        return self._session.state if self._session is not None else GatewayState.CLOSED

    @property
    def mode(self) -> str:
        return "ik"

    @property
    def remote_static(self) -> bytes | None:
        return self._session.remote_static if self._session is not None else None

    @property
    def remote_fingerprint(self) -> str | None:
        return self._session.remote_fingerprint if self._session is not None else None

    @property
    def transport(self) -> Any:
        if self._session is None:
            raise VadeError("INVALID_ARGUMENT", "transport not available: session is closed")
        return self._session.transport

    @property
    def attempted_keys(self) -> int:
        return self._next + (1 if self._session is not None else 0)

    # ------------------------------------------------------------------ handshake
    def start(self) -> None:
        return None  # responder

    def read_handshake(self, message: bytes) -> bytes | None:
        while True:
            if self._session is None:
                raise VadeError("PERMISSION_DENIED", "no candidate key could read the handshake", fatal=True)
            try:
                return self._session.read_handshake(message)
            except VadeError as exc:
                self._last_error = exc
                if self._session.state is not GatewayState.CLOSED or self._next + 1 >= len(self._candidates):
                    raise
                # This key could not authenticate msg1. Try the next stored one.
                self._next += 1
                self._session = self._make(self._candidates[self._next])

    def is_finished(self) -> bool:
        return bool(self._session.is_finished()) if self._session is not None else False

    def establish(self) -> None:
        if self._session is None:
            raise VadeError("INVALID_ARGUMENT", "session is closed")
        self._session.establish()

    # ------------------------------------------------------------------ records
    def seal(self, frame_type: FrameType, plaintext: bytes) -> bytes:
        if self._session is None:
            raise VadeError("INVALID_ARGUMENT", "session is closed")
        return self._session.seal(frame_type, plaintext)

    def open(self, wire: bytes) -> tuple[FrameHeader, bytes]:
        if self._session is None:
            raise VadeError("INVALID_ARGUMENT", "session is closed")
        return self._session.open(wire)

    def maybe_rekey(self) -> bytes | None:
        if self._session is None:
            raise VadeError("INVALID_ARGUMENT", "session is closed")
        return self._session.maybe_rekey()

    def send_generation(self) -> int:
        if self._session is None:
            raise VadeError("INVALID_ARGUMENT", "session is closed")
        return self._session.send_generation()

    def recv_generation(self) -> int:
        if self._session is None:
            raise VadeError("INVALID_ARGUMENT", "session is closed")
        return self._session.recv_generation()

    # ------------------------------------------------------------------ lifecycle
    def drain(self) -> None:
        if self._session is not None:
            self._session.drain()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
        self._closed = True


def _load_protocol() -> Any:
    try:
        import vade_protocol  # type: ignore
    except ImportError:  # pragma: no cover - only when the .so is missing
        raise VadeError(
            "INTERNAL",
            "protocol core python binding is not importable; build it with "
            "`cargo build --features uniffi` and put libvade_protocol.so next to vade_protocol.py",
        ) from None
    return vade_protocol
