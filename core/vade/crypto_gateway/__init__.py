"""Crypto gateway package: the only place Python touches protocol-core."""

from __future__ import annotations

from typing import Any

from .errors import from_ffi, gateway_error
from .session import CandidateKeySession, CryptoSession, GatewayState, MODES, fingerprint_of

__all__ = [
    "CryptoSession",
    "CandidateKeySession",
    "GatewayState",
    "MODES",
    "fingerprint_of",
    "from_ffi",
    "gateway_error",
    "install",
]


def install(app: Any) -> None:
    """Idempotent: expose the factories, do not create sessions eagerly."""
    current = getattr(app, "crypto_gateway", None)
    if isinstance(current, dict) and "factory" in current:
        return
    from ..transport.framing import MAX_FRAME_PLAINTEXT

    def factory(connection_id: str, **kwargs: Any) -> CryptoSession:
        kwargs.setdefault("whitelist", getattr(app, "whitelist", None))
        kwargs.setdefault("max_payload", MAX_FRAME_PLAINTEXT)
        return CryptoSession(app.identity.ffi, **kwargs)

    def module_responder(connection_id: str) -> CandidateKeySession:
        """Inbound module: IK responder with every whitelisted key as candidate."""
        whitelist = getattr(app, "whitelist", None)
        candidates = whitelist.candidate_keys() if whitelist is not None else ()
        return CandidateKeySession(
            app.identity.ffi,
            candidates,
            connection="module_initiated",
            whitelist=whitelist,
            max_payload=MAX_FRAME_PLAINTEXT,
        )

    app.crypto_gateway = {
        "session": CryptoSession,
        "candidate": CandidateKeySession,
        "factory": factory,
        "module_responder": module_responder,
        "state": GatewayState,
    }
