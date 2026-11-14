"""`python -m vade` entry point.

Pre-placed `admin` subparser (v1.18): `python -m vade admin <cmd>` is forwarded
to C5a's `vade.admin.cli:main`. Before C5a lands, the import failure must say
"admin module not installed" instead of dumping a traceback.

`--print-fingerprint` prints ONLY the core identity fp8. TLS fingerprint is a
separate command (`--print-tls-fingerprint`) and a separate value.

First run pairing (alpha A1-2): a browser can only attach after redeeming a
`purpose=browser` pair code, and there was no way to obtain one without a
cookie or the Noise handshake. On startup, once the listener is bound, the core
now mints one single-use browser code and prints a machine-parseable line:

    pair_code=青松-A7K2M9 purpose=browser expires_in=600s

`--no-pair-banner` suppresses both the mint and the print (scripts/tests).
The code is printed with `print`, never logged: `setup_logging` emits JSON
lines and a live pair code must not reach a log sink.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .app import App, create_app
from .config import ConfigError, load_config
from .identity import tls as tls_module
from .identity.pairing import Purpose
from .errors import VadeError

__all__ = [
    "main",
    "build_parser",
    "build_module_crypto",
    "announce_pair_code",
    "mint_browser_pair_code",
    "pair_banner_lines",
    "PAIR_BANNER_TTL",
]

#: Browser pair codes are short-lived on purpose: the banner prints once, and
#: a code that outlives the terminal scrolls away with its own blast radius.
PAIR_BANNER_TTL = 600


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vade", description="Vade core")
    parser.add_argument("--config", default=None, help="path to core.toml")
    parser.add_argument("--bind", default=None, help="override [core].listen, e.g. 0.0.0.0:8443")
    parser.add_argument("--admin-bind", default=None, help="override [core].admin_host (C5a listener)")
    parser.add_argument("--admin-allow-cidr", default=None, help="optional narrowing of admin sources")
    parser.add_argument("--san", action="append", default=None, help="extra TLS SAN (repeatable)")
    parser.add_argument("--print-fingerprint", action="store_true", help="print core identity fp8 and exit")
    parser.add_argument("--print-tls-fingerprint", action="store_true", help="print TLS certificate fingerprint and exit")
    parser.add_argument(
        "--no-pair-banner",
        action="store_true",
        help="do not mint/print the browser pair code on startup (scripts and tests)",
    )
    parser.add_argument("--strict", action="store_true", help="refuse to start when core components are missing")

    subparsers = parser.add_subparsers(dest="command")
    admin = subparsers.add_parser("admin", add_help=False, help="admin CLI (owned by C5a)")
    # `add_help=False` is needed so the subcommand flags reach REMAINDER untouched,
    # but argparse then rejects a bare `--help` before REMAINDER starts. Accept it
    # here and hand it to C5a's own parser instead of erroring with exit 2.
    admin.add_argument("-h", "--help", action="store_true", dest="admin_help",
                       help="show the admin CLI help")
    admin.add_argument("args", nargs=argparse.REMAINDER)
    admin.set_defaults(func=_admin_main)
    return parser


def _admin_main(args: argparse.Namespace) -> int:
    try:
        from .admin.cli import main as admin_main  # type: ignore
    except ImportError:
        print("vade: admin module is not installed (C5a has not landed yet)", file=sys.stderr)
        return 2
    forwarded = ["--help"] if getattr(args, "admin_help", False) else args.args
    return int(admin_main(forwarded) or 0)


def pair_banner_lines(code: str, *, purpose: str = "browser", ttl: int = PAIR_BANNER_TTL) -> tuple[str, str]:
    """Startup banner: line 1 machine-parseable, line 2 for humans.

    Line 1's shape is documented in `docs/PAIRING.md`: split on single spaces
    and read `pair_code=`, `purpose=`, `expires_in=` (always seconds).
    """
    machine = f"pair_code={code} purpose={purpose} expires_in={ttl}s"
    human = (
        "vade: first-run pairing — enter this code in the app/browser (it redeems via POST /ui/session). "
        f"Single-use, expires in {ttl}s; restart the core for a fresh one. "
        "Pass --no-pair-banner to hide this."
    )
    return machine, human


def mint_browser_pair_code(pairing: Any | None, *, ttl: int = PAIR_BANNER_TTL) -> str:
    """Mint one short-lived browser code; raise a C-04 error when pairing is off."""
    if pairing is None:
        raise VadeError("CODE_INVALID", "pairing is not configured; no browser pair code was minted")
    return pairing.create(Purpose.BROWSER, ttl=ttl)


def announce_pair_code(
    pairing: Any | None,
    *,
    enabled: bool = True,
    stream: Any = None,
    error_stream: Any = None,
    ttl: int = PAIR_BANNER_TTL,
) -> str | None:
    """Mint and print the first-run pair banner; return the code (or None).

    `print`, never `log`: `setup_logging` writes JSON lines, and a live pair
    code must not land in a log sink. `enabled=False` (`--no-pair-banner`)
    also skips minting, so a scripted start leaves no live row behind.
    """
    if not enabled:
        return None
    out = sys.stdout if stream is None else stream
    err = sys.stderr if error_stream is None else error_stream
    try:
        code = mint_browser_pair_code(pairing, ttl=ttl)
    except VadeError as exc:
        # Readable failure (pairing disabled), never a traceback: the core is
        # still usable for modules, it just cannot pair a browser this run.
        print(f"vade: {exc.code}: {exc.message}", file=err)
        return None
    except Exception as exc:  # noqa: BLE001 - a banner must not kill a live core
        print(f"vade: pair banner failed ({type(exc).__name__}): {exc}", file=err)
        return None
    machine, human = pair_banner_lines(code, ttl=ttl)
    # Flush: stdout is block-buffered when redirected to a pipe/file, and a
    # banner that only appears at process exit is useless for first-run setup.
    print(machine, file=out, flush=True)
    print(human, file=out, flush=True)
    return code


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
        try:
            print(app.print_fingerprint())
        except VadeError as exc:
            print(f"vade: {exc.code}: {exc}", file=sys.stderr)
            return 2
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

    return int(_serve(app, uvicorn, strict=args.strict, pair_banner=not args.no_pair_banner))


def build_module_crypto(app: App) -> Any:
    """IK responder factory for inbound modules.

    A stored fingerprint cannot be turned back into a public key, so the
    factory hands the gateway the whole whitelist pool and lets it try each key
    on msg1 (`CandidateKeySession`). An empty pool raises and `CoreApp` closes
    the socket with 1011 — fail closed, no anonymous session.
    """
    from .crypto_gateway.session import CandidateKeySession
    from .transport.framing import MAX_FRAME_PLAINTEXT

    whitelist = app.whitelist

    def make_crypto(connection_id: str) -> CandidateKeySession:
        if app.identity is None or whitelist is None:
            raise VadeError("INTERNAL", "core identity or whitelist is not loaded")
        return CandidateKeySession(
            app.identity.ffi,
            whitelist.candidate_keys(),
            connection="module_initiated",
            whitelist=whitelist,
            max_payload=MAX_FRAME_PLAINTEXT,
        )

    return make_crypto


def _serve(app: App, uvicorn: Any, *, strict: bool, pair_banner: bool = True) -> int:
    import asyncio

    from .app import install_components, make_rpc_handler
    from .transport.server import CoreApp

    asyncio.run(app.startup())
    install_components(app, strict=strict)

    core_app = CoreApp(
        config=app.config,
        identity=app.identity,
        registry=app.registry,
        pairing=app.pairing,
        # RFC-0004 §6.4: the same trust table the Noise handshake checks, used
        # to name a native peer after the handshake proved its key.
        whitelist=app.whitelist,
        make_crypto=build_module_crypto(app),
        on_rpc=make_rpc_handler(app),
        policy_mode="allow_all" if app.config.get("dev.allow_all") else "default",
        # RFC-0004: `/ui/blob*` lives on this listener, mounted by `vade.blob`.
        blob=getattr(app, "blob", None),
    )

    host, _, port_text = str(app.config.get("core.listen")).rpartition(":")
    tls = {}
    cert, key = app.config.get("tls.cert"), app.config.get("tls.key")
    if cert and key:
        tls = {"ssl_certfile": cert, "ssl_keyfile": key}
    server = uvicorn.Server(
        uvicorn.Config(
            core_app,
            host=host or "127.0.0.1",
            port=int(port_text),
            log_level="warning",
            # uvicorn's ProxyHeadersMiddleware trusts X-Forwarded-For from
            # 127.0.0.1 by default and rewrites scope["client"]. That silently
            # defeats the pair-code rate limit (any client can rotate the header).
            # Behind a real reverse proxy, enable it deliberately AND construct
            # CoreApp(trust_forwarded_for=True).
            proxy_headers=False,
            **tls,
        )
    )

    async def serve_until_stopped() -> None:
        # `uvicorn.run` blocks with no "ready" hook, so drive `Server` by hand
        # and announce the pair code only once the socket is bound: a failed
        # bind must never advertise a live code that points at nothing.
        serving = asyncio.create_task(server.serve())
        while not server.started and not serving.done():
            await asyncio.sleep(0.02)
        if server.started and not server.should_exit:
            announce_pair_code(app.pairing, enabled=pair_banner)
        await serving

    asyncio.run(serve_until_stopped())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
