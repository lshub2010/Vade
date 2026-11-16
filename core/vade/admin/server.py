"""The admin listener. Separate instance, separate port — never mounted on :8443.

Mounting the admin app on the data plane would put it behind the same origin as
third-party `ui` module code; one XSS there reads the audit log and issues
revokes. `HttpOnly` protects the cookie from being read, not from being used.

This module only assembles a listener; `vade.__main__._serve` owns its lifecycle
so the data and admin planes share one event loop and one SIGTERM.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from ..errors import VadeError
from ..logging import get_logger

__all__ = [
    "ADMIN_PORT", "DATA_PORT", "DEFAULT_ADMIN_BIND", "assert_isolated",
    "is_loopback_host", "parse_bind", "require_tls", "run", "log_banner", "make_server",
]

log = get_logger("vade.admin")

ADMIN_PORT = 8444
DATA_PORT = 8443
DEFAULT_ADMIN_BIND = f"127.0.0.1:{ADMIN_PORT}"


# ----------------------------------------------------------------- configuration
def parse_bind(value: object, *, default_host: str = "127.0.0.1", default_port: int = ADMIN_PORT) -> tuple[str, int]:
    """`"host:port"` (or bare `"host"`) → `(host, port)`.

    `[::1]:8444` is accepted too; a bare address gets `default_port`, so
    `--admin-bind 127.0.0.1` cannot silently fall back to the data port.
    """
    text = str(value or "").strip()
    if not text:
        return default_host, default_port
    if text.startswith("[") and "]" in text:
        host, _, rest = text[1:].partition("]")
        return host or default_host, _port(rest.lstrip(":"), default_port)
    host, sep, port_text = text.rpartition(":")
    if not sep:
        return text, default_port
    return host or default_host, _port(port_text, default_port)


def _port(text: str, default: int) -> int:
    if not text:
        return default
    try:
        port = int(text)
    except ValueError:
        raise VadeError("INVALID_ARGUMENT", f"admin bind port {text!r} is not a number") from None
    if not 0 <= port <= 65535:
        raise VadeError("INVALID_ARGUMENT", f"admin bind port {port} is out of range")
    return port


def is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost", ""}


def require_tls(host: str, cert: str | Path | None, key: str | Path | None = None) -> None:
    """Refuse a remote admin plane without TLS instead of serving plain HTTP.

    `/admin` carries the password, the bearer token and the setup token. On
    loopback plain HTTP is a local-only choice; on any other interface it is an
    unauthenticated wire. Fail closed *before* a socket is bound.
    """
    if is_loopback_host(host):
        return
    if cert and key:
        return
    raise VadeError(
        "INVALID_ARGUMENT",
        "refusing to start the admin plane on non-loopback host "
        f"{host!r} without TLS: /admin would be served over plain HTTP. "
        "Set [tls].cert and [tls].key, or bind the admin plane to 127.0.0.1.",
    )


def assert_isolated(host: str, port: int) -> None:
    if port == DATA_PORT:
        raise VadeError(
            "INVALID_ARGUMENT",
            "the admin plane must not share the data-plane port (8443); use 8444 or --admin-port",
        )
    if not is_loopback_host(host) and port in {80, 443}:
        # A public port with a self-signed cert is a support burden, not a
        # security win; the doc allows it, the warning stays loud.
        log.warning("admin plane bound to a public port %s", port, extra={"code": None, "msg": "admin_public_port"})


def log_banner(
    data_host: str = "127.0.0.1",
    admin_host: str = "127.0.0.1",
    admin_port: int = ADMIN_PORT,
    *,
    tls: bool = True,
) -> list[str]:
    ws_scheme, http_scheme = ("wss", "https") if tls else ("ws", "http")
    lines = [
        f"data plane  : {ws_scheme}://{data_host}:{DATA_PORT}   (modules, ui/session)",
        f"admin plane : {http_scheme}://{admin_host}:{admin_port} (auth: password [+ ed25519 key if remote])",
    ]
    for line in lines:
        log.info(line, extra={"code": None, "msg": "banner"})
    return lines


# ---------------------------------------------------------------------- assembly
def make_server(
    app: Any,
    *,
    host: str,
    port: int,
    cert: str | Path | None,
    key: str | Path | None,
) -> Any:
    import uvicorn

    assert_isolated(host, port)
    require_tls(host, cert, key)
    return uvicorn.Server(
        uvicorn.Config(
            app, host=host, port=port,
            ssl_certfile=str(cert) if cert else None,
            ssl_keyfile=str(key) if key else None,
            log_level="info",
            # uvicorn trusts X-Forwarded-For from 127.0.0.1 by default and
            # rewrites scope["client"]. The admin plane treats loopback as
            # already-authorised (setup unlock, `X-AC-Client: cli`), so a
            # remote client could rotate that header and *become* loopback.
            # Behind a real reverse proxy, enable it deliberately at the proxy.
            proxy_headers=False,
        )
    )


def run(
    app: Any,
    *,
    host: str = "127.0.0.1",
    port: int = ADMIN_PORT,
    cert: str | Path | None = None,
    key: str | Path | None = None,
) -> None:
    """Standalone (blocking) listener; `_serve` uses `make_server` instead."""
    server_obj = make_server(app, host=host, port=port, cert=cert, key=key)
    server_obj.run()
