# Vade（偕行）

[English](README.md) | 简体中文

**Vade**（中文名「偕行」）是一个内核极薄、能力全靠模块的 Agent 运行时。

内核只做四件事：**连接、路由、回合循环、密钥与审计**。模型、工具、记忆、客户端都是独立进程，主动接入内核。加能力、换模型、换客户端，都不必改内核。

> 名字取自拉丁语 `vade`（走），`vade mecum` 意为「与我同行」；中文名取自《诗经》「与子偕行」。

> [!WARNING]
> **当前是 Beta（`0.1.0-beta`）。** 协议与数据格式在本版本冻结，但界面行为仍可能变化。不建议用它承载不可恢复的数据。自托管或生产化部署请按[中文部署教程](docs/DEPLOYMENT.zh-CN.md)加固，并自担风险。项目由 AI 辅助开发：代码经过自动化测试和人工审查，但风格、注释和边界覆盖并不整齐；用于关键场景前，请自行审计、加固并补测试。

> **要部署 Vade？** 对外暴露前先读[中文部署教程](docs/DEPLOYMENT.zh-CN.md)或 [English deployment guide](docs/DEPLOYMENT.md)，按里面的自托管与生产化加固步骤走。

## Vade 是什么

Vade 只有一条硬规矩：**内核不许知道某个模型、某个工具、某个客户端叫什么。**

- 内核负责建连接、认身份、校验模块 manifest、转发 RPC、跑回合循环、落会话与事件、处理审批、写审计、注入会话记忆。到此为止。
- 工具、模型、记忆、界面页面都由**模块**提供。模块是独立进程，自己连上来，声明自己能做什么。
- 客户端不特殊：Android、桌面、Web 说的是 `ui/*` 契约，和其他模块说的 `module/*` 是同一套协议的不同方向。
- `schema/` 是唯一真相。服务端和所有客户端按同一份 JSON 契约校验；线协议用 Rust 实现，对 Python 暴露绑定。

| | |
|---|---|
| 版本 | `v0.1.0-beta`（pre-release） |
| 契约 | rpc `1.8.0`，三方向 50 个方法；events `2.0.0`，34 个事件；34 个错误码 |
| 验证 | [`docs/ALPHA.md`](docs/ALPHA.md) —— 每条验收句都有证据，已知缺口单列 |

## 为什么这么做

多数 Agent 框架把工具、模型、记忆的抽象塞在内核里。每加一个能力都要改内核，和内核的语言、发布节奏绑死；模块出问题还可能拖垮整个进程。

Vade 反过来：

- **能力是进程。** 模块可以用任何语言写、跑在另一台机器上，重启或摘掉都不必停内核。
- **契约是接入面。** 模块在 manifest 里声明工具、模型能力和可选页面；内核按能力路由，不按模块名写死。
- **客户端可替换。** 内核只暴露会话和事件；任何实现 `ui/*` 契约的客户端都能接入，多个客户端可以同时看同一个会话。
- **模型是后端，不是架构。** 可以用内置的 OpenAI 兼容适配器，也可以把模型挪进独立模块。

代价在运维：你要多跑几个进程并看住它们，打包也还很朴素。

## 0.1.0-beta 新增

Beta 补上了 0.0.5 留下的运行时缺口。

| 方面 | 落地内容 |
|---|---|
| **记忆（M4a）** | 真正的记忆模块：会话/全局两层、内容哈希去重、supersedes 链、评分与淘汰、会话 7 天 TTL、重启后仍可召回；`remember` / `recall` / `forget` 三个工具；私有 `memory/*` RPC。内核持有 `MemoryPort` 适配器，并在每轮注入会话层。 |
| **记忆治理** | 六个 `/admin/api/memory*` 路由替换 501 占位：分页筛选、详情（含 supersedes 链）、配额统计、晋升、归档（必填理由）、恢复。写操作审计、读操作不审计，且没有编辑正文的端点。管理面新增 `/memory` 页面（浏览器会话只读）。 |
| **自进化（M4b）** | 五个确定性触发器：`turn_end` 规则抽取、`turn_review` 骨架复盘、`session_close` 总结、`consolidate`、`skill_evolve`。提案只落在 `skills/pending/`，未经人工批准绝不进入 prompt。默认关闭。 |
| **MCP 桥（M5）** | 支持 stdio 与 HTTP 上游、工具名加前缀、未知 risk 一律按 `high`、动态 `capabilities.update`（失败保留旧表）、2 MB 结果上限、凭据脱敏、拆实例隔离。 |
| **多模型模块并存** | 多个模型模块可同时在线（FR-6.3）；`manifest.priority` 决定默认，`llm.model_module` 可固定。模型通道 `chat` 不再作为工具暴露给模型。 |
| **SDK** | 私有 RPC（`on_rpc`）、事件投递（`on_event`）、带外调用上下文（`current_call_context`）——记忆链路需要、旧传输层丢掉的三个钩子。 |

## 架构

```text
      Android · Windows 桌面 · Web                    模块
             （ui/* RPC）  echo · fs · shell · web · model · memory · mcp-bridge
                 │                                       │（module/* RPC）
                 └──────────────►  内核  ◄───────────────┘
                             连接 · 路由 · 回合循环
                             密钥 · 审计 · SQLite 存储 · 记忆端口

        数据面 :8443                          管理面 :8444

  schema/      契约 —— rpc 1.8.0（三方向 50 个方法）、
               events 2.0.0（34 个事件）、34 个错误码
  protocol/    Rust 线协议：Noise IK/XXpsk3 + ChaCha20-Poly1305，
               并给出内核使用的 Python 绑定
  sdk/         模块侧 SDK（Python）：manifest、工具、配对、传输
  admin-ui/    内核托管的管理前端（浏览器只读，App 内 WebView 可写）
  modules/     只有索引 —— 每个模块都在自己的仓里
```

- **内核里没有模型、工具、记忆存储的逻辑。** 它把一次回合路由给「注册了这项能力」的模块，访问记忆只经过 `MemoryPort`。
- **模块是进程，不是插件。** 模块自己起进程、连内核、声明能力和页面。参考实现是独立仓里的 echo 模块。
- **代码和契约打架时，契约赢。** `schema/` 不是文档，是运行时的校验依据。

## 下载

`0.1.0-beta` 的产物统一使用 `v0.1.0-beta` 这个 tag。

| 组件 | 产物 | 在哪 |
|---|---|---|
| 内核（Linux） | `vade-core-0.1.0-beta-linux-x86_64.tar.gz`、`...-aarch64.tar.gz`，附 SHA-256 校验和 | [Vade `v0.1.0-beta`](https://github.com/lshub2010/Vade/releases/tag/v0.1.0-beta) |
| 客户端 | debug 签名 APK；Windows MSI 与免安装 zip；Web（wasm）zip | [vade-client releases](https://github.com/lshub2010/vade-client/releases) |
| 模块 | 每个模块一个 `py3-none-any` wheel + `manifest.json` | 各模块仓的 Releases 页（见下表链接） |

内核压缩包里带 `core` 包、Python 协议绑定（`libvade_protocol.so`）、`schema/`、Python SDK、构建好的管理前端、`install.sh` 和许可文件。Python 依赖没有随包塞入：`install.sh` 会建虚拟环境并安装 `requirements.txt`。

内核包只有 Linux（`x86_64`、`aarch64`）。Windows 和 macOS 请从源码跑。Android APK 是 debug 签名，Windows 产物未签名 —— 详见[「0.1.0-beta 明确没做」](#0100-beta-明确没做)。

## 从发布包安装（Linux）

```bash
ARCH=x86_64   # 或 aarch64
BASE=https://github.com/lshub2010/Vade/releases/download/v0.1.0-beta

curl -LO "$BASE/vade-core-0.1.0-beta-linux-$ARCH.tar.gz"
sha256sum "vade-core-0.1.0-beta-linux-$ARCH.tar.gz"   # 与发布页上的校验和比对

tar xzf "vade-core-0.1.0-beta-linux-$ARCH.tar.gz"
cd "vade-core-0.1.0-beta-linux-$ARCH"
./install.sh
```

`install.sh` 会建 `.venv/` 并装好运行时依赖。然后写一份最小配置并启动内核：

```bash
cat > core.toml <<'EOF'
[core]
listen   = "127.0.0.1:8443"
data_dir = "./data"
EOF

PYTHONPATH="core:protocol/core/python" .venv/bin/python -m vade --config core.toml
```

首次启动时，内核会往 **stdout** 打印配对码：

```text
pair_code=青松-AAAAAA purpose=browser expires_in=600s
```

码是两个汉字加六位 base32，一次性，600 秒内有效。在客户端的配对界面输入即可。加 `--no-pair-banner` 会关掉打印（也不再铸码）。

桌面客户端连这个内核时，启动时带上 `VADE_SOURCE=real`、`VADE_HOST=127.0.0.1`、`VADE_PORT=8443`、`VADE_PAIR_CODE=<配对码>`。Android 从 intent extras 读同样的值，键名就是 `source`、`host`、`port`、`pair_code`（没有 `vade.` 前缀）。

## 从源码运行

前提：Python **3.11+**；只有需要重编协议绑定时才要 Rust **1.85+**。绑定文件（`libvade_protocol.so` / `.dll`）不入库，新克隆的仓库必须先编一次。

```bash
git clone https://github.com/lshub2010/Vade.git
cd Vade
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

cd protocol/core
cargo build --features uniffi --lib
cp target/debug/libvade_protocol.so python/     # Windows 是 libvade_protocol.dll
cd ../..

export PYTHONPATH="core:protocol/core/python"
.venv/bin/python -m vade --config core.toml
```

`PYTHONPATH` 里的两段缺一不可：`core` 提供 `vade` 包，`protocol/core/python` 提供原生绑定 `vade_protocol`。Windows 上把解释器换成 `.venv\Scripts\python.exe`，分隔符换成分号（`core;protocol/core/python`）。

跑测试和契约门禁：

```bash
.venv/bin/python -m pytest core/tests -q
.venv/bin/python tools/check_contracts.py
.venv/bin/python tools/check_ledger.py
.venv/bin/python docs/check_docs.py
```

## 接一个模型

内核自带 OpenAI 兼容适配器。在 `core.toml` 里加一段 `[llm]`：

```toml
[llm]
base_url    = "https://api.deepseek.com/v1"
model       = "deepseek-chat"
api_key_env = "VADE_LLM_API_KEY"
```

内核会在 `base_url` 后面拼 `/chat/completions`，所以 `/v1` 要留在地址里。密钥只从 `api_key_env` 指定的环境变量读，不写进配置文件，也不进日志。模型在客户端里显示为 `builtin/<model>`。

`provider = "mock"` 可以不起网络就跑回合；`provider = "module"` 把对话交给模型模块。多个模型模块同时在线时，`llm.model_module` 可固定一个，否则取 `manifest.priority` 最高的。

## 记忆

记忆模块和其他能力一样，是独立进程。它用自己的 SQLite 文件，不碰内核数据库。

```bash
git clone https://github.com/lshub2010/Vade-module-memory.git
cd Vade-module-memory
python3 -m venv .venv && .venv/bin/pip install -e ".[core]"
VADE_DATA_DIR=./data .venv/bin/python -m memory_module --core wss://127.0.0.1:8443 --pair-code "<配对码>"
```

内核会拿它做三件事：

- `memory/read_session` 作为会话层注入每轮（有预算，不是整段转录）；
- `remember`、`recall`、`forget` 变成普通工具，风险按 manifest（`forget` 是 `high`）；
- 管理面提供列表 / 详情 / 统计 / 晋升 / 归档 / 恢复。

自进化（M4b）**默认关闭**。在记忆模块上设 `MEMORY_EVOLVE_ENABLED=1` 才会启用；确定性的规则层与 `consolidate` 会运行，依赖 LLM 的 `turn_review` / `session_close` / `skill_evolve` 还需要给模块注入模型调用。触发器契约见 [`docs/agents/46-M4b-evolve.md`](docs/agents/46-M4b-evolve.md)。

## MCP 桥

它把 MCP 服务器的工具变成 Vade 工具，而内核不需要知道 MCP 是什么。

```bash
git clone https://github.com/lshub2010/Vade-module-mcp-bridge.git
cd Vade-module-mcp-bridge
python3 -m venv .venv && .venv/bin/pip install -e ".[core]"
cp config.example.toml config.toml     # 改：上游 command/url + 凭据的环境变量名
.venv/bin/python -m mcp_bridge --core wss://127.0.0.1:8443 --pair-code "<配对码>"
```

上游没声明风险的工具一律按 `high` 走审批。上游凭据只从环境变量读，错误和日志里会脱敏。

## 模块

模块源码不在本仓；[`modules/README.md`](modules/README.md) 只是一张索引。

| 模块 | 做什么 | 仓库 |
|---|---|---|
| echo | 参考实现：四个简单工具，用来证明 manifest → RPC → 结果这条链路 | [vade-module-echo](https://github.com/lshub2010/vade-module-echo) |
| fs | 在白名单根目录下做 `list` / `stat` / `read` / `write` / `delete` | [vade-module-fs](https://github.com/lshub2010/vade-module-fs) |
| shell | 执行命令，限制 cwd、超时和输出截断 | [vade-module-shell](https://github.com/lshub2010/vade-module-shell) |
| web | 带 SSRF 防护的 `fetch` / `head` | [vade-module-web](https://github.com/lshub2010/vade-module-web) |
| model | OpenAI 兼容的模型提供方（`chat`） | [vade-module-model](https://github.com/lshub2010/vade-module-model) |
| memory | 两层记忆、去重、TTL、召回、管理 RPC、M4b 自进化 | [Vade-module-memory](https://github.com/lshub2010/Vade-module-memory) |
| mcp-bridge | 把 stdio/HTTP MCP 服务器桥接成 Vade 工具 | [Vade-module-mcp-bridge](https://github.com/lshub2010/Vade-module-mcp-bridge) |

要写模块，从 [`sdk/python`](sdk/python) 的 Python SDK、[`docs/design.md`](docs/design.md) §15.1 的目录约定、[`schema/manifest.schema.json`](schema/manifest.schema.json) 的 manifest 契约入手。模块连上内核、注册 manifest，然后处理内核转发过来的调用。

SDK 的可选钩子是给非工具能力用的：`ModuleApp.on_rpc(method, handler)` 注册私有 RPC（如 `memory/read_session`），`ModuleApp.on_event(handler)` 接收 `events/deliver`，`current_call_context()` 读取内核注入的 `session_id`。它们都是可选的，普通工具模块不需要。

## 文档

| 读什么 | 用来做什么 |
|---|---|
| [`docs/OVERVIEW.md`](docs/OVERVIEW.md) | 一页总览 |
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | 起一个内核并接上客户端 |
| [`docs/PAIRING.md`](docs/PAIRING.md) | 配对码、浏览器会话、连不上时怎么查 |
| [`docs/design.md`](docs/design.md) | 技术方案：组件、协议、加密、模块 |
| [`docs/rfc/`](docs/rfc) + [`schema/`](schema) | 逐条 RFC 契约与 JSON schema |
| [`docs/ALPHA.md`](docs/ALPHA.md) | 验收证据与已知缺口 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) · [`docs/DEPLOYMENT.zh-CN.md`](docs/DEPLOYMENT.zh-CN.md) | 自托管与生产化加固（English / 中文） |
| [`docs/release/0.1.0-beta.md`](docs/release/0.1.0-beta.md) | Beta 范围、产物与已知缺口 |
| [`docs/status.md`](docs/status.md) | 批次状态与里程碑 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 分支、审查与合并规则 |

## 0.1.0-beta 明确没做

这些是明确延期，不是悄悄缺省：

- **iOS 客户端。** 只构建 Android、Windows 桌面和 Web。
- **生产签名与更新通道。** APK 是 debug 签名，Windows MSI 未签名，也没有更新检查。
- **接托管模型的自进化。** M4b 的规则层与 `consolidate` 可用；依赖 LLM 的 `turn_review` / `session_close` / `skill_evolve` 仍需给模块注入模型调用，当前发布物未配置。
- **T3 完整验收。** core 的四工具路由与客户端工具卡/审批卡绑定已合并；但客户端真模块 e2e 在缺少模块源码时会被 CI 跳过，所以仍不能算完整验收。
- **集群 / K8s 部署与模块市场。** 只有单机 SQLite。
- **IME、AGSL、Web 配对真机验证。** 桌面 CI 证明不了，按未验证对待。

发布清单和已知缺口分别见 [`docs/release/0.1.0-beta.md`](docs/release/0.1.0-beta.md) 和 [`docs/ALPHA.md`](docs/ALPHA.md)。

## 参与开发

日常改动落在 `dev_gingshan`；`debug` 是基线分支，只收阶段合入。审查与合并门槛见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 许可

**Apache License 2.0。** 完整条款见 [`LICENSE`](LICENSE)，署名与第三方声明见 [`NOTICE`](NOTICE)。修改后再分发时，保留许可与声明文件、在改过的文件里显著标注改动，并且不要暗示原作者为你背书。
