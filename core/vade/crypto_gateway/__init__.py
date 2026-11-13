"""Crypto gateway package: the only place Python touches protocol-core."""

from __future__ import annotations

from typing import Any

from .errors import from_ffi, gateway_error
from .session import CryptoSession, GatewayState, MODES, fingerprint_of

__all__ = ["CryptoSession", "GatewayState", "MODES", "fingerprint_of", "from_ffi", "gateway_error", "install"]


def install(app: Any) -> None:
    """Idempotent: expose the factory, do not create sessions eagerly."""
    if getattr(app, "crypto_gateway", None) is not None:
        return
    from ..transport.framing import MAX_FRAME_PLAINTEXT

    def factory(connection_id: str, **kwargs: Any) -> CryptoSession:
        kwargs.setdefault("whitelist", getattr(app, "whitelist", None))
        kwargs.setdefault("max_payload", MAX_FRAME_PLAINTEXT)
        return CryptoSession(app.identity.ffi, **kwargs)

    app.crypto_gateway = {"session": CryptoSession, "factory": factory, "state": GatewayState}
