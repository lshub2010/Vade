# Deploying Vade core in production

**English** | [简体中文](DEPLOYMENT.zh-CN.md)

This guide is for operators running the Vade core (`python -m vade`) on a Linux
server. Every command, path, key name and default below was checked against the
code on `dev_gingshan` for the `0.0.5-alpha` release; nothing here is invented
CLI surface. Where a feature is declared but not wired up, the text says so
explicitly instead of describing an intended behaviour.

| Item | Value |
|---|---|
| Release layout | `vade-core-0.0.5-alpha-linux-{x86_64,aarch64}.tar.gz` |
| Python | 3.11+ (CI and this guide use 3.12) |
| Rust | 1.85+ — **only** if you rebuild `libvade_protocol.so`; the release ships it |
| Init system | systemd (samples in `deploy/systemd/`) |
| Listener 1 | data plane `127.0.0.1:8443` — `/healthz`, `/ui/session`, `/ws`, `/ui/blob*` |
| Listener 2 | admin plane `127.0.0.1:8444` — `/admin/*`, `/login` |
| State | one `data_dir` per process, guarded by `data/core.lock` |

Two rules shape every deployment decision:

1. **The two planes are separate listeners with separate trust models.** The
   data plane never serves `/admin` (it answers `404`, not `403` on purpose).
   The admin plane is loopback-only by default and its `Host` allow-list is
   built from `[core].admin_host` — see §10.3 before you point a reverse proxy
   at it.
2. **Browser sessions are `Secure` cookies.** `POST /ui/session` always sets
   `__Host-ac_ui=…; Secure`. Over plain HTTP a real browser will discard it, so
   production needs TLS somewhere — at the proxy or on the core — unless you
   only ever use `http://127.0.0.1`.

---

## 1. What you are deploying

| Process | Port (default) | Protocol | Serves |
|---|---|---|---|
| core data plane | 8443 | HTTP + WebSocket | `/healthz`, `POST/DELETE /ui/session`, `/ws`, `/ui/blob*` |
| core admin plane | 8444 | HTTP (loopback) or HTTPS | `/admin/setup`, `/admin/bind`, `/admin/api/*`, `/login` |
| modules | outbound only | `ws://` / `wss://` to 8443 | connect to core, declare a manifest, serve RPC |
| clients | outbound only | `wss://…/ws` | pair once, then attach |

There is **one process** with **two listeners** and one event loop. `SIGTERM`
stops both (verified: the process logs `Application shutdown complete` and exits
0). Exactly one core may use a given `data_dir`; a second one refuses to start
with `another core is already running with data dir … (held: …/core.lock)`.

Minimum useful config is three keys. Everything else has a schema default.

---

## 2. System requirements

| Resource | Minimum | Notes |
|---|---|---|
| OS | Linux x86_64 or aarch64, glibc | The release `.so` is architecture-specific |
| Python | 3.11+ | `tomllib` is imported unconditionally, so 3.10 and older cannot load the config |
| RAM | 1 GiB | Argon2id hashes at 64 MiB per operation; SQLite page cache; WS buffers |
| Disk | ~250 MiB for the app + venv, plus data | default blob quota is 2 GiB **per device** |
| Ports | 8443/8444 loopback, or one public 443 at the proxy | never expose 8444 to the internet |
| Time | NTP-synced clock | pair codes and setup tokens are time-limited |

Install the OS packages the wheel set needs on a slim image:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates tar
```

`cryptography`, `argon2-cffi`, `uvicorn`, `starlette` and `httpx` all ship
manylinux wheels, so no compiler is needed for a normal install. You only need
Rust (`rustup`, 1.85+) plus `cargo` if you rebuild the protocol binding:

```bash
cd protocol/core
cargo build --release --features uniffi --lib
cp target/release/libvade_protocol.so python/
```

---

## 3. Install from the 0.0.2 release tarball

### 3.1 Download and verify

```bash
VADE_VERSION=0.0.5-alpha
ARCH=x86_64                     # or aarch64
BASE="https://github.com/lshub2010/Vade/releases/download/v${VADE_VERSION}"

curl -fLO "${BASE}/vade-core-${VADE_VERSION}-linux-${ARCH}.tar.gz"
curl -fLO "${BASE}/SHA256SUMS"

# SHA256SUMS covers every artifact in the release (core, modules, clients),
# so --ignore-missing lets you verify just the tarball you downloaded.
sha256sum -c SHA256SUMS --ignore-missing
```

Do not skip the checksum: the tarball contains a native library that the core
loads in-process.

### 3.2 Unpack and install the Python environment

```bash
sudo install -d -m 0755 /opt/vade/releases
sudo tar -C /opt/vade/releases -xzf "vade-core-${VADE_VERSION}-linux-${ARCH}.tar.gz"

sudo useradd --system --home /var/lib/vade --shell /usr/sbin/nologin vade
sudo chown -R vade:vade "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}"

# install.sh creates .venv/ and pip-installs requirements.txt as the invoking user
sudo -u vade "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}/install.sh"

sudo ln -sfn "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}" /opt/vade/current
```

`install.sh` prints the start command when it finishes:

```text
Installed. Start with:
  PYTHONPATH="core:protocol/core/python" .venv/bin/python -m vade --config core.toml
Copy deploy/core.toml.example to core.toml and edit it first.
```

### 3.3 What is inside the tree

```text
/opt/vade/current/
├── .venv/                     # created by install.sh
├── core/vade/                 # the `vade` package
├── protocol/core/python/      # vade_protocol.py + libvade_protocol.so
├── schema/                    # config.schema.json, manifest, UI/API contracts
├── sdk/python/                # module-side SDK
├── admin-ui/dist/             # prebuilt admin assets, mounted at /login and /assets (§7.4)
├── deploy/                    # the samples shipped with this document
├── docs/                      # DEPLOYMENT(+zh-CN), PAIRING
├── requirements.txt
└── install.sh
```

Both `PYTHONPATH` entries are required:

| Path | Provides | Missing → |
|---|---|---|
| `core` | the `vade` package | `No module named vade` |
| `protocol/core/python` | `vade_protocol` + `.so` | `protocol core python binding is not importable` |

Use absolute paths in the unit file so a wrong `WorkingDirectory` cannot break
the import.

---

## 4. Service account, directories, permissions

| Path | Owner | Mode | Contents |
|---|---|---|---|
| `/opt/vade/current` | `vade:vade` | 0755 (symlink) | code, read-only at runtime |
| `/etc/vade` | `root:vade` | 0750 | `core.toml`, `vade.env`, `tls/` |
| `/etc/vade/core.toml` | `root:vade` | 0640 | config (contains no secrets — enforced) |
| `/etc/vade/vade.env` | `root:vade` | 0640 | `VADE_LLM_API_KEY=…` and friends |
| `/etc/vade/tls/server.key` | `root:vade` | 0640 | only if core terminates TLS |
| `/var/lib/vade` | `vade:vade` | 0700 | parent of `data_dir` |
| `/var/lib/vade/data` | `vade:vade` | 0700 | created by core on first start |

```bash
sudo install -d -m 0750 -o root -g vade /etc/vade
sudo install -d -m 0700 -o vade -g vade /var/lib/vade
sudo install -d -m 0750 -o root -g vade /etc/vade/tls
```

The core creates `data_dir` with mode `0700` and its own key files with `0600`
(`core.key`, `setup.token`, `admin/admin.json`, `identity/whitelist.json`,
`ui_sessions.json`). It does **not** chmod everything it writes: SQLite files
(`pairing.sqlite3`, `sessions.db`, `blob.sqlite3`, `audit/audit.db`) and
`blobs/` inherit the process umask. Setting `UMask=0077` in the unit (the
sample does) keeps them at `0600`/`0700`.

Run everything as the dedicated user. Never run the core as root: it binds no
privileged port, and the admin plane treats loopback as already trusted.

---

## 5. `core.toml`

### 5.1 Minimal production config

`/etc/vade/core.toml` (mode 0640):

```toml
[core]
listen     = "127.0.0.1:8443"
admin_host = "127.0.0.1:8444"
data_dir   = "/var/lib/vade/data"
log_level  = "info"

# TLS: leave BOTH keys unset when Caddy/Nginx terminates TLS in front of the
# core. Setting only one of them silently disables TLS (see §6.3).
# [tls]
# cert = "/etc/vade/tls/server.crt"
# key  = "/etc/vade/tls/server.key"

[llm]
provider    = "openai_compatible"            # default; may be omitted
base_url    = "https://api.deepseek.com/v1"  # MUST include /v1; core appends /chat/completions
model       = "deepseek-chat"
api_key_env = "VADE_LLM_API_KEY"             # an env var NAME, never the key itself
```

### 5.2 What each key does

| Key | Default | Apply | Meaning |
|---|---|---|---|
| `core.listen` | `127.0.0.1:8443` | restart | data-plane bind. Pattern `^[^:]+:[0-9]{1,5}$` — **no IPv6 literal** |
| `core.admin_host` | `127.0.0.1:8444` | restart | admin-plane bind **and** the `Host`/`Origin` it accepts (§10.3) |
| `core.data_dir` | `./data` | restart | state directory, resolved against the **process CWD**, not this file |
| `core.log_level` | `info` | hot | `debug`/`info`/`warning`/`error`; JSON lines on stdout |
| `tls.cert` / `tls.key` | unset | restart | PEM paths. Both or neither; both must exist or startup fails |
| `tls.spki_pin` | unset | restart | accepted by the schema, **not consumed** by this build |
| `tls.san` | `[]` | restart | accepted, **not consumed** (no certificate is generated — §6.4) |
| `server.ui_origins` | `[]` | restart | exact CORS allowlist for `/ui/session` and `DELETE`; `*` is refused at load |
| `server.listen_hosts` | `["127.0.0.1","localhost"]` | restart | only used to validate `ui_origins` hosts; an origin whose host is absent aborts startup |
| `audit.retention_days` | `30` | restart | day tables older than this are dropped; `< 1` is refused |
| `audit.path` | `data/audit.sqlite3` | restart | the literal default means “use `<data_dir>/audit/audit.db`” |
| `attach.max_bytes` | `104857600` (100 MiB) | restart | per-upload ceiling — size your proxy body limit against it |
| `attach.quota_bytes` | `2147483648` (2 GiB) | hot | per-device blob quota |
| `llm.provider` | `openai_compatible` | restart | `openai_compatible`, `module`, or `mock` |
| `llm.base_url` | `""` | restart | endpoint root **including** `/v1` |
| `llm.model` | `""` | hot | default model name |
| `llm.api_key_env` | `""` → `VADE_LLM_API_KEY` | restart | name of the env var holding the key |
| `llm.default_model` | `""` | hot | global `<provider>/<model>` id when a session has no choice |
| `llm.model_module` | `""` | restart | with `provider="module"`, restrict to one module id |
| `llm.fallback_to_builtin` | `true` | restart | fall back to the builtin adapter when the module is offline |
| `llm.capabilities` | `{}` | restart | `display_name`, `supports_tools`, `context_window`, … |

The root has `additionalProperties: false`. **A typo aborts startup** with
`UNKNOWN_CONFIG_KEY: <dotted.key>` — it never silently falls back to a default.
The exhaustive list with ranges is `schema/config.schema.json`.

### 5.3 Secrets

`core.toml` must not contain credentials. Two checks run at load time:

* a value under a key named `api_key`, `secret`, `password`, `token`, … is
  refused;
* any string starting with `sk-`, `sk_`, `ghp_`, `xoxb-` or `AKIA` is refused.

Put the value in the environment and reference the **name**:

```bash
# /etc/vade/vade.env  (root:vade 0640)
VADE_LLM_API_KEY=[REDACTED]
```

### 5.4 Environment overrides

Every key can be overridden with `VADE_<SECTION>__<KEY>` (double underscore,
upper-case). Values are coerced to the schema type; arrays are JSON.

```bash
VADE_CORE__LOG_LEVEL=debug
VADE_TLS__CERT=/etc/vade/tls/server.crt
VADE_SERVER__UI_ORIGINS='["https://vade.example.com"]'
```

Precedence: **CLI > env > `<data_dir>/config.overrides.json` > `core.toml` >
defaults**. The admin page writes only `config.overrides.json`; nothing ever
rewrites your `core.toml`.

### 5.5 Two loading traps

* `--config` pointing at a **missing file does not fail**. The toml is skipped
  and the core starts on defaults (`127.0.0.1:8443`, `./data`) — which is
  exactly why the service can come up “healthy” on the wrong port. Check the
  `vade: listeners up — data ws://…` banner or `systemctl show -p ExecStart`.
* `data_dir` is relative to the **working directory**. Always use an absolute
  path, or the state lands wherever systemd started you.

---

## 6. TLS

### 6.1 Three supported models

| Model | Config | Use when |
|---|---|---|
| Proxy terminates TLS | core on `127.0.0.1`, no `[tls]` | recommended: Caddy/Nginx owns certificates, renewal and HSTS |
| Core terminates TLS | `[tls].cert` + `[tls].key` on both planes | no proxy, or admin bound to a LAN address |
| Loopback only | no `[tls]`, no proxy | development; browsers accept `Secure` cookies on `http://127.0.0.1` |

The same certificate is used for both listeners; there is no per-plane TLS
setting.

### 6.2 Proxy-terminated (recommended)

```toml
[core]
listen     = "127.0.0.1:8443"
admin_host = "127.0.0.1:8444"
```

Leave `[tls]` out entirely. Caddy/Nginx listens on 443, forwards to 8443 and
preserves the `Host` header (both samples do). See §10.

### 6.3 Core-terminated

```toml
[tls]
cert = "/etc/vade/tls/server.crt"
key  = "/etc/vade/tls/server.key"
```

* Both keys must be set. `cert` without `key` (or the reverse) results in
  **plaintext**, not an error — the code takes the TLS branch only when both
  are truthy.
* A configured path that does not exist fails startup with
  `configured TLS file … does not exist`.
* With TLS on, the data plane is `https`/`wss` and the setup banner prints
  `https://…` for the admin plane.
* Binding the admin plane to a non-loopback host **requires** cert+key:
  `refusing to start the admin plane on non-loopback host … without TLS`.

Fingerprints:

```bash
# SPKI pin, SHA-256(SubjectPublicKeyInfo)[:16] — what clients store as `tls=`
python -m vade --print-tls-fingerprint          # uses [tls].cert from the config
python -m vade admin print-tls-fingerprint --cert /etc/vade/tls/server.crt
```

### 6.4 Self-signed certificates are **not** generated

`schema/config.schema.json` notes “null = auto-generated self-signed”, and
`vade.identity.tls.ensure_certificates()` implements such a generator — but
**`python -m vade` never calls it**. With `[tls]` empty the core serves plain
HTTP and prints a `http://` admin URL. Treat certificate provisioning as your
job:

```bash
# internal CA or public CA either way; core only reads PEM files
sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout /etc/vade/tls/server.key -out /etc/vade/tls/server.crt \
  -days 825 -nodes -subj "/CN=vade.internal" \
  -addext "subjectAltName=DNS:vade.internal,IP:192.0.2.10"
sudo chown root:vade /etc/vade/tls/server.{crt,key}
sudo chmod 0640 /etc/vade/tls/server.key
```

`ensure_certificates()` is exercised by tests only; there is no CLI flag for
it. This is a documented gap, not a supported path.

---

## 7. First-run initialisation

Before initialisation the admin plane has no password. State lives in
`<data_dir>`:

| File | Before setup | After setup |
|---|---|---|
| `setup.token` | present, `acs_` + 43 chars, 0600 | deleted |
| `admin/admin.json` | absent | present, 0600 — **its existence is the only “initialised” flag** |
| `admin/core_identity.key` | maybe absent | present (admin-plane identity) |

On every start of an **uninitialised** core a new token is minted; the banner
prints only a mask:

```text
setup_token=acs_9f3c… setup_path=/var/lib/vade/data/setup.token
vade: first-run admin setup — open http://127.0.0.1:8444/admin/setup. A loopback request is already authorised; a remote browser needs the full token from the file above (`python -m vade admin print-setup-token`). The token rotates on every restart until setup completes.
```

Rules that matter operationally:

* the token is **regenerated on every restart** until setup completes;
* `VADE_SETUP_TOKEN` pins it instead (persisted to the file verbatim) — note it
  is visible in `/proc/<pid>/environ` and `docker inspect`, so the file is the
  better path;
* when the core is already initialised, any leftover `setup.token` is deleted
  at startup;
* a successful setup writes `admin.json` **first**, then unlinks the token.

### 7.1 Headless (recommended for servers)

Stop the core first — the CLI refuses to touch a `data_dir` whose `core.lock`
is held:

```bash
sudo systemctl stop vade-core

# 12+ chars, not in the weak list, more than 3 distinct characters
printf '%s\n' 'correct horse battery staple 9' \
  | sudo -u vade env PYTHONPATH=/opt/vade/current/core:/opt/vade/current/protocol/core/python \
      /opt/vade/current/.venv/bin/python -m vade admin init \
      --password-stdin --data-dir /var/lib/vade/data

sudo systemctl start vade-core
```

Verified output: `initialised`, `admin/admin.json` at 0600, `setup.token`
removed. Add `--pubkey-file device.pub` to enrol the first device key at the
same time.

### 7.2 JSON API, loopback

With the core running and uninitialised, a loopback request needs no token:

```bash
curl -s http://127.0.0.1:8444/admin/setup
# {"setup_required":true,"requires_token":false,
#  "bind_url":"vade://bind?h=127.0.0.1:8444&fp=<fp8>&t=<ticket>&v=1",
#  "fp8":"<fp8>","expires_at":1795441520}

curl -s -X POST http://127.0.0.1:8444/admin/setup \
  -H 'content-type: application/json' \
  -d '{"password":"correct horse battery staple 9"}'
# 200, empty body; admin.json written, setup.token deleted
```

Password policy (`vade.admin.auth.validate_password`): at least 12 characters,
at most 256, not in the common-password list, more than 3 distinct characters.
Hashes are Argon2id `m=64 MiB, t=3, p=1`.

### 7.3 Remote setup with the token

A non-loopback `GET /admin/setup` returns only
`{"setup_required":true,"requires_token":true}` — no `bind_url`, no `fp8`, no
`acs_` string. To proceed:

```bash
# 1) read the token on the server (full value only lives in the file)
sudo -u vade env PYTHONPATH=… /opt/vade/current/.venv/bin/python -m vade \
  admin print-setup-token --data-dir /var/lib/vade/data

# 2) exchange it for a short-lived unlock cookie (sets __Host-ac_setup)
curl -s -X POST http://<admin-host>:8444/admin/setup/unlock \
  -H 'content-type: application/json' \
  -d '{"setup_token":"acs_…"}'

# 3) then POST /admin/setup as in §7.2, carrying that cookie
```

Failed unlocks share the pair-code ledger: 5 wrong attempts per IP per 5
minutes → `ADMIN_LOCKED`. But remember §10.3: binding the admin plane to a
non-loopback address requires TLS, and proxying it under a public name is not
supported in this build.

### 7.4 The admin UI in the release tree

The release tarball contains a prebuilt `admin-ui/dist` (no Node.js on the
target). `python -m vade` resolves it as
`Path(__file__).parents[2] / "admin-ui" / "dist"` — i.e.
`/opt/vade/current/admin-ui/dist` — and passes it to
`create_admin_app(..., static_dist=…)`, so `GET /login` and `GET /` serve the
built `index.html`.

| Request | This build |
|---|---|
| `GET /login`, `GET /` | `200 text/html` (the SPA shell) |
| `GET /admin` | `404 {"code":"NOT_FOUND"}` — there is no `/admin` route; the entry point is `/login` |
| `GET /admin/setup` (uninitialised) | JSON state (`setup_required`, `bind_url`, `fp8`), not HTML |
| `GET /admin/setup` (initialised) | `302` → `/login` |
| `GET /assets/index-*.js` | `200` (the mounted SPA bundle) |

`create_admin_app` mounts the bundle through `vade.admin.static.mount_static`
when `admin-ui/dist/index.html` exists, so the release tree serves a working
SPA at `/login`. The setup wizard remains JSON (`GET /admin/setup`), and the
local CLI (§7.5) remains the recovery path if the UI cannot be reached.

A source checkout that never ran `npm run build` has no `admin-ui/dist`, so
`static_dist` is `None` and `/login` falls back to the JSON discovery response
`{"login":"POST /admin/api/login","setup_required":…}`. That fallback is also
what a trimmed install without the directory will show.

### 7.5 Recovery commands

| Goal | Command (always pass `--data-dir`) |
|---|---|
| print the identity fp8 | `python -m vade admin print-fingerprint --data-dir …` |
| print the setup token (pre-init only) | `python -m vade admin print-setup-token --data-dir …` |
| rotate the setup token (core stopped) | `python -m vade admin rotate-setup-token --data-dir …` |
| change the password (TTY, core stopped) | `python -m vade admin reset-password --data-dir …` |
| back to setup mode (wipes `data/admin`) | `python -m vade admin reset --data-dir …` (types `RESET`) |
| add a device key (rescue) | `python -m vade admin trust-key --pubkey-file … --label …` |
| revoke a device | `python -m vade admin revoke-device --id …` |

`admin --data-dir` defaults to `./data`, **not** to `[core].data_dir` (the CLI
calls `load_config()` without your `--config`). Always pass it explicitly.

---

## 8. Pairing a client

### 8.1 The banner

On every start, once **both** sockets are bound, the core mints one
single-use browser code and prints two lines to **stdout**:

```text
pair_code=幕峡-DVN25E purpose=browser expires_in=600s
vade: first-run pairing — enter this code in the app/browser (it redeems via POST /ui/session). Single-use, expires in 600s; restart the core for a fresh one. Pass --no-pair-banner to hide this.
```

* `purpose=browser` is fixed for the banner; `expires_in` is seconds (600).
* The code is `2 Chinese characters + "-" + 6 base32` and is never written to
  the JSON log or to disk in clear — only `scrypt(normalize(code))` in
  `pairing.sqlite3`.
* `--no-pair-banner` suppresses **both** the print and the mint (scripts,
  tests, and any deployment that pairs through another channel).
* With `--no-pair-banner`, `journalctl` has nothing to leak. Without it, the
  code sits in the journal for its 10-minute lifetime — treat the journal as a
  credential store for that window.

```bash
sudo journalctl -u vade-core -o cat | grep '^pair_code=' | tail -1
```

### 8.2 Redeem it

```bash
printf '{"pair_code":"%s","client_name":"ops-laptop"}' "$CODE" > body.json
curl -s -i -X POST https://vade.example.com/ui/session \
  -H 'content-type: application/json' --data-binary @body.json
```

Success is `200` with **two** cookies and `{"device_id":"ui-…"}`:

```text
set-cookie: __Host-ac_ui=…; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=2592000
set-cookie: __Host-ac_csrf=…; Path=/; Secure; SameSite=Lax; Max-Age=2592000
```

The session lives 30 days server-side (`ui_sessions.json`, digests only,
0600). A second redeem of the same code returns `CODE_INVALID`; an expired one
returns `CODE_EXPIRED`. Same code wrong 5 times is destroyed; 5 failures per IP
per 5 minutes locks that IP.

### 8.3 Client configuration

Desktop / WebAssembly clients read environment variables:

```bash
VADE_SOURCE=real VADE_HOST=vade.example.com VADE_PORT=443 \
VADE_PAIR_CODE='幕峡-DVN25E' ./vade-ui
```

Android reads intent extras `source`, `host`, `port`, `pair_code` (bare keys,
no `vade.` prefix). Details and the current “no pairing input box yet”
limitation are in [`PAIRING.md`](PAIRING.md).

`wss://` on 443 goes through your proxy; `wss://vade.example.com:8443` only
works if you expose 8443 directly (not recommended).

### 8.4 Revoke

```bash
curl -s -i -X DELETE https://vade.example.com/ui/session \
  -H 'Cookie: __Host-ac_ui=<token>; __Host-ac_csrf=<csrf>'
```

`204` means the row is gone and its `/ws` socket is closed. To log **every**
browser out, stop the core and delete `<data_dir>/ui_sessions.json`; that file
is the whole browser-session table.

---

## 9. Modules

A module is a separate process that connects **out** to the data plane,
performs a Noise IK handshake with its own X25519 key, and calls
`module/register` with a manifest. core has no module names in it.

### 9.1 Install

```bash
sudo install -d -m 0755 /srv/vade-modules/echo
sudo python3 -m venv /srv/vade-modules/echo/venv
sudo /srv/vade-modules/echo/venv/bin/pip install ./vade_module_echo-0.0.2-py3-none-any.whl
```

### 9.2 Create the module key and read its public half

The SDK reads `MODULE_KEY_PATH` (default `data/module.key`): 32 random bytes as
64 hex characters, mode 0600, and it **never overwrites** an existing file. A
new key is a new fingerprint and core will reject it, so back this file up.

```bash
sudo install -d -m 0700 -o vade -g vade /srv/vade-modules/echo/data
sudo -u vade sh -c 'openssl rand -hex 32 > /srv/vade-modules/echo/data/module.key'
sudo chmod 0600 /srv/vade-modules/echo/data/module.key

# public key + fp8, exactly the formula core uses: sha256(pub)[:16]
sudo -u vade /srv/vade-modules/echo/venv/bin/python - <<'EOF'
import hashlib, pathlib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
seed = bytes.fromhex(pathlib.Path("data/module.key").read_text().strip())
pub = X25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)
print("public_key:", pub.hex())
print("fp8:", hashlib.sha256(pub).hexdigest()[:16])
EOF
```

### 9.3 Whitelist it (manual today)

**Module pair codes do not work yet.** `POST /admin/api/invites
{"purpose":"module","module_id":"com.example.echo"}` mints a code and a
`vade://pair?…` payload, but the SDK refuses `--pair-code` with
`UNSUPPORTED` (“core only accepts IK responders today”), and core’s listener
builds an IK responder only. `vade admin trust-key` adds **device** keys, not
module keys. The working path is a manual whitelist entry.

The core public key the module must pin:

```bash
sudo -u vade env PYTHONPATH=/opt/vade/current/core:/opt/vade/current/protocol/core/python \
  /opt/vade/current/.venv/bin/python -c \
  "from vade.identity import load_or_create; print(load_or_create('/var/lib/vade/data').public_key_hex)"
```

Add the entry to `<data_dir>/identity/whitelist.json` **while the core is
stopped** (it is read once at startup and rewritten atomically):

```json
{
  "version": 1,
  "entries": [
    {
      "module_id": "com.example.echo",
      "instance_id": "main",
      "fingerprint": "09c6d95ef0df9891",
      "public_key": "b53744df66b49d57…",
      "added_at": 1795441520
    }
  ]
}
```

`fingerprint` must equal `sha256(bytes.fromhex(public_key))[:16]`, or the loader
drops the entry (fail closed) and `candidate_keys()` will never authenticate
it. `instance_id` defaults to `main` on the module side
(`--instance-id` / `VADE_INSTANCE_ID`). Restart the core after editing.

### 9.4 Run

```bash
sudo -u vade env \
  MODULE_KEY_PATH=/srv/vade-modules/echo/data/module.key \
  VADE_DATA_DIR=/srv/vade-modules/echo/data \
  CORE_PUBLIC_KEY=<64 hex from 9.3> \
  /srv/vade-modules/echo/venv/bin/python -m echo_module \
  --core ws://127.0.0.1:8443
```

* `--core` also reads `VADE_CORE_URL`; `ws://` only if the core is plaintext on
  loopback, otherwise `wss://vade.example.com`.
* For `wss://` with a self-signed certificate set `CORE_FINGERPRINT` to the
  **certificate DER** SHA-256 prefix — `openssl x509 -in server.crt -outform DER
  | sha256sum | cut -c1-16`. This is a different value from the SPKI pin that
  `--print-tls-fingerprint` prints; the SDK compares the certificate DER.
* The module logs `module identity loaded` with its fingerprint, then
  `registered`. Reconnects are automatic (0.5 s → 30 s, jittered, forever).

### 9.5 Inspect and revoke

The registry shows one row per whitelisted key (`GET /admin/api/nodes`,
authenticated). Revoking removes the whitelist record and closes live
connections; the module then loops on handshake failure until you re-pair.

---

## 10. systemd

Copy [`deploy/systemd/vade-core.service`](../deploy/systemd/vade-core.service)
to `/etc/systemd/system/vade-core.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vade-core
sudo systemctl status vade-core
sudo journalctl -u vade-core -f
```

The unit assumes:

* `User=vade`, `Group=vade`, `WorkingDirectory=/opt/vade/current`;
* `EnvironmentFile=-/etc/vade/vade.env` (optional file, hence the `-`);
* `PYTHONPATH` as absolute paths to `core` and `protocol/core/python`;
* `ExecStart=… -m vade --config /etc/vade/core.toml --strict`;
* `UMask=0077`, `StateDirectory=vade` (mode 0700), `ProtectSystem=strict`,
  `ProtectHome=true`, `PrivateTmp=true`, `NoNewPrivileges=true`;
* `Restart=on-failure`, `RestartSec=3`, `TimeoutStopSec=30`.

`--strict` makes a missing `session`/`policy`/`audit` component abort startup
instead of logging “dev mode only”. The release contains all of them, so this
is a safety net, not a risk. `SIGTERM` is handled: both listeners stop and the
process exits 0.

`StateDirectory=vade` creates `/var/lib/vade` (0700) and keeps it writable
under `ProtectSystem=strict`; `data_dir` inside it is still created by the
core.

---

## 11. Reverse proxy

Publish **only** the data plane. The admin plane stays on loopback and is
reached through an SSH tunnel (§10.3).

### 11.1 Caddy

```caddyfile
vade.example.com {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8443 {
		# Caddy preserves the original Host by default, which is what the
		# core's same-origin WebSocket check compares against.
		flush_interval -1
	}
	# Uploads: attach.max_bytes defaults to 100 MiB. Caddy has no default
	# body limit; uncomment to cap it.
	# request_body {
	# 	max_size 110MB
	# }
}
```

Full sample: [`deploy/Caddyfile`](../deploy/Caddyfile). Caddy handles the
WebSocket upgrade automatically and renews certificates itself.

### 11.2 Nginx

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    http2 on;
    server_name vade.example.com;

    ssl_certificate     /etc/letsencrypt/live/vade.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/vade.example.com/privkey.pem;

    client_max_body_size 110m;      # >= attach.max_bytes (100 MiB)

    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_http_version 1.1;
        proxy_set_header Host $host;              # keep the public Host
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;                 # long-lived /ws
        proxy_send_timeout 3600s;
        proxy_buffering off;                      # no batching for events
    }
}
```

Full sample: [`deploy/nginx.conf`](../deploy/nginx.conf) (drop-in for
`/etc/nginx/conf.d/`; it contains the `map`, which must live in `http{}`).

### 11.3 Why the admin plane is not proxied

`AdminConfig.allowed_hosts` is built from `[core].admin_host` plus
`127.0.0.1:8444` and `localhost:8444`; there is **no `[admin]` section** in the
schema and `admin.allowed_hosts` is not settable. Every `/admin` request whose
`Host` is not in that set gets `403 ORIGIN_MISMATCH`, and every request with an
`Origin` other than `http(s)://<admin_host>` is rejected the same way. Bind it
to `127.0.0.1` and tunnel:

```bash
ssh -N -L 8444:127.0.0.1:8444 ops@vade.example.com
# then open http://127.0.0.1:8444/login (see §7.4 for what this build serves)
```

If you must reach it over the network without SSH: set
`admin_host = "192.0.2.10:8444"` (an address of that host), provide
`[tls].cert`/`[tls].key` (mandatory for a non-loopback admin bind), firewall
8444 to trusted networks, and browse to `https://192.0.2.10:8444` — the
`Host`/`Origin` then match. A name-based vhost in front of it will not work.

`--admin-allow-cidr` is accepted by argparse but **not enforced** in this
build.

### 11.4 Proxy side effects you must know

* **The pair-code rate limit becomes global.** `uvicorn` runs with
  `proxy_headers=False` and `CoreApp` is constructed without
  `trust_forwarded_for`, so every proxied client is `127.0.0.1`. Five wrong
  codes lock pairing for everybody for 5 minutes. There is no config switch in
  this build.
* `client_max_body_size` must be at least `attach.max_bytes` or large uploads
  fail at the proxy with an HTML error instead of the core's JSON `413`.
* `X-Forwarded-For` is not trusted; do not build audit expectations on it.

---

## 12. Health checks

| Check | Command | Expected |
|---|---|---|
| data plane | `curl -fsS http://127.0.0.1:8443/healthz` | `{"ok": true, "version": "0.1.0", "policy_mode": "default"}` |
| admin plane | `curl -fsS http://127.0.0.1:8444/admin/api/health` | `{"ok":true,"version":"1.0.0","e2e_enabled":false,"audit":{…}}` |
| port bound | `ss -ltnp \| grep -E '8443\|8444'` | two listeners on the configured addresses |
| process | `systemctl is-active vade-core` | `active` |

`/healthz` is unauthenticated and deliberately returns no config, fingerprints
or module list. `version` is `vade.__version__` (for example `0.1.0-beta.1`),
so it tracks the release.

`policy_mode` is `allow_all` only when `[dev].allow_all = true`; production
must leave it `false` (the default).

A systemd timer or your monitor should treat a non-200 or a body without
`"ok": true` as down. There is no `/readyz` and no metrics endpoint.

---

## 13. Backup and restore

Back up **both** the state and the configuration:

```bash
sudo systemctl stop vade-core

sudo tar --numeric-owner -C / -czf "/var/backups/vade-$(date +%F-%H%M).tar.gz" \
  var/lib/vade etc/vade

sudo systemctl start vade-core
```

What is inside `data_dir` (all of it is state):

| Path | Holds | Restore impact |
|---|---|---|
| `core.key` | core X25519 identity seed (0600) | losing it changes `fp8`; every module pin breaks |
| `pairing.sqlite3` / `pairing.pepper` | hashed pair codes + pepper | losing it only invalidates outstanding codes |
| `identity/whitelist.json` | trusted module keys | losing it forces re-pairing every module |
| `admin/admin.json` | Argon2id password hash | losing it forces `admin reset` |
| `admin/core_identity.key` | admin bind identity | losing it invalidates bind tickets |
| `admin/devices.json` | device public keys | losing it revokes every device |
| `admin/sessions.json` | admin refresh sessions | losing it logs admins out |
| `ui_sessions.json` | browser sessions (`sha256(token)` only) | losing it logs browsers out |
| `sessions.db`, `blob.sqlite3`, `blobs/` | conversations, attachment metadata and bytes | losing it loses history/attachments |
| `audit/audit.db` | audit trail (daily tables) | losing it loses the trail |
| `skills/` | installed skills | losing it loses skills |
| `setup.token` | pre-init only | **do not restore**: it rotates on start |

```bash
# restore
sudo systemctl stop vade-core
sudo tar --numeric-owner -C / -xzf /var/backups/vade-YYYY-MM-DD-HHMM.tar.gz
sudo chown -R vade:vade /var/lib/vade
sudo systemctl start vade-core
curl -fsS http://127.0.0.1:8443/healthz
```

Notes:

* Stopping the core is the only way to get a guaranteed-consistent copy of
  SQLite files in WAL mode. If you cannot stop it, use
  `sqlite3 <db> ".backup '<out>'"` per database and copy `blobs/` separately —
  do not `cp` a live WAL database.
* Encrypt the archive: it contains `core.key`, `admin.json`, device keys and
  every conversation.
* Test the restore on a spare host before you need it. Losing `core.key` is
  recoverable only by re-pairing everything.

---

## 14. Upgrade and rollback

The release is relocatable and self-contained; `data_dir` is the only thing
that must survive.

```bash
VADE_VERSION=0.0.5-alpha
ARCH=x86_64
BASE="https://github.com/lshub2010/Vade/releases/download/v${VADE_VERSION}"

# 0) back up first (§13)
sudo systemctl stop vade-core

# 1) fetch + verify + unpack alongside the old tree
cd /tmp
curl -fLO "${BASE}/vade-core-${VADE_VERSION}-linux-${ARCH}.tar.gz"
curl -fLO "${BASE}/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing
sudo tar -C /opt/vade/releases -xzf "vade-core-${VADE_VERSION}-linux-${ARCH}.tar.gz"
sudo chown -R vade:vade "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}"
sudo -u vade "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}/install.sh"

# 2) switch and start
OLD=$(readlink -f /opt/vade/current)
sudo ln -sfn "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}" /opt/vade/current
sudo systemctl start vade-core
curl -fsS http://127.0.0.1:8443/healthz

# 3) rollback if needed
sudo systemctl stop vade-core
sudo ln -sfn "$OLD" /opt/vade/current
sudo systemctl start vade-core
```

Caveats:

* `core.toml` is not rewritten by upgrades, but **a new key in the schema is
  safe while an unknown key is fatal** — if you set a key that a newer build
  added, rolling back to the old build will refuse to start with
  `UNKNOWN_CONFIG_KEY`. Keep the previous `core.toml` with the previous tree.
* No migration framework is documented for `sessions.db` / `blob.sqlite3` /
  `audit.db`. If a future build changes a store, rollback means restoring the
  pre-upgrade backup, not just flipping the symlink.
* `config.overrides.json` written by the admin page survives in `data_dir` and
  is validated on load — same unknown-key rule applies.
* Keep the old release directory until the new one has served traffic for a
  few days; disk is cheap, re-pairing is not.

---

## 15. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named vade` | `PYTHONPATH` missing `core` | export both paths (absolute) |
| `protocol core python binding is not importable` | `PYTHONPATH` missing `protocol/core/python`, wrong arch, or missing `.so` | install the matching `ARCH` release |
| Starts on port 8443 instead of your port | `--config` path does not exist (silently ignored) | check the `vade: listeners up` line; use an absolute path |
| `UNKNOWN_CONFIG_KEY: llm.xxx` | typo or extra key | only the schema keys are accepted |
| `refusing to start the admin plane on non-loopback host` | remote `admin_host` without TLS | set `[tls].cert`+`key`, or bind to loopback |
| `configured TLS file … does not exist` | bad path or unreadable key | fix path/mode; both keys required |
| `another core is already running with data dir` | `core.lock` held | stop the other process; never delete a held lock |
| Admin returns `403 ORIGIN_MISMATCH` | `Host`/`Origin` ≠ `core.admin_host` | use the tunnel; do not proxy `/admin` by name |
| `/admin` on 8443 returns `404` | by design | use 8444 |
| Browser drops the session after pairing | `Secure` cookie over plain HTTP | use HTTPS, or `http://127.0.0.1` |
| `POST /ui/session` → `400 INVALID_ARGUMENT: pair_code is required` | body not valid UTF-8 JSON (often a shell/encoding issue with the Chinese characters) | write the body to a UTF-8 file and use `--data-binary @body.json` |
| `CODE_INVALID` on a fresh code | already redeemed, or `--no-pair-banner` minted nothing | restart for a new code |
| `CODE_EXPIRED` | older than 600 s | restart for a new code |
| `ADMIN_LOCKED` on setup | 5 wrong tokens from this IP in 5 min | wait 5 minutes |
| Pairing locks for everyone behind a proxy | all clients are `127.0.0.1` (§11.4) | wait; do not spray codes |
| Module: `core public key unknown` | `CORE_PUBLIC_KEY` unset and no `core_pin.json` | copy the 64-hex key from §9.3 |
| Module: `core only accepts IK responders today` | `--pair-code` used | remove it; whitelist the key (§9.3) |
| Module: handshake closes with 1011 | whitelist pool empty/unreadable | check `identity/whitelist.json` + fingerprint |
| Module: `PERMISSION_DENIED` TLS fingerprint mismatch | `CORE_FINGERPRINT` is not the cert-DER prefix | recompute with `openssl x509 … \| sha256sum` |
| Uploads fail with an HTML `413` | proxy `client_max_body_size` | raise it above `attach.max_bytes` |
| `admin print-setup-token` says “already initialised” | `admin/admin.json` exists | expected; the token is not used |
| `admin …` says “core appears to be running” | the CLI refuses to write a live `data_dir` | stop the service first |

---

## 16. Security checklist

Before you call it production:

- [ ] Dedicated `vade` user, no shell, no root, no sudo.
- [ ] `/var/lib/vade` 0700, `data_dir` 0700, `core.key` / `setup.token` /
      `whitelist.json` / `admin.json` 0600, `UMask=0077` in the unit.
- [ ] `/etc/vade/core.toml` 0640 `root:vade`; no secret literals (the loader
      enforces this, but check anyway).
- [ ] LLM key only in `/etc/vade/vade.env` (0600) or an orchestrator secret;
      `api_key_env` names it, never contains it.
- [ ] Both planes bind loopback; only 443 is public.
- [ ] TLS terminates at the proxy with a real certificate; HTTP redirects to
      HTTPS.
- [ ] The admin plane is **not** published by the proxy; access is an SSH
      tunnel or a firewalled LAN bind with core TLS.
- [ ] `[dev].allow_all = false`, `--strict` in the unit.
- [ ] Setup completed (`admin/admin.json` exists, `setup.token` gone); the
      password is ≥12 chars and stored only as Argon2id.
- [ ] `--no-pair-banner` used when you do not need a browser pairing this boot;
      otherwise rotate by restarting and pair immediately.
- [ ] `journalctl` access restricted (the pair code is a 10-minute credential).
- [ ] Audit retention (`audit.retention_days`, default 30) matches your policy,
      and `audit/audit.db` is on a volume with room.
- [ ] Backups are encrypted, off-box, and a restore has been tested.
- [ ] Release tarballs verified against `SHA256SUMS`.
- [ ] Firewall: 8443/8444 closed from untrusted networks; module keys
      whitelisted one by one.
- [ ] `ui_sessions.json` deletion documented as “log out all browsers”.
- [ ] Monitor `/healthz` and `/admin/api/health`; alert on process restart
      loops (`Restart=on-failure` will hide a crash loop from a human).

---

## Appendix A — `data_dir` map

```text
data/
├── core.key                  0600  core X25519 seed
├── core.lock                 0600  single-instance lock (held while running)
├── pairing.sqlite3           0600  hashed pair codes
├── pairing.pepper            0600  scrypt pepper
├── sessions.db               0600  conversations (SQLite, WAL)
├── blob.sqlite3              0600  attachment metadata
├── blobs/                    0700  attachment bytes (+ tmp/, orphan/)
├── audit/
│   ├── audit.db              0600  daily audit tables
│   └── audit.deadletter.jsonl      failed audit rows
├── identity/
│   └── whitelist.json        0600  trusted module keys
├── admin/
│   ├── admin.json            0600  password hash, created_at
│   ├── core_identity.key     0600  admin-plane identity
│   ├── devices.json          0600  device public keys
│   └── sessions.json         0600  admin refresh sessions
├── ui_sessions.json          0600  browser sessions (sha256(token) only)
├── setup.token               0600  pre-init only, deleted on success
├── skills/                         installed skills
└── config.overrides.json           written by the admin page, read at startup
```

## Appendix B — commands verified for this guide

Run on Linux x86_64, Python 3.12.3, `dev_gingshan`:

| Check | Result |
|---|---|
| `python -m vade --help` / `admin --help` | flags and 11 subcommands as listed above |
| start with a minimal `core.toml` | `vade: listeners up — data ws://127.0.0.1:18443 admin http://127.0.0.1:18444` |
| banner | `pair_code=… purpose=browser expires_in=600s` + `setup_token=acs_…` |
| `GET /healthz` | `{"ok": true, "version": "0.1.0", "policy_mode": "default"}` |
| `GET /admin/api/health` | `200` with audit counters |
| `GET /login`, `GET /` | `200 text/html` (built `admin-ui/dist/index.html`) |
| `GET /assets/index-*.js` | `200` — mounted SPA bundle (§7.4) |
| `GET /admin` | `404` JSON — the UI entry point is `/login`, not `/admin` |
| `GET /admin/setup` (loopback) | `setup_required:true`, `requires_token:false`, `bind_url`, `fp8` |
| `POST /admin/setup` | `200`, `admin/admin.json` 0600, `setup.token` removed |
| `admin init --password-stdin` | `initialised`, same files |
| `admin print-setup-token` / `print-fingerprint` | `acs_…` / `982d2236708d425b` |
| `POST /ui/session` | `200`, both `Set-Cookie` headers, `{"device_id":"ui-…"}` |
| second redeem | `400 CODE_INVALID` |
| `--strict`, `--no-pair-banner`, `--bind`, `--admin-bind` | honoured; no banner with `--no-pair-banner` |
| `SIGTERM` | `Application shutdown complete`, exit 0 |
| `ufw`-style port check | data and admin on the configured loopback ports |

Not executed in this environment: the systemd unit (no systemd PID 1 here),
Caddy/Nginx reloads, and a second-architecture tarball. The unit and proxy
files are checked for syntax only where a binary was available.
