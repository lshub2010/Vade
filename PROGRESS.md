# Vade 交接文档（2026-11-13 快照）

> 用途：换环境继续开发。读完这一页就知道「做到哪、怎么跑、还欠什么」。

## 一、仓库与分支

| 项 | 值 |
|---|---|
| 主仓 | https://github.com/lshub2010/Vade （私有） |
| 基线分支 | `debug`（只接受 PR） |
| 开发分支 | `dev_gingshan`（当前 HEAD `35c4dde`，win-fix 合并后） |
| 模块仓 | https://github.com/lshub2010/vade-module-echo （私有，`main`） |
| 本快照分支 | `tmp/u1-ui`（`153f73a`，U1 骨架 WIP）、`tmp/c0c-miuix`（当前头 `589ec1d`；另有两轮未合入：`7062f15`、`aa8c808`） |

主仓结构：

```
core/         Python core（C1–C5a）
protocol/     Rust 协议核心（C1）+ v1 契约
schema/       C-03/04/05/06/08/11/13/15/18
sdk/python/   Python SDK（C4，C-07）
ui/           Compose MP（U1，WIP）
admin-ui/     前端（未开工）
docs/         需求/设计/23 份任务书/RFC
spike/        C0a/C0b 验证；C0c 在 `tmp/c0c-miuix`（`spike/miuix/`）
modules/      只有 README 索引（模块独立仓）
```

## 二、已完成（均有真实测试与独立审查）

| 波次 | 批次 | 内容 | 合并 |
|---|---|---|---|
| W0 | C0a | UniFFI spike：12 tests，握手 38.75ms | PR #1 |
| W0 | C0b | Rust→Wasm：8 PASS，wasm 207KB（gzip 95KB），Kotlin/Wasm 真跑通（`BRIDGE_ENTER` + `handshake_bytes:144`） | PR #1 |
| W1 | C1 | 协议核心：Noise IK/XXpsk3、20B 帧、AEAD、重放窗、33 错误码、KAT、Python 绑定；**58 tests** | PR #2 |
| W2 | C2 | transport/registry/crypto_gateway/identity/config；C-03/04/05/18；**84→后续** | PR #3 |
| W3 | C3 | 会话库、串行可中断队列、路由、Agent 循环；**150 tests** | PR #4 |
| W4 | C4 | Python SDK v0.1 + echo 模块（已迁出主仓）；SDK 52 + echo 19 | PR #5 |
| W4 | C3b | 零执行 skill 加载器 + C-11；**108 tests** | PR #6 |
| W4 | C3d | 事件推送 + C-13/C-14 + `manifest.subscriptions`（RFC-0001）；**328 tests** | PR #8 |
| W4 | C5a | 审批/审计/admin 后端 + C-08/C-17；**308 tests**（Argon2 拖慢） | PR #9 |
| — | — | 模块迁出主仓（独立仓约定） | PR #7 |
| — | win-fix | 2026-11-13 换机 Windows 11 x86_64：uv 建 Python 3.12 venv、重编协议原生库 `protocol/core/python/vade_protocol.dll`、两条 POSIX 权限位断言加 `os.name != "nt"` 守卫（Linux 上仍校验）；全量 `pytest core/tests -q` → **378 passed**；环境见 `docs/dev-windows.md` | `35c4dde`（merge，含 98617b8 / ccf79f0；R1/R2 pass，报告：`../Vade-REVIEW-win-fix.md`） |

当前全量：`pytest core/tests -q` → **378 passed**（Windows 11 开发机，2026-11-13 基线）。

## 三、未完成

| 项 | 状态 | 说明 |
|---|---|---|
| **U1** | WIP `tmp/u1-ui` | 只有 Gradle 骨架（`ui/settings.gradle.kts` 等，`153f73a`）；C-09/C-10 契约、界面、测试都未做 |
| **C0c** | Desktop/Wasm 已验（有保留），Android 待升 AGP | **Desktop 通过（有保留）**：无头 `ImageComposeScene` 渲染（`:composeApp:screenshot`）在 Windows 上重生 8 张 `miuix-desktop-*.png`——**非窗口截图**，8 张中 Dialog 那张只画出触发按钮、弹窗本体未渲染；`:composeApp:run` 真实窗口未跑。**Wasm 只验构建**：`:composeApp:wasmJsBrowserProductionWebpack` 通过，**浏览器运行时未跑**；**同界面替换口径**下 Miuix 版 `app.wasm` gzip **936,611 B**，比纯 Material 对照 **1,013,216 B** 小 **76,605 B**——三皮肤（material + miuix + glass）共存的边际增量需 U2 另测，不能引用此差值；`skiko.wasm` gzip 3,328,940 B 为两端公共。**Android 未通过**：Miuix 0.9.2～0.9.4 的 AAR 均要求 `minCompileSdk=37`，当前 AGP 8.8.2 无法使用 API 37（无裸 android-37 平台、`compileSdkVersion("android-37.0")` 被拒、目录联接被判 inconsistent），需升 AGP 8.13+/9.x + Gradle ≥8.14.4。数据与复现命令以 `spike/miuix/RESULT.md` 为准（`tmp/c0c-miuix`） |
| **U4（iOS）** | **暂缓（用户 2026-11-13 决定）** | v1 首批只做 Android / Desktop / Web 三端；G4「iOS 真机」不再作为当前门禁。代码与任务书保持现状，恢复需用户重新拍板 |
| **C5a 前端** | 未开工 | `admin-ui/` 只有占位 README（B-4） |
| **W5** | 未开工 | fs/shell/web/memory/mcp-bridge/model 六个模块（各自独立仓）+ C5b 拓扑画布 + U2/U3 皮肤 |
| **W6** | 未开工 | M4b 自进化、F1 发布（U4 见上，已暂缓） |

## 四、已知缺陷 / 技术债（审查遗留，按优先级）

1. **C-18 缺 `llm.model_module` / `llm.fallback_to_builtin`**（仍在）：schema `additionalProperties:false` 会拒，代码用默认值绕过；需增补契约或 RFC。
2. **`turn.completed.skills` 未接 —— 阻塞（需 RFC）**：C3b 已回填 `activated_skills`，但 C-06 `schema/events.json` 的 `turn.completed.payload`（149–162 行）**没有** `skills` 字段；C-06 已冻结，改契约须先批准 `docs/rfc/RFC-0002-turn-completed-skills.md`，实现待批准后再动。
3. **模块吞 `CancelledError` 时**：核心路径已修，`tool_call` 与 `turn_completed` 审计两处已收口（已合入 `dev_gingshan`）—— `LoopEngine._is_interrupted()` 是唯一判定，turn 流的所有写入都经 `_turn_emit()` 前置守卫（`turn.delta`、`turn.tool_call`、`turn.tool_result`、`turn.approval_resolved` 与 max_steps 通知，命中抛 `CancelledError`），正常出口在写 `turn_completed` 审计前也查一次、命中改记 `turn_interrupted`；已真实执行的工具在守卫之前落审计并标 `aborted: "interrupted"`；queue 按 turn 自己的 task 清掉 `_cancelled_turns`（含 interrupt 落在 task 创建前的窗口），`_finalized` 与 Router 的 `_cancel_fired` 也在 turn/调用结束后回收。测试见 `core/tests/session/test_queue.py` 的 `test_interrupt_emits_no_tool_result_when_the_module_answers_after_cancel`、`test_interrupt_drops_a_late_delta_from_a_provider_that_swallows_the_cancel`、`test_interrupt_before_the_tool_call_keeps_the_tool_from_running`、`test_an_interrupted_turn_is_never_audited_as_completed` 与 `test_cancel_clear_is_bound_to_its_own_turn_when_the_previous_task_is_an_orphan`。
4. **`admin-ui` 未交付**，但 C5a RESULT 已如实标注。
5. **scrypt 旧哈希迁移**（仍在）：已支持在线重哈希，但没做批量迁移脚本。
6. **C0c 的 wasm 构建卡在 npm 下载**：需配 `registry.npmmirror.com` + `kotlin.js.yarn=false` + `nodejs.download=false`；2026-11-13 Windows 上 wasm 生产构建已跑通，这些 npm 配置是否仍需保留**待确认**。
7. **模块仓 SDK 依赖未锁版本**：echo 测试目前复用主仓 venv。

## 五、环境与命令

> 当前开发机为 **Windows 11 x86_64（16C/16G，2026-11-13 换机）**。工具链版本与位置、代理开关、dll 重建、平台差异详见 **`docs/dev-windows.md`**。下面 Linux 段保留为历史口径，Windows 段是现在实际在跑的。

Linux（历史口径）：

```bash
# Python（3.12 venv，含 pytest/uvicorn/starlette/websockets/httpx/pyyaml/cryptography/argon2-cffi）
cd Vade && .venv/bin/python -m pytest core/tests -q

# Rust
cd protocol/core && cargo test            # 58 tests
CARGO_PROFILE_DEV_DEBUG=0 cargo build --features uniffi -j 1   # 5GB 内存必须 -j1

# 契约门禁
.venv/bin/python tools/check_contracts.py

# Gradle（UI / Kotlin）
/opt/gradle/gradle-8.10.2/bin/gradle :composeApp:compileKotlinDesktop
```

Windows 11（当前开发机）：

```bash
# 工具链在 D:/Hermes/tools：cargo 1.100（x86_64-pc-windows-gnu）+ gcc 16.2 + JDK 21 + Gradle 8.10.2 + Android SDK 36
source D:/Hermes/tools/env/vade-env.sh

# venv 由 uv 建，Python 3.12
D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe -m pytest core/tests -q   # 378 passed
D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe tools/check_contracts.py  # contracts: OK
D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe docs/check_docs.py        # 12 项全 PASS

# 协议原生库：Rust cdylib → protocol/core/python/vade_protocol.dll（已 gitignore，不入库）
bash D:/Hermes/tools/env/build-protocol-dll.sh
```

Windows 上 `python` 默认解析到 Hermes 自带解释器，务必用仓库 venv 的绝对路径。两条 POSIX 权限位断言（admin 文档 `0600`、`pending/` `0700`）被 `os.name != "nt"` 守卫跳过，Linux 上照常校验——**Windows 绿灯不等于这两项通过**。

## 六、开发循环（重要）

1. `vade-wt.sh new <批次>` 开独立 worktree + `tmp/<批次>` 分支
2. 子代理只在自己 worktree 改，提交到分支，**不 push**
3. 父代理复跑验收 → 派独立审查（只读 + 突变）→ `git merge --no-ff` 进 `dev_gingshan`
4. push `dev_gingshan` → 开 PR 到 `debug` → squash 合并 → `debug` 合回 `dev_gingshan`
5. 一个小功能一次合并，不攒批

约定细节见 `skill: vade-dev-loop`。

## 七、证据位置

- 审查报告：`../Vade-REVIEW-*.md`（C1/C2/C3/C4/C3b/C3d/C5a 等）
- 各批次结果：`protocol/v1/RESULT.md`、`core/vade/session/RESULT.md`、`core/vade/events/RESULT.md`、`core/vade/admin/RESULT.md`、`sdk/PYTHON_SDK.md`
- C0c：`spike/miuix/RESULT.md`（`tmp/c0c-miuix`，实测数据以该文件为准）
- Windows 环境：`docs/dev-windows.md`
- KAT：`protocol/v1/kat.json`（sha256 `e154100b…`）
