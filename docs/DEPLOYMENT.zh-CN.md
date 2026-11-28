# core 生产环境部署

[English](DEPLOYMENT.md) | **简体中文**

本文面向在 Linux 服务器上运行 Vade core（`python -m vade`）的运维人员。文中
每一条命令、路径、键名与默认值都对照过 `dev_gingshan` 上 `0.0.5-alpha` 的代码；
没有凭空发明的 CLI 参数。凡是“schema 里有、这一版没接线”的东西，正文会直接
写明，而不是描述一个“应该如此”的行为。

| 项 | 值 |
|---|---|
| 发布包 | `vade-core-0.0.5-alpha-linux-{x86_64,aarch64}.tar.gz` |
| Python | 3.11+（CI 与本文实测均为 3.12） |
| Rust | 1.85+ —— **仅**在需要自行重编 `libvade_protocol.so` 时；发布包已带 |
| init | systemd（样例见 `deploy/systemd/`） |
| 监听 1 | 数据面 `127.0.0.1:8443` —— `/healthz`、`/ui/session`、`/ws`、`/ui/blob*` |
| 监听 2 | 管理面 `127.0.0.1:8444` —— `/admin/*`、`/login` |
| 状态 | 每进程一个 `data_dir`，由 `data/core.lock` 独占 |

先说两条决定后面所有配置的规则：

1. **两个面是两个监听、两套信任模型。** 数据面从不提供 `/admin`（故意回 `404`
   而不是 `403`）。管理面默认只听回环，且其 `Host` 白名单由
   `[core].admin_host` 生成 —— 想用反代指向它之前，先读 §10.3。
2. **浏览器会话是 `Secure` cookie。** `POST /ui/session` 恒定下发
   `__Host-ac_ui=…; Secure`。明文 HTTP 下真实浏览器会直接丢弃，所以生产环境
   必须在某处有 TLS（反代或 core 自身），除非你只在 `http://127.0.0.1` 上用。

> 版本说明：本文按 `0.0.5-alpha` 的代码写成，键名与默认值以
> `schema/config.schema.json` 为准；升级后若 schema 有增删，以新版本的
> schema 与实际代码为准。

---

## 1. 部署对象

| 进程 | 默认端口 | 协议 | 提供 |
|---|---|---|---|
| core 数据面 | 8443 | HTTP + WebSocket | `/healthz`、`POST/DELETE /ui/session`、`/ws`、`/ui/blob*` |
| core 管理面 | 8444 | HTTP（回环）或 HTTPS | `/admin/setup`、`/admin/bind`、`/admin/api/*`、`/login` |
| 模块 | 只出站 | `ws://` / `wss://` 连 8443 | 连上来、声明 manifest、处理 RPC |
| 客户端 | 只出站 | `wss://…/ws` | 配对一次后 attach |

**一个进程、两个监听、一个事件循环**。`SIGTERM` 会同时停掉两个（实测日志
`Application shutdown complete`，退出码 0）。同一个 `data_dir` 只允许一个
core，第二个会以 `another core is already running with data dir … (held:
…/core.lock)` 拒绝启动。

最小可用配置只有三个键，其余都有 schema 默认值。

---

## 2. 系统要求

| 资源 | 最低 | 说明 |
|---|---|---|
| 系统 | Linux x86_64 / aarch64，glibc | 发布包里的 `.so` 与架构绑定 |
| Python | 3.11+ | 无条件 `import tomllib`，3.10 及以下连配置都读不了 |
| 内存 | 1 GiB | Argon2id 每次 64 MiB；SQLite 页缓存；WS 缓冲 |
| 磁盘 | 程序 + venv 约 250 MiB，另加数据 | blob 默认配额 **每设备** 2 GiB |
| 端口 | 回环 8443/8444，或反代只开 443 | 8444 绝不对外 |
| 时间 | NTP 同步 | 配对码、setup token 都是时间敏感 |

精简镜像上补齐 wheel 所需的系统包：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates tar
```

`cryptography`、`argon2-cffi`、`uvicorn`、`starlette`、`httpx` 都有 manylinux
wheel，正常安装不需要编译器。只有重编协议绑定时才需要 Rust 1.85+：

```bash
cd protocol/core
cargo build --release --features uniffi --lib
cp target/release/libvade_protocol.so python/
```

---

## 3. 从 0.0.2 发布包安装

### 3.1 下载与校验

```bash
VADE_VERSION=0.0.5-alpha
ARCH=x86_64                     # 或 aarch64
BASE="https://github.com/lshub2010/Vade/releases/download/v${VADE_VERSION}"

curl -fLO "${BASE}/vade-core-${VADE_VERSION}-linux-${ARCH}.tar.gz"
curl -fLO "${BASE}/SHA256SUMS"

# SHA256SUMS 覆盖整个 release（core、模块、客户端），
# --ignore-missing 让你只校验下载了的那个包。
sha256sum -c SHA256SUMS --ignore-missing
```

不要跳过校验：包里是 core 进程内加载的原生库。

### 3.2 解包并装好 Python 环境

```bash
sudo install -d -m 0755 /opt/vade/releases
sudo tar -C /opt/vade/releases -xzf "vade-core-${VADE_VERSION}-linux-${ARCH}.tar.gz"

sudo useradd --system --home /var/lib/vade --shell /usr/sbin/nologin vade
sudo chown -R vade:vade "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}"

# install.sh 以当前用户建 .venv/ 并 pip install -r requirements.txt
sudo -u vade "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}/install.sh"

sudo ln -sfn "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}" /opt/vade/current
```

`install.sh` 结束时会打印启动命令：

```text
Installed. Start with:
  PYTHONPATH="core:protocol/core/python" .venv/bin/python -m vade --config core.toml
Copy deploy/core.toml.example to core.toml and edit it first.
```

### 3.3 目录内容

```text
/opt/vade/current/
├── .venv/                     # install.sh 创建
├── core/vade/                 # `vade` 包
├── protocol/core/python/      # vade_protocol.py + libvade_protocol.so
├── schema/                    # config.schema.json、manifest、UI/API 契约
├── sdk/python/                # 模块侧 SDK
├── admin-ui/dist/             # 预构建管理页，挂载在 /login 与 /assets（见 §7.4）
├── deploy/                    # 本文附带的样例
├── docs/                      # DEPLOYMENT(+zh-CN)、PAIRING
├── requirements.txt
└── install.sh
```

`PYTHONPATH` 两处缺一不可：

| 路径 | 提供 | 缺了会怎样 |
|---|---|---|
| `core` | `vade` 包 | `No module named vade` |
| `protocol/core/python` | `vade_protocol` 与 `.so` | `protocol core python binding is not importable` |

unit 文件里用**绝对路径**，这样 `WorkingDirectory` 写错也不会炸在 import 上。

---

## 4. 账号、目录与权限

| 路径 | 属主 | 权限 | 内容 |
|---|---|---|---|
| `/opt/vade/current` | `vade:vade` | 0755（符号链接） | 代码，运行期只读 |
| `/etc/vade` | `root:vade` | 0750 | `core.toml`、`vade.env`、`tls/` |
| `/etc/vade/core.toml` | `root:vade` | 0640 | 配置（不允许出现密钥，加载时强制） |
| `/etc/vade/vade.env` | `root:vade` | 0640 | `VADE_LLM_API_KEY=…` 等 |
| `/etc/vade/tls/server.key` | `root:vade` | 0640 | 仅 core 自身终止 TLS 时需要 |
| `/var/lib/vade` | `vade:vade` | 0700 | `data_dir` 的父目录 |
| `/var/lib/vade/data` | `vade:vade` | 0700 | core 首次启动时创建 |

```bash
sudo install -d -m 0750 -o root -g vade /etc/vade
sudo install -d -m 0700 -o vade -g vade /var/lib/vade
sudo install -d -m 0750 -o root -g vade /etc/vade/tls
```

core 会把 `data_dir` 建成 `0700`，并把自己写的密钥文件设成 `0600`
（`core.key`、`setup.token`、`admin/admin.json`、`identity/whitelist.json`、
`ui_sessions.json`）。但它**不会** chmod 所有产物：SQLite 文件
（`pairing.sqlite3`、`sessions.db`、`blob.sqlite3`、`audit/audit.db`）与
`blobs/` 继承进程 umask。unit 里设 `UMask=0077`（样例已设），它们才会是
`0600`/`0700`。

全程用专用用户跑。不要用 root：它不绑特权端口，而管理面把回环视为已授权。

---

## 5. `core.toml`

### 5.1 最小生产配置

`/etc/vade/core.toml`（0640）：

```toml
[core]
listen     = "127.0.0.1:8443"
admin_host = "127.0.0.1:8444"
data_dir   = "/var/lib/vade/data"
log_level  = "info"

# TLS：由前置的 Caddy/Nginx 终止时，这两个键都别写。
# 只写其中一个不会报错，而是静默退回明文（见 §6.3）。
# [tls]
# cert = "/etc/vade/tls/server.crt"
# key  = "/etc/vade/tls/server.key"

[llm]
provider    = "openai_compatible"            # 默认值，可省略
base_url    = "https://api.deepseek.com/v1"  # 必须写到 /v1，core 只拼 /chat/completions
model       = "deepseek-chat"
api_key_env = "VADE_LLM_API_KEY"             # 环境变量“名字”，不是密钥本身
```

### 5.2 键位说明

| 键 | 默认 | 生效方式 | 含义 |
|---|---|---|---|
| `core.listen` | `127.0.0.1:8443` | 重启 | 数据面绑定。模式 `^[^:]+:[0-9]{1,5}$`，**不支持 IPv6 字面量** |
| `core.admin_host` | `127.0.0.1:8444` | 重启 | 管理面绑定，**同时**是它接受的 `Host`/`Origin`（§10.3） |
| `core.data_dir` | `./data` | 重启 | 状态目录，相对**进程工作目录**解析，不是相对本文件 |
| `core.log_level` | `info` | 热 | `debug`/`info`/`warning`/`error`，stdout 输出 JSON 行 |
| `tls.cert` / `tls.key` | 未设 | 重启 | PEM 路径。要么都写、要么都不写；写了就必须存在 |
| `tls.spki_pin` | 未设 | 重启 | schema 接受，本版**未消费** |
| `tls.san` | `[]` | 重启 | schema 接受，本版**未消费**（不生成证书，见 §6.4） |
| `server.ui_origins` | `[]` | 重启 | `/ui/session` 的精确 CORS 白名单；`*` 加载期即拒 |
| `server.listen_hosts` | `["127.0.0.1","localhost"]` | 重启 | 只用于校验 `ui_origins` 的 host；host 不在表里则启动失败 |
| `audit.retention_days` | `30` | 重启 | 超过天数的日表会被删除；`< 1` 拒绝启动 |
| `audit.path` | `data/audit.sqlite3` | 重启 | 字面默认值表示“用 `<data_dir>/audit/audit.db`” |
| `attach.max_bytes` | `104857600`（100 MiB） | 重启 | 单次上传上限，反代 body 限制要大于它 |
| `attach.quota_bytes` | `2147483648`（2 GiB） | 热 | 每设备 blob 配额 |
| `llm.provider` | `openai_compatible` | 重启 | `openai_compatible` / `module` / `mock` |
| `llm.base_url` | `""` | 重启 | 端点根，**含** `/v1` |
| `llm.model` | `""` | 热 | 默认模型名 |
| `llm.api_key_env` | `""` → `VADE_LLM_API_KEY` | 重启 | 存放密钥的环境变量名 |
| `llm.default_model` | `""` | 热 | 会话未选模型时的全局 `<provider>/<model>` |
| `llm.model_module` | `""` | 重启 | `provider="module"` 时限定单个模块 |
| `llm.fallback_to_builtin` | `true` | 重启 | 模块离线时回落内置适配器 |
| `llm.capabilities` | `{}` | 重启 | `display_name`、`supports_tools`、`context_window` 等 |

根对象是 `additionalProperties: false`：**拼错一个键就启动失败**，报
`UNKNOWN_CONFIG_KEY: <点分键名>`，不会静默回落到默认值。带取值范围的完整
清单见 `schema/config.schema.json`。

### 5.3 密钥

`core.toml` 里不允许出现凭据，加载时有两道检查：

* 键名为 `api_key`、`secret`、`password`、`token` 等的字符串值被拒；
* 任何以 `sk-`、`sk_`、`ghp_`、`xoxb-`、`AKIA` 开头的字符串被拒。

值放环境变量，配置里只写**变量名**：

```bash
# /etc/vade/vade.env  （root:vade 0640）
VADE_LLM_API_KEY=<真实密钥>
```

### 5.4 环境变量覆盖

任何键都可用 `VADE_<SECTION>__<KEY>`（双下划线、大写）覆盖。值会按 schema
类型转换，数组用 JSON：

```bash
VADE_CORE__LOG_LEVEL=debug
VADE_TLS__CERT=/etc/vade/tls/server.crt
VADE_SERVER__UI_ORIGINS='["https://vade.example.com"]'
```

优先级：**CLI > 环境变量 > `<data_dir>/config.overrides.json` > `core.toml` >
默认值**。管理页只写 `config.overrides.json`，永不回写你的 `core.toml`。

### 5.5 两个加载陷阱

* `--config` 指向**不存在的文件不会报错**：toml 被跳过，core 用默认值启动
  （`127.0.0.1:8443`、`./data`）。端口对不上时先看
  `vade: listeners up — data ws://…` 这行，或 `systemctl show -p ExecStart`。
* `data_dir` 相对**工作目录**。永远写绝对路径，否则状态落在 systemd 启动时
  所在的位置。

---

## 6. TLS

### 6.1 三种可用模型

| 模型 | 配置 | 适用 |
|---|---|---|
| 反代终止 TLS | core 只听 `127.0.0.1`，不配 `[tls]` | 推荐：证书、续期、HSTS 都交给 Caddy/Nginx |
| core 终止 TLS | `[tls].cert` + `[tls].key`，两个面共用 | 没有反代，或管理面绑到内网地址 |
| 只用回环 | 不配 `[tls]`、不上反代 | 开发；浏览器把 `http://127.0.0.1` 当安全上下文，`Secure` cookie 可用 |

两个面共用同一份证书，没有“按面分别配 TLS”的开关。

### 6.2 反代终止（推荐）

```toml
[core]
listen     = "127.0.0.1:8443"
admin_host = "127.0.0.1:8444"
```

完全不要写 `[tls]`。Caddy/Nginx 听 443、转发到 8443，并保留 `Host`
（两份样例都保留）。见 §10。

### 6.3 core 终止

```toml
[tls]
cert = "/etc/vade/tls/server.crt"
key  = "/etc/vade/tls/server.key"
```

* 两个键必须同时存在。只配 `cert` 或只配 `key` 的结果是**明文**，不是报错
  —— 代码只在两者都真值时才走 TLS 分支。
* 配了但文件不存在：启动失败，`configured TLS file … does not exist`。
* 开了 TLS 后数据面是 `https`/`wss`，启动横幅里的管理面也会打成 `https://…`。
* 管理面绑非回环地址**必须**有证书：
  `refusing to start the admin plane on non-loopback host … without TLS`。

指纹：

```bash
# SPKI pin，SHA-256(SubjectPublicKeyInfo)[:16]，客户端存成 `tls=`
python -m vade --print-tls-fingerprint          # 用配置里的 [tls].cert
python -m vade admin print-tls-fingerprint --cert /etc/vade/tls/server.crt
```

### 6.4 不会自动生成自签证书

`schema/config.schema.json` 写着 “null = auto-generated self-signed”，
`vade.identity.tls.ensure_certificates()` 也确实实现了生成逻辑 —— 但
**`python -m vade` 从不调用它**。`[tls]` 留空时 core 就是明文 HTTP，横幅里
打的是 `http://` 管理面地址。请把证书当成你自己的事：

```bash
# 内部 CA 或公网 CA 都行；core 只读 PEM 文件
sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout /etc/vade/tls/server.key -out /etc/vade/tls/server.crt \
  -days 825 -nodes -subj "/CN=vade.internal" \
  -addext "subjectAltName=DNS:vade.internal,IP:192.0.2.10"
sudo chown root:vade /etc/vade/tls/server.{crt,key}
sudo chmod 0640 /etc/vade/tls/server.key
```

`ensure_certificates()` 只在测试里被调用，没有对应 CLI。这是已知缺口，
不是可用路径。

---

## 7. 首次初始化

初始化之前管理面没有密码。状态都在 `<data_dir>`：

| 文件 | 初始化前 | 初始化后 |
|---|---|---|
| `setup.token` | 存在，`acs_` + 43 字符，0600 | 删除 |
| `admin/admin.json` | 不存在 | 存在，0600 —— **它的存在就是唯一的“已初始化”标志** |
| `admin/core_identity.key` | 可能不存在 | 存在（管理面身份） |

**未初始化**的 core 每次启动都会换一个新 token，横幅只打掩码：

```text
setup_token=acs_9f3c… setup_path=/var/lib/vade/data/setup.token
vade: first-run admin setup — open http://127.0.0.1:8444/admin/setup. A loopback request is already authorised; a remote browser needs the full token from the file above (`python -m vade admin print-setup-token`). The token rotates on every restart until setup completes.
```

运维上要记住：

* 初始化完成前，**每次重启都换 token**；
* `VADE_SETUP_TOKEN` 可以钉住它（原样写入文件）—— 但它会出现在
  `/proc/<pid>/environ` 和 `docker inspect` 里，文件方式是更好的选择；
* 已初始化时，启动阶段会删掉残留的 `setup.token`；
* 初始化成功时**先**写 `admin.json`，**再**删 token（顺序反了会变成
  “token 没了、密码没写”）。

### 7.1 无头初始化（服务器推荐）

先停 core —— `core.lock` 被持有时 CLI 拒绝写 `data_dir`：

```bash
sudo systemctl stop vade-core

# 至少 12 字符、不在弱口令表、不同字符超过 3 个
printf '%s\n' 'correct horse battery staple 9' \
  | sudo -u vade env PYTHONPATH=/opt/vade/current/core:/opt/vade/current/protocol/core/python \
      /opt/vade/current/.venv/bin/python -m vade admin init \
      --password-stdin --data-dir /var/lib/vade/data

sudo systemctl start vade-core
```

实测输出 `initialised`，`admin/admin.json` 为 0600，`setup.token` 被删除。
加 `--pubkey-file device.pub` 可同时登记第一把设备公钥。

### 7.2 JSON API（回环）

core 在跑且未初始化时，回环请求不需要 token：

```bash
curl -s http://127.0.0.1:8444/admin/setup
# {"setup_required":true,"requires_token":false,
#  "bind_url":"vade://bind?h=127.0.0.1:8444&fp=<fp8>&t=<ticket>&v=1",
#  "fp8":"<fp8>","expires_at":1795441520}

curl -s -X POST http://127.0.0.1:8444/admin/setup \
  -H 'content-type: application/json' \
  -d '{"password":"correct horse battery staple 9"}'
# 200，空响应体；写 admin.json、删 setup.token
```

口令策略（`vade.admin.auth.validate_password`）：12–256 字符、不在常见弱口令
表、不同字符多于 3 个。哈希为 Argon2id `m=64 MiB, t=3, p=1`。

### 7.3 远程初始化（用 token）

非回环 `GET /admin/setup` 只回
`{"setup_required":true,"requires_token":true}` —— 没有 `bind_url`、没有
`fp8`、没有 `acs_` 字符串。要走下去：

```bash
# 1) 在服务器上取完整 token（明文只在文件里）
sudo -u vade env PYTHONPATH=… /opt/vade/current/.venv/bin/python -m vade \
  admin print-setup-token --data-dir /var/lib/vade/data

# 2) 换成短时 unlock cookie（下发 __Host-ac_setup）
curl -s -X POST http://<管理面地址>:8444/admin/setup/unlock \
  -H 'content-type: application/json' \
  -d '{"setup_token":"acs_…"}'

# 3) 带上这个 cookie，按 §7.2 发 POST /admin/setup
```

解锁失败与配对码共用账本：同一 IP 5 分钟错 5 次 → `ADMIN_LOCKED`。但注意
§10.3：管理面绑非回环必须配 TLS，而在本版把 `/admin` 放到公网域名反代后面
是不支持的。

### 7.4 发布包里的管理页

发布包里带着预构建的 `admin-ui/dist`（目标机不需要 Node.js）。`python -m vade`
用 `Path(__file__).parents[2] / "admin-ui" / "dist"` 定位它 —— 即
`/opt/vade/current/admin-ui/dist` —— 并传给
`create_admin_app(..., static_dist=…)`，所以 `GET /login` 与 `GET /` 会返回
构建好的 `index.html`。

| 请求 | 本版行为 |
|---|---|
| `GET /login`、`GET /` | `200 text/html`（SPA 外壳） |
| `GET /admin` | `404 {"code":"NOT_FOUND"}` —— 没有 `/admin` 路由，入口是 `/login` |
| `GET /admin/setup`（未初始化） | JSON 状态（`setup_required`、`bind_url`、`fp8`），不是 HTML |
| `GET /admin/setup`（已初始化） | `302` → `/login` |
| `GET /assets/index-*.js` | `200`（已挂载的 SPA bundle） |

`admin-ui/dist/index.html` 存在时，`create_admin_app` 会通过
`vade.admin.static.mount_static` 挂载 `/assets`，所以发布包在 `/login` 提供可用
的 SPA。初始化向导本身仍是 JSON（`GET /admin/setup`）；界面不可达时用本机 CLI
（§7.5）恢复。

源码树如果从未跑过 `npm run build`，就没有 `admin-ui/dist`，此时
`static_dist` 为 `None`，`/login` 退回 JSON discovery
（`{"login":"POST /admin/api/login","setup_required":…}`）。裁剪掉该目录的
安装也是同样表现。

### 7.5 恢复类命令

| 目的 | 命令（务必带 `--data-dir`） |
|---|---|
| 打印身份 fp8 | `python -m vade admin print-fingerprint --data-dir …` |
| 打印 setup token（仅初始化前） | `python -m vade admin print-setup-token --data-dir …` |
| 轮换 setup token（需停 core） | `python -m vade admin rotate-setup-token --data-dir …` |
| 改密码（TTY，需停 core） | `python -m vade admin reset-password --data-dir …` |
| 回到 setup 模式（清空 `data/admin`） | `python -m vade admin reset --data-dir …`（输入 `RESET`） |
| 加设备公钥（救援） | `python -m vade admin trust-key --pubkey-file … --label …` |
| 吊销设备 | `python -m vade admin revoke-device --id …` |

`admin --data-dir` 默认是 `./data`，**不是** `[core].data_dir`（CLI 调
`load_config()` 时没带你的 `--config`）。永远显式传。

---

## 8. 客户端配对

### 8.1 启动横幅

每次启动、且**两个** socket 都绑定成功后，core 铸一个一次性浏览器码，
往 **stdout** 打两行：

```text
pair_code=幕峡-DVN25E purpose=browser expires_in=600s
vade: first-run pairing — enter this code in the app/browser (it redeems via POST /ui/session). Single-use, expires in 600s; restart the core for a fresh one. Pass --no-pair-banner to hide this.
```

* 横幅的 `purpose` 恒为 `browser`；`expires_in` 单位是秒（600）。
* 码形如 `2 个汉字-6 位 base32`，不进 JSON 日志、不落明文磁盘 ——
  `pairing.sqlite3` 里只有 `scrypt(normalize(code))`。
* `--no-pair-banner` 同时关掉“打印”和“铸码”（脚本、测试、以及走别的通道
  配对的部署）。
* 用了 `--no-pair-banner`，journald 里就没有可泄露的码；不用的话，这个码在
  10 分钟内等同于凭据，日志要按凭据对待。

```bash
sudo journalctl -u vade-core -o cat | grep '^pair_code=' | tail -1
```

### 8.2 兑换

```bash
printf '{"pair_code":"%s","client_name":"ops-laptop"}' "$CODE" > body.json
curl -s -i -X POST https://vade.example.com/ui/session \
  -H 'content-type: application/json' --data-binary @body.json
```

成功是 `200`，下发 **两个** cookie，响应体 `{"device_id":"ui-…"}`：

```text
set-cookie: __Host-ac_ui=…; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=2592000
set-cookie: __Host-ac_csrf=…; Path=/; Secure; SameSite=Lax; Max-Age=2592000
```

会话在服务端活 30 天（`ui_sessions.json`，只存摘要，0600）。同一个码第二次
兑换回 `CODE_INVALID`，过期回 `CODE_EXPIRED`。同一个码错 5 次即销毁；同一 IP
5 分钟内错 5 次会被锁。

### 8.3 客户端配置

桌面 / WebAssembly 客户端读环境变量：

```bash
VADE_SOURCE=real VADE_HOST=vade.example.com VADE_PORT=443 \
VADE_PAIR_CODE='幕峡-DVN25E' ./vade-ui
```

Android 读 intent extras：`source`、`host`、`port`、`pair_code`（裸键，没有
`vade.` 前缀）。细节与“三端仍无配对输入框”的现状见 [`PAIRING.md`](PAIRING.md)。

走 443 的 `wss://` 经过你的反代；`wss://vade.example.com:8443` 只有直接暴露
8443 才成立（不推荐）。

### 8.4 吊销会话

```bash
curl -s -i -X DELETE https://vade.example.com/ui/session \
  -H 'Cookie: __Host-ac_ui=<token>; __Host-ac_csrf=<csrf>'
```

`204` 表示行已删、对应的 `/ws` 已关闭。要让**所有**浏览器下线：停 core，
删除 `<data_dir>/ui_sessions.json` —— 那张表就是全部浏览器会话。

---

## 9. 模块

模块是独立进程：**出站**连数据面，用自己的 X25519 密钥做 Noise IK，然后
`module/register` 交 manifest。core 里没有任何具体模块名。

### 9.1 安装

```bash
sudo install -d -m 0755 /srv/vade-modules/echo
sudo python3 -m venv /srv/vade-modules/echo/venv
sudo /srv/vade-modules/echo/venv/bin/pip install ./vade_module_echo-0.0.2-py3-none-any.whl
```

### 9.2 生成模块密钥并读出公钥

SDK 读 `MODULE_KEY_PATH`（默认 `data/module.key`）：64 个 hex 字符（32 字节），
0600，且**永不覆盖**已有文件。换密钥就是换指纹，core 会拒绝，所以务必备份。

```bash
sudo install -d -m 0700 -o vade -g vade /srv/vade-modules/echo/data
sudo -u vade sh -c 'openssl rand -hex 32 > /srv/vade-modules/echo/data/module.key'
sudo chmod 0600 /srv/vade-modules/echo/data/module.key

# 公钥与 fp8，公式与 core 完全一致：sha256(pub)[:16]
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

### 9.3 加入白名单（当前只能手工）

**模块配对码还没打通。** `POST /admin/api/invites
{"purpose":"module","module_id":"com.example.echo"}` 会铸码并返回
`vade://pair?…`，但 SDK 用 `--pair-code` 会以 `UNSUPPORTED` 退出
（“core only accepts IK responders today”），而 core 的监听端也只构造 IK
responder。`vade admin trust-key` 加的是**设备**公钥，不是模块的。可用路径
只有手工白名单。

模块要钉的 core 公钥：

```bash
sudo -u vade env PYTHONPATH=/opt/vade/current/core:/opt/vade/current/protocol/core/python \
  /opt/vade/current/.venv/bin/python -c \
  "from vade.identity import load_or_create; print(load_or_create('/var/lib/vade/data').public_key_hex)"
```

**停 core 后**编辑 `<data_dir>/identity/whitelist.json`（启动时读一次，写入是
原子替换）：

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

`fingerprint` 必须等于 `sha256(bytes.fromhex(public_key))[:16]`，否则加载时
整条被丢弃（fail closed），`candidate_keys()` 永远认证不了它。模块侧
`instance_id` 默认 `main`（`--instance-id` / `VADE_INSTANCE_ID`）。改完重启
core。

### 9.4 启动

```bash
sudo -u vade env \
  MODULE_KEY_PATH=/srv/vade-modules/echo/data/module.key \
  VADE_DATA_DIR=/srv/vade-modules/echo/data \
  CORE_PUBLIC_KEY=<9.3 里的 64 位 hex> \
  /srv/vade-modules/echo/venv/bin/python -m echo_module \
  --core ws://127.0.0.1:8443
```

* `--core` 也读 `VADE_CORE_URL`；只有 core 在回环上是明文时才用 `ws://`，
  否则 `wss://vade.example.com`。
* `wss://` + 自签证书时，把 `CORE_FINGERPRINT` 设为**证书 DER** 的 SHA-256
  前缀：`openssl x509 -in server.crt -outform DER | sha256sum | cut -c1-16`。
  这与 `--print-tls-fingerprint` 打的 SPKI pin 是**不同的值**，SDK 比的是
  证书 DER。
* 模块日志先打 `module identity loaded`（带指纹），再打 `registered`。断线
  自动重连（0.5 s → 30 s，带抖动，永不放弃）。

### 9.5 查看与吊销

注册表按白名单键一行一条（`GET /admin/api/nodes`，需鉴权）。吊销会删白名单
记录并断开在线的连接；之后模块会一直卡在握手失败，直到重新配对。

---

## 10. systemd

把 [`deploy/systemd/vade-core.service`](../deploy/systemd/vade-core.service)
拷到 `/etc/systemd/system/vade-core.service`，然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vade-core
sudo systemctl status vade-core
sudo journalctl -u vade-core -f
```

unit 的假设：

* `User=vade`、`Group=vade`、`WorkingDirectory=/opt/vade/current`；
* `EnvironmentFile=-/etc/vade/vade.env`（可缺，故有 `-`）；
* `PYTHONPATH` 用绝对路径指向 `core` 与 `protocol/core/python`；
* `ExecStart=… -m vade --config /etc/vade/core.toml --strict`；
* `UMask=0077`、`StateDirectory=vade`（0700）、`ProtectSystem=strict`、
  `ProtectHome=true`、`PrivateTmp=true`、`NoNewPrivileges=true`；
* `Restart=on-failure`、`RestartSec=3`、`TimeoutStopSec=30`。

`--strict` 让 `session`/`policy`/`audit` 组件缺失时直接启动失败，而不是打一行
“dev mode only”。发布包三个都在，所以它只是保险。`SIGTERM` 有处理：两个
监听都会停，进程退出码 0。

`StateDirectory=vade` 会建 `/var/lib/vade`（0700），并在
`ProtectSystem=strict` 下保持可写；里面的 `data_dir` 仍由 core 创建。

---

## 11. 反向代理

只发布**数据面**。管理面留在回环，用 SSH 隧道访问（§10.3）。

### 11.1 Caddy

```caddyfile
vade.example.com {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8443 {
		# Caddy 默认保留原始 Host，core 的同源 WebSocket 校验依赖它。
		flush_interval -1
	}
	# 上传：attach.max_bytes 默认 100 MiB。Caddy 默认不限 body，按需打开。
	# request_body {
	# 	max_size 110MB
	# }
}
```

完整样例：[`deploy/Caddyfile`](../deploy/Caddyfile)。Caddy 自动处理
WebSocket 升级，也自动续证书。

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

    client_max_body_size 110m;      # >= attach.max_bytes（100 MiB）

    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_http_version 1.1;
        proxy_set_header Host $host;              # 保留公网 Host
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;                 # /ws 长连接
        proxy_send_timeout 3600s;
        proxy_buffering off;                      # 事件不要攒批
    }
}
```

完整样例：[`deploy/nginx.conf`](../deploy/nginx.conf)（可直接放进
`/etc/nginx/conf.d/`；里面含 `map`，它必须位于 `http{}` 内）。

### 11.3 为什么管理面不能反代

`AdminConfig.allowed_hosts` 由 `[core].admin_host` 加 `127.0.0.1:8444`、
`localhost:8444` 组成；schema 里**没有 `[admin]` 段**，`admin.allowed_hosts`
也设不了。任何 `/admin` 请求，只要 `Host` 不在这个集合里就是
`403 ORIGIN_MISMATCH`；`Origin` 不是 `http(s)://<admin_host>` 同样被拒。所以
把它绑在 `127.0.0.1` 上开隧道：

```bash
ssh -N -L 8444:127.0.0.1:8444 ops@vade.example.com
# 然后开 http://127.0.0.1:8444/login（本版实际提供什么见 §7.4）
```

确实需要不走 SSH 的远程访问时：把 `admin_host` 设成该主机的一个地址
（如 `192.0.2.10:8444`），配 `[tls].cert`/`[tls].key`（非回环管理面强制），
防火墙只放可信网段，浏览器访问 `https://192.0.2.10:8444` —— 此时
`Host`/`Origin` 才对得上。在它前面套一层按域名分发的 vhost 是不行的。

`--admin-allow-cidr` 只是 argparse 接受了参数，本版**没有实施**。

### 11.4 反代带来的副作用

* **配对码限流会变成全局的。** `uvicorn` 以 `proxy_headers=False` 运行，
  `CoreApp` 也没有传 `trust_forwarded_for`，所以所有经反代的客户端都是
  `127.0.0.1`。5 次错码会把**所有人**锁 5 分钟。本版没有配置开关。
* `client_max_body_size` 必须 ≥ `attach.max_bytes`，否则大上传在反代层就变成
  一个 HTML 错误，而不是 core 的 JSON `413`。
* `X-Forwarded-For` 不被信任，别把审计预期建在它上面。

---

## 12. 健康检查

| 检查 | 命令 | 期望 |
|---|---|---|
| 数据面 | `curl -fsS http://127.0.0.1:8443/healthz` | `{"ok": true, "version": "0.1.0", "policy_mode": "default"}` |
| 管理面 | `curl -fsS http://127.0.0.1:8444/admin/api/health` | `{"ok":true,"version":"1.0.0","e2e_enabled":false,"audit":{…}}` |
| 端口 | `ss -ltnp \| grep -E '8443\|8444'` | 配置地址上的两个监听 |
| 进程 | `systemctl is-active vade-core` | `active` |

`/healthz` 不需要鉴权，也刻意不返回配置、指纹或模块清单。`version` 取自
`vade.__version__`（例如 `0.1.0-beta.1`），与发布版本一致。

`policy_mode` 只有在 `[dev].allow_all = true` 时才是 `allow_all`；生产必须
保持默认的 `false`。

监控应把“非 200”或“body 里没有 `ok: true`”都判为不健康。没有 `/readyz`，
也没有 metrics 端点。

---

## 13. 备份与恢复

状态和配置都要备：

```bash
sudo systemctl stop vade-core

sudo tar --numeric-owner -C / -czf "/var/backups/vade-$(date +%F-%H%M).tar.gz" \
  var/lib/vade etc/vade

sudo systemctl start vade-core
```

`data_dir` 里是什么（全都是状态）：

| 路径 | 内容 | 丢失后果 |
|---|---|---|
| `core.key` | core X25519 身份种子（0600） | `fp8` 改变，所有模块 pin 失效 |
| `pairing.sqlite3` / `pairing.pepper` | 哈希后的配对码 + pepper | 只是未用的码失效 |
| `identity/whitelist.json` | 受信模块公钥 | 所有模块需重新配对 |
| `admin/admin.json` | Argon2id 口令哈希 | 只能 `admin reset` |
| `admin/core_identity.key` | 管理面绑定身份 | 绑定票据失效 |
| `admin/devices.json` | 设备公钥 | 所有设备被吊销 |
| `admin/sessions.json` | 管理面 refresh 会话 | 管理员被登出 |
| `ui_sessions.json` | 浏览器会话（只存 `sha256(token)`） | 浏览器被登出 |
| `sessions.db`、`blob.sqlite3`、`blobs/` | 会话、附件元数据与字节 | 丢历史与附件 |
| `audit/audit.db` | 审计日表 | 丢审计轨迹 |
| `skills/` | 已安装技能 | 丢技能 |
| `setup.token` | 仅初始化前存在 | **不要恢复**：启动会轮换 |

```bash
# 恢复
sudo systemctl stop vade-core
sudo tar --numeric-owner -C / -xzf /var/backups/vade-YYYY-MM-DD-HHMM.tar.gz
sudo chown -R vade:vade /var/lib/vade
sudo systemctl start vade-core
curl -fsS http://127.0.0.1:8443/healthz
```

注意：

* 要拿到 WAL 模式下 SQLite 的一致副本，停 core 是唯一可靠办法。不能停时，
  对每个库用 `sqlite3 <db> ".backup '<out>'"`，`blobs/` 另行复制 —— 不要
  `cp` 正在写的 WAL 库。
* 归档要加密：里面有 `core.key`、`admin.json`、设备公钥和全部对话。
* 在备用机上演练一次恢复。`core.key` 丢了只能把所有东西重新配对。

---

## 14. 升级与回滚

发布包是可重定位的自包含目录，唯一必须活下来的是 `data_dir`。

```bash
VADE_VERSION=0.0.5-alpha
ARCH=x86_64
BASE="https://github.com/lshub2010/Vade/releases/download/v${VADE_VERSION}"

# 0) 先备份（§13）
sudo systemctl stop vade-core

# 1) 下载、校验、解到旧树旁边
cd /tmp
curl -fLO "${BASE}/vade-core-${VADE_VERSION}-linux-${ARCH}.tar.gz"
curl -fLO "${BASE}/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing
sudo tar -C /opt/vade/releases -xzf "vade-core-${VADE_VERSION}-linux-${ARCH}.tar.gz"
sudo chown -R vade:vade "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}"
sudo -u vade "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}/install.sh"

# 2) 切换并启动
OLD=$(readlink -f /opt/vade/current)
sudo ln -sfn "/opt/vade/releases/vade-core-${VADE_VERSION}-linux-${ARCH}" /opt/vade/current
sudo systemctl start vade-core
curl -fsS http://127.0.0.1:8443/healthz

# 3) 需要时回滚
sudo systemctl stop vade-core
sudo ln -sfn "$OLD" /opt/vade/current
sudo systemctl start vade-core
```

注意：

* 升级不会改写 `core.toml`，但**新版本新增的键是安全的，未知键是致命的**
  —— 如果你用了新版本才有的键，回滚到旧版本会以 `UNKNOWN_CONFIG_KEY`
  拒绝启动。把旧 `core.toml` 和旧树放在一起。
* `sessions.db` / `blob.sqlite3` / `audit.db` 没有文档化的迁移框架。若未来版本
  改了存储，回滚意味着恢复升级前备份，而不是只切符号链接。
* 管理页写的 `config.overrides.json` 留在 `data_dir` 并在加载时校验，同样
  适用未知键规则。
* 旧版本目录留几天再删：磁盘便宜，重新配对不便宜。

---

## 15. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `No module named vade` | `PYTHONPATH` 少了 `core` | 两个路径都用绝对路径导出 |
| `protocol core python binding is not importable` | 少了 `protocol/core/python`、架构不符或 `.so` 缺失 | 装对应 `ARCH` 的发布包 |
| 端口不是配置的端口 | `--config` 指向的文件不存在（被静默跳过） | 看 `vade: listeners up` 这行；用绝对路径 |
| `UNKNOWN_CONFIG_KEY: llm.xxx` | 键名拼错或多写 | 只保留 schema 里的键 |
| `refusing to start the admin plane on non-loopback host` | 远程 `admin_host` 没配 TLS | 配 `[tls].cert`+`key`，或绑回环 |
| `configured TLS file … does not exist` | 路径错或密钥不可读 | 修路径/权限；两个键都要有 |
| `another core is already running with data dir` | `core.lock` 被占 | 停掉另一个进程；不要删正在持有的锁 |
| 管理面 `403 ORIGIN_MISMATCH` | `Host`/`Origin` ≠ `core.admin_host` | 用隧道；不要按域名反代 `/admin` |
| 8443 上访问 `/admin` 得到 `404` | 设计如此 | 用 8444 |
| 配对后浏览器立刻掉会话 | 明文 HTTP 下的 `Secure` cookie | 上 HTTPS，或用 `http://127.0.0.1` |
| `POST /ui/session` → `400 pair_code is required` | body 不是合法 UTF-8 JSON（常是中文码被 shell 转码） | 把 body 写成 UTF-8 文件，用 `--data-binary @body.json` |
| 新码也 `CODE_INVALID` | 已兑换过，或 `--no-pair-banner` 根本没铸 | 重启拿新码 |
| `CODE_EXPIRED` | 超过 600 秒 | 重启拿新码 |
| 初始化 `ADMIN_LOCKED` | 该 IP 5 分钟内错 5 次 | 等 5 分钟 |
| 反代后所有人配对都被锁 | 客户端都是 `127.0.0.1`（§11.4） | 等待；不要连续试码 |
| 模块 `core public key unknown` | 没设 `CORE_PUBLIC_KEY`，也没有 `core_pin.json` | 用 §9.3 的 64 位 hex |
| 模块 `core only accepts IK responders today` | 用了 `--pair-code` | 去掉；改手工白名单（§9.3） |
| 模块握手被 1011 关闭 | 白名单池为空/读不出来 | 查 `identity/whitelist.json` 与 fingerprint |
| 模块 `PERMISSION_DENIED` TLS 指纹不符 | `CORE_FINGERPRINT` 不是证书 DER 前缀 | 用 `openssl x509 … \| sha256sum` 重算 |
| 上传被 HTML `413` 挡住 | 反代 `client_max_body_size` 太小 | 调到大于 `attach.max_bytes` |
| `admin print-setup-token` 说 already initialised | `admin/admin.json` 已存在 | 正常，token 已不再使用 |
| `admin …` 说 core appears to be running | CLI 拒绝写运行中的 `data_dir` | 先停服务 |

---

## 16. 安全清单

上线前逐条确认：

- [ ] 专用 `vade` 用户，无 shell、无 root、无 sudo。
- [ ] `/var/lib/vade` 0700、`data_dir` 0700，`core.key` / `setup.token` /
      `whitelist.json` / `admin.json` 0600，unit 里 `UMask=0077`。
- [ ] `/etc/vade/core.toml` 0640 `root:vade`，没有密钥字面量（加载器会拦，
      但仍要自查）。
- [ ] LLM 密钥只在 `/etc/vade/vade.env`（0600）或编排器 secret 里；
      `api_key_env` 只写变量名。
- [ ] 两个面都绑回环，公网只开 443。
- [ ] TLS 在反代终止且用正式证书，HTTP 跳 HTTPS。
- [ ] 管理面**不**由反代发布；访问走 SSH 隧道，或绑内网地址 + core TLS +
      防火墙。
- [ ] `[dev].allow_all = false`，unit 带 `--strict`。
- [ ] 初始化已完成（`admin/admin.json` 存在、`setup.token` 已删），口令 ≥12
      字符且只以 Argon2id 存储。
- [ ] 本次启动不需要浏览器配对时用 `--no-pair-banner`；否则重启后立刻配对。
- [ ] `journalctl` 访问受限（配对码是 10 分钟凭据）。
- [ ] `audit.retention_days`（默认 30）符合合规要求，`audit/audit.db`
      所在卷空间充足。
- [ ] 备份加密、异地，且演练过恢复。
- [ ] 发布包按 `SHA256SUMS` 校验。
- [ ] 防火墙：8443/8444 对不可信网络关闭；模块密钥逐个白名单。
- [ ] 已知“删除 `ui_sessions.json` = 所有浏览器下线”。
- [ ] 监控 `/healthz` 与 `/admin/api/health`；对反复重启告警
      （`Restart=on-failure` 会把崩溃循环藏起来）。

---

## 附录 A —— `data_dir` 结构

```text
data/
├── core.key                  0600  core X25519 种子
├── core.lock                 0600  单实例锁（运行期持有）
├── pairing.sqlite3           0600  哈希后的配对码
├── pairing.pepper            0600  scrypt pepper
├── sessions.db               0600  会话（SQLite，WAL）
├── blob.sqlite3              0600  附件元数据
├── blobs/                    0700  附件字节（含 tmp/、orphan/）
├── audit/
│   ├── audit.db              0600  审计日表
│   └── audit.deadletter.jsonl      写失败的审计行
├── identity/
│   └── whitelist.json        0600  受信模块公钥
├── admin/
│   ├── admin.json            0600  口令哈希、创建时间
│   ├── core_identity.key     0600  管理面身份
│   ├── devices.json          0600  设备公钥
│   └── sessions.json         0600  管理面 refresh 会话
├── ui_sessions.json          0600  浏览器会话（只存 sha256(token)）
├── setup.token               0600  仅初始化前，成功后删除
├── skills/                        已安装技能
└── config.overrides.json          管理页写入，启动时读取
```

## 附录 B —— 本文实测过的命令

环境：Linux x86_64、Python 3.12.3、`dev_gingshan`。

| 检查 | 结果 |
|---|---|
| `python -m vade --help` / `admin --help` | 参数与 11 个子命令与上文一致 |
| 最小 `core.toml` 启动 | `vade: listeners up — data ws://127.0.0.1:18443 admin http://127.0.0.1:18444` |
| 启动横幅 | `pair_code=… purpose=browser expires_in=600s` + `setup_token=acs_…` |
| `GET /healthz` | `{"ok": true, "version": "0.1.0", "policy_mode": "default"}` |
| `GET /admin/api/health` | `200`，带审计计数 |
| `GET /login`、`GET /` | `200 text/html`（`admin-ui/dist/index.html`） |
| `GET /assets/index-*.js` | `200` —— 已挂载 SPA bundle（§7.4） |
| `GET /admin` | `404` JSON —— 入口是 `/login`，不是 `/admin` |
| `GET /admin/setup`（回环） | `setup_required:true`、`requires_token:false`、`bind_url`、`fp8` |
| `POST /admin/setup` | `200`，`admin/admin.json` 0600，`setup.token` 删除 |
| `admin init --password-stdin` | `initialised`，文件同上 |
| `admin print-setup-token` / `print-fingerprint` | `acs_…` / `982d2236708d425b` |
| `POST /ui/session` | `200`，两个 `Set-Cookie`，`{"device_id":"ui-…"}` |
| 二次兑换 | `400 CODE_INVALID` |
| `--strict`、`--no-pair-banner`、`--bind`、`--admin-bind` | 均生效；`--no-pair-banner` 无横幅 |
| `SIGTERM` | `Application shutdown complete`，退出码 0 |
| 端口检查 | 数据面与管理面都在配置的回环端口上 |

本环境未执行：systemd unit（此处没有 systemd PID 1）、Caddy/Nginx reload、
aarch64 发布包。unit 与反代文件只在有对应二进制时做了语法检查。
