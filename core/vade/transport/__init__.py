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
from .server import AsgiWebSocket, CONNECTIONS, CoreApp

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
__all__ += ["CONNECTIONS"]


def install(app: Any) -> None:
    """Idempotent: attach the transport facade to the app object.

    `App.startup` imports this module before calling `install`, so
    `app.transport` is briefly the module itself. Treat both that and a
    previous facade dict as "already installed" only for the dict; the module
    is replaced with the facade so governance reads `connections` from one
    place.
    """
    current = getattr(app, "transport", None)
    if isinstance(current, dict) and "connections" in current:
        return
    app.transport = {"connections": CONNECTIONS, "core_app": CoreApp, "module": current}
