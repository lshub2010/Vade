#!/usr/bin/env python3
"""Fake module fixture: connect to core over WSS, handshake, register, answer calls.

Not a production module (M-group owns those). Its job is to prove the C2
acceptance path end-to-end against a real listener:

    python tests/fixtures/fake_module.py \
        --core ws://127.0.0.1:8443/ws \
        --core-pub <core public key hex> \
        --manifest tests/fixtures/manifests/good.json

`--core-pub` is what the QR code/pairing hands a real module (the core's
X25519 static public key). The module keeps its own identity in
`--identity` (default: a temp file next to this script) so repeated runs reuse
one fingerprint; delete the file to simulate a new module.

Output is one JSON object per line: the `module/register` reply on success, or
`{"error": {code, message}}` with exit code 1 on rejection. The CLI prints the
MANIFEST_INVALID message verbatim, so the field pointer is visible.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT / "core"), str(ROOT / "protocol" / "core" / "python")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from vade.crypto_gateway.session import CryptoSession  # noqa: E402
from vade.errors import VadeError  # noqa: E402
from vade.identity.store import load_or_create  # noqa: E402
from vade.transport.framing import FrameType, decode_handshake, encode_handshake  # noqa: E402

DEFAULT_IDENTITY = ROOT / "core" / "tests" / ".fake-module.key"
DEFAULT_TIMEOUT = 5.0


class FakeModule:
    """Blocking WS client. `websockets.sync` keeps the fixture readable."""

    def __init__(
        self,
        url: str,
        core_pub: bytes,
        *,
        identity_path: Path | str = DEFAULT_IDENTITY,
        timeout: float = DEFAULT_TIMEOUT,
        ssl_context: Any | None = None,
    ) -> None:
        from websockets.sync.client import connect

        self.url = url
        self.timeout = timeout
        self.identity = load_or_create(identity_path)
        self._ws = connect(url, open_timeout=timeout, close_timeout=timeout, ssl=ssl_context, max_size=2**20)
        self.session = CryptoSession(
            self.identity.ffi,
            mode="ik",
            role="initiator",
            connection="module_initiated",
            remote_pub=core_pub,
        )
        self._next_id = 0
        self._handshake()

    # ------------------------------------------------------------------ handshake
    def _handshake(self) -> None:
        first = self.session.start()
        if first:
            self._ws.send(encode_handshake(first))
        while not self.session.is_finished():
            wire = self._ws.recv(timeout=self.timeout)
            if not isinstance(wire, (bytes, bytearray)):
                raise VadeError("PROTOCOL_MISMATCH", "core sent a text frame during the handshake")
            reply = self.session.read_handshake(decode_handshake(bytes(wire)))
            if reply:
                self._ws.send(encode_handshake(reply))
        self.session.establish()

    # ------------------------------------------------------------------ rpc
    def call(self, method: str, params: Any = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._ws.send(self.session.seal(FrameType.RPC, json.dumps(payload).encode("utf-8")))
        while True:
            wire = self._ws.recv(timeout=self.timeout)
            header, plaintext = self.session.open(bytes(wire))
            if header.type is FrameType.HEARTBEAT:
                continue
            message = json.loads(plaintext.decode("utf-8"))
            if message.get("id") == request_id:
                return message

    def register(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return self.call("module/register", manifest)

    # ------------------------------------------------------------------ io
    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # pragma: no cover - best effort on teardown
            pass

    def __enter__(self) -> "FakeModule":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake_module", description="C2 fake module fixture")
    parser.add_argument("--core", default="ws://127.0.0.1:8443/ws", help="core data-plane WS URL")
    parser.add_argument("--core-pub", required=True, help="core static public key (hex, 32 bytes)")
    parser.add_argument("--manifest", required=True, help="manifest JSON file")
    parser.add_argument("--identity", default=str(DEFAULT_IDENTITY), help="module identity seed file")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--once", action="store_true", help="register and exit (default)")
    args = parser.parse_args(argv)

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    try:
        module = FakeModule(args.core, bytes.fromhex(args.core_pub), identity_path=args.identity, timeout=args.timeout)
    except VadeError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        return 1
    try:
        reply = module.register(manifest)
    finally:
        module.close()
    print(json.dumps(reply, ensure_ascii=False))
    error = reply.get("error")
    if error:
        print(f"{error.get('code')}: {error.get('message')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
