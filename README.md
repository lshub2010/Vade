# Vade

**Vade**（中文名「偕行」）是一个**内核极薄、能力全靠模块**的 Agent 运行时。

core 只做四件事：**连接、路由、回合循环、密钥与审计**。模型、工具、记忆、界面全部是独立进程，主动接入——换模型、加能力、换客户端，都不动内核。

> 名字取自拉丁语 `vade`（走），`vade mecum` 意为「与我同行」；中文取《诗经》「与子偕行」。

---

## 当前发布

| 项 | 值 |
|---|---|
| 版本 | **`0.0.1-alpha`**（pre-release） |
| 契约 | rpc **1.7.0**，三方向 **49** 个方法，事件 **2.0.0**（34 项），错误码 **34** |
| 服务端测试 | `pytest core/tests` → **1203 passed** |
| 客户端 | 独立仓 [lshub2010/vade-client](https://github.com/lshub2010/vade-client)（Android / 桌面 / Web） |
| 验收台账 | [`docs/ALPHA.md`](docs/ALPHA.md) —— 21 条 alpha 范围逐条标注证据，0 阻断，已知缺陷见 §八 |

alpha 的含义：**协议与数据格式不稳定**，可以随时变；不要拿它存长期数据。

---

## 30 秒理解架构

```
   客户端 (Android/桌面/Web)          模块 (工具/模型/记忆/页面)
        │  ui/*  →                       │  module/* →
        └──────────►  core  ◄────────────┘
                 连接 · 路由 · 回合循环
                 密钥 · 审计 · 存储(SQLite)
```

- **core 不含任何智能**：它不知道什么是「模型」，只知道怎么把一次回合的请求路由给注册了该能力的模块。
- **模块是进程，不是插件**：模块自己起进程、自己连 core、声明自己的能力和页面（随仓的 echo 模块是一份最小可跑样例，见 `modules/README.md`）。
- **契约先于实现**：`schema/` 下的 JSON 是唯一真相，服务端与客户端都从它生成/校验。

---

## 快速开始

### 环境

Python **3.11+**（core）、SQLite（内置）。可选：Rust 1.100（重编协议 SDK）、JDK 21 + Android SDK（客户端）。

```bash
git clone https://github.com/lshub2010/Vade.git
cd Vade
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 起一个 core

```bash
export PYTHONPATH="core;protocol/core/python"     # Linux / macOS 用冒号分隔
.venv/Scripts/python.exe -m vade --config core.toml
```

`PYTHONPATH` 里**两处都必须在**，缺一个都起不来：

| 路径 | 提供什么 | 缺了会怎样 |
|---|---|---|
| `core` | `vade` 包本身 | `No module named vade` |
| `protocol/core/python` | 原生绑定 `vade_protocol` | `protocol core python binding is not importable` |

两条都是相对路径 ⇒ 必须先 `cd` 到仓库根（写绝对路径也行）。细节见 [`docs/PAIRING.md`](docs/PAIRING.md)。

第一次启动会做 setup（生成密钥、设备绑定），并打印一行
`pair_code=茶源-45JV2U purpose=browser expires_in=600s`：**2 汉字 + 6 位 base32，一次性、600 秒（10 分钟）有效**。
`--no-pair-banner` 可关掉这次打印。

### 接一个客户端

客户端仓见 [lshub2010/vade-client](https://github.com/lshub2010/vade-client)。它默认跑假数据，连真 core 只要传四个键：

```bash
# 桌面
VADE_SOURCE=real VADE_HOST=127.0.0.1 VADE_PORT=8443 VADE_PAIR_CODE=<配对码> ./vade-ui
# Android
adb shell am start -n vade.ui/.MainActivity --es source real --es host 127.0.0.1 --es port 8443 --es pair_code <配对码>
```

### 写一个模块（最小骨架）

模块 = 一个连 core 的进程，声明 `manifest`（能力 + 页面），然后处理 core 发来的请求：

```python
# 1) 启动时向 core 注册：manifest 里声明能力与页面
# 2) 收到 core 的 RPC 就回结果；core 不认识你现在有什么，只认 manifest
```

参考实现见 [`modules/README.md`](modules/README.md)：随仓的 `echo` 模块已迁到独立仓
[lshub2010/Vade-module-echo](https://github.com/lshub2010/Vade-module-echo)（四个工具 + 一个页面），
它同时是模块协议的最小可跑样例。协议细节见 [`docs/rfc/`](docs/rfc)。

### 常用命令

```bash
python -m pytest core/tests -q          # 服务端全量测试
python tools/check_contracts.py         # 契约自检（冻结项 / appends / 事件门禁）
python tools/check_ledger.py            # RFC 台账与实现路径核对
python docs/check_docs.py               # 文档一致性（12 项）
```

---

## 接一个模型

core **自己不含模型**——它只做路由。要用真模型，两条路：

### 路线 A：内置适配器（OpenAI 兼容，先走这条）

任何讲 OpenAI `/v1/chat/completions` 的服务都能接：OpenAI、DeepSeek、Moonshot、通义、vLLM、Ollama、LM Studio……

在 `core.toml` 里加一段 `[llm]`：

```toml
[core]
listen   = "127.0.0.1:8443"
data_dir = "./data"

[llm]
provider    = "openai_compatible"            # 默认值，可省略
base_url    = "https://api.deepseek.com/v1"  # 必须写到 /v1 这一级：core 会拼 /chat/completions
model       = "deepseek-chat"
api_key_env = "VADE_LLM_API_KEY"             # 只写变量名，密钥本身不写进配置文件
```

密钥放进环境变量（**core 只从环境读**，不落配置、不进日志）：

```bash
export VADE_LLM_API_KEY="sk-..."        # PowerShell: $env:VADE_LLM_API_KEY="sk-..."
export PYTHONPATH="core;protocol/core/python"
.venv/Scripts/python.exe -m vade --config core.toml
```

起来后客户端模型 chip 里会出现 **`builtin/deepseek-chat`**（模型 id 形如 `builtin/<model>`），选中它就能对话。

`[llm]` 一共只有下面 8 个键，**多一个都会拒绝启动**（拼错键名不会被默默忽略）：

| 键 | 默认 | 作用 |
|---|---|---|
| `provider` | `openai_compatible` | `openai_compatible` / `module` / `mock` |
| `base_url` | 空 | 端点根；**含 `/v1`**，core 只在后面拼 `/chat/completions` |
| `model` | 空 | 默认模型名（如 `deepseek-chat`） |
| `api_key_env` | 空 → 兜底 `VADE_LLM_API_KEY` | 存放密钥的**环境变量名** |
| `default_model` | 空 | 全局默认模型：会话没显式选模型时用它；会话的选择永远优先、不会被静默替换 |
| `model_module` | 空 | `provider="module"` 时限定只问哪个模块；空 = 任何声明了 `chat` 的模块 |
| `fallback_to_builtin` | `true` | 模块离线时是否回落到内置适配器 |
| `capabilities` | `{}` | 声明模型能力：`display_name` / `supports_tools` / `context_window` / `input_modalities` / `max_image_bytes` / `max_image_px` / `max_images_per_turn` |

**连不上怎么查**：

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即报 `UNKNOWN_CONFIG_KEY: llm.xxx` | `[llm]` 里键名拼错或写多 | 只保留上表 8 个键 |
| 回合失败 `401` | 环境变量没设，或 `api_key_env` 名字与真实变量名不一致 | `echo $VADE_LLM_API_KEY` 确认非空 |
| 回合失败 `404` / `405` | `base_url` 少了 `/v1`（拼成了 `/v1/v1` 或根域名） | 只到 `/v1` 为止 |
| `ImportError: httpx` | 运行环境缺 `httpx`（内置适配器才需要） | `pip install -r requirements.txt`（已包含） |
| 请求发出但没回复 | `model` 名与端点上的名字不符 | 用端点自己列出的模型名 |

### 路线 B：模型做成模块（进程隔离）

模型可以完全不在 core 里，而是一个独立进程，通过契约注册进来：

```toml
[llm]
provider            = "module"
model_module        = "com.example.model"   # 留空 = 任何声明了 chat 的模块都可服务
fallback_to_builtin = true                  # 模块离线时回落内置（false 则直接 turn.failed(UNAVAILABLE)）
```

模块在 manifest 的 `capabilities.models[]` 里声明它提供哪些模型（名字、上下文窗口、是否支持工具与图片），
core 把在线模块的声明聚合成 `model/list`，客户端 chip 直接读这份列表。协议细节见 [`docs/rfc/RFC-0005-model-capabilities-usage.md`](docs/rfc/RFC-0005-model-capabilities-usage.md)。

### 先不接模型：`provider = "mock"`

```toml
[llm]
provider = "mock"
```

此时模型列表里只有 `builtin/mock`，回合不联网——用来验证「core 起没起来、客户端连没连上」，不花一分钱。

### 用量与预算

同一个配置文件里的 `[usage]` 管用量：

| 键 | 默认 | 含义 |
|---|---|---|
| `enabled` | `true` | 是否统计用量 |
| `session_tokens` | `0` | 单会话 token 预算；**0 = 不限** |
| `daily_tokens` | `0` | 每日 token 预算；**0 = 不限** |
| `warn_ratio` | `0.8` | 用到预算的多少比例开始告警 |
| `include_aux` | `true` | 辅助调用（压缩 / 记忆 / 自演化）是否也计入；`false` = 只算对话 |
| `enforce` | `"block"` | `block` = 超预算直接拒绝 `ui/submit`（`BUDGET_EXCEEDED`）；`warn` = 只告警不拦。台账不可信时会自动降级成告警，避免误吞消息 |

## 仓库结构

```
.
├── core/              core 进程（会话、回合、路由、密钥、审计、blob、模块页）
├── schema/            契约：rpc / events / errors / skill / manifest（唯一真相）
├── protocol/          Rust 协议 SDK（Noise + AEAD），UniFFI 导出 Kotlin/Swift/Python/JS
├── modules/           随仓模块（echo：四工具 + 一页）
├── sdk/               模块侧 SDK（Python / Kotlin）
├── admin-ui/          管理面前端（core 内置托管，仅回环与 App WebView）
├── docs/              设计、RFC、任务书、alpha 台账
└── deploy/            部署样例
```

## 文档

| 想看什么 | 去哪 |
|---|---|
| 一页总览 | [`docs/OVERVIEW.md`](docs/OVERVIEW.md) / [`overview.html`](overview.html) |
| 需求与决策台账（D1–D110） | [`docs/requirements.md`](docs/requirements.md) |
| 技术方案 | [`docs/design.md`](docs/design.md) |
| 协议契约（逐条 RFC） | [`docs/rfc/`](docs/rfc) + [`schema/`](schema) |
| alpha 验收台账与已知缺陷 | [`docs/ALPHA.md`](docs/ALPHA.md) |
| 配对与设备绑定 | [`docs/PAIRING.md`](docs/PAIRING.md) |
| 批次状态 / 里程碑 | [`docs/status.md`](docs/status.md) |
| 开发任务书 | [`docs/agents/`](docs/agents) |

## 分支模型

```
debug                 基线分支（只收来自 dev_gingshan 的阶段合入 PR）
└── dev_gingshan      日常集成分支
    └── tmp/<主题>     一次改动一条，合入后即删
```

## 许可

**Apache License 2.0** —— 完整条款见 [`LICENSE`](LICENSE)，署名与第三方声明见 [`NOTICE`](NOTICE)。

```
Copyright (c) 2026 Vade contributors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

### 二次修改与再分发（必须标明原作者）

Apache-2.0 是宽松许可（可商用、可闭源衍生），但**署名不能省**：

- 再分发时必须保留 [`LICENSE`](LICENSE) 与 [`NOTICE`](NOTICE)（§4(c)(d)）；
- **凡改动过的文件，必须在文件内显著声明「你改过」**（§4(b)）；
- 不得暗示原作者为你背书（§4(g)），不得使用项目商标（§6）。

一句话：**可以改、可以商用、可以不开源，但不能抹掉原作者、不能把改过的版本说成原版。**

## 第三方组件与许可

下表按 `requirements.txt` 里各包自带的元数据核实（2026-11-15）：

| 组件 | 版本 | 许可 | 用途 |
|---|---|---|---|
| uvicorn | 0.54.0 | BSD-3-Clause | ASGI 服务器 |
| starlette | 1.9.0 | BSD-3-Clause | Web 框架（HTTP / WebSocket 路由） |
| websockets | 15.0.1 | BSD-3-Clause | WebSocket 实现 |
| PyYAML | 6.0.3 | MIT | 配置与模块 manifest 解析 |
| argon2-cffi | 25.1.0 | MIT | 口令 / 密钥派生 |

> **兼容性**：BSD-3-Clause 与 MIT 均与 GPLv3 兼容；上述声明随源码与二进制一同保留。
> 客户端的第三方组件（Compose、Ktor、Miuix、haze、Tabler Icons、KernelSU 等）见
> [vade-client README](https://github.com/lshub2010/vade-client#第三方组件与许可)。

## 引用

```bibtex
@software{vade2026,
  title     = {Vade: a thin-core, module-first agent runtime},
  author    = {{Vade contributors}},
  year      = {2026},
  version   = {0.0.1-alpha},
  url       = {https://github.com/lshub2010/Vade}
}
```

机器可读元数据见 [`CITATION.cff`](CITATION.cff)。
