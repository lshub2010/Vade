"""The admin listener. Separate instance, separate port — never mounted on :8443.

Mounting the admin app on the data plane would put it behind the same origin as
third-party `ui` module code; one XSS there reads the audit log and issues
revokes. `HttpOnly` protects the cookie from being read, not from being used.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from ..errors import VadeError
from ..logging import get_logger

__all__ = ["ADMIN_PORT", "DATA_PORT", "assert_isolated", "run", "log_banner", "make_server"]

log = get_logger("vade.admin")

ADMIN_PORT = 8444
DATA_PORT = 8443


def assert_isolated(host: str, port: int) -> None:
    if port == DATA_PORT:
        raise VadeError(
            "INVALID_ARGUMENT",
            "the admin plane must not share the data-plane port (8443); use 8444 or --admin-port",
        )
    if not _is_loopback_host(host) and port in {80, 443}:
        # A public port with a self-signed cert is a support burden, not a
        # security win; the doc allows it, the warning stays loud.
        log.warning("admin plane bound to a public port %s", port, extra={"code": None, "msg": "admin_public_port"})


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost", ""}


def log_banner(data_host: str = "127.0.0.1", admin_host: str = "127.0.0.1", admin_port: int = ADMIN_PORT) -> list[str]:
    lines = [
        f"data plane  : wss://{data_host}:{DATA_PORT}   (modules, ui/session)",
        f"admin plane : https://{admin_host}:{admin_port} (auth: password [+ ed25519 key if remote])",
    ]
    for line in lines:
        log.info(line, extra={"code": None, "msg": "banner"})
    return lines


def make_server(app: Any, *, host: str, port: int, cert: str | Path | None, key: str | Path | None) -> Any:
    import uvicorn

    assert_isolated(host, port)
    return uvicorn.Server(
        uvicorn.Config(
            app, host=host, port=port,
            ssl_certfile=str(cert) if cert else None,
            ssl_keyfile=str(key) if key else None,
            log_level="info",
        )
    )


def run(app: Any, *, host: str = "127.0.0.1", port: int = ADMIN_PORT,
        cert: str | Path | None = None, key: str | Path | None = None) -> None:
    if not _is_loopback_host(host) and not cert:
        raise VadeError("INVALID_ARGUMENT", "the admin plane requires TLS when it is not loopback")
    server_obj = make_server(app, host=host, port=port, cert=cert, key=key)
    server_obj.run()
