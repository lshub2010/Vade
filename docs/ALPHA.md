# Alpha 口径与验收清单

> 本文档定义「第一个 alpha 版本」的含义。**在此之前，仓库里没有 alpha 的定义**——里程碑表只有 G1–G5，全部标「未达成」，且 `release/`、`tests/e2e/` 不存在、`deploy/` 只有 README。
>
> 状态：**草案口径 · 台账已校准（2026-11-14）**。21 条验收句已按仓库实际状态逐条标注；口径本身仍待产品侧确认后转正。
>
> 证据路径约定：前缀 `vade-client:` 指**独立客户端仓** `D:/HermesWorkspace/Vade-client`（`ui/` 已于 2026-11-14 迁出本仓），其余路径相对本仓。标 `[x]` 表示验收句**本身**成立；「组件在、但没接进真链路」的一律保持 `[ ]` 并写现状。

## 一、为什么需要一个「小口径」alpha

G1–G5 是**发布级**目标（端到端 + SDK 冻结 / 审批审计闭环 / 三端聊天 / iOS 真机 / 抓包 0 泄漏）。按 G5 口径，alpha 必须等六个模块（fs/shell/web/memory/mcp-bridge/model）齐备、admin 前端完成、M4b 自进化完成——这是数周量级。

但在那之前有一个更高优先级的问题：**「架构到底对不对」还没被真实验证过**。草案时口径：客户端协议层 19/33 方法、UI 全部假数据；真模块只有一个 `echo`（4 个 trivial 工具，用途是证明管道）。截至 2026-11-14，协议层已是 **34/34** 方法有类型封装 + 契约覆盖率守卫测试（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/protocol/VadeClient.kt:110-145`），但 UI 数据层默认仍走 fixtures（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/App.kt:42` 默认 `FakeSessionSource`）——**协议齐了，窗口里的内容还没换**。真模块仍只有 `echo`，且在独立仓 `Vade-module-echo`，本仓 `modules/` 只有 README。在这种情况下继续堆模块，是在未验证的地基上加层。

所以先做**口径 A**：让整条链路真跑起来，用最小成本证伪架构错误。

## 二、三个口径

| 口径 | 含义 | 估时 | 何时选它 |
|---|---|---|---|
| **A 桌面 alpha** | 本机双击可用：真连接、真聊天、真流式、echo 模块可调、能打包分发 | 3–5 天 | **默认**。先验证架构 |
| **B 可交付 alpha** | A + `fs` 与 `model` 两个真模块 + APK 真机验证 + Web 可写安全评审 | 1.5–2.5 周 | 要给外部试用者 |
| **C G1–G5 全绿** | 六模块齐 + admin 闭环 + 抓包 0 泄漏 | 3–5 周 | 正式发布前 |

**估算不是承诺**，表内估时是 2026-11-13～14 的一次性估算（**当时口径**），不外推到剩余工作。依据是 2026-11-13～14 的实测速度——当时：服务端 378 → 1041 测试、客户端 21 → 154 测试、单棒 28–69 分钟、12 路并行；**现为**服务端 1089 passed、客户端 588 tests（见 §三 A6）。若回执质量不达一半，估时要 +50%。

## 三、口径 A 的验收条件（逐条可验）

**校准汇总（2026-11-14）：21 条中 8 条已达成、13 条部分达成、0 条未做。**

### A1 连接与凭证
- [x] 客户端能在本机连上 core（`ui/attach` 成功，拿到 32-hex session id + 快照） — 证据：`vade-client: shots/w1-proto/smoke.md`（真 core 回 `sessionId=8488050c39f649338930012fbbeaf276`、`fromSeq=0`、快照全字段解码）；封装见 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/protocol/VadeClient.kt:509`。注：本条验的是协议层，窗口内仍是 fixtures（见 A2）。
- [ ] 首次运行有配对流程（一次性配对码或等价机制），**不把 token 明文落日志** — 现状：机制齐——服务端 `core/vade/identity/pairing.py:152` 的 `PairingStore.create` 铸一次性码，客户端 `POST /ui/session` 换 `__Host-ac_ui` cookie、token 不进日志（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/protocol/VadeTransport.kt:58-120`）；缺：三端都没有**输入配对码的首次运行界面**（`BrowserPairing` 只能由代码注入），双击产物连不上 core。
- [ ] 断线后自动重连（指数退避，有上限），界面上有明确连接状态 — 现状：指数退避 500 ms → 30 s 上限 + 自动 re-attach 已实现且有测试（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/protocol/VadeClient.kt:96`、`vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/RealSessionState.kt:28`）；缺：界面无连接状态（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/shell/TopBar.kt` 未绑 `RealSessionState`），且桌面默认 `FakeSessionSource`，连接根本不会发生。

### A2 真聊天
- [ ] 发送消息走 `ui/submit`，返回 `turn_id` 与 `queue_pos` — 现状：类型化封装 + 真 core 冒烟已证（`SubmitResult(turnId=7b2a7edf733e46b9be13ec8db32d9000, queuePos=1)`，`vade-client: shots/w1-proto/smoke.md`）；缺：`ChatState.submit()` 只改本地 `items`、不调用 source（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/ChatState.kt:450`），窗口里的「发送」没有出网。
- [ ] 流式增量走 `turn.delta`，界面逐字上屏（不是等完成再整段替换） — 现状：`LiveTranscript` 按 `(turn_id, part_index)` 累加（StringBuilder 追加）+ `ChatState.applyLive` 钩子 + 23 条测试（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/LiveTranscript.kt`、`vade-client: shots/g-chatlive/RESULT.md`）；缺：`applyLive` 在 `composeApp/src` 内只有定义与测试调用，聊天页未接事件流。
- [ ] `turn.completed` 后消息落库，重启客户端仍可见（走 snapshot 恢复） — 现状：服务端落库 + attach 快照带 `messages[]`；客户端 `SnapshotMessages.kt` 快照→`ChatMessage` 映射有测试；缺：聊天页不读 `state.messages`（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/destinations/SessionDetailScreen.kt:94` 仍 `ChatState(session.id, preset)` 自带 fixtures），重启可见无从验证。
- [ ] 中断（`ui/interrupt`）后已生成文本仍可见（RFC-0013 的 partial 语义） — 现状：`LiveTranscript` 对 `turn.interrupted` 保留 partial、`partial_chars` 优先取 wire 值（缺省按码点计），快照 `partial` 字段有解析；缺：同上未接进 `ChatState`，UI 无真中断可验。
- [ ] 错误有可读提示（34 个错误码至少按四类呈现：可重试 / 需重连 / 需用户操作 / 致命） — 现状：34 码 → 四类映射表 + 穷举测试（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/VadeErrorCategory.kt`、`vade-client: composeApp/src/desktopTest/kotlin/vade/ui/data/VadeErrorMappingTest.kt:18`），模型/模块页已按类别出中文文案（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/model/ModelFailure.kt:127`）；缺：聊天回合的 `turn.failed` 未经真事件流上屏（仍停在 `LiveTranscript.failureOf` 侧信道）。

### A3 模块可用
- [ ] `echo` 模块在线时，模型能调用它的四个工具，工具卡在会话内正确渲染 — 现状：工具卡渲染、模块页宿主、真 `RealModulePageSource`（`ui/page.get` / `ui/state.get`）均已合并并有测试（`vade-client: shots/g-module/RESULT.md`、`vade-client: shots/g-modulewire/RESULT.md`）；缺：**模块清单拉不到**——`ui_to_core` 34 个方法里没有「列模块」，`module.status` 只是事件、attach 快照不带模块表（`schema/rpc.json`），UI 无从知道有哪些模块，工具卡只能由 fixtures 驱动。
- [ ] 模块离线时给出明确提示（不是静默失败） — 现状：模块页有离线 / 状态不可用 / 需要会话三种横幅（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/module/ModulePageRenderer.kt:333`），`module.status` 已解码；缺：离线状态无真来源（同 A3-1 的模块清单/状态缺失）。
- [ ] 审批（`approval/decide`）以内联卡片出现在会话里，**不弹独立窗口** — 现状：会话内内联 `ApprovalCardRow`（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/ChatThread.kt:516`），不弹独立窗口；协议层有 `approval/decide` 封装；缺：UI 从未调用 `approval/decide`（`commonMain` 仅协议层出现该名字），审批结果仍由 fixtures 决定。

### A4 会话管理
- [ ] 会话列表、新建、重命名、置顶、归档、删除可用 — 现状：列表读真源已接（`UiState.groups()` 走 `SessionsLoader`，`vade-client: composeApp/src/commonMain/kotlin/vade/ui/shell/UiState.kt:161`、loader 见同文件 `:88`），九个元数据动作有真适配器 `MetaActionsReal`（`session/rename|pin|archive|delete`，40 条测试，`vade-client: shots/w-adapt/RESULT.md`）；缺：注入行——`App()` 没有 metaActions 参数（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/App.kt:42` 只有 `source`），动作仍打在 fixtures 上。
- [ ] 分组创建 / 改图标 / 改颜色 / 删除可用（删组不删会话） — 现状：UI 完整 + `session/group.list|create|update|delete|assign` 五个 RPC 适配器均在（服务端 `unassigned` 只读不写）；缺：同上未接线。

### A5 可分发
- [x] 桌面端有明确的分发产物（安装包或免安装目录 + 启动脚本） — 证据：MSI `composeApp/build/compose/binaries/main/msi/vade-ui-0.1.0.msi` **93,312,713 B**（`vade-client:` 本机实存，未签名）+ 免安装 app-image **154.7 MB**（`vade-client: shots/g-clean/RESULT.md` §1）+ APK **22,789,790 B**（`composeApp/build/outputs/apk/debug/`）；启动脚本 `vade-client: deploy/desktop/run.bat`、`run.sh`。
- [x] 版本号单一来源，界面「关于」页可见 — 证据：唯一字面量 `vade-client: gradle.properties:33`（`vadeVersion=0.1.0-alpha.1`）→ 生成 `VadeBuildConfig` → `AboutPage`（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/destinations/ProfileScreen.kt:248`）；7 条测试含 `runComposeUiTest` 真渲染（`vade-client: shots/g-pack/RESULT.md` §3–4）。
- [x] 干净机器上能启动（不依赖本机 `D:/Hermes/tools` 里的任何路径） — 证据：app-image 241 个文件 + 95 个 jar 的 23,721 个 zip 条目字节级扫描，`D:/Hermes`、`C:/Users`、`D:/Android`、`GRADLE_USER_HOME` 等致命路径命中 **0**，剥离环境变量后启动成功（`vade-client: shots/g-clean/RESULT.md` §2、`vade-client: shots/g-clean/scan-report.txt`、`vade-client: shots/g-clean/isolated-launch-window.png`）。口径：**环境隔离**；真·第二台无 JDK 机器未验（见 §七 N6）。

### A6 质量门
- [x] 服务端全量测试全绿（当前基线 1089）、契约检查 OK、文档门禁 12/12 — 证据：`docs/rfc/LEDGER.md` §1（`1089 passed, 6 warnings in 65.79s`）；本次校准复跑 `tools/check_contracts.py` → `contracts: OK`、`docs/check_docs.py` → `合计 12 项，失败 0 项`、`tools/check_ledger.py` → `ledger: OK (14 RFC rows … appends 10+8+3)`。
- [x] 客户端全量测试全绿（当前基线 588）、三端编译通过 — 证据：`gradle :composeApp:desktopTest --rerun` 逐 XML 求和 **588 tests / 0 failures**（校准时的父代实跑口径，本次未复跑）+ `assembleDebug`、`compileKotlinWasmJs` 三目标绿；`vade-client: docs/QUICKSTART.md` §4 记的 368 是旧数，勿引用。
- [ ] `gitleaks` 与私钥 grep 0 命中 — 现状：`vade-client: shots/g-gitleaks/raw/` 已有 4 组原始日志——main / client 的 **history 扫描 `no leaks found`**；**workdir 扫描 2 命中**，均在 `.venv/Lib/site-packages/cryptography/hazmat/bindings/_rust/openssl/hpke.pyi`（vendor 文件，`generic-api-key` 误报）；缺：排除规则定稿 + 私钥 grep 归档。**该项未定稿，不得计为通过。**
- [x] 桌面真机截图 ≥3 张（亮/暗、会话/能力/活动） — 证据：`vade-client: shots/w2-final2/light.png` 与 `vade-client: shots/w2-final2/dark.png`（亮/暗两帧 md5 不同，暗色修复后的真图）、`vade-client: shots/w2-bottombar-fix/phone-activity.png`（活动）、`vade-client: shots/w2-bottombar-fix/phone-capabilities.png`（能力）。
- [x] **Android 运行时验收**：在 API 37 模拟器（手势导航，验 edge-to-edge 安全区）与 API 30 真机（验 miuix-blur 降级不崩）上各装一次，启动无崩溃、底栏不被系统手势条压住，截图存档 — 证据：`vade-client: shots/c-inset/RESULT.md`（API 37 `api37-fixed.png` / `api37-floating.png` / `api37-floating-blur.png`、API 30 `api30-fixed.png` / `api30-floating.png`；两台 `logcat -d -b crash` 中 `vade.ui` 命中 0 条）。

## 四、已知硬阻塞（不是时间问题）

| 阻塞 | 影响 | 解法 |
|---|---|---|
| ~~本机无 Android 设备/模拟器~~ **已解除**（2026-11-14） | 现已可做真机验证 | 模拟器 `emulator-5554`（API 37 / x86_64 / 手势导航）+ 真机 `192.168.1.180:5555`（API 30 / arm64 / 三键导航）；截图走 `adb exec-out screencap -p`，不经过 Windows 窗口 |
| **`deploy/` 只有 README**（本仓） | 无发布目录、无签名、无更新通道 | 客户端仓已有 `deploy/desktop/`（`run.bat`/`run.sh`），产物也能打出 MSI/app-image；签名与更新通道仍未做（§七 N1、N3），本仓 `deploy/` 仍只有 README |
| **真模块只有 echo**（且在独立仓 `Vade-module-echo`，本仓 `modules/` 只有 README） | alpha 能演示但无实用价值 | 口径 B 做 fs/model |
| **`admin-ui/` 只有占位** | 管理面不可用（不影响聊天主链路） | 口径 B/C |
| **Web 可写（D100）未评审** | Web 端不能开放写操作 | 口径 B 的合并门槛 |

## 五、波次计划（口径 A）

| 波 | 内容 | 状态（2026-11-14 校准） |
|---|---|---|
| W1 | 协议补齐 + 真数据层 + 元数据/输入/用量/子代理 UI + 暗色 + 工具修复 | **已完成**：协议 34/34 有类型封装 + 覆盖率守卫；数据层与各面板均有测试 |
| W2 | 数据层接线到 UI（会话列表 / 聊天流 / 状态机）、模块页入口、搜索接真接口 | **部分完成**：会话列表 / 搜索 / `UiState` 读源已接；**聊天流与动作适配器未接**（§七 B2、B3） |
| W3 | 打包与分发（桌面产物、版本号、关于页、启动脚本）、首次运行配对引导 | **部分完成**：产物 / 版本号 / 关于页 / 启动脚本已交付；**配对引导未做**（§七 B5） |
| W4 | 端到端联调：本机 core + 桌面客户端真跑，逐个修阻塞项 | **未开始**（依赖 W2 收口） |

**W4 是唯一无法并行、也无法预估的一波**——它就是把假数据换成真数据之后暴露出来的所有问题。

## 六、不做（口径 A 明确排除）

- iOS（用户 2026-11-13 决定暂缓）
- 六个模块里除 `echo` 外的全部
- admin-ui 前端
- M4b 自进化
- 抓包 0 泄漏的正式验收（口径 C 才做）

## 七、当前差距清单（自动校准于 2026-11-14）

口径：**阻断项**做完才能把 §三 的 21 条全部勾上、才配叫 alpha；**非阻断项**可在 alpha 说明里列为已知缺陷后发布。**5 项阻断、8 项非阻断。**

| # | 阻断项（必须做完才能叫 alpha） | # | 非阻断项（可带缺陷发布） |
|---|---|---|---|
| B1 | **A3 模块清单**：`ui_to_core` 34 个方法里没有「列模块」，`module.status` 只是事件、attach 快照不含模块表 → 能力页 / 模块页入口 / echo 工具卡无来源。需新增契约（`ui/module.list` 或快照 `modules` 块） | N1 | **代码签名**：jpackage 产物 `.jpackage.xml` 记 `signed=false`，MSI/EXE 未签名，SmartScreen/杀软告警 |
| B2 | **聊天真接线**：`ChatState` 未调 `LiveTranscript.applyLive`、`submit()` 不调 source，聊天页仍 `ChatFixtures` → 流式 / 落库恢复 / 中断 partial 全部停在投影器与测试层 | N2 | **Web 字体**：wasm 首帧中文豆腐块，字体就绪后重绘（`vade-client: shots/g-wasm/RESULT.md` §3、§6） |
| B3 | **适配器接线**：`MetaActionsReal` / `TurnGatewayReal` / `BlobUploaderReal` / `ModelActionsReal` 已合并，但 `App()` / `UiState` 无注入点（`App.kt:42` 只有 `source`），改名 / 置顶 / 分组 / 模型切换 / 附件上传仍打在 fixtures 上（`g-adapter-bundle` 在做） | N3 | **无发布通道**：产物在 `build/` 下不进库，无 artifact 上传、无更新检查；alpha 只能打包机重建或手动拷贝 |
| B4 | **gitleaks 定稿**：history 扫描 0 泄漏已出，workdir 扫描 2 命中（`.venv` 内 vendor `.pyi` 误报）；缺排除规则 + 私钥 grep 归档，**当前不可计为通过** | N4 | **ask_user 卡片未合并**：实现完成（37 条测试）在 `tmp/c-askuser`，未进 `main` |
| B5 | **配对引导（A1-2）**：一次性码与 `POST /ui/session` 机制齐，但三端没有输入配对码的首次运行界面，双击产物连不上 core | N5 | **关于页「E2E 设备标识」仍是占位**（`vade-client: shots/g-pack/RESULT.md` §5.5） |
| — | — | N6 | **真·干净机器未验**：A5-3 是环境隔离口径（剥离 PATH/JDK/SDK 变量），未在无 JDK 的第二台机器双击 MSI / app-image |
| — | — | N7 | **`ui/blob.token` RPC 不存在**：附件上传按实读协议走 HTTP `POST /ui/blob` 三步 + `DELETE` 取消，与设计稿的方法名不一致（有真适配器，契约缺条目） |
| — | — | N8 | **Android 验收遗留项**：w-verify 报告记 P0=0 / P1=4 / P2=4（该报告在 worktree，未合并），无崩溃级问题 |

**校准依据（本次复跑）**：`docs/check_docs.py` → 12 项 / 失败 0；`tools/check_contracts.py` → `contracts: OK`；`tools/check_ledger.py` → `ledger: OK (14 RFC rows; rpc 1.5.0 47 methods, events 2.0.0 34 entries, 34 error codes, appends 10+8+3)`。
