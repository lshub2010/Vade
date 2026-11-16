"""Alpha A1-2: first-run pairing must work from a plain `python -m vade`.

A browser only attaches after `POST /ui/session` redeems a `purpose=browser`
pair code. Without the `__Host-ac_ui` cookie the core treats the socket as a
device and demands a Noise handshake (`transport/server.py`), so a JSON client
cannot get in. Standard startup had no way to mint or see such a code — the
user could start the core and still never pair a browser.

What is pinned here:

* the banner line is machine-parseable (`pair_code=… purpose=browser
  expires_in=600s`), printed after the listener is up;
* the printed code redeems exactly once through the real `/ui/session` route;
* expiry, purpose separation and the "pairing disabled" path are readable;
* `--no-pair-banner` prints nothing and mints nothing;
* the code is never written to a log record or to a file (only its scrypt hash
  reaches `pairing.sqlite3`);
* existing flags keep their defaults, and read-only commands never mint;
* a real `python -m vade` subprocess prints a code that a real HTTP client can
  redeem — the end-to-end shape of the alpha blocker.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from vade.__main__ import (
    PAIR_BANNER_TTL,
    _serve,
    announce_pair_code,
    build_parser,
    main,
    mint_browser_pair_code,
    pair_banner_lines,
)
from vade.config import load_config
from vade.errors import VadeError
from vade.identity.pairing import PairingStore, Purpose, code_shape_ok, normalize
from vade.transport.server import CoreApp

BANNER_RE = re.compile(
    r"^pair_code=(?P<code>\S+) purpose=(?P<purpose>browser|module) expires_in=(?P<ttl>\d+)s$"
)
ROOT = Path(__file__).resolve().parents[2]


class _Clock:
    """Controllable clock for `PairingStore` (no sleeping in tests)."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(tmp_path: Path, clock: Callable[[], float] | None = None) -> PairingStore:
    return PairingStore(tmp_path / "pairing.sqlite3", clock=clock or time.time)


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "core.toml"
    path.write_text(
        "[core]\n"
        f'data_dir = {json.dumps(tmp_path.as_posix())}\n'
        'listen = "127.0.0.1:0"\n',
        encoding="utf-8",
    )
    return path


def _parse_banner(line: str) -> re.Match[str]:
    match = BANNER_RE.match(line)
    if match is None:
        raise AssertionError("stdout line is not the documented pair banner shape (value withheld)")
    return match


def _assert_absent(blob: bytes, code: str, where: str) -> None:
    """Never echo the code in a failure message."""
    if code.encode("utf-8") in blob:
        raise AssertionError(f"pair code leaked into {where}")


# ---------------------------------------------------------------- banner shape
def test_banner_line_is_machine_parseable(tmp_path):
    store = _store(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    try:
        code = announce_pair_code(store, stream=out, error_stream=err)
    finally:
        store.close()
    assert code is not None
    lines = out.getvalue().splitlines()
    assert len(lines) == 2, "expected the machine line plus one human hint"
    match = _parse_banner(lines[0])
    assert match.group("code") == code
    assert match.group("purpose") == "browser"
    assert int(match.group("ttl")) == PAIR_BANNER_TTL == 600
    assert code_shape_ok(normalize(code))
    assert "single-use" in lines[1].lower()
    assert "--no-pair-banner" in lines[1]
    assert err.getvalue() == ""


def test_pair_banner_lines_carries_purpose_and_ttl():
    machine, _human = pair_banner_lines("青松-AAAAAA", purpose="browser", ttl=600)
    assert machine == "pair_code=青松-AAAAAA purpose=browser expires_in=600s"
    match = _parse_banner(machine)
    assert match.group("purpose") == "browser" and match.group("ttl") == "600"


def test_each_announcement_mints_a_fresh_code(tmp_path):
    store = _store(tmp_path)
    try:
        first = mint_browser_pair_code(store)
        second = mint_browser_pair_code(store)
        assert first != second
        assert store.consume(first, Purpose.BROWSER, ip="127.0.0.1").purpose is Purpose.BROWSER
        assert store.consume(second, Purpose.BROWSER, ip="127.0.0.1").purpose is Purpose.BROWSER
    finally:
        store.close()


# ------------------------------------------------------------- redemption path
async def _post_session(app: CoreApp, code: str) -> tuple[dict[str, Any], dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    body = json.dumps({"pair_code": code, "client_name": "pytest"}).encode()
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        frames.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/ui/session",
            "headers": [],
            # A source address is mandatory now: `ip is None` is refused
            # (fail closed), so a scope without one can never redeem a code.
            "client": ("127.0.0.1", 54321),
        },
        receive,
        send,
    )
    start = next(frame for frame in frames if frame["type"] == "http.response.start")
    payload = next(frame for frame in frames if frame["type"] == "http.response.body")
    return start, payload


def test_printed_code_unlocks_a_browser_session_exactly_once(tmp_path):
    store = _store(tmp_path)
    out = io.StringIO()
    code = announce_pair_code(store, stream=out, error_stream=io.StringIO())
    assert code is not None

    config = load_config(cli={"core.data_dir": str(tmp_path)})
    core = CoreApp(config=config, pairing=store)
    try:
        start, payload = asyncio.run(_post_session(core, code))
        assert start["status"] == 200, payload
        assert json.loads(payload["body"])["device_id"].startswith("ui-")
        cookies = [value for key, value in start["headers"] if key.lower() == b"set-cookie"]
        assert any(b"__Host-ac_ui=" in value for value in cookies)

        # The same code cannot be redeemed twice — the client's second login
        # attempt must be refused rather than silently reusing the session.
        again, again_payload = asyncio.run(_post_session(core, code))
        assert again["status"] == 400
        assert json.loads(again_payload["body"])["code"] == "CODE_INVALID"
    finally:
        store.close()


def test_printed_code_is_single_use_at_the_store(tmp_path):
    store = _store(tmp_path)
    try:
        code = mint_browser_pair_code(store)
        assert store.consume(code, Purpose.BROWSER, ip="127.0.0.1")
        with pytest.raises(VadeError) as excinfo:
            store.consume(code, Purpose.BROWSER, ip="127.0.0.1")
        assert excinfo.value.code == "CODE_INVALID"
    finally:
        store.close()


# --------------------------------------------------------------------- expiry
def test_printed_code_expires_after_its_ttl(tmp_path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    try:
        code = mint_browser_pair_code(store, ttl=600)
        clock.advance(601)
        with pytest.raises(VadeError) as excinfo:
            store.consume(code, Purpose.BROWSER, ip="127.0.0.1")
        assert excinfo.value.code == "CODE_EXPIRED"
    finally:
        store.close()


def test_zero_ttl_banner_code_is_dead_on_the_next_tick(tmp_path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    out = io.StringIO()
    try:
        code = announce_pair_code(store, ttl=0, stream=out, error_stream=io.StringIO())
        assert code is not None
        assert _parse_banner(out.getvalue().splitlines()[0]).group("ttl") == "0"
        clock.advance(1)
        with pytest.raises(VadeError) as excinfo:
            store.consume(code, Purpose.BROWSER, ip="127.0.0.1")
        assert excinfo.value.code == "CODE_EXPIRED"
    finally:
        store.close()


# ------------------------------------------------------------------ purposes
def test_browser_code_cannot_be_consumed_as_a_module_code(tmp_path):
    store = _store(tmp_path)
    try:
        code = mint_browser_pair_code(store)
        with pytest.raises(VadeError) as excinfo:
            store.consume(code, Purpose.MODULE, ip="127.0.0.1", fingerprint="a" * 16)
        assert excinfo.value.code == "CODE_INVALID"
    finally:
        store.close()


def test_module_code_is_not_accepted_as_a_browser_code(tmp_path):
    store = _store(tmp_path)
    try:
        code = store.create(Purpose.MODULE, module_id="com.example.fs")
        with pytest.raises(VadeError) as excinfo:
            store.consume(code, Purpose.BROWSER, ip="127.0.0.1")
        assert excinfo.value.code == "CODE_INVALID"
    finally:
        store.close()


# ------------------------------------------------------- disabled / readable
def test_pairing_disabled_reports_a_readable_error():
    out, err = io.StringIO(), io.StringIO()
    assert announce_pair_code(None, stream=out, error_stream=err) is None
    assert out.getvalue() == ""
    message = err.getvalue()
    assert "CODE_INVALID" in message
    assert "pairing is not configured" in message
    assert "Traceback" not in message


def test_mint_without_a_pairing_store_raises_a_c04_error():
    with pytest.raises(VadeError) as excinfo:
        mint_browser_pair_code(None)
    assert excinfo.value.code == "CODE_INVALID"
    assert "pairing is not configured" in excinfo.value.message


def test_unexpected_mint_failure_is_readable_and_keeps_the_core_alive():
    class Broken:
        def create(self, purpose, ttl=None):
            raise RuntimeError("pairing store is unavailable")

    out, err = io.StringIO(), io.StringIO()
    assert announce_pair_code(Broken(), stream=out, error_stream=err) is None
    assert out.getvalue() == ""
    assert "pair banner failed" in err.getvalue()
    assert "RuntimeError" in err.getvalue()
    assert "Traceback" not in err.getvalue()


# ----------------------------------------------------- suppression and wiring
def test_no_pair_banner_prints_nothing_and_mints_nothing(tmp_path):
    store = _store(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    assert announce_pair_code(store, enabled=False, stream=out, error_stream=err) is None
    assert out.getvalue() == "" and err.getvalue() == ""
    store.close()
    with sqlite3.connect(tmp_path / "pairing.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM pair_codes").fetchone()[0] == 0


def test_no_pair_banner_flag_reaches_the_server(monkeypatch, tmp_path):
    calls: list[dict[str, Any]] = []

    def fake_serve(app: Any, uvicorn: Any, *, strict: bool, pair_banner: bool = True) -> int:
        calls.append({"strict": strict, "pair_banner": pair_banner})
        return 0

    monkeypatch.setattr("vade.__main__._serve", fake_serve)
    config = _write_config(tmp_path)

    assert main(["--config", str(config), "--no-pair-banner"]) == 0
    assert calls[-1] == {"strict": False, "pair_banner": False}
    assert main(["--config", str(config)]) == 0
    assert calls[-1] == {"strict": False, "pair_banner": True}
    # Default is unchanged: a plain start still announces.
    assert inspect.signature(_serve).parameters["pair_banner"].default is True


# ------------------------------------------------------ secrets stay out of logs
def test_pair_code_never_reaches_a_log_or_a_file(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    store = _store(tmp_path)
    out = io.StringIO()
    try:
        code = announce_pair_code(store, stream=out, error_stream=io.StringIO())
    finally:
        store.close()
    assert code is not None

    # The banner is printed, not logged: no record may carry the live code.
    assert [record for record in caplog.records if "pair_code=" in record.getMessage()] == []
    _assert_absent(caplog.text.encode("utf-8"), code, "the log stream")
    # Prove the capture is wired (a negative alone could be vacuous).
    logging.getLogger("vade.probe").warning("capture probe %s", "NOT-A-REAL-CODE")
    assert any("NOT-A-REAL-CODE" in record.getMessage() for record in caplog.records)

    # Nor may it land on disk: pairing.sqlite3 keeps only the scrypt hash.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            _assert_absent(path.read_bytes(), code, str(path.name))
    with sqlite3.connect(tmp_path / "pairing.sqlite3") as db:
        rows = db.execute("SELECT code_hash FROM pair_codes").fetchall()
    assert len(rows) == 1
    _assert_absent(rows[0][0].encode("utf-8"), code, "the pairing row")


# ------------------------------------------------------------------ regression
def test_startup_flags_keep_their_defaults():
    parser = build_parser()
    plain = parser.parse_args([])
    assert plain.no_pair_banner is False
    assert plain.bind is None and plain.config is None and plain.san is None
    assert plain.print_fingerprint is False and plain.print_tls_fingerprint is False
    assert plain.strict is False
    assert plain.command is None

    explicit = parser.parse_args(["--bind", "127.0.0.1:9000", "--strict", "--no-pair-banner"])
    assert explicit.bind == "127.0.0.1:9000"
    assert explicit.strict is True and explicit.no_pair_banner is True

    admin = parser.parse_args(["admin", "print-fingerprint"])
    assert admin.command == "admin" and admin.args == ["print-fingerprint"]


def test_read_only_commands_never_mint_a_code(tmp_path, capsys):
    from vade.identity.store import load_or_create

    load_or_create(tmp_path)  # core.key, so --print-fingerprint can answer
    config = _write_config(tmp_path)
    assert main(["--config", str(config), "--print-fingerprint"]) == 0
    captured = capsys.readouterr()
    assert "pair_code=" not in captured.out
    assert not (tmp_path / "pairing.sqlite3").exists()


# ------------------------------------------------------------------- end to end
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _CoreProcess:
    """A real `python -m vade` with stdout/stderr drained on threads."""

    def __init__(self, tmp_path: Path, port: int, *extra: str) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "core"), str(ROOT / "protocol" / "core" / "python")]
        )
        env["PYTHONIOENCODING"] = "utf-8"
        self.port = port
        self.lines: list[str] = []
        self.errors: list[str] = []
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "vade", "--bind", f"127.0.0.1:{port}",
             # The core now also starts the admin listener; port 0 keeps this
             # test from colliding with a real core (or a parallel test) on 8444.
             "--admin-bind", "127.0.0.1:0", *extra],
            cwd=str(tmp_path),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self._threads = [
            threading.Thread(target=self._drain, args=(self.proc.stdout, self.lines), daemon=True),
            threading.Thread(target=self._drain, args=(self.proc.stderr, self.errors), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    @staticmethod
    def _drain(stream: Any, sink: list[str]) -> None:
        for line in stream:
            sink.append(line.rstrip("\n"))

    def wait_for(self, predicate: Callable[[], Any], timeout: float = 45.0, what: str = "condition") -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if self.proc.poll() is not None:
                raise AssertionError(f"core exited before {what} (stdout/stderr withheld)")
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what} (stdout/stderr withheld)")

    def healthz(self) -> bool:
        try:
            return httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def test_python_m_vade_prints_a_code_a_browser_can_redeem(tmp_path):
    port = _free_port()
    core = _CoreProcess(tmp_path, port)
    try:
        line = core.wait_for(
            lambda: next((item for item in core.lines if item.startswith("pair_code=")), None),
            what="the pair banner",
        )
        code = _parse_banner(line).group("code")
        response = httpx.post(
            f"http://127.0.0.1:{port}/ui/session",
            json={"pair_code": code, "client_name": "e2e"},
            timeout=10,
        )
        assert response.status_code == 200, "the printed code did not open a browser session"
        assert "__Host-ac_ui=" in response.headers.get("set-cookie", "")
        # Second redemption of the same code must fail.
        again = httpx.post(
            f"http://127.0.0.1:{port}/ui/session",
            json={"pair_code": code},
            timeout=10,
        )
        assert again.status_code == 400
        assert again.json()["code"] == "CODE_INVALID"
    finally:
        core.stop()


def test_python_m_vade_no_pair_banner_starts_without_a_code_line(tmp_path):
    port = _free_port()
    core = _CoreProcess(tmp_path, port, "--no-pair-banner")
    try:
        core.wait_for(core.healthz, what="the listener")
        time.sleep(0.3)  # give a (wrong) banner a chance to appear
        assert not [line for line in core.lines if line.startswith("pair_code=")]
        with sqlite3.connect(tmp_path / "data" / "pairing.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM pair_codes").fetchone()[0] == 0
    finally:
        core.stop()
