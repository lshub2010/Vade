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

The admin plane (C5a) shares this process and this event loop. `_serve` starts
a second uvicorn listener on `[core].admin_host` (default `127.0.0.1:8444`)
next to the data plane, and one SIGTERM sets `should_exit` on both. A
non-loopback admin bind without `[tls].cert`/`[tls].key` is refused before any
socket exists: `/admin` carries the password and the setup token, so plain
HTTP there is not a "dev mode", it is the whole credential on the wire.

First-run setup: on every start of an uninitialised core, `setup.token` is
regenerated (D59) — unless `VADE_SETUP_TOKEN` pins it — and its **masked**
form is printed with the listener banner. The plaintext is only in the file
(0600) or via `python -m vade admin print-setup-token`.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any, Callable

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
    "announce_setup_token",
    "mint_browser_pair_code",
    "pair_banner_lines",
    "setup_banner_lines",
    "install_shutdown_handlers",
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


def _mask_setup_token(token: str) -> str:
    """Lazy import: `vade.admin` pulls Starlette, and `--print-fingerprint`
    must keep working on an install without it."""
    from .admin.setup_token import mask

    return mask(token)


def setup_banner_lines(token: str, *, admin_url: str, token_file: str | Path) -> tuple[str, str]:
    """First-run banner: line 1 machine-parseable, line 2 for humans.

    The token is **masked** on purpose: this line lands in terminal scrollback,
    CI logs and `docker logs`. The full value is in `token_file` (0600) and can
    be printed explicitly with `admin print-setup-token`.
    """
    machine = f"setup_token={_mask_setup_token(token)} setup_path={token_file}"
    human = (
        f"vade: first-run admin setup — open {admin_url}/admin/setup. "
        "A loopback request is already authorised; a remote browser needs the full token "
        "from the file above (`python -m vade admin print-setup-token`). "
        "The token rotates on every restart until setup completes."
    )
    return machine, human


def announce_setup_token(
    token: str | None,
    *,
    admin_url: str,
    token_file: str | Path,
    stream: Any = None,
) -> str | None:
    """Print the masked setup banner; return the mask (or None when initialised).

    `print`, never `log`: even a masked token does not belong in a JSON log
    record that may be shipped off-box.
    """
    if not token:
        return None
    out = sys.stdout if stream is None else stream
    machine, human = setup_banner_lines(token, admin_url=admin_url, token_file=token_file)
    print(machine, file=out, flush=True)
    print(human, file=out, flush=True)
    return _mask_setup_token(token)


def install_shutdown_handlers(loop: Any, servers: list[Any] | tuple[Any, ...]) -> list[int]:
    """Route SIGTERM/SIGINT to **every** listener, not just the last one.

    `uvicorn.Server.serve()` installs its own handler per server, and the second
    install silently replaces the first. With two servers in one process that
    means SIGTERM leaves one plane running (or kills the process mid-shutdown
    via the signal re-raise). Installing after both are serving makes one signal
    set `should_exit` on all of them, so `serve()` returns normally in both.
    """
    def request_exit(*_args: Any) -> None:
        for server in servers:
            server.should_exit = True

    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_exit)
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            # Windows (no add_signal_handler) or a worker thread (tests): fall
            # back to signal.signal, which still works in the main thread.
            try:
                signal.signal(sig, request_exit)
            except (ValueError, OSError):
                continue
        installed.append(sig)
    return installed


def _bound_port(server: Any) -> int | None:
    """Actual port after `startup()` — `:0` is replaced by the OS."""
    for listener in getattr(server, "servers", None) or []:
        for sock in getattr(listener, "sockets", None) or []:
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError, TypeError):
                continue
    return None


def _adopt_bound_port(ctx: Any, host: str, port: int) -> None:
    """`admin_host = host:0` must not leave the Host allow-list at `:0`."""
    bound = f"{host}:{port}"
    if bound == ctx.config.admin_host:
        return
    ctx.config.admin_host = bound
    ctx.config.allowed_hosts = frozenset(set(ctx.config.allowed_hosts) | {bound, f"localhost:{port}"})
    # `bind_url` and the bind-hello signature embed the admin host:port; a
    # `:0` in a QR code points the App at nothing.
    ctx.setup.admin_host = bound


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

    try:
        return int(_serve(app, uvicorn, strict=args.strict, pair_banner=not args.no_pair_banner))
    except VadeError as exc:
        # A refused admin bind (non-loopback without TLS, port clash) is an
        # operator mistake, not a crash: one readable line, exit 2.
        print(f"vade: {exc.code}: {exc.message}", file=sys.stderr)
        return 2


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


def _serve(
    app: App,
    uvicorn: Any,
    *,
    strict: bool,
    pair_banner: bool = True,
    on_ready: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    """Run both listeners on one loop until a signal (or a test) stops them.

    `on_ready` is a test/integration hook: it fires once both sockets are bound
    and receives the live `uvicorn.Server` objects (so a test can set
    `should_exit`), the actual bound ports, and the **masked** setup token.
    """
    import asyncio

    from .admin import server as admin_server
    from .admin.app import create_admin_app
    from .admin.setup_token import ensure_setup_token
    from .app import install_components, make_rpc_handler
    from .transport.server import CoreApp

    admin_host, admin_port = admin_server.parse_bind(app.config.get("core.admin_host"))
    data_host_raw, _, data_port_text = str(app.config.get("core.listen")).rpartition(":")
    data_host = data_host_raw or "127.0.0.1"
    data_port = int(data_port_text)
    cert, key = app.config.get("tls.cert"), app.config.get("tls.key")
    tls_cert, tls_key = (cert, key) if cert and key else (None, None)
    # Before any side effect: a remote admin bind without TLS would serve
    # /admin (password, bearer, setup token) over plain HTTP. Refusing here
    # also means no data listener and no data-dir mutation on a bad config.
    admin_server.require_tls(admin_host, tls_cert, tls_key)
    if admin_port != 0 and (admin_host, admin_port) == (data_host, data_port):
        raise VadeError(
            "INVALID_ARGUMENT",
            f"the admin plane and the data plane cannot share {data_host}:{data_port}; "
            "set [core].admin_host to a different port",
        )

    asyncio.run(app.startup())
    install_components(app, strict=strict)

    data_dir = Path(str(app.config.get("core.data_dir", "./data")))
    admin_ctx = getattr(app, "admin", None)
    admin_app = create_admin_app(admin_ctx) if admin_ctx is not None else None
    setup_token_value: str | None = None
    if admin_ctx is not None:
        # D59: uninitialised ⇒ rotate on every start; initialised ⇒ delete any
        # leftover file; VADE_SETUP_TOKEN pinned ⇒ keep exactly that value.
        setup_token_value = ensure_setup_token(data_dir, initialized=admin_ctx.setup.initialized())
    else:
        print(
            "vade: admin plane was not assembled; /admin will not listen",
            file=sys.stderr,
            flush=True,
        )

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

    tls_opts = {"ssl_certfile": cert, "ssl_keyfile": key} if tls_cert else {}
    data_server = uvicorn.Server(
        uvicorn.Config(
            core_app,
            host=data_host,
            port=data_port,
            log_level="warning",
            # uvicorn's ProxyHeadersMiddleware trusts X-Forwarded-For from
            # 127.0.0.1 by default and rewrites scope["client"]. That silently
            # defeats the pair-code rate limit (any client can rotate the header).
            # Behind a real reverse proxy, enable it deliberately AND construct
            # CoreApp(trust_forwarded_for=True).
            proxy_headers=False,
            **tls_opts,
        )
    )

    servers: list[Any] = [data_server]
    admin_server_obj = None
    if admin_app is not None:
        admin_server_obj = admin_server.make_server(
            admin_app, host=admin_host, port=admin_port, cert=tls_cert, key=tls_key,
        )
        servers.append(admin_server_obj)

    ready: dict[str, Any] = {}
    ws_scheme, http_scheme = ("wss", "https") if tls_cert else ("ws", "http")

    async def serve_until_stopped() -> None:
        # `uvicorn.run` blocks with no "ready" hook, so drive `Server` by hand
        # and announce only once every socket is bound: a failed bind must
        # never advertise a live pair code / setup token pointing at nothing.
        loop = asyncio.get_running_loop()
        tasks = [loop.create_task(server.serve()) for server in servers]
        installed: list[int] = []
        try:
            while not all(server.started for server in servers) and not any(t.done() for t in tasks):
                await asyncio.sleep(0.02)
            if all(server.started for server in servers) and not any(server.should_exit for server in servers):
                data_actual = _bound_port(data_server) or data_port
                admin_actual = _bound_port(admin_server_obj) if admin_server_obj is not None else None
                if admin_ctx is not None and admin_actual:
                    _adopt_bound_port(admin_ctx, admin_host, admin_actual)
                if admin_server_obj is not None:
                    admin_url = f"{http_scheme}://{admin_host}:{admin_actual or admin_port}"
                    print(
                        f"vade: listeners up — data {ws_scheme}://{data_host}:{data_actual} admin {admin_url}",
                        flush=True,
                    )
                else:
                    admin_url = None
                    print(f"vade: listener up — data {ws_scheme}://{data_host}:{data_actual}", flush=True)
                announce_pair_code(app.pairing, enabled=pair_banner)
                if admin_url is not None:
                    announce_setup_token(
                        setup_token_value, admin_url=admin_url,
                        token_file=data_dir / "setup.token",
                    )
                ready.update({
                    "data": data_server, "admin": admin_server_obj,
                    "data_host": data_host, "data_port": data_actual,
                    "admin_host": admin_host, "admin_port": admin_actual or admin_port,
                    "admin_url": admin_url, "servers": servers,
                    "setup_token": _mask_setup_token(setup_token_value) if setup_token_value else None,
                })
                # After both `serve()` coroutines installed their own handlers:
                # ours must be the process handler, otherwise SIGTERM only stops
                # whichever plane started last.
                installed = install_shutdown_handlers(loop, servers)
                if on_ready is not None:
                    on_ready(ready)
            await asyncio.gather(*tasks)
        except BaseException:
            # One plane failed to bind: stop the other instead of leaving a
            # half-alive core that looks healthy from one port.
            for server in servers:
                server.should_exit = True
            pending = [task for task in tasks if not task.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise
        finally:
            for sig in installed:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError, ValueError, OSError):
                    pass

    try:
        asyncio.run(serve_until_stopped())
    finally:
        try:
            asyncio.run(app.shutdown())
        except Exception:  # noqa: BLE001 - cleanup must not mask the real error
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
