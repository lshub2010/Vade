# Vade Python SDK · v0.1.0 (C-07, frozen)

`vade-sdk==0.1.x` is the only supported way to write a Vade module in Python.
It owns four public names — `ModuleApp`, `ToolSpec`, `ToolError`, `run` — plus
the CLI below. Everything else (`vade_sdk.transport`, `.standalone`, `.logging`,
`._pairing`) is internal and may change in a patch release.

Interface status: **frozen at G1 (2026-11-13)**. In 0.1.x only *optional*
keyword arguments may be added; existing fields keep their meaning.

---

## 1. Thirty-line module

```python
# src/hello_module/tools.py — no SDK import here
from typing import Any

def hello(name: str) -> dict[str, Any]:
    return {"hello": name}


# src/hello_module/server.py
from vade_sdk import ModuleApp, ToolSpec, ToolError

app = ModuleApp(
    module_id="com.example.hello",   # reverse DNS, lowercase
    version="1.0.0",
    concurrency="pooled",            # exclusive / pooled / mirrored
    display_name="Hello",            # optional
    priority=0,                      # optional, model-module selection (M6)
    capabilities=["tools"],          # only "tools" exists in 0.1
)

app.add_tool(ToolSpec(
    name="hello",
    fn=hello,
    description="Say hello to a name",
    risk="low",
    timeout_ms=5000,
    idempotent=True,
))

if __name__ == "__main__":
    raise SystemExit(app.run())
```

Run it:

```bash
python -m hello_module --print-manifest                       # registration payload
python -m hello_module --check                                # validate, exit 2 on error
python -m hello_module --standalone hello --args '{"name":"Ada"}'
python -m hello_module --core ws://127.0.0.1:8443             # serve
```

`app.run()` reads the CLI and the environment, and does exactly one thing:
picks `--standalone` **or** `--core`. Both at once is a usage error (exit 2).

| Flag | Meaning |
|---|---|
| `--print-manifest` | print the exact JSON sent as `module/register` and exit 0 |
| `--check` | validate module id, tool names, descriptions, risk, timeout, schema, ≤64 tools; exit 2 on any problem |
| `--standalone TOOL --args '{"k":1}'` | run one tool locally, print one JSON object to stdout |
| `--core URL` | connect (`ws://` or `wss://`); also `VADE_CORE_URL` |
| `--pair-code CODE` | reserved; fails closed today (see §5) |
| `--instance-id ID` | override `VADE_INSTANCE_ID` (default `main`) |

Environment: `VADE_CORE_URL`, `VADE_INSTANCE_ID`, `MODULE_KEY_PATH`,
`VADE_DATA_DIR`, `CORE_PUBLIC_KEY`, `CORE_FINGERPRINT`.

## 2. `ToolSpec` fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | required | `^[a-z][a-z0-9_]{0,63}$`; `module_id/name` is globally unique |
| `fn` | callable | required | plain sync or `async` function; its signature **is** the schema |
| `description` | `str` | required | non-blank, ≤512 chars; the model picks tools by this |
| `risk` | `str` | `"low"` | `low` / `medium` / `high`; core may only raise it |
| `timeout_ms` | `int` | `30000` | 100…600000; core also applies its own deadline |
| `idempotent` | `bool` | `True` | retry safety hint |
| `input_schema` | — | derived | read-only; computed from `fn`, not settable in 0.1 |

`ModuleApp(module_id, version, concurrency="exclusive", *, display_name=None,
priority=0, capabilities=("tools",), instance_id=None, role="tool",
max_concurrency=4, max_payload=65519, protocol="vade/v1", logger=None,
env_prefix=None, ui_pages=())`.

`ui_pages` declares C-19 module pages (RFC-0009 §9.2) — the `ui.pages[]` block
that gives a module an entry in `ui/modules`. The SDK checks only that every
entry is a mapping carrying `id`/`title`/`version`/`schema` and that `id`
matches `^[a-z][a-z0-9_-]{0,31}$`, and it raises `ValueError` naming the page.
The rest (icon enum, node tree, budgets, `navigate` targets) belongs to core's
`validate_ui()`, which **drops** a bad page into `Manifest.ui_warnings` instead
of failing registration. The manifest gains a `ui` key **only** when at least
one page is declared: `ui.pages` absent or empty means "no entry anywhere"
(FR-8.79), so an empty `ui_pages` leaves the payload byte-for-byte unchanged.

`add_tool` may only be called before the module is live. After registration use
`await app.replace_tools([...])`: it sends `module/capabilities.update` and
swaps the local table **only after core accepts**; a rejection raises
`ToolError("MANIFEST_INVALID", ...)` and the old tools stay routable.

## 3. Signature → JSON Schema

| Python annotation | JSON Schema | Required |
|---|---|---|
| `str` | `{"type":"string"}` | yes |
| `int` | `{"type":"integer"}` | yes |
| `float` | `{"type":"number"}` | yes |
| `bool` | `{"type":"boolean"}` | yes |
| `list` / `list[T]` | `{"type":"array"}` / with `items` | yes |
| `dict` / `dict[str, T]` | `{"type":"object"}` / with `additionalProperties` | yes |
| `Optional[T]` / `T \| None` | schema of `T` | no |
| parameter with a default | schema + `"default": value` | no |

The object always gets `"additionalProperties": false`: an argument that is not
in the signature is `INVALID_ARGUMENT`, never silently ignored.

**Rejected at startup** (a `SchemaError`, not a downgrade to `{}`): `bytes`,
`Any`, `tuple`, `set`, `Union[A, B]` (non-optional), `*args`, `**kwargs`, and
any parameter without an annotation. `bool` is not accepted where `int` is
expected (and vice versa) — both in the schema and at dispatch time.

Return values: `dict`, `list`, `str`, `int`, `float`, `bool`, `None`. Returning
`bytes` is rejected (`INTERNAL`, "base64-encode it"): the wire is JSON and a
silent `str(b"...")` would corrupt binary payloads.

## 4. Errors

`ToolError(code, message, retriable=None, data=None)`; `code` must exist in
`schema/errors.json` (C-04) or construction raises `ValueError`. The table in
`vade_sdk/errors.py` is checked against the schema file at import time when the
repository is reachable, so drift fails loudly.

Rules that matter in practice:

* `INVALID_ARGUMENT` — bad/unknown/missing argument (the SDK produces this
  before your function is even called).
* `PERMISSION_DENIED` — an authorization decision by *your* module (path outside
  a root, etc.). Never `FORBIDDEN`; it is not a C-04 code and core rewrites it.
* `TIMEOUT` — the SDK produces it when `timeout_ms` is exceeded.
* `CANCELLED` — `tools/cancel` arrived before the tool returned.
* `INTERNAL` — anything your tool raised that is not a `ToolError`. The
  traceback goes to the module log only; core and the model see a one-line
  message.
* `MODULE_DRAINING` — a call arrived after `module/drain`.

`message` is truncated to 300 characters. Never put secrets, pair codes or
absolute host paths you would not show an operator in it.

## 5. Pairing, keys, reconnects, containers

**Module key.** `MODULE_KEY_PATH` (default `data/module.key`) holds a 32-byte
X25519 seed as 64 hex chars, mode 0600. If the file exists it is **reused and
never overwritten** — a new key means a new fingerprint, and core's whitelist
will reject the module. Persist this file across container restarts: mount a
volume at `/data` and set `MODULE_KEY_PATH=/data/module.key` (AMEND-01). Losing
it is equivalent to re-pairing.

**Core public key.** IK needs core's static X25519 public key before the first
frame. Supply it as `CORE_PUBLIC_KEY` (64 hex) or let `data/core_pin.json`
(`{"core_public_key": "...", "fingerprint": "..."}`, 0600) provide it. Without
one the SDK exits 2 with `PERMISSION_DENIED` instead of guessing.

**Pairing (`--pair-code`).** `_pairing.normalize()` and the PSK derivation
(HKDF-SHA256 over `utf8(normalize(code)) || BLAKE2s256(core_pub)`, salt
`vade/pair/v1`, empty info) are implemented and verified against the four
equivalent spellings in `protocol/v1/kat.json`. The **handshake path is not
usable yet**: core's listener builds an IK responder only, and XXpsk3 needs the
PSK after msg2 while `FfiHandshake` takes it at construction. `--pair-code`
therefore fails closed with `UNSUPPORTED` and a message telling you to whitelist
the key out of band. This is a core-side gap (C2), not an SDK switch.

**TLS.** `wss://` verifies the system CA store by default. For a self-signed
core set `CORE_FINGERPRINT` to the TLS certificate's SHA-256 prefix (16 hex);
the SDK then requires an exact match. The TLS fingerprint is a different value
from the Noise `fp8` used for pairing — do not compare them.

**Reconnect.** Every disconnect restarts from IK + `module/register` (core may
have restarted and lost the table). Backoff is 0.5 s → 30 s, ×2 per attempt,
±20 % jitter, never gives up; repeated failures are logged at 1st/10th/… to
avoid a 30 s log flood. In-flight calls fail with `UNAVAILABLE`; core's router
is what retries them (a module must not retry on its own — that would double
side effects).

**Heartbeat / drain.** Every 10 s the SDK sends `module/heartbeat {load,
inflight}`; three failures in a row close the socket and trigger a reconnect.
On `module/drain {grace_ms}` the module stops accepting calls, answers
`{accepted:true}`, waits up to `grace_ms` for in-flight work, then exits 0.

**Concurrency.** `max_concurrency` (default 4) is an `asyncio.Semaphore`; sync
tools run in worker threads so one blocking call cannot stall the heartbeat.
Core, not the module, decides `OVERLOADED`.

## 6. `--standalone` vs connected

| Capability | `--standalone` | `--core` |
|---|---|---|
| Run a tool, get its result | ✅ | ✅ |
| Argument validation / typed errors | ✅ | ✅ |
| Manifest generation | ✅ (`--print-manifest`) | ✅ (same function) |
| Approval (`turn.approval_required`) | ❌ — nothing to approve against | ✅ (core policy) |
| Event broadcast (`module/event`, `events/deliver`) | ❌ | ✅ |
| Heartbeat, drain, `tools/cancel`, reconnect | ❌ | ✅ |
| `replace_tools` against a live table | local swap only | `capabilities.update`, core decides |

`--standalone` imports **no** transport module: on a machine without
`websockets` or the Noise binding it still works. Its stdout is one JSON object
(`{"ok":true,"result":...}` or `{"ok":false,"error":{...}}`); logs go to stderr
as JSON lines with `ts`, `level`, `module_id`, `instance_id`, `tool`,
`duration_ms`, `code`.

## 7. Freeze declaration

> **C-07 · `vade-sdk` 0.1.x is frozen as of 2026-11-13 (G1).**
>
> * The public names are `ModuleApp`, `ToolSpec`, `ToolError`, `run`.
> * No existing field, default, CLI flag, manifest field or error-envelope
>   shape may change meaning inside 0.1.x.
> * Only **new optional keyword arguments**, new CLI flags and new helper
>   methods may be added.
> * Removing a public name, changing a required `ToolSpec` field, or changing
>   the signature→schema mapping requires 0.2 and a migration note.
> * Any change to a wire-visible behaviour also requires a C-01/C-04/C-05 RFC
>   (`docs/agents/01-contracts.md` §七) — the SDK cannot lead the protocol.

Modules pin `vade-sdk==0.1.*` (SemVer 0.x: minor versions may break, so do not
use `>=0.1`).

---

## Appendix · what the SDK deliberately does not do

* No Noise implementation of its own: it calls the C-01 binding, so a byte-level
  mismatch is impossible by construction.
* No payload fragmentation (v1 has none): >65519-byte args/results are refused
  with `PAYLOAD_TOO_LARGE`.
* No retry of tool calls, no local approval, no logging of pair codes or keys.
* No module-specific knowledge: `fs`/`shell`/`memory` never appear in the SDK.
