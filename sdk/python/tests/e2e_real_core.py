"""G1 end-to-end: a REAL core (uvicorn + Noise) registers the echo module and calls its tools.

Not collected by pytest (filename does not start with `test_`): it needs the
Rust binding, uvicorn and two free ports. Run it explicitly:

    PYTHONPATH=core:protocol/core/python python3 sdk/python/tests/e2e_real_core.py

What it proves, in order:
  1. module/register over Noise IK, with the module key whitelisted beforehand
  2. com.example.echo/echo returns {"text","len"} through core's Router
  3. a ToolError keeps its code all the way back (boom -> INTERNAL)
  4. a tool that exceeds its deadline comes back as TIMEOUT
  5. tools/cancel on an async tool comes back as CANCELLED
  6. module/drain is acknowledged and the module exits 0
  7. after core restarts, the module re-registers without intervention

The core data dir must be prepared by `prepare()` below (key + whitelist). That
is the honest stand-in for pairing: core's listener only builds IK responders,
so a module key has to be trusted out of band (documented in PYTHON_SDK.md §5).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HOST = "127.0.0.1"
PORT = int(os.environ.get("VADE_E2E_PORT", "18443"))
URL = f"ws://{HOST}:{PORT}"


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


async def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="vade-e2e-"))
    data = work / "core-data"
    data.mkdir(parents=True)
    config = work / "core.toml"
    config.write_text(
        "\n".join(
            [
                "[core]",
                f'listen = "{HOST}:{PORT}"',
                f'data_dir = "{data}"',
                'log_level = "warning"',
                "",
                "[dev]",
                "allow_missing_components = true",
                "allow_all = true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO)]
    # The Noise binding lives next to its .so; a built copy is passed in rather
    # than generated here (no cargo in this environment).
    binding_dir = os.environ.get("VADE_PROTOCOL_PY")
    if binding_dir:
        sys.path.insert(0, binding_dir)

    from vade.__main__ import build_module_crypto
    from vade.app import App, install_components, make_rpc_handler
    from vade.config import load_config
    from vade.transport.server import CoreApp
    import uvicorn

    from vade_sdk.transport import fingerprint_of, load_or_create_key

    module_key_path = work / "module.key"
    _seed, module_public = load_or_create_key(module_key_path)
    env = dict(os.environ)
    env["VADE_PROTOCOL_PY"] = binding_dir or ""
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                [
                    str(REPO / "sdk" / "python"),
                    str(REPO / "modules" / "echo" / "src"),
                    binding_dir or str(REPO / "protocol" / "core" / "python"),
                    env.get("PYTHONPATH", ""),
                ]
            ),
            "MODULE_KEY_PATH": str(module_key_path),
            "VADE_DATA_DIR": str(work / "module-data"),
        }
    )

    cfg = load_config(toml_path=str(config))
    app = App(cfg)
    await app.startup()
    install_components(app, strict=False)
    app.whitelist.add(
        "com.example.echo",
        "main",
        fingerprint_of(module_public),
        public_key=module_public,
    )
    env["CORE_PUBLIC_KEY"] = app.identity.public_key.hex()

    core_app = CoreApp(
        config=app.config,
        identity=app.identity,
        registry=app.registry,
        pairing=app.pairing,
        make_crypto=build_module_crypto(app),
        on_rpc=make_rpc_handler(app),
        policy_mode="allow_all",
    )
    uvicorn_config = uvicorn.Config(core_app, host=HOST, port=PORT, log_level="warning", proxy_headers=False)
    server = uvicorn.Server(uvicorn_config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "core did not start"

    log_path = work / "echo.log"
    log_file = log_path.open("w", encoding="utf-8")
    module = subprocess.Popen(
        [sys.executable, "-m", "echo_module", "--core", URL],
        cwd=REPO / "modules" / "echo",
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}", flush=True)
        if not condition:
            failures.append(name)

    try:
        # 1. registration
        _banner("register")
        record = None
        deadline = time.time() + 20
        while time.time() < deadline:
            for item in app.registry.snapshot():
                if item.module_id == "com.example.echo" and item.connection_id:
                    record = item
                    break
            if record is not None:
                break
            if module.poll() is not None:
                break
            await asyncio.sleep(0.1)
        check("module registered", record is not None, f"tools={getattr(record, 'tool_names', None)}")
        if record is None:
            print(log_path.read_text(encoding="utf-8")[-3000:])
            return 1

        # 2. echo through core's router
        _banner("tool call: echo")
        result = await app.router.call("com.example.echo/echo", {"text": "hello"}, session_id="e2e")
        check("echo ok", result.ok and result.result == {"text": "hello", "len": 5}, repr(result.result))

        # 3. structured error keeps its code
        _banner("tool call: boom")
        failed = await app.router.call("com.example.echo/boom", {"message": "kaboom"}, session_id="e2e")
        check(
            "boom keeps INTERNAL",
            (not failed.ok) and failed.code == "INTERNAL" and failed.message == "kaboom",
            f"code={failed.code} msg={failed.message}",
        )

        # 4. deadline -> TIMEOUT (module sleeps 3000ms, core deadline 300ms)
        _banner("tool call: sleep (deadline)")
        started = time.monotonic()
        timed_out = await app.router.call(
            "com.example.echo/sleep", {"ms": 3000}, session_id="e2e", deadline_ms=300
        )
        elapsed = time.monotonic() - started
        check("sleep times out", (not timed_out.ok) and timed_out.code == "TIMEOUT", f"code={timed_out.code} in {elapsed:.2f}s")

        # 5. cancel (async tool) — through the router, so core really emits
        #    `tools/cancel` for this call (a notify we invent here would only
        #    test our own test).
        _banner("tool call: sleep (cancel)")
        cancel_call = asyncio.create_task(
            app.router.call(
                "com.example.echo/sleep", {"ms": 5000}, session_id="e2e", turn_id="e2e-turn", deadline_ms=10_000
            )
        )
        await asyncio.sleep(0.4)
        app.router.cancel_turn("e2e-turn")
        cancelled = await cancel_call
        check("sleep cancelled", (not cancelled.ok) and cancelled.code == "CANCELLED", f"code={cancelled.code}")

        # 6. drain
        _banner("drain")
        connection = None
        connections = app.transport["connections"] if isinstance(app.transport, dict) else getattr(app.transport, "CONNECTIONS", {})
        for item in connections.values():
            if getattr(item, "crypto", None) is not None and item.connection_id == record.connection_id:
                connection = item
        if connection is not None:
            ack = await connection.request("module/drain", {"grace_ms": 1000}, timeout_s=5)
            check("drain acknowledged", ack == {"accepted": True}, repr(ack))
            for _ in range(100):
                if module.poll() is not None:
                    break
                await asyncio.sleep(0.05)
            check("module exited 0", module.wait(timeout=10) == 0, f"rc={module.returncode}")
        else:
            check("drain acknowledged", False, "connection object not found")

        # 7. restart + automatic re-registration
        _banner("reconnect after core restart")
        if module.poll() is not None:
            module = subprocess.Popen(
                [sys.executable, "-m", "echo_module", "--core", URL],
                cwd=REPO / "modules" / "echo",
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            await asyncio.sleep(1.0)
        server.should_exit = True
        await serve_task
        for _ in range(40):
            if not server.started:
                break
            await asyncio.sleep(0.05)

        server2 = uvicorn.Server(uvicorn_config)
        serve_task2 = asyncio.create_task(server2.serve())
        for _ in range(200):
            if server2.started:
                break
            await asyncio.sleep(0.05)
        reconnected_at = None
        started_at = time.monotonic()
        while time.monotonic() - started_at < 15:
            for item in app.registry.snapshot():
                if item.module_id == "com.example.echo" and item.connection_id and item.connection_id != record.connection_id:
                    reconnected_at = time.monotonic() - started_at
                    break
            if reconnected_at is not None:
                break
            await asyncio.sleep(0.1)
        check("re-registered < 5s", reconnected_at is not None and reconnected_at < 5.0, f"{reconnected_at}")
        if reconnected_at is not None:
            again = await app.router.call("com.example.echo/echo", {"text": "again"}, session_id="e2e")
            check("echo works after reconnect", again.ok, repr(again.result))
        server2.should_exit = True
        await serve_task2
    finally:
        if module.poll() is None:
            module.send_signal(signal.SIGTERM)
            try:
                module.wait(timeout=5)
            except subprocess.TimeoutExpired:
                module.kill()
        log_file.close()
        print("\n--- echo module log (tail) ---")
        print("\n".join(log_path.read_text(encoding="utf-8").splitlines()[-25:]))
        shutil.copy(log_path, Path("/tmp/vade-e2e-echo.log"))

    _banner("result")
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
