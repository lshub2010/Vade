# Vade Agent Runtime

**Vade** · *Walks with you.*

> 一个「内核极薄、能力全靠模块」的 Agent 运行时。core 只做连接、路由、循环、密钥、审计；工具、记忆、模型、客户端全部是独立进程，主动接入。

## 名字

**Vade Agent Runtime** 是正式全称，日常只用 **Vade**。名字取自拉丁语 `vade`（走）——`vade mecum` 是「与我同行」；中文取《诗经·秦风·无衣》的「与子偕行」。

**Vade** 是简称。三种写法，不要混：

| 场景 | 写法 | 例 |
|---|---|---|
| 产品简称、UI 文案、文档正文 | `Vade` | 「打开 Vade App」 |
| 代码标识符 | `vade`（全小写） | 包名 `vade`、URI `vade://bind`、协议 `vade/v1`、`python -m vade` |
| 环境变量 | `VADE_`（全大写） | `VADE_SETUP_TOKEN` |

配置文件仍叫 `core.toml`——它指 core 进程的配置，不是产品名；页面改动的值落 `data/config.overrides.json`，不回写 toml。

全称只用于正式场合（文档标题、发布说明、对外介绍）；正文、UI、口语一律用 Vade。

> 名字只说体验，不说功能。功能由这一行承担：**Agent 运行时**。

## 状态

| 项 | 值 |
|---|---|
| 阶段 | 设计与任务书就绪，**代码尚未开工** |
| 文档版本 | v1.18 |
| 需求基线 | `docs/requirements.md` v1.0 + AMEND-01～21，86 条决策（D1–D86） |

## 仓库结构

```
.
├── docs/                 设计文档与开发任务书
│   ├── requirements.md   需求基线、AMEND 明细、决策台账
│   ├── design.md         技术方案
│   ├── OVERVIEW.md       一页总览（Markdown 源）
│   ├── status.md         批次台账、里程碑、阻塞项
│   ├── build_overview.py OVERVIEW.md → overview.html 生成脚本
│   └── agents/           各批次开发 / 审查任务书
└── overview.html         总览渲染版（含架构图、协议帧、拓扑示意）
```

## 技术栈

| 层 | 选型 |
|---|---|
| core | Python 3.11 + SQLite |
| 协议 SDK | Rust 单实现（`snow` Noise / AEAD），UniFFI 导出 Kotlin / Swift / Python / JS |
| 客户端 | Kotlin Multiplatform，Compose MP + iOS SwiftUI |
| 管理面 | React + React Flow（core 内置，仅回环与 App WebView） |
| 存储 | SQLite（会话、记忆、审计、画布） |

## 从哪读起

| 你有 | 读这些 |
|---|---|
| 5 分钟 | `docs/OVERVIEW.md` 或 `overview.html` |
| 30 分钟 | `docs/OVERVIEW.md` + `docs/design.md` §1–§7 |
| 要开工 | `docs/agents/README.md` → `00-orchestration.md` → `01-contracts.md` |
| 要审查 | `docs/agents/7x-R*.md` |
| 要看进度 | `docs/status.md` |

## 分支模型

```
debug                 基线分支（默认分支，受保护）
└── dev_gingshan      日常开发分支（受保护）
    └── tmp/<主题>    临时分支（一次 PR 一条，合并后即删）
```

- **`debug`** 是唯一基线。只有一类 PR 能进：`dev_gingshan` → `debug` 的阶段合入。禁止直推、禁 force push、禁删除。
- **`dev_gingshan`** 是日常开发分支。所有功能、修复、重构提交到这里，同样禁直推。
- **临时分支** 从 `dev_gingshan` 拉出，命名 `tmp/<主题>`（如 `tmp/c3-session-loop`），PR 合入后立即删除，不保留长期特性分支。

## 提交与 PR

- 提交信息：`<type>(<scope>): <subject>`，type ∈ `feat|fix|docs|refactor|test|chore`，scope 用批次号或模块名，如 `feat(c3): 会话循环支持中断重入`。
- 契约优先：改动 `schema/` 下的契约必须先提 PR 并在 `docs/agents/01-contracts.md` 登记，实现随后跟上。
- 每个批次一个 PR，模板见 `.github/pull_request_template.md`。
- **PR 审核由子代理执行**：按 `docs/agents/70-R1-spec-review.md`、`71-R2-correctness-review.md`、`72-R3-security-review.md` 的清单逐条核对，结论以 review 形式贴回 PR。审核者不得是被审代码的编写者。详见 `CONTRIBUTING.md`。
