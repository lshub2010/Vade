"""`python -m vade` entry point.

Pre-placed `admin` subparser (v1.18): `python -m vade admin <cmd>` is forwarded
to C5a's `vade.admin.cli:main`. Before C5a lands, the import failure must say
"admin module not installed" instead of dumping a traceback.

`--print-fingerprint` prints ONLY the core identity fp8. TLS fingerprint is a
separate command (`--print-tls-fingerprint`) and a separate value.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .app import App, create_app
from .config import ConfigError, load_config
from .identity import tls as tls_module
from .errors import VadeError

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vade", description="Vade core")
    parser.add_argument("--config", default=None, help="path to core.toml")
    parser.add_argument("--bind", default=None, help="override [core].listen, e.g. 0.0.0.0:8443")
    parser.add_argument("--admin-bind", default=None, help="override [core].admin_host (C5a listener)")
    parser.add_argument("--admin-allow-cidr", default=None, help="optional narrowing of admin sources")
    parser.add_argument("--san", action="append", default=None, help="extra TLS SAN (repeatable)")
    parser.add_argument("--print-fingerprint", action="store_true", help="print core identity fp8 and exit")
    parser.add_argument("--print-tls-fingerprint", action="store_true", help="print TLS certificate fingerprint and exit")
    parser.add_argument("--strict", action="store_true", help="refuse to start when core components are missing")

    subparsers = parser.add_subparsers(dest="command")
    admin = subparsers.add_parser("admin", add_help=False, help="admin CLI (owned by C5a)")
    admin.add_argument("args", nargs=argparse.REMAINDER)
    admin.set_defaults(func=_admin_main)
    return parser


def _admin_main(args: argparse.Namespace) -> int:
    try:
        from .admin.cli import main as admin_main  # type: ignore
    except ImportError:
        print("vade: admin module is not installed (C5a has not landed yet)", file=sys.stderr)
        return 2
    return int(admin_main(args.args) or 0)


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if args.bind:
        overrides["core.listen"] = args.bind
    if args.admin_bind:
        overrides["core.admin_host"] = args.admin_bind
    if args.san:
        overrides["tls.san"] = list(args.san)
    return overrides


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is not None:
        return int(args.func(args))

    try:
        config = load_config(toml_path=args.config, cli=_cli_overrides(args))
    except (ConfigError, VadeError) as exc:
        print(f"vade: {exc.code}: {exc}", file=sys.stderr)
        return 2

    app = App(config)
    if args.print_fingerprint:
        print(app.print_fingerprint())
        return 0
    if args.print_tls_fingerprint:
        cert = config.get("tls.cert")
        if not cert:
            print("vade: [tls].cert is not configured (self-signed material is generated on first start)", file=sys.stderr)
            return 2
        print(tls_module.cert_fingerprint_hex(cert)[:16])
        return 0

    # Starting a live listener needs an ASGI server (uvicorn). Keep the import
    # local so `--print-fingerprint` and config checks work on a bare install.
    try:
        import uvicorn
    except ImportError:
        print(
            "vade: uvicorn is not installed; config loaded OK but no listener was started",
            file=sys.stderr,
        )
        return 3

    return int(_serve(app, uvicorn, strict=args.strict))


def _serve(app: App, uvicorn: Any, *, strict: bool) -> int:
    import asyncio

    from .app import install_components
    from .crypto_gateway.session import CryptoSession
    from .transport.framing import MAX_FRAME_PLAINTEXT
    from .transport.server import CoreApp

    asyncio.run(app.startup())
    install_components(app, strict=strict)

    def make_crypto(connection_id: str) -> CryptoSession:
        return CryptoSession(
            app.identity.ffi,
            mode="ik",
            role="responder",
            connection="module_initiated",
            whitelist=app.whitelist,
            max_payload=MAX_FRAME_PLAINTEXT,
        )

    core_app = CoreApp(
        config=app.config,
        identity=app.identity,
        registry=app.registry,
        pairing=app.pairing,
        make_crypto=make_crypto,
        policy_mode="allow_all" if app.config.get("dev.allow_all") else "default",
    )

    host, _, port_text = str(app.config.get("core.listen")).rpartition(":")
    tls = {}
    cert, key = app.config.get("tls.cert"), app.config.get("tls.key")
    if cert and key:
        tls = {"ssl_certfile": cert, "ssl_keyfile": key}
    uvicorn.run(core_app, host=host or "127.0.0.1", port=int(port_text), log_level="warning", **tls)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
