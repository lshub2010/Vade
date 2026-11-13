"""`--standalone`: run one tool with no core, no websockets, no Noise import.

Output goes to stdout as a single JSON object; diagnostics go to stderr. The
envelope here is the local shape `{"ok": ..., "result"|"error": ...}` — no
`duration_ms`/`truncated`, because nothing crossed a process boundary. The
duration is still logged as a JSON line.
"""

from __future__ import annotations

import asyncio
import json
import sys

__all__ = ["run_standalone"]


def run_standalone(app, tool_name: str, args_json: str) -> int:
    if app.tool(tool_name) is None:
        sys.stderr.write(f"NOT_FOUND: unknown tool {tool_name!r}; module declares {list(app.tool_names)}\n")
        return 1
    try:
        args = json.loads(args_json or "{}")
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"INVALID_ARGUMENT: --args is not valid JSON: {exc}\n")
        return 2
    if not isinstance(args, dict):
        sys.stderr.write("INVALID_ARGUMENT: --args must be a JSON object\n")
        return 2

    envelope = asyncio.run(app.call_tool(tool_name, args, call_id="standalone"))
    out = {"ok": bool(envelope.get("ok"))}
    if envelope.get("ok"):
        out["result"] = envelope.get("result")
    else:
        out["error"] = envelope.get("error")
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if out["ok"] else 1
