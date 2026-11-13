"""Log lines and the standalone CLI contract (stdout stays pure JSON)."""

from __future__ import annotations

import json
import sys

import pytest

from vade_sdk import ModuleApp, ToolSpec
from vade_sdk.logging import JsonLogger


def echo(text: str) -> dict:
    return {"text": text, "len": len(text)}


def raise_tool(message: str) -> dict:
    from vade_sdk import ToolError

    raise ToolError("PERMISSION_DENIED", message)


def app() -> ModuleApp:
    application = ModuleApp("com.example.cli", "0.1.0")
    application.add_tool(ToolSpec(name="echo", fn=echo, description="echo"))
    application.add_tool(ToolSpec(name="deny", fn=raise_tool, description="denied"))
    return application


def test_log_line_is_one_json_object_with_the_contract_fields():
    stream = _Collect()
    logger = JsonLogger("com.example.cli", "main", stream=stream)  # type: ignore[arg-type]
    logger.info("tool ok", tool="echo", duration_ms=3, code=None)
    record = json.loads(stream.value.strip())
    assert record["module_id"] == "com.example.cli"
    assert record["instance_id"] == "main"
    assert record["level"] == "info"
    assert record["tool"] == "echo"
    assert record["duration_ms"] == 3
    assert "code" not in record  # omitted, not null
    assert record["ts"].endswith("Z")


def test_standalone_success_prints_exactly_ok_result(capsys):
    assert app().run(["--standalone", "echo", "--args", '{"text":"hi"}']) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "result": {"text": "hi", "len": 2}}
    assert "tool ok" in captured.err  # logs go to stderr, never to the JSON stream


def test_standalone_tool_error_is_nonzero_and_structured(capsys):
    assert app().run(["--standalone", "deny", "--args", '{"message":"no"}']) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PERMISSION_DENIED"


def test_standalone_rejects_bad_json_and_non_object(capsys):
    assert app().run(["--standalone", "echo", "--args", "not-json"]) == 2
    assert app().run(["--standalone", "echo", "--args", '["hi"]']) == 2
    assert "INVALID_ARGUMENT" in capsys.readouterr().err


def test_standalone_does_not_import_the_transport():
    """`--standalone` must work on a machine without websockets/Noise."""
    for name in [m for m in list(sys.modules) if m == "vade_sdk.transport"]:
        del sys.modules[name]
    assert app().run(["--standalone", "echo", "--args", '{"text":"x"}']) == 0
    assert "vade_sdk.transport" not in sys.modules


def test_mutually_exclusive_modes(capsys):
    assert app().run(["--standalone", "echo", "--core", "ws://127.0.0.1:1"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


class _Collect:
    def __init__(self) -> None:
        self.value = ""

    def write(self, text: str) -> int:
        self.value += text
        return len(text)

    def flush(self) -> None:
        return None
