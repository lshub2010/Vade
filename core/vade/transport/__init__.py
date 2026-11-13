"""Transport package: WSS listener, connection state machine, framing.

`install(app)` is the component contract. The live ASGI object is built by
`vade.__main__` (or by C5a's runner), never by importing this module.
"""

from __future__ import annotations

from typing import Any

from .connection import Connection, ConnectionState, LoopLagMonitor
from .framing import (
    DEFAULT_MAX_PAYLOAD,
    HEADER_LEN,
    MAX_FRAME_PLAINTEXT,
    MAX_HANDSHAKE_MESSAGE,
    FrameHeader,
    FrameType,
    ReplayWindow,
    decode_handshake,
    encode_handshake,
    open_frame,
    seal,
    split_stream,
)
from .server import AsgiWebSocket, CoreApp

__all__ = [
    "Connection",
    "ConnectionState",
    "LoopLagMonitor",
    "CoreApp",
    "AsgiWebSocket",
    "FrameHeader",
    "FrameType",
    "ReplayWindow",
    "HEADER_LEN",
    "MAX_FRAME_PLAINTEXT",
    "MAX_HANDSHAKE_MESSAGE",
    "DEFAULT_MAX_PAYLOAD",
    "encode_handshake",
    "decode_handshake",
    "seal",
    "open_frame",
    "split_stream",
    "install",
]

#: Live connections by connection_id; filled by CoreApp, read by governance.
CONNECTIONS: dict[str, Connection] = {}


def install(app: Any) -> None:
    """Idempotent: attach the transport facade to the app object."""
    if getattr(app, "transport", None) is not None:
        return
    app.transport = {"connections": CONNECTIONS, "core_app": CoreApp}
