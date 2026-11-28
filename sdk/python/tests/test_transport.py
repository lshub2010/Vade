"""Transport-level invariants that do not need a live core."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from vade_sdk.transport import Client, HEADER_LEN, TransportError, _frame, _peek, fingerprint_of, load_or_create_key


def test_key_file_is_created_0600_and_never_overwritten(tmp_path):
    path = tmp_path / "data" / "module.key"
    seed, public = load_or_create_key(path)
    assert len(seed) == 64 and len(public) == 32
    # POSIX mode bits are a no-op on Windows (chmod does nothing), so the 0600 claim
    # is only checkable on POSIX. Same guard, same wording as
    # core/tests/admin/test_passwords.py and core/tests/test_skill/test_install.py —
    # Linux still validates; nothing else in this test is relaxed.
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, oct(mode)

    again, again_public = load_or_create_key(path)
    assert again == seed
    assert again_public == public
    assert fingerprint_of(public) == fingerprint_of(again_public)


def test_a_corrupt_key_file_is_an_error_not_a_regeneration(tmp_path):
    path = tmp_path / "module.key"
    path.write_text("not-a-key\n", encoding="utf-8")
    with pytest.raises(TransportError, match="64 hex"):
        load_or_create_key(path)
    assert path.read_text(encoding="utf-8") == "not-a-key\n"


def test_header_peek_accepts_a_handshake_frame():
    wire = _frame(0x01, b"hello")
    header = _peek(wire)
    assert header["type"] == 0x01
    assert header["payload_len"] == 5
    assert len(wire) == HEADER_LEN + 5


@pytest.mark.parametrize("version", [0, 2, 255])
def test_wrong_version_is_rejected_before_any_decryption(version):
    wire = bytearray(_frame(0x02, b"x"))
    wire[0] = version
    with pytest.raises(TransportError) as excinfo:
        _peek(bytes(wire))
    assert excinfo.value.code == "PROTOCOL_MISMATCH"
    assert excinfo.value.fatal is True


def test_reserved_bits_and_unknown_types_are_fatal():
    wire = bytearray(_frame(0x02, b"x"))
    wire[2] = 1
    with pytest.raises(TransportError, match="reserved"):
        _peek(bytes(wire))
    with pytest.raises(TransportError, match="unknown frame type"):
        _peek(bytes(_frame(0x09, b"x")))


def test_encrypted_frame_with_counter_zero_is_rejected():
    header = bytearray(_frame(0x02, b"x"))
    header[12:20] = (0).to_bytes(8, "big")
    with pytest.raises(TransportError, match="counter 0"):
        _peek(bytes(header))


async def test_transport_passes_ctx_and_dispatches_private_rpc():
    replies = []
    errors = []
    seen = {}

    class FakeApp:
        module_id = "com.example.private"

        async def call_tool(self, tool, args, *, call_id="", context=None):
            seen["tool"] = (tool, args, call_id, context)
            return {"ok": True, "result": {"tool": tool}}

        async def dispatch_rpc(self, method, params):
            if method == "memory/admin_stats":
                return {"global_count": 0}
            from vade_sdk import ToolError

            raise ToolError("NOT_FOUND", "no such private RPC")

    client = object.__new__(Client)
    client.app = FakeApp()

    async def reply(request_id, result):
        replies.append((request_id, result))

    async def reply_error(request_id, code, message):
        errors.append((request_id, code, message))

    client._reply = reply
    client._reply_error = reply_error
    await client._handle_request(
        {
            "id": 1,
            "method": "tools/call",
            "params": {"tool": "remember", "args": {"text": "x"}, "call_id": "c", "ctx": {"session_id": "s-1"}},
        }
    )
    assert seen["tool"] == ("remember", {"text": "x"}, "c", {"session_id": "s-1"})
    assert replies[-1] == (1, {"ok": True, "result": {"tool": "remember"}})

    await client._handle_request({"id": 2, "method": "memory/admin_stats", "params": {}})
    assert replies[-1] == (2, {"global_count": 0})

    await client._handle_request({"id": 3, "method": "memory/unknown", "params": {}})
    assert errors[-1][0:2] == (3, "NOT_FOUND")


async def test_private_rpc_handler_bug_is_internal_and_never_a_fake_success():
    """A handler exception is INTERNAL; a None return is an error, not an empty page."""
    replies = []
    errors = []

    class FakeApp:
        module_id = "com.example.bug"

        async def dispatch_rpc(self, method, params):
            if method == "memory/raises":
                raise KeyError("db")
            return None

    client = object.__new__(Client)
    client.app = FakeApp()

    async def reply(request_id, result):
        replies.append((request_id, result))

    async def reply_error(request_id, code, message):
        errors.append((request_id, code, message))

    client._reply = reply
    client._reply_error = reply_error

    await client._handle_request({"id": 1, "method": "memory/raises", "params": {}})
    assert errors[-1][0:2] == (1, "INTERNAL")
    assert replies == []

    await client._handle_request({"id": 2, "method": "memory/empty", "params": {}})
    assert errors[-1][0:2] == (2, "INTERNAL")
    assert "object" in errors[-1][2]
    assert replies == []


async def test_private_rpc_non_mapping_result_is_an_error():
    """A list/str return must not be wrapped into a fake empty page."""
    errors = []

    class FakeApp:
        module_id = "com.example.badshape"

        async def dispatch_rpc(self, method, params):
            return []

    client = object.__new__(Client)
    client.app = FakeApp()

    async def reply(request_id, result):  # pragma: no cover - must not run
        raise AssertionError("must not reply with a non-mapping result")

    async def reply_error(request_id, code, message):
        errors.append((request_id, code, message))

    client._reply = reply
    client._reply_error = reply_error
    await client._handle_request({"id": 3, "method": "memory/list", "params": {}})
    assert errors[-1][0:2] == (3, "INTERNAL")
    assert "object" in errors[-1][2]


async def test_private_rpc_that_never_returns_is_a_timeout():
    """A wedged handler must not leave the caller hanging forever."""
    import asyncio

    errors = []

    class SlowApp:
        module_id = "com.example.slow"

        async def dispatch_rpc(self, method, params):
            await asyncio.sleep(5)
            return {"items": []}

    client = object.__new__(Client)
    client.app = SlowApp()
    client.rpc_timeout = 0.05

    async def reply(request_id, result):  # pragma: no cover - must not run
        raise AssertionError("timed-out RPC must not reply with a result")

    async def reply_error(request_id, code, message):
        errors.append((request_id, code, message))

    client._reply = reply
    client._reply_error = reply_error
    await client._handle_request({"id": 4, "method": "memory/admin_list", "params": {}})
    assert errors and errors[-1][1] == "TIMEOUT"
