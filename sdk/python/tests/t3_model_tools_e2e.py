"""T3 core-half e2e: a REAL model module drives a REAL echo tool call.

Not collected by pytest (the filename does not start with ``test_``): it needs
the Rust binding, uvicorn, a free port and two module checkouts. Run it
explicitly:

    python sdk/python/tests/t3_model_tools_e2e.py

It needs an exact binding: build ``protocol/core/python/libvade_protocol.so``
in this checkout, or set ``VADE_PROTOCOL_SO`` (and optionally
``VADE_PROTOCOL_PY``) to the file you mean to test. It never borrows another
worktree's build silently.

What it proves, in order:

  1. a real core (`App.startup()` + `install_components()` + uvicorn + Noise IK)
     starts with ``[llm] provider = "module"`` and no builtin fallback;
  2. the real ``com.example.model`` module registers and serves ``chat``
     against a stdlib ``http.server`` fake OpenAI-compatible upstream;
  3. the upstream answers the first request with a ``tool_calls`` entry whose
     name is the echo tool from core's frozen snapshot (``echo__echo``);
  4. core routes that call to the real ``com.example.echo`` module over Noise,
     gets ``{"text", "len"}`` back, and sends it to the model as a ``tool``
     message (the second upstream request);
  5. the upstream's second answer is the final assistant text and the turn
     completes;
  6. the durable event stream is ordered
     ``turn.tool_call`` -> ``turn.tool_result`` -> ``turn.completed``.

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
FINAL_TEXT = "T3-FINAL: the echo tool returned hello-from-t3"

MODEL_MODULE_ID = "com.example.model"
ECHO_MODULE_ID = "com.example.echo"


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


PORT = _free_port()


# --------------------------------------------------------------------- binding
def _binding_dir(work: Path) -> Path:
    """Locate `vade_protocol.py` + its `.so`, copying them together if needed.

    The generated `.py` is committed; the shared library is not. The binding
    loads `libvade_protocol.so` from the directory of `vade_protocol.py`, so a
    worktree without a built copy gets a temp dir with both files.
    """
    explicit = os.environ.get("VADE_PROTOCOL_PY")
    if explicit:
        return Path(explicit)

    source = REPO / "protocol" / "core" / "python"
    if (source / "libvade_protocol.so").is_file():
        return source

    env_so = os.environ.get("VADE_PROTOCOL_SO")
    if not env_so:
        raise SystemExit(
            "no libvade_protocol.so in this worktree. Build it, or set "
            "VADE_PROTOCOL_SO to the exact .so you mean to test with "
            "(VADE_PROTOCOL_PY points at a directory holding both files). "
            "Refusing to borrow a sibling worktree's binding silently."
        )
    candidate = Path(env_so)
    if not candidate.is_file():
        raise SystemExit(f"VADE_PROTOCOL_SO does not exist: {candidate}")

    target = work / "binding"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "vade_protocol.py", target / "vade_protocol.py")
    try:
        (target / "libvade_protocol.so").symlink_to(candidate)
    except OSError:
        shutil.copy2(candidate, target / "libvade_protocol.so")
    print(f"[info] binding .so: {candidate}", flush=True)
    return target


# -------------------------------------------------------------------- upstream
class UpstreamState:
    """What the fake upstream saw. Written by handler threads, read by the test."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.echo_tool: str | None = None
        self.problem: str | None = None

    def record(self, body: dict[str, Any]) -> int:
        with self.lock:
            self.requests.append(body)
            return len(self.requests)

    def set_echo_tool(self, name: str) -> None:
        with self.lock:
            self.echo_tool = name

    def fail(self, problem: str) -> None:
        with self.lock:
            self.problem = problem

    def snapshot(self) -> tuple[list[dict[str, Any]], str | None, str | None]:
        with self.lock:
            return list(self.requests), self.echo_tool, self.problem


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
            # Second round: core has executed the tool and sent the result back.
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

        # First round: pick the echo tool core advertised and call it.
        names = []
        for spec in body.get("tools") or []:
            function = (spec or {}).get("function") or {}
            name = function.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        echo_names = [name for name in names if name.endswith("__echo")]
        if not echo_names:
            self.state.fail(f"no echo tool was advertised; tools={names!r}")
            self._send(400, {"error": {"type": "t3_no_echo_tool", "message": f"tools={names}"}})
            return
        if len(echo_names) > 1:
            self.state.fail(f"ambiguous echo tools: {echo_names!r}")
            self._send(400, {"error": {"type": "t3_ambiguous_echo", "message": f"tools={names}"}})
            return
        echo_name = echo_names[0]
        self.state.set_echo_tool(echo_name)
        self._send(200, {
            "id": "chatcmpl-t3-1",
            "object": "chat.completion",
            "model": body.get("model") or "fake-t3",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_t3_echo_1",
                        "type": "function",
                        "function": {
                            "name": echo_name,
                            "arguments": json.dumps({"text": ECHO_TEXT}, ensure_ascii=False),
                        },
                    }],
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
                f'data_dir = "{data}"',
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
            "echo module serves echo",
            ECHO_MODULE_ID in records,
            f"tools={[s.name for s in records[ECHO_MODULE_ID].manifest.tools] if ECHO_MODULE_ID in records else None}",
        )
        if set(records) != set(expected):
            return 1

        # 2. one real turn: submit -> model tool_call -> echo -> model -> done
        _banner("turn: model calls echo")
        session = app.session.create_session()
        sink = CollectSink()
        await app.session.attach(session.id, None, sink)
        await app.session.submit(
            session.id,
            [{"type": "text", "text": "Please echo the phrase hello-from-t3."}],
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
        print("[info] event timeline:", [(i, t) for i, t, _ in timeline], flush=True)
        types = [event_type for _, event_type, _ in timeline]
        check(
            "event order tool_call -> tool_result -> turn.completed",
            types == ["turn.tool_call", "turn.tool_result", "turn.completed"],
            f"types={types}",
        )

        tool_calls = [payload for _, event_type, payload in timeline if event_type == "turn.tool_call"]
        tool_results = [payload for _, event_type, payload in timeline if event_type == "turn.tool_result"]
        completed = [payload for _, event_type, payload in timeline if event_type == "turn.completed"]

        requests, echo_tool, problem = upstream_state.snapshot()
        check("upstream saw exactly two requests", len(requests) == 2, f"count={len(requests)}")
        check("upstream found one advertised echo tool", problem is None and bool(echo_tool),
              f"echo_tool={echo_tool!r} problem={problem!r}")
        if echo_tool is None:
            echo_tool = "echo__echo"

        check(
            "tool_call names the echo tool",
            bool(tool_calls) and tool_calls[0].get("tool") == echo_tool,
            f"tool={tool_calls[0].get('tool') if tool_calls else None} expected={echo_tool}",
        )
        check(
            "tool_result is ok and carries the echo payload",
            bool(tool_results) and tool_results[0].get("ok") is True
            and ECHO_TEXT in str(tool_results[0].get("summary", "")),
            f"ok={tool_results[0].get('ok') if tool_results else None} "
            f"summary={tool_results[0].get('summary') if tool_results else None!r}",
        )
        check(
            "turn completed",
            bool(completed) and completed[0].get("state") == "completed",
            f"state={completed[0].get('state') if completed else None}",
        )

        # 3. the tool result really travelled back to the model upstream
        if len(requests) >= 2:
            first, second = requests[0], requests[1]
            first_tools = [
                ((spec or {}).get("function") or {}).get("name")
                for spec in (first.get("tools") or [])
            ]
            check(
                "first request advertised echo + model tools",
                echo_tool in first_tools and "model__chat" in first_tools,
                f"tools={first_tools}",
            )
            second_tool_messages = [
                m for m in (second.get("messages") or [])
                if isinstance(m, dict) and m.get("role") == "tool"
            ]
            check(
                "second request carries the current tool result",
                len(second_tool_messages) == 1
                and second_tool_messages[0].get("tool_call_id") == "call_t3_echo_1"
                and ECHO_TEXT in str(second_tool_messages[0].get("content", "")),
                f"tool_messages={second_tool_messages}",
            )
            assistant_calls = [
                m for m in (second.get("messages") or [])
                if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
            ]
            check(
                "second request carries the assistant tool_call",
                bool(assistant_calls)
                and assistant_calls[-1]["tool_calls"][0]["function"]["name"] == echo_tool,
                f"assistant_calls={assistant_calls}",
            )
        else:
            check("second request carries the current tool result", False, "only one request")

        # 4. transcript + final answer
        messages = app.session_store.load(session.id).messages
        roles = [message.role for message in messages]
        check(
            "transcript is user/assistant/tool/assistant",
            roles == ["user", "assistant", "tool", "assistant"],
            f"roles={roles}",
        )
        final_message = messages[-1] if messages else None
        check(
            "final assistant answer is the upstream's second reply",
            final_message is not None and FINAL_TEXT in str(final_message.content),
            f"content={getattr(final_message, 'content', None)!r}",
        )
        check(
            "live sink saw the tool events too",
            "turn.tool_call" in sink.types() and "turn.tool_result" in sink.types(),
            f"sink_types={sink.types()}",
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
        print(f"\n[info] work dir: {work}", flush=True)
        if os.environ.get("VADE_KEEP_WORK") != "1":
            shutil.rmtree(work, ignore_errors=True)
            print("[info] work dir removed (VADE_KEEP_WORK=1 keeps it)", flush=True)

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
