"""`python -m vade` really starts the admin plane, and the setup token obeys D59.

This module pins behaviour, not pieces:

* both listeners come up in one process, on the configured
  `[core].admin_host` — including `:0`, where the Host allow-list and the
  `bind_url` must follow the port the OS actually gave us;
* one SIGTERM sets `should_exit` on **both** servers (uvicorn's per-server
  handler would otherwise stop only the last one);
* a non-loopback admin bind without TLS refuses to start at all;
* `setup.token` is generated on first start, rotated on every uninitialised
  start, pinned by `VADE_SETUP_TOKEN`, and removed once `admin.json` exists;
* the startup banner carries only the mask — never the plaintext token.
"""

from __future__ import annotations

import io
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from vade.__main__ import _serve, announce_setup_token, install_shutdown_handlers, setup_banner_lines
from vade.admin import server as admin_server
from vade.admin import setup_token
from vade.app import App
from vade.config import load_config
from vade.errors import VadeError

ROOT = Path(__file__).resolve().parents[2]
TOKEN_SHAPE = re.compile(r"^acs_[A-Za-z0-9_-]{43}$")


# --------------------------------------------------------------------- helpers
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(tmp_path: Path, *, admin_host: str = "127.0.0.1:0", listen: str = "127.0.0.1:0"):
    return load_config(cli={
        "core.data_dir": str(tmp_path),
        "core.listen": listen,
        "core.admin_host": admin_host,
    })


def _start_core(tmp_path: Path, *, admin_host: str = "127.0.0.1:0") -> tuple[dict[str, Any], threading.Thread, list[BaseException]]:
    """Run `_serve` in a worker thread; return once both sockets are bound."""
    import uvicorn

    app = App(_config(tmp_path, admin_host=admin_host))
    ready: dict[str, Any] = {}
    event = threading.Event()
    errors: list[BaseException] = []

    def on_ready(info: dict[str, Any]) -> None:
        ready.update(info)
        event.set()

    def run() -> None:
        try:
            _serve(app, uvicorn, strict=False, pair_banner=False, on_ready=on_ready)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            errors.append(exc)
            event.set()

    thread = threading.Thread(target=run, name="vade-serve", daemon=True)
    thread.start()
    deadline = time.monotonic() + 45
    while not event.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not event.is_set():
        raise AssertionError(f"core did not become ready (errors={errors!r})")
    if errors:
        raise AssertionError(f"core failed before becoming ready: {errors!r}")
    return ready, thread, errors


def _stop_core(ready: dict[str, Any], thread: threading.Thread, errors: list[BaseException]) -> None:
    for server in ready["servers"]:
        server.should_exit = True
    thread.join(30)
    assert not thread.is_alive(), "listeners did not stop after should_exit"
    assert errors == [], f"core raised during shutdown: {errors!r}"


def _port_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


# -------------------------------------------------------------- bind parsing
def test_parse_bind_reads_host_and_port():
    assert admin_server.parse_bind("127.0.0.1:8444") == ("127.0.0.1", 8444)
    assert admin_server.parse_bind("0.0.0.0:0") == ("0.0.0.0", 0)
    assert admin_server.parse_bind(None) == ("127.0.0.1", admin_server.ADMIN_PORT)
    assert admin_server.parse_bind("") == ("127.0.0.1", admin_server.ADMIN_PORT)
    # A bare host must not silently inherit the data-plane port.
    assert admin_server.parse_bind("localhost") == ("localhost", admin_server.ADMIN_PORT)
    assert admin_server.parse_bind("[::1]:8444") == ("::1", 8444)
    with pytest.raises(VadeError) as excinfo:
        admin_server.parse_bind("127.0.0.1:not-a-port")
    assert excinfo.value.code == "INVALID_ARGUMENT"


def test_admin_plane_must_not_take_the_data_port():
    with pytest.raises(VadeError) as excinfo:
        admin_server.assert_isolated("127.0.0.1", admin_server.DATA_PORT)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert "8443" in excinfo.value.message


# ------------------------------------------------------------ TLS enforcement
def test_make_server_refuses_a_remote_plane_without_tls():
    with pytest.raises(VadeError) as excinfo:
        admin_server.make_server(object(), host="0.0.0.0", port=8444, cert=None, key=None)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert "TLS" in excinfo.value.message
    assert "plain HTTP" in excinfo.value.message


def test_make_server_allows_loopback_without_tls():
    server = admin_server.make_server(object(), host="127.0.0.1", port=0, cert=None, key=None)
    assert server.config.host == "127.0.0.1"


def test_make_server_allows_a_remote_plane_when_tls_is_configured(tmp_path):
    server = admin_server.make_server(
        object(), host="0.0.0.0", port=8444,
        cert=tmp_path / "server.crt", key=tmp_path / "server.key",
    )
    assert server.config.host == "0.0.0.0"
    assert server.config.ssl_certfile == str(tmp_path / "server.crt")


def test_serve_refuses_an_admin_port_that_is_the_data_port(tmp_path):
    data_dir = tmp_path / "data"
    config = load_config(cli={
        "core.data_dir": str(data_dir),
        "core.listen": "127.0.0.1:9001",
        "core.admin_host": "127.0.0.1:9001",
    })
    import uvicorn

    with pytest.raises(VadeError) as excinfo:
        _serve(App(config), uvicorn, strict=False, pair_banner=False)
    assert "cannot share" in excinfo.value.message
    assert not data_dir.exists()


def test_serve_refuses_remote_admin_before_any_side_effect(tmp_path):
    data_dir = tmp_path / "data"
    config = load_config(cli={
        "core.data_dir": str(data_dir),
        "core.listen": "127.0.0.1:0",
        "core.admin_host": "0.0.0.0:8444",
    })
    import uvicorn

    with pytest.raises(VadeError) as excinfo:
        _serve(App(config), uvicorn, strict=False, pair_banner=False)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert "TLS" in excinfo.value.message
    # Refused before identity/pairing/token creation: no half-started core.
    assert not data_dir.exists()


# --------------------------------------------------------- setup token (D59)
def test_first_start_mints_a_47_char_token_with_private_modes(tmp_path):
    token = setup_token.ensure_setup_token(tmp_path, env={})
    assert token is not None and TOKEN_SHAPE.match(token)
    path = tmp_path / "setup.token"
    assert path.read_text(encoding="ascii") == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_uninitialised_restart_rotates_and_invalidates_the_old_value(tmp_path):
    first = setup_token.ensure_setup_token(tmp_path, env={})
    second = setup_token.ensure_setup_token(tmp_path, env={})
    assert first and second and first != second
    assert setup_token.read_token(tmp_path) == second
    # The old value is dead, not merely superseded: comparison is against the
    # current file, with no history window.
    assert setup_token.compare(setup_token.read_token(tmp_path), first) is False
    assert setup_token.compare(second, first) is False


def test_env_pin_is_written_back_and_never_rotated(tmp_path):
    pinned = "acs_" + "p" * 43
    env = {"VADE_SETUP_TOKEN": pinned}
    assert setup_token.ensure_setup_token(tmp_path, env=env) == pinned
    assert setup_token.read_token(tmp_path) == pinned
    assert setup_token.ensure_setup_token(tmp_path, env=env) == pinned
    assert setup_token.read_token(tmp_path) == pinned


def test_initialised_core_drops_a_leftover_token(tmp_path):
    setup_token.ensure_setup_token(tmp_path, env={})
    assert (tmp_path / "setup.token").exists()
    assert setup_token.ensure_setup_token(tmp_path, env={}, initialized=True) is None
    assert not (tmp_path / "setup.token").exists()
    # Idempotent: a second start after cleanup must not raise.
    assert setup_token.ensure_setup_token(tmp_path, env={}, initialized=True) is None


# --------------------------------------------------------------- setup banner
def test_setup_banner_prints_the_mask_never_the_token():
    token = "acs_" + "A1b2" * 10 + "xyz"
    out = io.StringIO()
    assert announce_setup_token(token, admin_url="http://127.0.0.1:8444",
                                token_file="/data/setup.token", stream=out) == setup_token.mask(token)
    text = out.getvalue()
    assert token not in text, "the plaintext setup token reached the startup banner"
    assert f"setup_token={setup_token.mask(token)}" in text
    assert "/data/setup.token" in text
    assert "print-setup-token" in text
    assert "http://127.0.0.1:8444/admin/setup" in text


def test_setup_banner_is_silent_once_initialised():
    out = io.StringIO()
    assert announce_setup_token(None, admin_url="https://127.0.0.1:8444",
                                token_file="/data/setup.token", stream=out) is None
    assert out.getvalue() == ""


def test_setup_banner_lines_are_machine_parseable():
    token = "acs_" + "Q" * 43
    machine, human = setup_banner_lines(token, admin_url="https://h:8444", token_file="/d/setup.token")
    fields = dict(part.split("=", 1) for part in machine.split(" ") if "=" in part)
    assert fields["setup_token"] == setup_token.mask(token)
    assert fields["setup_path"] == "/d/setup.token"
    assert token not in machine and token not in human


# -------------------------------------------------------- SIGTERM propagation
class _FakeLoop:
    def __init__(self) -> None:
        self.handlers: dict[int, Callable[..., None]] = {}

    def add_signal_handler(self, sig: int, callback: Callable[..., None], *args: Any) -> None:
        self.handlers[sig] = callback

    def remove_signal_handler(self, sig: int) -> bool:
        return self.handlers.pop(sig, None) is not None


class _FakeServer:
    started = True

    def __init__(self) -> None:
        self.should_exit = False


def test_one_sigterm_stops_every_listener():
    loop = _FakeLoop()
    data, admin = _FakeServer(), _FakeServer()
    installed = install_shutdown_handlers(loop, [data, admin])

    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    loop.handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert data.should_exit is True
    assert admin.should_exit is True


def test_a_sigint_also_stops_every_listener():
    loop = _FakeLoop()
    servers = [_FakeServer(), _FakeServer()]
    install_shutdown_handlers(loop, servers)
    loop.handlers[signal.SIGINT](signal.SIGINT, None)
    assert [server.should_exit for server in servers] == [True, True]


# --------------------------------------------------- both listeners on one loop
def test_serve_starts_a_working_admin_listener_beside_the_data_plane(tmp_path, monkeypatch):
    monkeypatch.delenv("VADE_SETUP_TOKEN", raising=False)
    ready, thread, errors = _start_core(tmp_path)
    try:
        admin_port = ready["admin_port"]
        data_port = ready["data_port"]
        assert admin_port != data_port

        health = httpx.get(f"http://127.0.0.1:{admin_port}/admin/api/health", timeout=10)
        assert health.status_code == 200, health.text
        assert health.json()["ok"] is True
        # The Host allow-list must have followed the kernel-assigned port; a
        # stale `127.0.0.1:0` would answer 403 ORIGIN_MISMATCH here.
        assert httpx.get(f"http://127.0.0.1:{data_port}/healthz", timeout=10).status_code == 200

        setup = httpx.get(f"http://127.0.0.1:{admin_port}/admin/setup", timeout=10)
        assert setup.status_code == 200
        body = setup.json()
        assert body["setup_required"] is True and body["requires_token"] is False
        assert f":{admin_port}" in body["bind_url"], "bind_url still points at the :0 placeholder"

        token_file = tmp_path / "setup.token"
        assert token_file.exists()
        token = token_file.read_text(encoding="ascii")
        assert ready["setup_token"] == setup_token.mask(token)
    finally:
        _stop_core(ready, thread, errors)

    assert _port_closed(ready["admin_port"]) and _port_closed(ready["data_port"])


def test_initialised_core_does_not_banner_or_keep_a_stale_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VADE_SETUP_TOKEN", raising=False)
    (tmp_path / "admin").mkdir(parents=True)
    (tmp_path / "admin" / "admin.json").write_text('{"version": 1, "password": "x"}', encoding="utf-8")
    (tmp_path / "setup.token").write_text("acs_" + "s" * 43, encoding="utf-8")

    ready, thread, errors = _start_core(tmp_path)
    try:
        assert not (tmp_path / "setup.token").exists(), "a token left over after setup must be removed"
        health = httpx.get(f"http://127.0.0.1:{ready['admin_port']}/admin/api/health", timeout=10)
        assert health.status_code == 200
    finally:
        _stop_core(ready, thread, errors)

    assert "setup_token=" not in capsys.readouterr().out


# ------------------------------------------------------------- real SIGTERM e2e
def _spawn_core(cwd: Path, data_port: int, admin_port: int) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "core"), str(ROOT / "protocol" / "core" / "python")])
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("VADE_SETUP_TOKEN", None)
    return subprocess.Popen(
        [sys.executable, "-m", "vade",
         "--bind", f"127.0.0.1:{data_port}",
         "--admin-bind", f"127.0.0.1:{admin_port}",
         "--no-pair-banner"],
        cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
    )


def _wait_for_http(url: str, proc: subprocess.Popen[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"core exited before {url} answered (rc={proc.returncode})")
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {url}")


def test_python_m_vade_admin_listener_stops_cleanly_on_sigterm(tmp_path):
    data_port, admin_port = _free_port(), _free_port()
    proc = _spawn_core(tmp_path, data_port, admin_port)
    try:
        _wait_for_http(f"http://127.0.0.1:{admin_port}/admin/api/health", proc)
        assert httpx.get(f"http://127.0.0.1:{data_port}/healthz", timeout=5).status_code == 200
        proc.terminate()
        assert proc.wait(timeout=30) == 0, "SIGTERM must be a graceful stop for both planes"
        out = proc.stdout.read() if proc.stdout is not None else ""
        # The banner exists, is masked, and the real token is not in stdout.
        token = (tmp_path / "data" / "setup.token").read_text(encoding="ascii")
        assert f"setup_token={setup_token.mask(token)}" in out
        assert token not in out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert _port_closed(data_port) and _port_closed(admin_port)
