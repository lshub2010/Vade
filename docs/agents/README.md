# 子代理开发文档索引

> 本目录是 23 个开发子代理 + 3 个审查子代理的**开工说明**。
> 每个文件都是可直接作为 prompt 投喂给一个新上下文子代理的完整任务书。
> 上游依据只有两份：`docs/requirements.md`（需求基线 v1.0 + **AMEND-01～22**）与 `docs/design.md`（方案 **v1.18**）。两者冲突时以需求为准，并立刻升级给编排者。

## 一、文档清单

| 文件 | 角色 | 波次 | 依赖 | 目录所有权 |
|---|---|---|---|---|
| `00-orchestration.md` | 编排者（主线，不写业务代码） | 全程 | — | 全部（只读业务目录） |
| `01-contracts.md` | 契约台账（所有 agent 必读） | 全程 | — | 只读（契约由 C1/C2/C5a 产出） |
| `10-C0a-uniffi.md` | 验证：UniFFI 绑定 | W0 | — | `spike/uniffi/` |
| `11-C0b-wasm.md` | 验证：Rust→Wasm | W0 | — | `spike/wasm/` |
| `12-C0c-miuix.md` | 验证：Miuix on CMP | W0 | — | `spike/miuix/` |
| `20-C1-protocol-core.md` | Rust 协议核心 | W1 | C0a/C0b 结论 | `protocol/core/`、`protocol/v1/` |
| `21-C2-transport-registry.md` | 传输 + 注册表 + manifest 校验 | W2 | C1 | `core/vade/transport/`、`registry/`、`crypto_gateway/`、`schema/` |
| `22-C3-session-loop.md` | 会话 + 队列 + 路由 + Agent 循环 | W3 | C2 | `core/vade/session/`、`router/`、`loop/`、`llm/` |
| `23-C3b-skill-loader.md` | skill 加载器（说明书，不含执行通道） | W4 | C3 的 prompt 组装点 | `core/vade/skill/`、`schema/skill.schema.json` |
| `24-C3d-events-skill-proposal.md` | 事件推送 + skill 提案受理 | W4 | C3b 的 schema | `core/vade/events/`、`skill/proposal.py` |
| `30-C4-sdk-echo.md` | Python SDK v0.1 + echo 模块 | W4 | C3 | `sdk/python/`、`modules/echo/` |
| `31-C5a-governance-admin.md` | 审批 + 审计 + admin API + 列表页 | W4 | C3 | `core/vade/policy/`、`audit/`、`admin/`、`admin-ui/src/pages/` |
| `32-C5b-topology-canvas.md` | 拓扑画布 | W5 | C5a 的 API 契约 | `admin-ui/src/canvas/` |
| `40-M1-fs.md` | 文件模块 | W5 | C4 SDK v0.1 | `modules/fs/` |
| `41-M2-shell.md` | 命令模块 | W5 | C4 | `modules/shell/` |
| `42-M3-web.md` | 网络模块 | W5 | C4 | `modules/web/` |
| `43-M4-memory.md` | 记忆存储与召回（两层） | W5 | C4 | `modules/memory/` |
| `44-M5-mcp-bridge.md` | MCP 桥 | W5 | C4 | `modules/mcp_bridge/` |
| `45-M6-model.md` | 模型模块 | W5 | C4 | `modules/model/` |
| `46-M4b-evolve.md` | 自进化（memory 的后台角色） | W6 | M4a + C3b + C3d | `modules/memory/src/memory_module/evolve/` |
| `50-U1-ui-skeleton.md` | UI 骨架 + material 皮肤 | W4 | C0a/C0b、C4 协议 API | `ui/` |
| `51-U2-skin-miuix.md` | miuix 皮肤 | W5 | U1 的 `Skin` 冻结 | `ui/composeApp/src/commonMain/kotlin/skin/miuix/` |
| `52-U3-skin-glass.md` | glass 皮肤 | W5 | U1 的 `Skin` 冻结 | `ui/composeApp/src/commonMain/kotlin/skin/glass/` |
| `53-U4-ios.md` | iOS SwiftUI 客户端 | W6 | U1 的 shared 契约 | `ui/ios/` |
| `60-F1-integration.md` | 联调 + 安全 + 打包 | W6 | 全部 | `deploy/`、`tests/e2e/`、`release/` |
| `70-R1-spec-review.md` | 审查 A：规格符合性 | 每批 | 批次交付 | 只读 |
| `71-R2-correctness-review.md` | 审查 B：产物正确性 | 每批 | 批次交付 | 只读 |
| `72-R3-security-review.md` | 审查 C：安全专项 | C1/C2/C5a/C5b/M1–M6/F1 | 批次交付 | 只读 |

## 二、怎么用

1. **编排者**先读 `00-orchestration.md`，按波次表派活。
2. 派活时**只传 brief 路径 + 上游契约路径**，不转述内容、不粘贴整份需求（子代理自己读）。
3. 子代理交付后，**另起两个新上下文**分别跑 R1/R2（C1/C2/C5a/C5b/M1–M6/F1 加 R3，与 `00-orchestration.md` §四一致）。
4. 打回时 `resume` 原开发子代理，把问题清单原样给它；**不要新开代理重写**。
5. 每批的状态回写 `00-orchestration.md` 的台账表。

## 三、三条全局硬约束

| 约束 | 判据 |
|---|---|
| **目录所有权** | 只能写自己 brief 里列出的目录；跨目录改动一律先提 RFC |
| **契约先落文件** | 接口以 `schema/*.json`、`protocol/v1/*.md`、`Skin.kt`、`*.pyi` 形式先提交，代码后写 |
| **不做局部绿** | 合并前跑全量测试 + 契约一致性检查；只跑自己包的测试不算完成 |

## 四、缩写

| 缩写 | 含义 |
|---|---|
| W0–W5 | 波次（Wave），同一波次内的 agent 真并行 |
| KAT | Known Answer Test，固定密钥/明文 → 固定密文的协议锁定测试 |
| 五道门 | 模块交付门禁：独立运行 / 独立测试 / 独立部署 / 独立失败 / 独立观测 |
| 三通道 | 状态编码必须同时用颜色 + 线型，不允许只靠颜色 |
| R1/R2/R3 | 三类独立审查：规格、正确性、安全 |
