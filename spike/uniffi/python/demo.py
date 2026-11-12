#!/usr/bin/env python3
"""C0a · T4b acceptance demo: drive the Rust core from the generated Python binding.

Runs against the real Rust test server (spike-server), not a mock:

    python demo.py --server ../protocol-spike/target/release/spike-server \
                   --key-dir ../protocol-spike/target/spike-keys

Prints HANDSHAKE_OK on success. The key material is throwaway and lives in 0600
files; this script exports paths/hex through the environment because that is what
`keys::load_client_keys` reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(binary: Path, key_dir: Path) -> tuple[subprocess.Popen, str]:
    env = dict(os.environ, VADE_SPIKE_KEY_DIR=str(key_dir))
    proc = subprocess.Popen(
        [str(binary)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    deadline = time.time() + 15
    addr = None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("LISTEN="):
            addr = line.split("=", 1)[1]
            break
        if line.startswith("SERVER_PUB="):
            print(f"SERVER_PUB={line.split('=', 1)[1]}")
        else:
            print(f"SERVER_LOG {line}")
    if addr is None:
        proc.kill()
        raise SystemExit("server did not print LISTEN= within 15s")
    return proc, addr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--key-dir", required=True)
    args = ap.parse_args()

    server_bin = Path(args.server).resolve()
    key_dir = Path(args.key_dir).resolve()
    if not server_bin.is_file():
        raise SystemExit(f"missing server binary: {server_bin}")

    # The Rust core reads the client identity from the process environment.
    pub = (key_dir / "server_static.pub.hex").read_text().strip()
    os.environ["VADE_SPIKE_SERVER_PUB"] = pub
    os.environ["VADE_SPIKE_CLIENT_SK_FILE"] = str(key_dir / "client_static.sk.hex")

    import protocol_spike as ps  # imported after env is set, like a real app would

    print(f"BINDING_PYTHON version={ps.version()}")
    print(f"MODULE_FILE={ps.__file__}")
    print(f"PUBLIC_NAMES={','.join(sorted(n for n in dir(ps) if not n.startswith('_')))}")

    proc, addr = start_server(server_bin, key_dir)
    try:
        asyncio.run(run_demo(ps, addr))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def run_demo(ps, addr: str) -> None:
    report = await ps.handshake(addr)  # UniFFI 0.32 emits a real `async def`
    print(f"HANDSHAKE_JSON={report}")
    body = json.loads(report)
    assert body["ok"] is True, report
    assert body["protocol"] == "Noise_IK_25519_ChaChaPoly_BLAKE2s", report
    print(f"HANDSHAKE_MS={body['handshake_ms']}")
    print(f"SESSION_COUNT_AFTER_HANDSHAKE={ps.session_count()}")
    assert ps.session_count() == 1

    plaintext = b"hello vade from python"
    echoed = await ps.send_encrypted(addr, plaintext)
    assert echoed == plaintext, f"echo mismatch: {echoed!r}"
    print(f"ECHO_OK bytes={len(echoed)}")
    print(f"ECHO_IS_SAME_OBJECT={echoed is plaintext}")

    # Error path: does `code` survive the FFI boundary for a refused connection?
    dead = free_port()
    try:
        await ps.handshake(f"127.0.0.1:{dead}")
        code = "NO_ERROR"
    except ps.SpikeError.Io as e:  # variant + field, not a string
        code = e.code
    print(f"ERROR_CODE={code}")
    assert code == "UNAVAILABLE", code

    assert ps.close_session(addr) is True
    print(f"SESSION_COUNT_AFTER_CLOSE={ps.session_count()}")
    print("HANDSHAKE_OK")


if __name__ == "__main__":
    main()
    sys.exit(0)
