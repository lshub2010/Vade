# Vade（偕行）

[English](README.md) | 简体中文

**Vade**（中文名「偕行」）是一个内核极薄、能力全靠模块的 Agent 运行时。

内核只做四件事：**连接、路由、回合循环、密钥与审计**。模型、工具、记忆、客户端都是独立进程，主动接入内核。加能力、换模型、换客户端，都不必改内核。

> 名字取自拉丁语 `vade`（走），`vade mecum` 意为「与我同行」；中文名取自《诗经》「与子偕行」。

> [!WARNING]
> **当前是 alpha（`0.0.4-alpha`）。** 协议、数据格式和界面行为都可能随时变化。不建议用它承载不可恢复的数据。自托管或生产化部署请按[中文部署教程](docs/DEPLOYMENT.zh-CN.md)加固，并自担风险。项目由 AI 辅助开发：代码经过自动化测试和人工审查，但风格、注释和边界覆盖并不整齐；用于关键场景前，请自行审计、加固并补测试。

> **要部署 Vade？** 对外暴露前先读[中文部署教程](docs/DEPLOYMENT.zh-CN.md)或 [English deployment guide](docs/DEPLOYMENT.md)，按里面的自托管与生产化加固步骤走。

## Vade 是什么

Vade 只有一条硬规矩：**内核不许知道某个模型、某个工具、某个客户端叫什么。**

- 内核负责建连接、认身份、校验模块 manifest、转发 RPC、跑回合循环、落会话与事件、处理审批、写审计。到此为止。
- 工具、模型、界面页面都由**模块**提供。模块是独立进程，自己连上来，声明自己能做什么。
- 客户端不特殊：Android、桌面、Web 说的是 `ui/*` 契约，和其他模块说的 `module/*` 是同一套协议的不同方向。
- `schema/` 是唯一真相。服务端和所有客户端按同一份 JSON 契约校验；线协议用 Rust 实现，对 Python 暴露绑定。

| | |
|---|---|
| 版本 | `v0.0.4-alpha`（pre-release） |
| 契约 | rpc `1.7.0`，三方向 49 个方法；events `2.0.0`，34 个事件；34 个错误码 |
| 验证 | [`docs/ALPHA.md`](docs/ALPHA.md) —— 每条 alpha 验收句都有证据，已知缺陷单列 |

## 为什么这么做

多数 Agent 框架把工具、模型、记忆的抽象塞在内核里。每加一个能力都要改内核，和内核的语言、发布节奏绑死；模块出问题还可能拖垮整个进程。

Vade 反过来：

- **能力是进程。** 模块可以用任何语言写、跑在另一台机器上，重启或摘掉都不必停内核。
- **契约是接入面。** 模块在 manifest 里声明工具、模型能力和可选页面；内核按能力路由，不按模块名写死。
- **客户端可替换。** 内核只暴露会话和事件；任何实现 `ui/*` 契约的客户端都能接入，多个客户端可以同时看同一个会话。
- **模型是后端，不是架构。** 可以用内置的 OpenAI 兼容适配器，也可以把模型挪进独立模块。

代价在运维：你要多跑几个进程并看住它们，alpha 阶段的打包也还很朴素。

## 架构

```text
      Android · Windows 桌面 · Web                    模块
             （ui/* RPC）                echo · fs · shell · web · model
                 │                                       │（module/* RPC）
                 └──────────────►  内核  ◄───────────────┘
                             连接 · 路由 · 回合循环
                             密钥 · 审计 · SQLite 存储

        数据面 :8443                          管理面 :8444

  schema/      契约 —— rpc 1.7.0（三方向 49 个方法）、
               events 2.0.0（34 个事件）、34 个错误码
  protocol/    Rust 线协议：Noise IK/XXpsk3 + ChaCha20-Poly1305，
               并给出内核使用的 Python 绑定
  sdk/         模块侧 SDK（Python）：manifest、工具、配对、传输
  admin-ui/    内核托管的管理前端（浏览器只读，App 内 WebView 可写）
  modules/     只有索引 —— 每个模块都在自己的仓里
```

- **内核里没有模型、工具、记忆的逻辑。** 它只知道怎么把一次回合路由给「注册了这项能力」的模块。
- **模块是进程，不是插件。** 模块自己起进程、连内核、声明能力和页面。参考实现是独立仓里的 echo 模块。
- **代码和契约打架时，契约赢。** `schema/` 不是文档，是运行时的校验依据。

## 0.0.4-alpha 的现状

| 层面 | 现状 |
|---|---|
| 内核 | 数据面 WebSocket `:8443`；设备/模块走 Noise 握手，浏览器走 cookie 会话；模块注册表与路由；FIFO 回合队列，带 `queue_pos` 和中断；事件流带 `seq` 与重放；SQLite 会话与 attach 快照；会话内审批；只追加审计；用量预算 |
| 模型 | 内置 OpenAI 兼容适配器（`/v1/chat/completions`）；`model/discover` 可拉取上游 `/models`，最后一次成功列表会跨重启保留；`provider = "module"` 可把模型放进独立模块；`mock` 可离线跑通回合 |
| 客户端 | Android、Windows 桌面、Web（wasm）；默认走假数据，显式开启后连真内核；发送/流式/中断、会话与分组管理、内联审批、模块页面 |
| 管理面 | `:8444` 上的管理监听与 React 前端。登录和节点治理已实现；setup、audit、settings 仍是占位页 |
| 模块 | 五个独立仓：echo（参考实现）、fs、shell、web、model；各有自己的 CI 和测试 |

## 下载

`0.0.4-alpha` 的产物统一使用 `v0.0.4-alpha` 这个 tag。

| 组件 | 产物 | 在哪 |
|---|---|---|
| 内核（Linux） | `vade-core-0.0.4-alpha-linux-x86_64.tar.gz`、`...-aarch64.tar.gz`，附 SHA-256 校验和 | [Vade `v0.0.4-alpha`](https://github.com/lshub2010/Vade/releases/tag/v0.0.4-alpha) |
| 客户端 | debug 签名 APK；Windows MSI 与免安装 zip；Web（wasm）zip | [vade-client releases](https://github.com/lshub2010/vade-client/releases) |
| 模块 | 每个模块一个 `py3-none-any` wheel + `manifest.json` | 各模块仓的 Releases 页（见下表链接） |

内核压缩包里带 `core` 包、Python 协议绑定（`libvade_protocol.so`）、`schema/`、Python SDK、构建好的管理前端、`install.sh` 和许可文件。Python 依赖没有随包塞入：`install.sh` 会建虚拟环境并安装 `requirements.txt`。

内核包只有 Linux（`x86_64`、`aarch64`）。Windows 和 macOS 请从源码跑。Android APK 是 debug 签名，Windows 产物未签名 —— 详见[「0.0.4-alpha 明确没做」](#004-alpha-明确没做)。

## 从发布包安装（Linux）

```bash
ARCH=x86_64   # 或 aarch64
BASE=https://github.com/lshub2010/Vade/releases/download/v0.0.4-alpha

curl -LO "$BASE/vade-core-0.0.4-alpha-linux-$ARCH.tar.gz"
sha256sum "vade-core-0.0.4-alpha-linux-$ARCH.tar.gz"   # 与发布页上的校验和比对

tar xzf "vade-core-0.0.4-alpha-linux-$ARCH.tar.gz"
cd "vade-core-0.0.4-alpha-linux-$ARCH"
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

`provider = "mock"` 可以不起网络就跑回合；`provider = "module"` 把对话交给模型模块（见 [`docs/rfc/RFC-0005-model-capabilities-usage.md`](docs/rfc/RFC-0005-model-capabilities-usage.md)）。

## 模块

模块源码不在本仓；[`modules/README.md`](modules/README.md) 只是一张索引。

| 模块 | 做什么 | 仓库 |
|---|---|---|
| echo | 参考实现：四个简单工具，用来证明 manifest → RPC → 结果这条链路 | [vade-module-echo](https://github.com/lshub2010/vade-module-echo) |
| fs | 在白名单根目录下做 `list` / `stat` / `read` / `write` / `delete` | [vade-module-fs](https://github.com/lshub2010/vade-module-fs) |
| shell | 执行命令，限制 cwd、超时和输出截断 | [vade-module-shell](https://github.com/lshub2010/vade-module-shell) |
| web | 带 SSRF 防护的 `fetch` / `head` | [vade-module-web](https://github.com/lshub2010/vade-module-web) |
| model | OpenAI 兼容的模型提供方（`chat`） | [vade-module-model](https://github.com/lshub2010/vade-module-model) |

要写模块，从 [`sdk/python`](sdk/python) 的 Python SDK、[`docs/design.md`](docs/design.md) §15.1 的目录约定、[`schema/manifest.schema.json`](schema/manifest.schema.json) 的 manifest 契约入手。模块连上内核、注册 manifest，然后处理内核转发过来的调用。

## 文档

| 读什么 | 用来做什么 |
|---|---|
| [`docs/OVERVIEW.md`](docs/OVERVIEW.md) | 一页总览 |
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | 起一个内核并接上客户端 |
| [`docs/PAIRING.md`](docs/PAIRING.md) | 配对码、浏览器会话、连不上时怎么查 |
| [`docs/design.md`](docs/design.md) | 技术方案：组件、协议、加密、模块 |
| [`docs/rfc/`](docs/rfc) + [`schema/`](schema) | 逐条 RFC 契约与 JSON schema |
| [`docs/ALPHA.md`](docs/ALPHA.md) | alpha 验收证据与已知缺陷 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) · [`docs/DEPLOYMENT.zh-CN.md`](docs/DEPLOYMENT.zh-CN.md) | 自托管与生产化加固（English / 中文） |
| [`docs/release/0.0.2.md`](docs/release/0.0.2.md) | 0.0.2 发布范围与待办 |
| [`docs/status.md`](docs/status.md) | 批次状态与里程碑 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 分支、审查与合并规则 |

## 0.0.4-alpha 明确没做

这些是明确延期，不是悄悄缺省：

- **iOS 客户端。** 只构建 Android、Windows 桌面和 Web。
- **记忆与 M4b 自演化。** 没有 memory 模块，没有 memory schema，内核里也没有记忆子系统。
- **mcp-bridge 模块。** 没建。
- **生产签名与更新通道。** APK 是 debug 签名，Windows MSI 未签名，也没有更新检查。
- **上游 `/v1/models` 自动发现、多模型模块并存。** 模型名要手工配；跨模块 `chat` 冲突要单独出 RFC。
- **真模型驱动的工具调用端到端验收（T3）。** 工具卡和路由已经有了，模型自主调用工具的完整链路还没验收。
- **登录和节点以外的管理页。** setup、audit、settings 是占位页；skills、memory、topology 还没实现。

发布清单和已知缺陷分别见 [`docs/release/0.0.2.md`](docs/release/0.0.2.md) 和 [`docs/ALPHA.md`](docs/ALPHA.md)。

## 参与开发

日常改动落在 `dev_gingshan`；`debug` 是基线分支，只收阶段合入。审查与合并门槛见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 许可

**Apache License 2.0。** 完整条款见 [`LICENSE`](LICENSE)，署名与第三方声明见 [`NOTICE`](NOTICE)。修改后再分发时，保留许可与声明文件、在改过的文件里显著标注改动，并且不要暗示原作者为你背书。
