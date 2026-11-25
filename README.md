# Vade

English | [简体中文](README.zh-CN.md)

**Vade** (偕行, "walking together") is a thin-core, module-first agent runtime.

The core does four jobs: **connect, route, run the turn loop, and manage keys and audit**. Models, tools, memory, and clients are separate processes that attach to it. Adding a capability, swapping a model, or replacing a client does not touch the core.

> The name is Latin *vade* ("go"), as in *vade mecum*, "go with me". The Chinese name 偕行 comes from the Book of Songs: 「与子偕行」, "walk together with you".

> [!WARNING]
> **Alpha software (`0.0.4-alpha`).** The protocol, data formats, and UI behavior can change without notice. Do not make it the only copy of anything you cannot afford to lose. If you self-host it or run it in production, follow the [deployment guide](docs/DEPLOYMENT.md), harden it for your environment, and accept the risk. The project is AI-assisted: the code has automated tests and human review, but style, comments, and edge-case coverage are not uniform. Audit and harden it before using it for anything critical.

> **Deploying Vade?** Read the [deployment guide](docs/DEPLOYMENT.md) (English) or the [中文部署教程](docs/DEPLOYMENT.zh-CN.md) before exposing it. They cover self-hosting and the production hardening checklist.

## What is Vade?

Vade is an agent runtime built around one rule: **the core must not know what a model, a tool, or a client is.**

- The core opens connections, authenticates peers, validates module manifests, routes RPC, runs the turn loop, persists sessions and events, asks for approvals, and writes audit records. That is all.
- Tools, model providers, and UI pages are contributed by **modules** — independent processes that connect to the core and declare what they provide.
- A client is not special: Android, desktop, and Web clients speak the same `ui/*` contract as any other module speaks `module/*`.
- `schema/` is the source of truth. The server and every client validate against the same JSON contract; the wire protocol is implemented in Rust and exposed to Python.

| | |
|---|---|
| Release | `v0.0.4-alpha` (pre-release) |
| Contract | rpc `1.7.0` — 49 methods in 3 directions · events `2.0.0` — 34 events · 34 error codes |
| Verification | [`docs/ALPHA.md`](docs/ALPHA.md) — evidence per alpha criterion and the known-defect list |

## Why?

Most agent frameworks put tool, model, and memory abstractions inside the core. Every new capability edits the core, couples to its language and release cycle, and can take the whole process down when it fails.

Vade inverts that:

- **Capabilities are processes.** A module can be written in any language, run on another machine, and be restarted or removed without stopping the core.
- **The contract is the integration point.** A module declares tools, model capabilities, and optional UI pages in a manifest; the core routes requests by capability, not by module name.
- **Clients are replaceable.** The core exposes sessions and events; any client that implements the `ui/*` contract can attach, and several clients can watch the same session.
- **The model is a backend, not the architecture.** Use the built-in OpenAI-compatible adapter, or move the model into its own module.

The trade-off is operational: you run and supervise more than one process, and alpha packaging is still basic.

## Architecture

```text
      Android · Windows desktop · Web                 modules
             (ui/* RPC)                    echo · fs · shell · web · model
                 │                                       │ (module/* RPC)
                 └──────────────►  core  ◄───────────────┘
                             connect · route · turn loop
                             keys · audit · SQLite store

       data plane :8443                         admin plane :8444

  schema/      the contract — rpc 1.7.0 (49 methods / 3 directions),
               events 2.0.0 (34 events), 34 error codes
  protocol/    Rust wire protocol: Noise IK/XXpsk3 + ChaCha20-Poly1305,
               with the Python binding used by the core
  sdk/         module-side SDK (Python): manifest, tools, pairing, transport
  admin-ui/    React admin served by the core (browser read-only;
               app WebView can write)
  modules/     index only — every module lives in its own repository
```

- **core** contains no model, tool, or memory logic. It knows how to route a turn to whichever module registered the required capability.
- **Modules are processes, not plugins.** A module starts its own process, connects to the core, and declares its capabilities and pages. The reference implementation is the echo module in its own repository.
- **`schema/` wins.** When code and contract disagree, the contract is the bug report.

## What is in 0.0.4-alpha

| Area | What is there |
|---|---|
| Core | WebSocket data plane on `:8443`; Noise handshakes for devices and modules, cookie sessions for browsers; module registry and routing; FIFO turn queue with `queue_pos` and interrupt; event stream with `seq` and replay; SQLite sessions and attach snapshots; inline approvals; append-only audit; usage budgets |
| Models | Built-in OpenAI-compatible adapter (`/v1/chat/completions`), optional `model/discover` upstream `/models` refresh (last good list persisted across restarts), `provider = "module"` for model modules, and a `mock` provider for offline UI work |
| Clients | Android, Windows desktop, and Web (wasm); fixtures by default, real core when explicitly enabled; chat send / stream / interrupt, session and group management, inline approvals, module pages |
| Admin | Admin listener on `:8444` with a React UI. Login and node governance are implemented; setup, audit, and settings pages are still placeholders |
| Modules | Five separate module repositories: echo (reference), fs, shell, web, model — each with its own CI and tests |

## Download

All `0.0.4-alpha` artifacts use the tag `v0.0.4-alpha`.

| Component | Artifact | Where |
|---|---|---|
| Core (Linux) | `vade-core-0.0.4-alpha-linux-x86_64.tar.gz` and `...-aarch64.tar.gz`, with SHA-256 checksums | [Vade `v0.0.4-alpha`](https://github.com/lshub2010/Vade/releases/tag/v0.0.4-alpha) |
| Clients | Debug-signed APK; Windows MSI and portable zip; Web (wasm) zip | [vade-client releases](https://github.com/lshub2010/vade-client/releases) |
| Modules | `py3-none-any` wheel plus `manifest.json` for each module | Each module repository's Releases page (links below) |

The core archive contains the `core` package, the Python protocol binding (`libvade_protocol.so`), `schema/`, the Python SDK, the built admin UI, `install.sh`, and the license files. Python dependencies are not vendored: `install.sh` creates a virtualenv and installs `requirements.txt`.

The core archive is Linux-only (`x86_64`, `aarch64`). On Windows or macOS, run from source. The Android APK is debug-signed and the Windows artifacts are unsigned — see [Not in 0.0.4-alpha](#not-in-004-alpha).

## Install from the release package (Linux)

```bash
ARCH=x86_64   # or aarch64
BASE=https://github.com/lshub2010/Vade/releases/download/v0.0.4-alpha

curl -LO "$BASE/vade-core-0.0.4-alpha-linux-$ARCH.tar.gz"
sha256sum "vade-core-0.0.4-alpha-linux-$ARCH.tar.gz"   # compare with the checksum on the release page

tar xzf "vade-core-0.0.4-alpha-linux-$ARCH.tar.gz"
cd "vade-core-0.0.4-alpha-linux-$ARCH"
./install.sh
```

`install.sh` creates `.venv/` and installs the runtime requirements. Then create a minimal config and start the core:

```bash
cat > core.toml <<'EOF'
[core]
listen   = "127.0.0.1:8443"
data_dir = "./data"
EOF

PYTHONPATH="core:protocol/core/python" .venv/bin/python -m vade --config core.toml
```

On first start the core prints a pairing code to **stdout**:

```text
pair_code=青松-AAAAAA purpose=browser expires_in=600s
```

The code is two Chinese characters plus six base32 characters, single-use, and valid for 600 seconds. Enter it in the client's pairing screen. `--no-pair-banner` suppresses the code (and the mint).

To make a desktop client attach to this core, launch it with `VADE_SOURCE=real`, `VADE_HOST=127.0.0.1`, `VADE_PORT=8443`, and `VADE_PAIR_CODE=<code>`. The Android client reads the same values from the intent extras `source`, `host`, `port`, and `pair_code` (bare keys, no `vade.` prefix).

## Run from source

Prerequisites: Python **3.11+**, and Rust **1.85+** only if you need to rebuild the protocol binding. The binding (`libvade_protocol.so` / `.dll`) is not committed, so a fresh clone must build it.

```bash
git clone https://github.com/lshub2010/Vade.git
cd Vade
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

cd protocol/core
cargo build --features uniffi --lib
cp target/debug/libvade_protocol.so python/     # Windows: libvade_protocol.dll
cd ../..

export PYTHONPATH="core:protocol/core/python"
.venv/bin/python -m vade --config core.toml
```

Both `PYTHONPATH` entries are required: `core` provides the `vade` package, and `protocol/core/python` provides the native `vade_protocol` binding. On Windows use `.venv\Scripts\python.exe` and a semicolon (`core;protocol/core/python`).

Run the tests and contract gates:

```bash
.venv/bin/python -m pytest core/tests -q
.venv/bin/python tools/check_contracts.py
.venv/bin/python tools/check_ledger.py
.venv/bin/python docs/check_docs.py
```

## Connect a model

The core ships with an OpenAI-compatible adapter. Add an `[llm]` section to `core.toml`:

```toml
[llm]
base_url    = "https://api.deepseek.com/v1"
model       = "deepseek-chat"
api_key_env = "VADE_LLM_API_KEY"
```

The core appends `/chat/completions` to `base_url`, so keep the `/v1` suffix. The key is read only from the environment variable named by `api_key_env`; it is never written to the config file or the logs. The model then appears in the client as `builtin/<model>`.

Set `provider = "mock"` to run a turn loop without a network call, or `provider = "module"` to serve chat from a model module (see [`docs/rfc/RFC-0005-model-capabilities-usage.md`](docs/rfc/RFC-0005-model-capabilities-usage.md)).

## Modules

Modules are not in this repository; [`modules/README.md`](modules/README.md) is only an index.

| Module | What it does | Repository |
|---|---|---|
| echo | Reference implementation: four trivial tools that prove manifest → RPC → result | [vade-module-echo](https://github.com/lshub2010/vade-module-echo) |
| fs | `list` / `stat` / `read` / `write` / `delete` under allow-listed roots | [vade-module-fs](https://github.com/lshub2010/vade-module-fs) |
| shell | Command execution with a cwd allow-list, timeout, and output truncation | [vade-module-shell](https://github.com/lshub2010/vade-module-shell) |
| web | `fetch` / `head` with SSRF protection | [vade-module-web](https://github.com/lshub2010/vade-module-web) |
| model | OpenAI-compatible model provider (`chat`) | [vade-module-model](https://github.com/lshub2010/vade-module-model) |

To write a module, start with the Python SDK in [`sdk/python`](sdk/python), the directory conventions in [`docs/design.md`](docs/design.md) §15.1, and the manifest contract in [`schema/manifest.schema.json`](schema/manifest.schema.json). A module connects to the core, registers its manifest, and then serves the calls the core routes to it.

## Documentation

| Read this | For |
|---|---|
| [`docs/OVERVIEW.md`](docs/OVERVIEW.md) | One-page overview |
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Start a core and connect a client |
| [`docs/PAIRING.md`](docs/PAIRING.md) | Pairing codes, browser sessions, troubleshooting |
| [`docs/design.md`](docs/design.md) | Technical design: components, protocol, crypto, modules |
| [`docs/rfc/`](docs/rfc) + [`schema/`](schema) | Per-RFC protocol contracts and the JSON schema |
| [`docs/ALPHA.md`](docs/ALPHA.md) | Alpha acceptance evidence and known defects |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) · [`docs/DEPLOYMENT.zh-CN.md`](docs/DEPLOYMENT.zh-CN.md) | Self-hosting and production hardening (English / 中文) |
| [`docs/release/0.0.2.md`](docs/release/0.0.2.md) | 0.0.2 release scope and open tasks |
| [`docs/status.md`](docs/status.md) | Batch status and milestones |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Branches, review, and merge rules |

## Not in 0.0.4-alpha

These are explicitly deferred, not silently missing:

- **iOS client.** Only Android, Windows desktop, and Web are built.
- **Memory and M4b self-evolution.** There is no memory module, no `memory` schema, and no core memory subsystem.
- **mcp-bridge module.** Not built.
- **Production signing and updates.** The APK is debug-signed, the Windows MSI is unsigned, and there is no update channel.
- **Upstream `/v1/models` discovery and multiple model modules side by side.** You configure the model name explicitly; cross-module `chat` conflicts need a separate RFC.
- **Real-model tool-call end-to-end acceptance (T3).** Tool cards and routing exist; the full model-driven loop is not yet accepted.
- **Admin pages beyond login and nodes.** Setup, audit, and settings are placeholder pages; skills, memory, and topology are not implemented.

The release checklist and the known-defect list are in [`docs/release/0.0.2.md`](docs/release/0.0.2.md) and [`docs/ALPHA.md`](docs/ALPHA.md).

## Contributing

Day-to-day work lands on `dev_gingshan`; `debug` is the baseline branch and only receives stage merges. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the review and merge gates.

## License

**Apache License 2.0.** See [`LICENSE`](LICENSE) for the full text and [`NOTICE`](NOTICE) for attribution and third-party notices. If you modify and redistribute Vade, keep the license and notice files, mark modified files, and do not imply endorsement by the original authors.
