"""T3 core-half e2e: a REAL model module drives FOUR REAL echo tools in one step.

Not collected by pytest (the filename does not start with ``test_``): it needs
the Rust binding, uvicorn, a free port and two module checkouts. Run it
explicitly:

    python sdk/python/tests/t3_model_tools_e2e.py

It needs an exact binding: build the cdylib
(``protocol/core/python/libvade_protocol.so`` on POSIX,
``protocol/core/python/vade_protocol.dll`` on Windows) in this checkout, or
set ``VADE_PROTOCOL_LIB`` (and optionally ``VADE_PROTOCOL_PY``) to the file you
mean to test. It never borrows another worktree's build silently.

What it proves, in order:

  1. a real core (`App.startup()` + `install_components()` + uvicorn + Noise IK)
     starts with ``[llm] provider = "module"`` and no builtin fallback;
  2. the real ``com.example.model`` module registers and serves ``chat``
     against a stdlib ``http.server`` fake OpenAI-compatible upstream;
  3. the upstream answers the first request with FOUR ``tool_calls`` in ONE
     assistant message, each name resolved from core's frozen snapshot by
     suffix (``echo__echo`` / ``echo__add`` / ``echo__sleep`` / ``echo__boom``)
     — never hard-coded names core did not advertise;
  4. core runs all four against the real ``com.example.echo`` module over
     Noise, one by one: ``echo`` / ``add`` / ``sleep`` succeed and ``boom``
     returns a structured ``INTERNAL`` failure. A failed tool does not fail the
     turn — core sends all four results back as ``tool`` messages;
  5. the upstream's second answer is the final assistant text and the turn
     completes with ``state == "completed"``;
  6. the durable event stream holds four ``turn.tool_call`` and four
     ``turn.tool_result`` events, every call precedes its own result, all four
     results precede ``turn.completed``, and every ``tool_call_id`` matches.

Every module key is whitelisted out of band (same honest stand-in for pairing
as ``sdk/python/tests/e2e_real_core.py`` documents).
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]

HOST = "127.0.0.1"
ECHO_TEXT = "hello-from-t3"
BOOM_MESSAGE = "t3-boom"
FINAL_TEXT = "T3-FINAL: echo, add and sleep ran; boom failed with INTERNAL as designed"

MODEL_MODULE_ID = "com.example.model"
ECHO_MODULE_ID = "com.example.echo"

#: (tool suffix, tool_call id, arguments, expected outcome) in the order the
#: upstream returns them. The actual names come from what core advertised.
CALL_SPECS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("echo", "call_t3_echo_1", {"text": ECHO_TEXT}),
    ("add", "call_t3_echo_2", {"a": 2, "b": 3}),
    ("sleep", "call_t3_echo_3", {"ms": 1}),
    ("boom", "call_t3_echo_4", {"message": BOOM_MESSAGE}),
)

TOOL_SUFFIXES = tuple(suffix for suffix, _, _ in CALL_SPECS)


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


PORT = _free_port()


# --------------------------------------------------------------------- binding
def _shared_lib_name() -> str:
    """The cdylib file name the generated binding looks for beside itself.

    `_uniffi_load_indirect()` in `vade_protocol.py` hardcodes the platform's
    name (``vade_protocol.dll`` on Windows, ``libvade_protocol.so`` on
    POSIX/ELF, ``libvade_protocol.dylib`` on macOS) and loads it from its own
    directory — so whatever we copy into a temp dir must use exactly that name.
    """
    if sys.platform == "darwin":
        return "libvade_protocol.dylib"
    if os.name == "nt":
        return "vade_protocol.dll"
    return "libvade_protocol.so"


SHARED_LIB = _shared_lib_name()


def _check_binding_dir(directory: Path, origin: str) -> Path:
    """Fail loudly when `directory` cannot serve as an importable binding."""
    directory = directory.expanduser().resolve()
    if not (directory / "vade_protocol.py").is_file():
        raise SystemExit(
            f"{origin}={directory} has no vade_protocol.py; point it at the "
            "directory holding both vade_protocol.py and the built "
            f"{SHARED_LIB}."
        )
    if not (directory / SHARED_LIB).is_file():
        raise SystemExit(
            f"{origin}={directory} has no {SHARED_LIB}; that is the name the "
            f"generated binding loads on this platform. Build it (cargo build "
            "--features uniffi --lib in protocol/core) and copy the cdylib next "
            "to vade_protocol.py, or set VADE_PROTOCOL_LIB to the exact file."
        )
    return directory


def _binding_dir(work: Path) -> Path:
    """Locate `vade_protocol.py` + its cdylib, copying them together if needed.

    The generated `.py` is committed; the shared library is not
    (`.gitignore` ignores both `*.so` and `*.dll`). The binding loads
    `SHARED_LIB` from the directory of `vade_protocol.py`, so a worktree
    without a built copy gets a temp dir with both files under the names the
    binding expects.
    """
    explicit = os.environ.get("VADE_PROTOCOL_PY")
    if explicit:
        resolved = _check_binding_dir(Path(explicit), "VADE_PROTOCOL_PY")
        print(f"[info] binding dir (VADE_PROTOCOL_PY): {resolved}", flush=True)
        return resolved

    source = REPO / "protocol" / "core" / "python"
    if (source / SHARED_LIB).is_file():
        print(f"[info] binding lib (in tree): {source / SHARED_LIB}", flush=True)
        return source

    env_lib = os.environ.get("VADE_PROTOCOL_LIB") or os.environ.get("VADE_PROTOCOL_SO")
    if not env_lib:
        raise SystemExit(
            f"no {SHARED_LIB} in this worktree ({source}). Build it "
            "(cargo build --features uniffi --lib in protocol/core) and copy "
            f"the cdylib into {source}, or set VADE_PROTOCOL_LIB to the exact "
            f"{SHARED_LIB} you mean to test (VADE_PROTOCOL_SO is the legacy "
            "name for the same thing; VADE_PROTOCOL_PY points at a directory "
            "holding both files). Refusing to borrow a sibling worktree's "
            "binding silently."
        )
    candidate = Path(env_lib).expanduser().resolve()
    if not candidate.is_file():
        raise SystemExit(f"VADE_PROTOCOL_LIB does not exist: {candidate}")

    target = work / "binding"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "vade_protocol.py", target / "vade_protocol.py")
    try:
        (target / SHARED_LIB).symlink_to(candidate)
    except OSError:
        shutil.copy2(candidate, target / SHARED_LIB)
    print(f"[info] binding lib: {candidate} -> {target / SHARED_LIB}", flush=True)
    return target


# -------------------------------------------------------------------- upstream
class UpstreamState:
    """What the fake upstream saw. Written by handler threads, read by the test."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.tools: dict[str, str] = {}
        self.problem: str | None = None

    def record(self, body: dict[str, Any]) -> int:
        with self.lock:
            self.requests.append(body)
            return len(self.requests)

    def set_tools(self, mapping: dict[str, str]) -> None:
        with self.lock:
            self.tools = dict(mapping)

    def fail(self, problem: str) -> None:
        with self.lock:
            self.problem = problem

    def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, str], str | None]:
        with self.lock:
            return list(self.requests), dict(self.tools), self.problem


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "T3FakeUpstream/1.0"

    state: UpstreamState  # set on the handler class before serving

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # keep the test output readable; assertions carry the evidence

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": {"type": "not_found", "message": self.path}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError, OSError) as exc:
            self.state.fail(f"upstream could not parse the request body: {exc}")
            self._send(400, {"error": {"type": "bad_request", "message": str(exc)}})
            return

        self.state.record(body)
        messages = body.get("messages") or []
        tool_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]

        if tool_messages:
            # Second round: core has executed all four tools and sent the
            # results back. Answer with the final assistant text.
            self._send(200, {
                "id": "chatcmpl-t3-2",
                "object": "chat.completion",
                "model": body.get("model") or "fake-t3",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": FINAL_TEXT, "tool_calls": []},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 42, "completion_tokens": 11},
            })
            return

        # First round: resolve the four tools from what core advertised, by
        # suffix — hard-coding `echo__echo` would pass even if core's snapshot
        # advertised a different module id.
        names = []
        for spec in body.get("tools") or []:
            function = (spec or {}).get("function") or {}
            name = function.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        chosen: dict[str, str] = {}
        for suffix in TOOL_SUFFIXES:
            matches = [name for name in names if name.endswith("__" + suffix)]
            if len(matches) != 1:
                problem = (
                    f"expected exactly one advertised tool ending in '__{suffix}', "
                    f"got {matches!r}; tools={names!r}"
                )
                self.state.fail(problem)
                self._send(400, {"error": {"type": "t3_tool_mismatch", "message": problem}})
                return
            chosen[suffix] = matches[0]
        self.state.set_tools(chosen)

        # Four tool_calls in ONE assistant message, ids in declaration order.
        tool_calls = [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": chosen[suffix],
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
            for suffix, call_id, args in CALL_SPECS
        ]
        self._send(200, {
            "id": "chatcmpl-t3-1",
            "object": "chat.completion",
            "model": body.get("model") or "fake-t3",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 31, "completion_tokens": 7},
        })


def _start_upstream() -> tuple[ThreadingHTTPServer, UpstreamState, str]:
    state = UpstreamState()
    handler = type("T3FakeOpenAIHandler", (FakeOpenAIHandler,), {"state": state})
    server = ThreadingHTTPServer((HOST, 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="t3-fake-upstream", daemon=True)
    thread.start()
    port = int(server.server_address[1])
    return server, state, f"http://{HOST}:{port}/v1"


# ---------------------------------------------------------------------- helpers
def _module_env(
    base: dict[str, str],
    *,
    binding: Path,
    module_src: Path,
    key_path: Path,
    data_dir: Path,
    core_public_key: str,
) -> dict[str, str]:
    env = dict(base)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "sdk" / "python"), str(module_src), str(binding), env.get("PYTHONPATH", "")]
    )
    env["VADE_PROTOCOL_PY"] = str(binding)
    env["MODULE_KEY_PATH"] = str(key_path)
    env["VADE_DATA_DIR"] = str(data_dir)
    env["CORE_PUBLIC_KEY"] = core_public_key
    env.pop("VADE_PAIR_CODE", None)
    return env


def _spawn(module_dir: Path, module: str, env: dict[str, str], log: Any) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", module, "--core", f"ws://{HOST}:{PORT}"],
        cwd=module_dir,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _json_or_none(text: Any) -> Any:
    """Parse a tool message's content, returning None when it is not JSON."""
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _toml_path(path: Path) -> str:
    """Render `path` as a TOML basic-string *body* (no surrounding quotes).

    On Windows ``str(Path)`` is ``D:\\Hermes\\...`` and a backslash is an escape
    introducer inside a TOML basic string (``\\H`` reads as a ``\\xHH`` escape
    and tomllib raises ``Invalid hex value``). ``as_posix()`` yields
    ``D:/Hermes/...``, which both Python and TOML accept as-is; escaping the
    two characters that would still break the string keeps it correct even for
    exotic directory names. On POSIX ``as_posix()`` is exactly ``str(path)``,
    so this renders the same text as before.
    """
    return path.as_posix().replace("\\", "\\\\").replace('"', '\\"')


async def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="vade-t3-"))
    if os.environ.get("VADE_KEEP_WORK") != "1":
        # The inner finally only runs after setup succeeds; an early SystemExit
        # (missing .so/module) must not leave 76 MB behind either.
        atexit.register(shutil.rmtree, work, True)
    data = work / "core-data"
    data.mkdir(parents=True)
    binding = _binding_dir(work)

    config = work / "core.toml"
    config.write_text(
        "\n".join(
            [
                "[core]",
                f'listen = "{HOST}:{PORT}"',
                f'data_dir = "{_toml_path(data)}"',
                'log_level = "warning"',
                "",
                "[dev]",
                "allow_missing_components = true",
                "allow_all = true",
                "",
                "[llm]",
                'provider = "module"',
                f'model_module = "{MODEL_MODULE_ID}"',
                "fallback_to_builtin = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO), str(binding)]

    import uvicorn
    from vade.__main__ import build_module_crypto
    from vade.app import App, install_components, make_rpc_handler
    from vade.config import load_config
    from vade.errors import VadeError
    from vade.llm.module import ModuleLLMProvider
    from vade.transport.server import CoreApp
    from vade_sdk.transport import fingerprint_of, load_or_create_key

    workspace = REPO.parent if (REPO.parent / "Vade-module-model").is_dir() else REPO.parent.parent
    model_dir = Path(os.environ.get("VADE_MODEL_DIR", workspace / "Vade-module-model"))
    echo_dir = Path(os.environ.get("VADE_ECHO_DIR", workspace / "Vade-module-echo"))
    if not (model_dir / "src" / "model_module").is_dir():
        raise SystemExit(f"model module not found at {model_dir}; set VADE_MODEL_DIR")
    if not (echo_dir / "src" / "echo_module").is_dir():
        raise SystemExit(f"echo module not found at {echo_dir}; set VADE_ECHO_DIR")

    cfg = load_config(toml_path=str(config))
    app = App(cfg)
    await app.startup()
    install_components(app, strict=False)
    assert isinstance(app.llm, ModuleLLMProvider), app.llm
    assert app.router is not None

    model_key_path = work / "model.key"
    echo_key_path = work / "echo.key"
    _seed, model_public = load_or_create_key(model_key_path)
    _seed, echo_public = load_or_create_key(echo_key_path)
    app.whitelist.add(MODEL_MODULE_ID, "main", fingerprint_of(model_public), public_key=model_public)
    app.whitelist.add(ECHO_MODULE_ID, "main", fingerprint_of(echo_public), public_key=echo_public)

    core_public_key = app.identity.public_key.hex()
    base_env = dict(os.environ)
    model_env = _module_env(
        base_env,
        binding=binding,
        module_src=model_dir / "src",
        key_path=model_key_path,
        data_dir=work / "model-data",
        core_public_key=core_public_key,
    )
    model_env.update(
        {
            "MODEL_NAME": "fake-t3",
            # No silent retries: a second identical POST would hide a bug and
            # make the "exactly two requests" assertion flaky.
            "MODEL_MAX_RETRIES": "0",
            "MODEL_RETRY_BASE_S": "0",
        }
    )
    echo_env = _module_env(
        base_env,
        binding=binding,
        module_src=echo_dir / "src",
        key_path=echo_key_path,
        data_dir=work / "echo-data",
        core_public_key=core_public_key,
    )

    core_app = CoreApp(
        config=app.config,
        identity=app.identity,
        registry=app.registry,
        pairing=app.pairing,
        make_crypto=build_module_crypto(app),
        on_rpc=make_rpc_handler(app),
        policy_mode="allow_all",
        data_dir=data,
    )
    server = uvicorn.Server(
        uvicorn.Config(core_app, host=HOST, port=PORT, log_level="warning", proxy_headers=False)
    )
    serve_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        raise SystemExit("core did not start")

    # Only now take a second ephemeral port: binding it before uvicorn could
    # hand the fake upstream the very port core is about to want.
    upstream, upstream_state, upstream_url = _start_upstream()
    model_env["MODEL_BASE_URL"] = upstream_url
    print(f"[info] fake OpenAI upstream: {upstream_url}", flush=True)

    model_log_path = work / "model.log"
    echo_log_path = work / "echo.log"
    model_log = model_log_path.open("w", encoding="utf-8")
    echo_log = echo_log_path.open("w", encoding="utf-8")
    model_proc: subprocess.Popen | None = None
    echo_proc: subprocess.Popen | None = None

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}", flush=True)
        if not condition:
            failures.append(name)

    try:
        # 1. both real modules register over Noise IK
        _banner("modules register")
        model_proc = _spawn(model_dir, "model_module", model_env, model_log)
        echo_proc = _spawn(echo_dir, "echo_module", echo_env, echo_log)

        expected = {MODEL_MODULE_ID: "chat", ECHO_MODULE_ID: "echo"}
        records: dict[str, Any] = {}
        deadline = time.time() + 25
        while time.time() < deadline and len(records) < len(expected):
            for record in app.registry.snapshot():
                manifest = getattr(record, "manifest", None)
                if manifest is None or not record.connection_id:
                    continue
                wanted = expected.get(manifest.module_id)
                if wanted and any(spec.name == wanted for spec in manifest.tools):
                    records[manifest.module_id] = record
            if len(records) == len(expected):
                break
            if (model_proc and model_proc.poll() is not None) or (
                echo_proc and echo_proc.poll() is not None
            ):
                break
            await asyncio.sleep(0.1)
        check(
            "both modules registered",
            set(records) == set(expected),
            f"online={sorted(records)}",
        )
        check(
            "model module serves chat",
            MODEL_MODULE_ID in records,
            f"tools={[s.name for s in records[MODEL_MODULE_ID].manifest.tools] if MODEL_MODULE_ID in records else None}",
        )
        check(
            "echo module serves echo/add/sleep/boom",
            ECHO_MODULE_ID in records
            and {s.name for s in records[ECHO_MODULE_ID].manifest.tools} >= {"echo", "add", "sleep", "boom"},
            f"tools={[s.name for s in records[ECHO_MODULE_ID].manifest.tools] if ECHO_MODULE_ID in records else None}",
        )
        if set(records) != set(expected):
            return 1

        # 2. one real turn: submit -> model returns FOUR tool_calls -> echo
        #    module runs all four -> model -> done
        _banner("turn: model calls echo/add/sleep/boom in one step")
        session = app.session.create_session()
        sink = CollectSink()
        await app.session.attach(session.id, None, sink)
        await app.session.submit(
            session.id,
            [{"type": "text", "text": "Run echo, add, sleep and boom, then summarise."}],
            "t3-msg-1",
        )
        try:
            await app.session.wait_idle(session.id, timeout=45)
        except TimeoutError:
            check("turn finished", False, "wait_idle timed out")

        events = app.session_store.read_since(session.id, 0)
        terminal = [e for e in events if e.type == "turn.completed"]
        for _ in range(100):
            if terminal:
                break
            await asyncio.sleep(0.05)
            events = app.session_store.read_since(session.id, 0)
            terminal = [e for e in events if e.type == "turn.completed"]

        timeline = [
            (index, event.type, event.payload)
            for index, event in enumerate(events)
            if event.type in ("turn.tool_call", "turn.tool_result", "turn.completed")
        ]
        print("[info] event timeline:", [(i, t, p.get("call_id")) for i, t, p in timeline], flush=True)
        types = [event_type for _, event_type, _ in timeline]
        call_positions = {
            str(payload.get("call_id")): index
            for index, event_type, payload in timeline if event_type == "turn.tool_call"
        }
        result_positions = {
            str(payload.get("call_id")): index
            for index, event_type, payload in timeline if event_type == "turn.tool_result"
        }
        completed_positions = [index for index, event_type, _ in timeline if event_type == "turn.completed"]
        expected_ids = [call_id for _, call_id, _ in CALL_SPECS]
        completed_index = completed_positions[0] if completed_positions else None

        check("four turn.tool_call events", len(call_positions) == 4,
              f"count={len(call_positions)} types={types}")
        check("four turn.tool_result events", len(result_positions) == 4,
              f"count={len(result_positions)} types={types}")
        check("exactly one turn.completed", len(completed_positions) == 1,
              f"count={len(completed_positions)}")
        check(
            "tool_call ids are call_t3_echo_1..4",
            sorted(call_positions) == sorted(expected_ids),
            f"call_ids={sorted(call_positions)}",
        )
        check(
            "every tool_call precedes its own tool_result",
            set(call_positions) == set(result_positions)
            and all(call_positions[call_id] < result_positions[call_id] for call_id in expected_ids
                    if call_id in call_positions and call_id in result_positions),
            f"calls={call_positions} results={result_positions}",
        )
        check(
            "all four tool_results precede turn.completed",
            completed_index is not None
            and len(result_positions) == 4
            and all(position < completed_index for position in result_positions.values()),
            f"results={sorted(result_positions.values())} completed={completed_index}",
        )

        calls_by_id = {
            str(payload.get("call_id")): payload
            for _, event_type, payload in timeline if event_type == "turn.tool_call"
        }
        results_by_id = {
            str(payload.get("call_id")): payload
            for _, event_type, payload in timeline if event_type == "turn.tool_result"
        }
        completed = [payload for _, event_type, payload in timeline if event_type == "turn.completed"]

        requests, advertised, problem = upstream_state.snapshot()
        check("upstream saw exactly two requests", len(requests) == 2, f"count={len(requests)}")
        check(
            "upstream resolved all four advertised tools by suffix",
            problem is None and set(advertised) == set(TOOL_SUFFIXES),
            f"tools={advertised} problem={problem!r}",
        )
        expected_names = [advertised.get(suffix) for suffix, _, _ in CALL_SPECS]
        actual_names = [
            calls_by_id[call_id].get("tool") if call_id in calls_by_id else None
            for _, call_id, _ in CALL_SPECS
        ]
        check(
            "four tool_calls name the advertised echo tools",
            all(name for name in expected_names) and actual_names == expected_names,
            f"tools={actual_names} expected={expected_names}",
        )

        def result_ok(call_id: str) -> bool | None:
            payload = results_by_id.get(call_id)
            return None if payload is None else payload.get("ok")

        def result_summary(call_id: str) -> str:
            payload = results_by_id.get(call_id)
            return "" if payload is None else str(payload.get("summary", ""))

        echo_id, add_id, sleep_id, boom_id = expected_ids
        check(
            "echo result is ok and carries the echo payload",
            result_ok(echo_id) is True and ECHO_TEXT in result_summary(echo_id),
            f"ok={result_ok(echo_id)} summary={result_summary(echo_id)!r}",
        )
        check(
            "add result is ok and sums 2 + 3",
            result_ok(add_id) is True and '"sum": 5' in result_summary(add_id),
            f"ok={result_ok(add_id)} summary={result_summary(add_id)!r}",
        )
        check(
            "sleep result is ok and reports the slept duration",
            result_ok(sleep_id) is True and "slept_ms" in result_summary(sleep_id),
            f"ok={result_ok(sleep_id)} summary={result_summary(sleep_id)!r}",
        )
        check(
            "boom is a structured INTERNAL failure, not a turn failure",
            result_ok(boom_id) is False
            and results_by_id.get(boom_id, {}).get("code") == "INTERNAL"
            and BOOM_MESSAGE in result_summary(boom_id),
            f"ok={result_ok(boom_id)} code={results_by_id.get(boom_id, {}).get('code')!r} "
            f"summary={result_summary(boom_id)!r}",
        )
        check(
            "turn completed",
            bool(completed) and completed[0].get("state") == "completed",
            f"state={completed[0].get('state') if completed else None}",
        )

        # 3. all four tool results really travelled back to the model upstream
        if len(requests) >= 2:
            first, second = requests[0], requests[1]
            first_tools = [
                ((spec or {}).get("function") or {}).get("name")
                for spec in (first.get("tools") or [])
            ]
            check(
                "first request advertised the four echo tools and hid the provider channel",
                all(name in first_tools for name in expected_names if name)
                and "model__chat" not in first_tools,
                f"tools={first_tools}",
            )
            second_tool_messages = [
                m for m in (second.get("messages") or [])
                if isinstance(m, dict) and m.get("role") == "tool"
            ]
            check(
                "second request carries exactly four tool messages",
                len(second_tool_messages) == 4,
                f"tool_messages={second_tool_messages}",
            )
            by_call_id = {
                str(m.get("tool_call_id")): m for m in second_tool_messages
                if isinstance(m, dict)
            }
            check(
                "tool messages cover all four tool_call ids",
                set(by_call_id) == set(expected_ids),
                f"ids={sorted(by_call_id)} expected={expected_ids}",
            )
            echo_payload = _json_or_none(by_call_id.get(echo_id, {}).get("content"))
            add_payload = _json_or_none(by_call_id.get(add_id, {}).get("content"))
            sleep_payload = _json_or_none(by_call_id.get(sleep_id, {}).get("content"))
            boom_payload = _json_or_none(by_call_id.get(boom_id, {}).get("content"))
            check(
                "echo result reached the model intact",
                isinstance(echo_payload, dict)
                and echo_payload.get("text") == ECHO_TEXT
                and echo_payload.get("len") == len(ECHO_TEXT),
                f"content={by_call_id.get(echo_id, {}).get('content')!r}",
            )
            check(
                "add result reached the model intact",
                isinstance(add_payload, dict) and add_payload.get("sum") == 5,
                f"content={by_call_id.get(add_id, {}).get('content')!r}",
            )
            check(
                "sleep result reached the model intact",
                isinstance(sleep_payload, dict) and sleep_payload.get("slept_ms") == 1,
                f"content={by_call_id.get(sleep_id, {}).get('content')!r}",
            )
            check(
                "boom failure reached the model as a structured error",
                isinstance(boom_payload, dict)
                and boom_payload.get("ok") is False
                and (boom_payload.get("error") or {}).get("code") == "INTERNAL"
                and (boom_payload.get("error") or {}).get("message") == BOOM_MESSAGE,
                f"content={by_call_id.get(boom_id, {}).get('content')!r}",
            )
            assistant_calls = [
                m for m in (second.get("messages") or [])
                if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
            ]
            carried = assistant_calls[-1].get("tool_calls") if assistant_calls else []
            check(
                "second request carries all four assistant tool_calls",
                len(carried) == 4
                and [c.get("id") for c in carried] == expected_ids
                and [((c.get("function") or {}).get("name")) for c in carried] == expected_names,
                f"assistant_calls={carried}",
            )
        else:
            check("second request carries the four tool results", False, "only one request")

        # 4. transcript + final answer
        messages = app.session_store.load(session.id).messages
        roles = [message.role for message in messages]
        check(
            "transcript is user/assistant/tool x4/assistant",
            roles == ["user", "assistant", "tool", "tool", "tool", "tool", "assistant"],
            f"roles={roles}",
        )
        tool_message_ids = [
            str(message.tool_call_id) for message in messages if message.role == "tool"
        ]
        check(
            "transcript tool messages keep the four tool_call ids",
            tool_message_ids == expected_ids,
            f"ids={tool_message_ids}",
        )
        final_message = messages[-1] if messages else None
        check(
            "final assistant answer is the upstream's second reply",
            final_message is not None
            and final_message.role == "assistant"
            and FINAL_TEXT in str(final_message.content),
            f"content={getattr(final_message, 'content', None)!r}",
        )
        sink_types = sink.types()
        check(
            "live sink saw all four tool_calls and tool_results",
            sink_types.count("turn.tool_call") == 4 and sink_types.count("turn.tool_result") == 4,
            f"tool_call={sink_types.count('turn.tool_call')} "
            f"tool_result={sink_types.count('turn.tool_result')} sink_types={sink_types}",
        )
    finally:
        _stop(model_proc)
        _stop(echo_proc)
        model_log.close()
        echo_log.close()
        print("\n--- model module log (tail) ---")
        print("\n".join(model_log_path.read_text(encoding="utf-8").splitlines()[-20:]))
        print("\n--- echo module log (tail) ---")
        print("\n".join(echo_log_path.read_text(encoding="utf-8").splitlines()[-20:]))
        upstream.shutdown()
        upstream.server_close()
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=10)
        except TimeoutError:
            serve_task.cancel()
        try:
            await app.session.close()
        except (VadeError, RuntimeError, OSError) as exc:  # cleanup must not mask the real failure
            print(f"[warn] session.close: {exc}", flush=True)
        await app.shutdown()
        if os.environ.get("VADE_KEEP_WORK") == "1":
            print(f"\n[info] work dir kept: {work}", flush=True)
        else:
            shutil.rmtree(work, ignore_errors=True)
            print("\n[info] work dir removed (VADE_KEEP_WORK=1 keeps it)", flush=True)

    _banner("result")
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}", flush=True)
        return 1
    print("all checks passed", flush=True)
    return 0


class CollectSink:
    """Same shape as core/tests/session/helpers.py; kept local so this script
    has no dependency on the pytest tree."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    def types(self) -> list[str]:
        return [str(event["type"]) for event in self.events]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
