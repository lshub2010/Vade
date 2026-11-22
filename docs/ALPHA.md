# Alpha 口径与验收清单

> 本文档定义「第一个 alpha 版本」的含义。**在此之前，仓库里没有 alpha 的定义**——里程碑表只有 G1–G5，全部标「未达成」，且 `release/`、`tests/e2e/` 不存在、`deploy/` 只有 README。
>
> 状态：**草案口径 · 台账已校准（2026-11-15，tmp/s-alpha-truth2 棒）**。21 条验收句已按仓库实际状态逐条标注；本轮校准的口径：每个 `[x]` 必须同时满足 **①实现在仓库里可指认（文件+符号）②有可重跑的验证（测试名/命令/证据路径）③本轮亲自复跑过**，三项缺一即保持 `[ ]` 并写明差什么。§七 阻断表已清零（**0 阻断 / 7 非阻断**），新增 §八「alpha 已知缺陷清单」。全部原始输出与逐条判定表见 `shots/s-alpha-truth2/RESULT.md`。口径本身仍待产品侧确认后转正。
>
> 证据路径约定：前缀 `vade-client:` 指**独立客户端仓** `D:/HermesWorkspace/Vade-client`（`ui/` 已于 2026-11-14 迁出本仓），其余路径相对本仓。标 `[x]` 表示验收句**本身**成立；「组件在、但没接进真链路」的一律保持 `[ ]` 并写现状。

## 一、为什么需要一个「小口径」alpha

G1–G5 是**发布级**目标（端到端 + SDK 冻结 / 审批审计闭环 / 三端聊天 / iOS 真机 / 抓包 0 泄漏）。按 G5 口径，alpha 必须等六个模块（fs/shell/web/memory/mcp-bridge/model）齐备、admin 前端完成、M4b 自进化完成——这是数周量级。

但在那之前有一个更高优先级的问题：**「架构到底对不对」还没被真实验证过**。草案时口径：客户端协议层 19/33 方法、UI 全部假数据；真模块只有一个 `echo`（4 个 trivial 工具，用途是证明管道）。截至 2026-11-15，协议层已是 **35/35** 方法有类型封装（RFC-0015 的 `ui/modules` 已登记，rpc 现为 1.7.0 / 49 方法，客户端 codec 同步补齐）+ 契约覆盖率守卫测试（`vade-client: composeApp/src/desktopTest/kotlin/vade/ui/protocol/VadeContractCoverageGuardTest.kt`，对运行时读到的契约文件逐方法比对 `VadeMethod` 枚举，客户端 `composeApp/src/commonMain/kotlin/vade/ui/protocol/VadeClient.kt`），UI 数据层**默认**仍走 fixtures（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/App.kt` 的 `source` 形参默认 `FakeSessionSource()`），但**这是刻意的**：真模式永远是显式选择（桌面 `VADE_SOURCE=real` / Android intent extra / Wasm `?source=real`）。2026-11-15 起真源已接线并有端到端证据（`RealCoreUiE2ETest`：起真 core，断言界面出现 core 铸造的会话标题、且假数据专有字符串一个都不在；模块页面另有真 core + 真 echo 模块的端到端证据 `RealCoreModulePageE2ETest`，见 `vade-client: shots/g-module-page/RESULT.md`）。**聊天写侧（发送 / 中断 / 审批决策）也已全部真出网**（§三 A2、A3-3；§七 B1/B6 已解除）。真模块仍只有 `echo`，且在独立仓 `Vade-module-echo`，本仓 `modules/` 只有 README。在这种情况下继续堆模块，是在未验证的地基上加层。

所以先做**口径 A**：让整条链路真跑起来，用最小成本证伪架构错误。

## 二、三个口径

| 口径 | 含义 | 估时 | 何时选它 |
|---|---|---|---|
| **A 桌面 alpha** | 本机双击可用：真连接、真聊天、真流式、echo 模块可调、能打包分发 | 3–5 天 | **默认**。先验证架构 |
| **B 可交付 alpha** | A + `fs` 与 `model` 两个真模块 + APK 真机验证 + Web 可写安全评审 | 1.5–2.5 周 | 要给外部试用者 |
| **C G1–G5 全绿** | 六模块齐 + admin 闭环 + 抓包 0 泄漏 | 3–5 周 | 正式发布前 |

**估算不是承诺**，表内估时是 2026-11-13～14 的一次性估算（**当时口径**），不外推到剩余工作。依据是 2026-11-13～14 的实测速度——当时：服务端 378 → 1041 测试、客户端 21 → 154 测试、单棒 28–69 分钟、12 路并行；**现为**服务端 1557 passed（2026-11-22 f15 复跑）、客户端 1442 tests（见 §三 A6，tmp/s-alpha-truth2 本轮实测）。若回执质量不达一半，估时要 +50%。

## 三、口径 A 的验收条件（逐条可验）

**校准汇总（2026-11-15，tmp/s-alpha-truth2 复核）：21 条中 18 条已达成、3 条部分达成、0 条未做。**（三档相加 = 21；4 条未达成项逐条写明缺口。）17 个 `[x]` 的验证本轮**全部亲自复跑**：服务端 pytest 全量、客户端 desktopTest 全量 `--rerun`、两仓 gitleaks 工作区扫描、门禁三件套；§三 证据路径逐条 `test -e` 在盘（原数见文末校准依据）。

### A1 连接与凭证
- [x] 客户端能在本机连上 core（`ui/attach` 成功，拿到 32-hex session id + 快照） — 证据：`vade-client: shots/w1-proto/smoke.md`（真 core 回 `sessionId=8488050c39f649338930012fbbeaf276`、`fromSeq=0`、快照全字段解码）；封装见 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/protocol/VadeClient.kt`（`attachInternal`）。可重跑验证：`RealCoreUiE2ETest.realCoreSessionTitlesRenderInAppAndFixturesStayAway`（起真 core、真 attach、断言 seeded 会话标题上屏 + fixtures 串零出现），本轮 desktopTest 全量亲跑全绿。注：本条验的是协议层；窗口默认 fixtures 是刻意设计，真模式显式开启。
- [x] 首次运行有配对流程（一次性配对码或等价机制），**不把 token 明文落日志** — 实现：三端共用的首次运行引导卡 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/destinations/PairingCard.kt`（一次性码输入 + 「配对」按钮 + 「码在哪里找」，真源且缺码时出现在会话列表顶部）；提交走现有连接链（`SourceConfig.parse` → `BrowserPairing` → `KtorTransportFactory.connect()`，恰好一次 `POST /ui/session` + 一次 WS 升级），运行期桥 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/SourceConfig.kt` 的 `needsFirstRunPairing` / `PairingRuntime`；服务端一次性码铸自 `core/vade/identity/pairing.py` 的 `PairingStore.create`。**码不落盘不进日志**：扫描守卫 `PairingFirstRunTest.enteredPairCodeHasNoPersistenceSink` 对 `SourceConfig.kt` + `PairingCard.kt` 剥注释断言无持久化/日志/URL sink。验证：`vade-client: composeApp/src/desktopTest/kotlin/vade/ui/data/PairingFirstRunTest.kt`（10 条）+ `vade-client: composeApp/src/desktopTest/kotlin/vade/ui/destinations/PairingCardUiTest.kt`（6 条，真组合出现/不出现/三失败文案），本轮 desktopTest 全量亲跑全绿；真核截图证据 `vade-client: shots/g-pair-first-run/`（5 图）。已知边界（wasm 配对不可用、Android extras 键名文档漂移）见 §八 D8/D9。
- [ ] 断线后自动重连（指数退避，有上限），界面上有明确连接状态 — 现状：指数退避 500 ms → 30 s 上限 + 自动 re-attach 已实现且有测试（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/protocol/VadeClient.kt` 的 `backoffDelayMs`、`vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/RealSessionState.kt` 的 `Reconnecting`）；列表首读三态横幅在（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/destinations/ListStatusBanner.kt` 的 `ListStatus`：假数据 / 正在连接 core（带真实 endpoint）/ 失败（带分类、传输原话与可照抄的启动命令），Connected 刻意不画）。桌面默认 `FakeSessionSource` 是刻意设计，真模式靠 `VADE_SOURCE=real` 显式开启，`RealCoreUiE2ETest` 已用真 core 端到端验证过（本轮亲跑）。**缺：会话中段掉线/重连没有任何界面呈现**——顶栏不绑 `RealSessionState`（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/shell/TopBar.kt` grep 零命中），横幅只反映列表首读；用户在聊天中途只会看到事件停止推进。— 差：顶栏/会话中段的连接状态指示（重连机制本身已实现且经测试）。

### A2 真聊天
- [x] 发送消息走 `ui/submit`，返回 `turn_id` 与 `queue_pos` — 实现：composer → `vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/ChatState.kt` 的 `submit()` → `submitOverWire` 回包 `SubmitReceipt(turnId, queuePos)` 落状态，排队序号只认 core 的 `queue_pos`（事件与回包谁先到都不撒谎；封装链：`vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/ChatTurns.kt` 的 `ChatTurnGateway.submit` → `vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/TurnGatewayReal.kt` 的 `submit`，`client.uiSubmit`）。验证：`ChatSendWireTest.realSubmitSendsExactlyOneUiSubmitWithTheBubblesClientMsgId`（恰一次 `ui/submit`、session_id/parts/client_msg_id 逐字对上）+ `TurnGatewayRealTest` 的帧级断言 + `fakeSourceSubmitAndStopNeverTouchTheGateway`（假源出网 0 次回归钉）；真核冒烟 `vade-client: shots/w1-proto/smoke.md`（`SubmitResult(turnId=…, queuePos=1)`）。本轮 desktopTest 全量亲跑全绿。
- [x] 流式增量走 `turn.delta`，界面逐字上屏（不是等完成再整段替换） — 实现：读侧（`g-chat-real` 已合）+ 触发端（B1 已合）都通了：`vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/ChatThread.kt` 先 `collectLiveEvents()` 再 `loadHistory()`；`turn.delta` 经 `ChatState.applyLiveEvent` → `vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/LiveTranscript.kt` 的 `apply` 按 `(turn_id, part_index)` StringBuilder 追加、行进 Running；TEXT 之外的 delta kind 不入正文；假源一条预设都不渲染、失败不回退 fixtures。验证：`LiveTranscriptTest.deltasReplayTheRealE2eWireSentenceAndUsage` / `twoDeltasOnOnePartAccumulateInsteadOfOverwriting` / `outOfOrderDeltasBufferUntilTheGapFills` / `replayingTheSameSeqDoesNotAppend` / `coalescedGapJumpsForwardAsSelfContained`（乱序/重放/缝隙全钉），本轮全量亲跑全绿。
- [ ] `turn.completed` 后消息落库，重启客户端仍可见（走 snapshot 恢复） — 现状：服务端落库 + attach 快照带 `messages[]`；客户端读侧已接——真源时 `SessionDetailScreen` 经 `resolveChatState`（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/destinations/SessionDetailScreen.kt`）拿到真 turns/uploader，`ChatState.loadHistory()` 拉 `channel.history()`（attach 快照行）种子进 `items`；`RealCoreUiE2ETest` 已断言真 core 快照行上屏、假数据 id/字符串零出现（本轮亲跑）；发送侧与落库半环均已通（A2-1）。— 差：「重启客户端→重连→快照恢复」的显式端到端未做（两半各有证据，闭环没人走过）。
- [x] 中断（`ui/interrupt`）后已生成文本仍可见（RFC-0013 的 partial 语义） — 实现：`ChatState.stop()` 真源分支（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/ChatState.kt` 的 `stop()` → `interruptOverWire` → `vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/TurnGatewayReal.kt` 的 `interrupt`，`client.uiInterrupt`，目标 = `turn.started` 记下的 runningTurnId）；重复 ■ 在途去重不重发；`vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/LiveTranscript.kt` 的 `onInterrupted` 保留 partial、`partial_chars` 优先取 wire 值（缺省按码点计）；`turn.interrupted` 事件负责收尾上屏，本地不编 stop 语义。验证：`ChatSendWireTest.realStopSendsExactlyOneInterruptAndDedupesDoubleClicks`（恰一次 `ui/interrupt`）+ `interruptGatewayErrorBecomesVisibleState`（中断失败可见化、不假装已停止）+ `LiveTranscriptTest.interruptKeepsPartialTextAndCountsCodePoints` / `interruptPrefersTheWirePartialChars`。本轮 desktopTest 全量亲跑全绿。
- [x] 错误有可读提示（34 个错误码至少按四类呈现：可重试 / 需重连 / 需用户操作 / 致命） — 实现：34 码 → 四类冻结映射 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/VadeErrorCategory.kt`（`categoryOf`，冻结表外新码兜底「可重试」）；`turn.failed` 经真事件流把行染 error 色 + `ChatState.makeFailureReadable` 把原始 C-04 码徽标换成「回合失败（<四类>）：<core 原话>」；submit/interrupt 失败气泡就地 Failed + 四类徽标；失败行可重试（`turn/regenerate`，旧行标旧版本）。验证：`vade-client: composeApp/src/desktopTest/kotlin/vade/ui/data/VadeErrorMappingTest.kt`（`assertEquals(34, …)` 穷举）+ `ChatSendWireTest.turnFailedShowsReadableCopyAndRetryGoesFromOneToTwoCalls` / `turnFailedMapsTheOtherErrorBucketsAndTheUnknownCodeFallback` / `submitGatewayErrorBecomesVisibleStateOnTheBubble`。本轮全量亲跑全绿。

### A3 模块可用
- [ ] `echo` 模块在线时，模型能调用它的四个工具，工具卡在会话内正确渲染 — 现状：**工具卡渲染这一半已闭合**（2026-11-15，`a3-tool-cards` 合入客户端 `a3668f7`）：`turn.tool_call` 按**事件到达顺序**建卡（插在同回合最后一条之后，不是全塞尾部）、`turn.tool_result` 按 `call_id` 就地更新同一条、同 `call_id` 重放幂等不建第二张、FULL args 永不上卡；审批卡也从时间线尾部**归位**到对应工具卡旁（契约的 `turn.approval_required` 不带 `call_id`，按 `turn_id`+工具名+未绑定+Running 取最早一张，映射不到才降级尾部）。验证：`vade-client: composeApp/src/desktopTest/kotlin/vade/ui/chat/ToolCardProjectionTest.kt` 12 例（建卡/到达序/就地更新/重放幂等/双请求 FIFO 绑定/尾部降级/未知 result 留痕/重建不丢/假源零动）；模块页面真 core 端到端证据见 `RealCoreModulePageE2ETest` + `shots/g-module-page/RESULT.md`。— 差：**「模型真能调用四个工具」的模型侧链路仍无人跑过**（真 model 模块驱动 tool-call 的 end-to-end），故留 `[ ]`
- [x] 模块离线时给出明确提示（不是静默失败） — 实现：在线态唯一来源 = `ui/modules` 回包——`vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/VadeAdapters.kt` 把 `moduleRegistry` 注入 `RealModulePageSource(client, moduleRegistry)`，其 `status()` 读 `registry.status(moduleId)`（registry 缺席才 fail-closed false，不再恒假）；离线 / 状态不可用 / 需要会话三种横幅在 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/module/ModulePageRenderer.kt` 的 `BannerRow`。验证：`ModulePageStatusBannerTest.statusComesFromTheRegistryNotFromAConstant` / `anOfflineModuleStillDrawsTheOfflineBanner` / `anOnlineModuleRendersThePageWithoutTheOfflineBanner` + 真核 e2e `RealCoreModulePageE2ETest`（在线时「模块离线」「页面不可用」「页面无法渲染」全树零出现）。本轮 desktopTest 全量亲跑全绿。
- [x] 审批（`approval/decide`）以内联卡片出现在会话里，**不弹独立窗口** — 实现：会话内内联 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/chat/ChatThread.kt` 的 `ApprovalCardRow`；`turn.approval_required` 事件建卡（`ChatState.onApprovalRequired`，卡 id = `approval:<request_id>` 确定性幂等）；点击 → `ChatState.decideApproval` → `ChatTurnGateway.decideApproval` → `vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/RealSessionSource.kt` 的 `decideApproval`（全仓唯一 `client.approvalDecide` 出网点），**`request_id` 取服务端事件（不是本地 callId）**；乐观更新先于往返、失败回滚 + 四类错误文案、`turn.approval_resolved` 以服务端为准；无 request_id 的夹具卡只做本地投影（`fixtureCardClickProjectsLocallyAndNeverTouchesTheGateway` 钉住出网 0 次）。验证：`ApprovalWireTest`（20 条，判据 `approveClickSendsExactlyOneApprovalDecideWithVerbatimParams`：恰一次、params 逐字 `{"request_id","decision":"approve","scope":"once"}`）+ `ApprovalDecideWireTest`（8 条帧级）+ 变异自验（删出网行 8 条红、scope 改 SESSION 判据红，见 `vade-client: shots/b6-approval/RESULT.md`）。本轮 desktopTest 全量亲跑全绿。已知缺陷（卡插在时间线尾部而非事件原位）见 §八 D2。

### A4 会话管理
- [x] 会话列表、新建、重命名、置顶、归档、删除可用 — 现状：列表读真源已接（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/shell/UiState.kt` 的 `groups()` 走 `SessionsLoader`）；分组行显示真组名已接（组合根 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/shell/AppShell.kt` 的 `metaGroupsOf` 把 `session/group.list` 同时喂宽/窄两处 `SessionsScreen`）；重命名/置顶/归档/删除/移动分组：行长按 → `vade-client: composeApp/src/commonMain/kotlin/vade/ui/meta/SessionMetaOverlay.kt` 的 `SessionMetaOverlayHost`（AppShell 三处挂载：双栏/窄屏/抽屉）→ `adapters.meta(sessionId)` → `vade-client: composeApp/src/commonMain/kotlin/vade/ui/data/MetaActionsReal.kt`（`session/rename|pin|archive|delete`；乐观投影、失败整表回滚、删除墓碑防复活）。验证：`MetaActionsRealTest`（14 条）+ `SessionMetaOverlayTest`（9 条）+ `SessionMetaLongPressUiTest`（4 条）+ `SessionMenuTest` / `MetaWireTest`，本轮全量亲跑全绿。**缺：「新建会话」仍只建本地草稿**——`vade-client: composeApp/src/commonMain/kotlin/vade/ui/shell/UiState.kt` 的 `newSession()` 不出网（KDoc 自述 draft only），真源下没有会话创建出口。— 差：新建会话出网（§八 D4）。 　**新建已接（2026-11-15，`a4-new-session`，客户端 `a3668f7`）**：`UiState.newSession()` 真源分支 → `SessionSource.createSession()` → `ui/attach` 传 `session_id: null`（core `manager.attach()` 走 `create_session()` 并向其他设备广播 `session_list_changed(added)`），拿回 id 才切会话、随后 `ui/submit` 带的就是这个 id——修掉了 `_require_session` 对草稿假 id 回 `NOT_FOUND` 的病。验证：`NewSessionWiringTest` 6 + `RealSessionCreateTest` 4（帧级 `{"session_id":null}`、双击只建一个、失败不伪造会话且可重试、变异 4/4 被杀）；合并后门禁 **154 类 / 1467 tests / 0 失败**，连跑 2 次绿。出处 `vade-client: shots/a4-new-session/RESULT.md`
- [x] 分组创建 / 改图标 / 改颜色 / 删除可用（删组不删会话） — 实现：组编辑器（名称 / 39 图标 / 10 色盘 / 删组不删会话）在 `vade-client: composeApp/src/commonMain/kotlin/vade/ui/meta/SessionMetaOverlay.kt` 的编辑弹层，经 `adapters.meta().groupEdit` → `vade-client: composeApp/src/commonMain/kotlin/vade/ui/meta/SessionMetaReal.kt` 的 `MetaGroupEditReal`（`session/group.create|update|delete|assign`，服务端已实现，`unassigned` 只读不写）；`session/group.list` 真组名喂列表（同 A4-1）。验证：`vade-client: composeApp/src/desktopTest/kotlin/vade/ui/meta/GroupFlowTest.kt` 的 `createGroupAppendsWithServerShapeAndWhitelistedDefaults` / `iconAndColorCanBeSetTogetherWithoutClobbering` / `deletingGroupKeepsSessionsAndUntagsThem`（删组不删会话正身）/ `createWithBlankNameIsRejectedWithoutCallback` + `MetaIconWhitelistTest` / `MetaColorTest` + `SessionMetaOverlayTest`（弹层挂载）。本轮 desktopTest 全量亲跑全绿。

### A5 可分发
- [x] 桌面端有明确的分发产物（安装包或免安装目录 + 启动脚本） — 证据：MSI `composeApp/build/compose/binaries/main/msi/vade-ui-0.1.0.msi` **93,312,713 B**（`vade-client:` 本机实存 2026-11-15 复核同值，未签名）+ APK **22,845,270 B**（`vade-client: composeApp/build/outputs/apk/debug/composeApp-debug.apk`，tmp/s-alpha-truth2 本轮 `assembleDebug` 实产）；免安装 app-image **154.7 MB** 为 `g-clean` 当时产物（`vade-client: shots/g-clean/RESULT.md` §1；当前 build 目录已清理，历史记录）；启动脚本 `vade-client: deploy/desktop/run.bat`、`run.sh`。本轮验证：`gradle :composeApp:assembleDebug` BUILD SUCCESSFUL（亲跑）。
- [x] 版本号单一来源，界面「关于」页可见 — 证据：唯一字面量 `vade-client: gradle.properties`（`vadeVersion=0.1.0-alpha.1`）→ 生成 `VadeBuildConfig` → `AboutPage`（`vade-client: composeApp/src/commonMain/kotlin/vade/ui/destinations/ProfileScreen.kt`，`版本 ${VadeBuildConfig.VERSION}`）；验证：`vade-client: composeApp/src/desktopTest/kotlin/vade/ui/about/AboutPageTest.kt`（含 `runComposeUiTest` 真渲染），本轮 desktopTest 全量亲跑全绿。
- [x] 干净机器上能启动（不依赖本机 `D:/Hermes/tools` 里的任何路径） — 证据：app-image 241 个文件 + 95 个 jar 的 23,721 个 zip 条目字节级扫描，`D:/Hermes`、`C:/Users`、`D:/Android`、`GRADLE_USER_HOME` 等致命路径命中 **0**，剥离环境变量后启动成功（`vade-client: shots/g-clean/RESULT.md` §2、`vade-client: shots/g-clean/scan-report.txt`、`vade-client: shots/g-clean/isolated-launch-window.png`；本轮逐路径 `test -e` 在盘）。口径：**环境隔离**；真·第二台无 JDK 机器未验（见 §七 N6）。

### A6 质量门
- [x] 服务端全量测试全绿（2026-11-22 复跑基线 1557；下列证据保留 2026-11-15 的 1203 历史跑）、契约检查 OK、文档门禁 12/12 — 证据：本轮（tmp/s-alpha-truth2，@ `f24313a`，补 gitignored `vade_protocol.dll` 后）`pytest core/tests -q` → **1203 passed, 9 warnings in 133.68s**（0 failed；前轮 tmp/s-alpha-truth 曾见 `core/tests/admin/test_config_usage.py` 一次性冷跑抖动、单跑与 3 连跑全绿，本轮未复现）；`tools/check_contracts.py` → `contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, …)`；`docs/check_docs.py` → `合计 12 项，失败 0 项`；`tools/check_ledger.py` → `ledger: OK (16 RFC rows; rpc 1.7.0 49 methods in 3 directions, events 2.0.0 34 entries, 34 error codes, appends 12+9+3)`。台账出处 `docs/rfc/LEDGER.md` §1。
- [x] 客户端全量测试全绿（当前基线 1442）、三端编译通过 — 证据：本轮 @ `vade-client: 3fda055`：`gradle :composeApp:desktopTest --rerun` 逐 XML 求和 **files=154 / tests=1467 / failures=0 / errors=0 / skipped=1**（`a3668f7`，第二批 `a4-new-session`+`a3-tool-cards` 合入后；连跑 2 次均绿）（skip = `VadeClientSmokeTest.attachSubmitAndLiveEventsAgainstARealCore`，需 `VADE_SMOKE_PORT` 真核的既有 env 门）+ `assembleDebug` BUILD SUCCESSFUL（APK 22,845,270 B）+ `compileKotlinWasmJs` BUILD SUCCESSFUL（三目标本轮全亲跑）；e2e 启动预算 **180 s** 死锁保险 + 失败消息可 grep 判词（`vade-client: composeApp/src/desktopTest/kotlin/vade/ui/e2e/CoreBootDiagnostics.kt` 的 `CoreBootBudget.READY_TIMEOUT_MS = 180_000L`；满载 8 轮全绿、81 轮 972 次起核全 READY，`vade-client: shots/g-e2e-budget2/RESULT.md`）。
- [x] `gitleaks` 与私钥 grep **工作区** 0 命中（B4 定稿口径；全历史存量如实记录、历史重写明确不做） — 证据：本轮亲跑——主仓 `gitleaks dir`（工作区，含未跟踪）**0 命中**、客户端 `gitleaks dir` **0 命中**、两仓 `git grep -nE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"` 工作区 **0 / 0**；全历史（`--log-opts=--all`）gitleaks **5 条存量**命中（均为 `core/tests/` 夹具旧提交：`test_audit.py` / `test_args_redaction.py` / `test_passwords.py`，工作区现行文件已 0）、私钥 grep 全历史 **1200 行**（同属存量）；规则零削弱：`.gitleaks.toml` 仅 4 条路径级排除、零 allowlist、金丝雀反向自证 PASS（主仓 `shots/s-gitleaks-final/`、客户端同构证据 `vade-client: shots/s-gitleaks-final/`，客户端规则定稿于 `83d2b0e`）。
- [x] 桌面真机截图 ≥3 张（亮/暗、会话/能力/活动） — 证据：`vade-client: shots/w2-final2/light.png` 与 `vade-client: shots/w2-final2/dark.png`（本轮复核 md5 不同：`497a6a71…` vs `365825fa…`，暗色修复后的真图）、`vade-client: shots/w2-bottombar-fix/phone-activity.png`（活动）、`vade-client: shots/w2-bottombar-fix/phone-capabilities.png`（能力）。
- [x] **Android 运行时验收**：在 API 37 模拟器（手势导航，验 edge-to-edge 安全区）与 API 30 真机（验 miuix-blur 降级不崩）上各装一次，启动无崩溃、底栏不被系统手势条压住，截图存档 — 证据：`vade-client: shots/c-inset/RESULT.md`（API 37 `api37-fixed.png` / `api37-floating.png` / `api37-floating-blur.png`、API 30 `api30-fixed.png` / `api30-floating.png`；两台 `logcat -d -b crash` 中 `vade.ui` 命中 0 条；本轮逐路径 `test -e` 在盘）。

## 四、已知硬阻塞（不是时间问题）

| 阻塞 | 影响 | 解法 |
|---|---|---|
| ~~本机无 Android 设备/模拟器~~ **已解除**（2026-11-14） | 现已可做真机验证 | 模拟器 `emulator-5554`（API 37 / x86_64 / 手势导航）+ 真机 `192.168.1.180:5555`（API 30 / arm64 / 三键导航）；截图走 `adb exec-out screencap -p`，不经过 Windows 窗口 |
| **`deploy/` 只有 README**（本仓） | 无发布目录、无签名、无更新通道 | 客户端仓已有 `deploy/desktop/`（`run.bat`/`run.sh`），产物也能打出 MSI/app-image；签名与更新通道仍未做（§七 N1、N3），本仓 `deploy/` 仍只有 README |
| **真模块只有 echo**（且在独立仓 `Vade-module-echo`，本仓 `modules/` 只有 README） | alpha 能演示但无实用价值 | 口径 B 做 fs/model |
| **`admin-ui/` 只有占位** | 管理面不可用（不影响聊天主链路） | 口径 B/C |
| **Web 可写（D100）未评审** | Web 端不能开放写操作 | 口径 B 的合并门槛 |

## 五、波次计划（口径 A）

| 波 | 内容 | 状态（2026-11-15 校准，tmp/s-alpha-truth2） |
|---|---|---|
| W1 | 协议补齐 + 真数据层 + 元数据/输入/用量/子代理 UI + 暗色 + 工具修复 | **已完成**：协议 **35/35**（rpc 现为 1.7.0 / 49 方法）有类型封装 + 覆盖率守卫；数据层与各面板均有测试 |
| W2 | 数据层接线到 UI（会话列表 / 聊天流 / 状态机）、模块页入口、搜索接真接口 | **已完成**：会话列表 / 搜索 / 组名 / 活动页 / 模型页 / 聊天**读侧**（历史 + 实时事件流）/ 模块清单与页面渲染链路 / **聊天写侧**（`ui/submit`、`ui/interrupt`）/ 元数据动作挂载（长按菜单 + 分组管理 + 乐观回滚）/ **审批决策**（`approval/decide` 真出网，`request_id` 取自 `turn.approval_required`）均已接（§七 B1/B2/B6 已解除） |
| W3 | 打包与分发（桌面产物、版本号、关于页、启动脚本）、首次运行配对引导 | **已完成**：产物 / 版本号 / 关于页 / 启动脚本 + **配对引导**（三端引导卡，`g-pair-first-run`）均已交付 |
| W4 | 端到端联调：本机 core + 桌面客户端真跑，逐个修阻塞项 | **进行中**：`g-live` 实跑手册（`vade-client: shots/g-live/PLAYBOOK.md`，2026-11-14 逐命令实跑）+ 两条真核 e2e（`RealCoreUiE2ETest` / `RealCoreModulePageE2ETest`）已在门禁里跑通；§七 阻断项已全部收口（0 阻断），**系统性人工联调仍未做**——这是 alpha 前最后一波，剩余已知缺陷见 §八 |

**W4 是唯一无法并行、也无法预估的一波**——它就是把假数据换成真数据之后暴露出来的所有问题。

## 六、不做（口径 A 明确排除）

- iOS（用户 2026-11-13 决定暂缓）
- 六个模块里除 `echo` 外的全部
- admin-ui 前端
- **M4a 记忆 + M4b 自进化**（用户 2026-11-15 拍板：**alpha 先不做记忆**）。注意口径：**设计已到实现级**（`docs/agents/43-M4-memory.md` 存储/staging/`MemoryBackend`；`docs/agents/46-M4b-evolve.md` 五个触发器、预算、晋升阀值、幂等要求），缺的是**契约文件**（C-12 `schema/memory.schema.json` 未落）与**实现**（`core/vade/memory` 不存在）——不是「没设计」，验收时别把这三层混为一谈
- 抓包 0 泄漏的正式验收（口径 C 才做）

## 七、当前差距清单（2026-11-15 校准于 tmp/s-alpha-truth2）

口径：**阻断项**做完才能把 §三 的 21 条全部勾上、才配叫 alpha；**非阻断项**可在 alpha 说明里列为已知缺陷后发布。**0 项阻断、7 项非阻断**（B1/B2/B4/B5/B6 已解除、B3 判假阳性撤销、N2 已解决——全部保留编号防断链）。

| # | 阻断项（必须做完才能叫 alpha） | # | 非阻断项（可带缺陷发布） |
|---|---|---|---|
| ~~B1~~ | ~~**A3 模块清单**：`ui_to_core` 34 个方法里没有「列模块」~~ **已解除**（2026-11-15）：RFC-0015 `ui/modules` 登记进契约、core 实现并带真 WS e2e、客户端 codec/订阅/组合根接线全通 | N1 | **代码签名**：jpackage 产物 `.jpackage.xml` 记 `signed=false`，MSI/EXE 未签名，SmartScreen/杀软告警 |
| ~~B1~~ | ~~**聊天写侧**~~ **已解除**（2026-11-15，`g-chat-send` 合入客户端）：`ChatState.submit()` 真发 `ui/submit`（`ChatState.kt` 的 `submitOverWire`）、`stop()` 真发 `ui/interrupt`（`interruptOverWire`）、`turn.failed` 四类可读文案 + 可重试；排队序号只认 core 的 `queue_pos`，假源出网 0 次由探针钉住（`ChatSendWireTest.fakeSourceSubmitAndStopNeverTouchTheGateway`） | ~~N2~~ | ~~**Web 字体**：wasm 首帧中文豆腐块~~ **已解决**（2026-11-15，`g-font-bundle2`）：CJK 字体（`vade-sans-sc.woff2`，OFL 许可随包，`vade-client: composeApp/src/wasmJsMain/resources/fonts/`）打进 wasm 包——首帧即正确、0 个 gstatic 请求、断网输出逐字节相同（`vade-client: shots/g-font-bundle2/RESULT.md`） |
| ~~B2~~ | ~~**元数据动作挂载**~~ **已解除**（2026-11-15，`s-meta-host` 合入客户端 `d5c0f80` + 父代补洞 `7797dcc`）：行长按 → 上下文菜单（重命名/置顶↔取消/归档/移动分组/新建分组/删除），全部经 `adapters.meta(sessionId)` 真调适配器；乐观可见 + 失败整表回滚 + 错误横条；分组编辑器（名称/39 图标/10 色盘/删组不删会话）。**父代补的洞**：抽屉路径不经过 `DestinationHost`，元数据悬浮面宿主原本只在双栏分支 ⇒ 手机上长按无任何渲染（已把 `overlayHost` 槽加进 `SessionsDrawerHost`，宿主渲染在抽屉层内）。同一提交修掉 `projectGroups` 的假空态：未同步时直通、同步后不认识的新行原样画 | N3 | **无发布通道**：产物在 `build/` 下不进库，无 artifact 上传、无更新检查；alpha 只能打包机重建或手动拷贝 |
| ~~B3~~ | ~~**模块页面生产三洞**~~ **撤销（判定为假阳性，2026-11-15）**：三洞已由 `tmp/g-modulepage-wire` 全堵（能力页 `LaunchedEffect` 首拉 + `ModulePageBody` 异步取体 + `ModuleRegistryView` 真在线态）；视觉审计 G2「永远正在拉取」是**旧基点**造成的误判——真 core + 真模块 + 真 WS 的 `RealCoreModulePageE2ETest` 在合并后树上 1 test / 0 failures。父代复核结论写在 `vade-client: shots/g-audit-gaps/RESULT.md` 末节 | N4 | **ask_user 卡片未合并**：实现完成（37 条测试）在 `tmp/c-askuser`，未进 `main`（2026-11-15 复核：commonMain 仍只有协议层 `AskUser`/`InputRequired` 模型，无卡片组件） |
| ~~B4~~ | ~~**gitleaks 定稿**~~ **已解除**（2026-11-15，`s-gitleaks-final` 规则定稿 + `s-gitleaks-fixtures` 夹具改运行时拼接）：口径 = **工作区 0 命中**、规则零削弱（仅 4 条路径级排除、零 allowlist、金丝雀反向自证 PASS）；本轮两仓工作区复扫 **0 命中**亲证（§三 A6-3）。全历史存量命中（gitleaks 5 / 私钥 grep 1200）属历史提交，需重写历史，明确不做 | N5 | **关于页「E2E 设备标识」仍是占位**（`vade-client: shots/g-pack/RESULT.md` §5.5） |
| ~~B5~~ | ~~**配对引导（A1-2）**~~ **已解除**（2026-11-15，`g-pair-first-run` 合入客户端）：三端列表顶部引导卡（一次性码输入 + 配对按钮 + 码在哪里找），提交走现有连接链且**恰好一次**；三种失败分开报（码错 / 服务端未起带启动命令 / 网络可重试）；码只走函数参数与内存，扫描守卫断言零持久化/零日志/零 URL。`vade-client: shots/g-pair-first-run/RESULT.md` | N6 | **真·干净机器未验**：A5-3 是环境隔离口径（剥离 PATH/JDK/SDK 变量），未在无 JDK 的第二台机器双击 MSI / app-image |
| ~~B6~~ | ~~**审批决策未接线**~~ **已解除**（2026-11-15，`tmp/b6-approval` 合入客户端 `3fda055`）：`SessionSource.decideApproval` + `RealSessionSource` 走 `client.approvalDecide`（全仓唯一出网点）+ **`request_id` 取自 `turn.approval_required`（不是本地 `callId`）** + 乐观回滚 + 四类错误文案 + `turn.approval_resolved` 以服务端为准。遗留（非阻断）：审批卡插在时间线尾部而非事件原位，记入 §八 D2 | N7 | **附件上传端到端仍断**：`ui/blob.token` RPC 不存在（实读协议走 HTTP `POST /ui/blob` 三步 + `DELETE` 取消，有真适配器）；**RFC-0016 已 proposed**（`docs/rfc/`，2026-11-15 用户已拍板四项：绑设备 / 600s 可重放 / 收紧读面），实施未排 |
| — | — | N8 | **Android 验收遗留项**：w-verify 报告记 P0=0 / P1=4 / P2=4（该报告在 worktree，未合并），无崩溃级问题 |

## 八、alpha 已知缺陷清单（可带缺陷发布）

以下缺陷**不阻断**口径 A（§七 0 阻断），每条写「用户会看到什么」与「怎么绕」。D1/D2 是 b6-approval 与本轮复核确认的真缺口；D6/D7 来自 `g-chat-send` 遗留清单；D8/D9 来自 `g-pair-first-run` 发现；N 系列存量见 §七 表，不在此重复。

- ~~**D1 真源下普通工具卡不显示**~~ **已修**（2026-11-15，`a3-tool-cards`，客户端 `a3668f7`）：`LiveTranscript.project()` 现在把 `turn.tool_call` 按到达顺序建成工具卡、`turn.tool_result` 按 `call_id` 就地更新、重放幂等；FULL args 永不上卡。验证 `ToolCardProjectionTest` 12 例。出处 `vade-client: shots/a3-tool-cards/RESULT.md`
- ~~**D2 审批卡插在时间线尾部**~~ **已修**（同批）：审批卡按 `turn_id`+工具名+未绑定+Running 取最早那张工具卡**原地升级**，不再另建卡；契约的 `turn.approval_required` 不带 `call_id`，映射不到（tool_call 缺席 / 子代理审批的 turn_id 是子回合）时**降级**回尾部——这是启发式，边界已写进 RESULT。
- **D3 会话中段无连接状态指示**：横幅只反映列表首读三态（`ListStatus`，Connected 刻意不画），顶栏不绑 `RealSessionState`。用户会看到：聊天中途掉线时没有「正在重连」提示，界面像停住（流事件不再推进）。绕：重连自动进行（500 ms → 30 s 指数退避）；若列表读取也失败，会出现带可照抄启动命令的失败横幅。出处：§三 A1-3。
- ~~**D4 「新建会话」只建本地草稿**~~ **已修**（2026-11-15，`a4-new-session`，客户端 `a3668f7`）：`ui/attach` 传 `session_id: null` 让 core `create_session()` 铸会话并广播给其他设备；失败不伪造会话、可重试。验证 `NewSessionWiringTest` 6 + `RealSessionCreateTest` 4。出处 `vade-client: shots/a4-new-session/RESULT.md`
- **D5 「重启客户端仍可见」闭环无端到端**：快照读侧与发送侧各自有证据，但「落库→重启→重连→恢复」没人端到端走过。用户会看到：理论可用、无证明。绕：重启后列表/历史走 attach 快照（`RealCoreUiE2ETest` 已证真核快照行上屏）。出处：§三 A2-3。
- **D6 取消排队回合不出网**：`ui/queue.cancel` 未接（`ChatTurnGateway` 尚无该方法）。用户会看到：点取消后本地排队条目消失，core 侧回合照跑。绕：等回合自然完成；运行中的回合用 ■（`ui/interrupt` 已通）。出处：`vade-client: shots/g-chat-send/RESULT.md` §遗留 1。
- **D7 发送失败的气泡无专用重试钮**：失败气泡保留在屏上（Failed + 四类徽标），重走 = 编辑重发或重新输入；对它做 `message/edit_resubmit` 会被 core 以 `NOT_FOUND` 拒（可见不静默）。绕：编辑重发；后续应对 client_msg_id 幂等重发 `ui/submit`。出处：`vade-client: shots/g-chat-send/RESULT.md` §遗留 2。
- **D8 wasm 首次配对不可用**：浏览器 JS 引擎读不到 `Set-Cookie`（`KtorTransportFactory` KDoc 注明），引导卡在 wasm 上任何提交都会落「码错」分类。用户会看到：wasm 上配对总失败，detail 行带真实原因。绕：首次配对用桌面/Android，或走页面壳。出处：`vade-client: shots/g-pair-first-run/RESULT.md` §发现 2。
- **D9 Android 启动 extras 键名文档漂移**：`vade-client: contracts/vade/docs/PAIRING.md` §三与 `MainActivity.kt` KDoc 写 `vade.source`/`vade.host`/`vade.port`，`SourceKeys` 实为裸键 `source`/`host`/`port`/`pair_code`。用户会看到：按文档传 `--es vade.source real` 无效、**静默落到假源**。绕：用裸键传参。出处：`vade-client: shots/g-pair-first-run/RESULT.md` §发现 1。
- **D10 打开会话 = 同连接 2 个 sink，且从不 detach**（**既有基线，非本轮引入**，`a4-new-session` 核实）：`SessionsLoader.selectSession` 与 `RealAdapters.chatTranscript.history` 各发一次 `ui/attach`，`ui/attach` 每次调用都 `_register_sink()` 新建一个 SinkWorker（core 侧无去重），而 `RealSessionSource.detach` 生产调用点为 0 ⇒ 同一会话在一条连接上**双投递事件、sink 泄漏到连接结束**。用户会看到：正常使用看不出来（事件幂等），但事件量大时同一 delta 会被处理两次、长连接内存缓慢增长。绕：重启客户端即回收；新建会话已用 `isDraft=true` 避免再多一个 sink。收口需单独批次（读侧两个调用点合并为一次 attach + 离开会话时 detach）。

---

**校准依据（2026-11-15；第一批 `tmp/s-alpha-truth2` @ `f24313a` + 客户端 `3fda055` 亲自复跑；第二批由父代在 `a4-new-session`/`a3-tool-cards` 合入后复跑，客户端 @ `a3668f7`）**：
- 服务端 `pytest core/tests -q` → **1203 passed, 9 warnings in 133.68s**（0 failed；2026-11-22 f15 复跑 **1557 passed, 14 warnings in 481.82s**）
- 客户端 `gradle :composeApp:desktopTest --rerun` → 逐 XML 求和 **files=154 / tests=1467 / failures=0 / errors=0 / skipped=1**（`a3668f7`，第二批 `a4-new-session`+`a3-tool-cards` 合入后；连跑 2 次均绿）；`assembleDebug`、`compileKotlinWasmJs` 均 BUILD SUCCESSFUL
- `tools/check_contracts.py` → `contracts: OK`；`tools/check_ledger.py` → `ledger: OK (16 RFC rows; rpc 1.7.0 49 methods in 3 directions, events 2.0.0 34 entries, 34 error codes, appends 12+9+3)`；`docs/check_docs.py` → 12 项 / 失败 0
- gitleaks：主仓与客户端工作区 `dir` 扫描 **0 命中**、私钥 grep 工作区 **0/0**（全历史存量：gitleaks 5 / 私钥 grep 1200，历史重写明确不做）
- §三 证据路径逐条 `test -e`：**59/59 在盘**（本轮重写后清点；§八 引用的 `vade-client: shots/…` 证据路径另计、亦全部在盘）

---

## 九、待办（alpha 之后第一批）

alpha 0.0.1 的发布面**不包含**下面这些；它们要么需要真模型、要么是体验/健壮性补口，已确认不阻断发布。

| # | 待办 | 现状 | 收口判据 |
|---|---|---|---|
| T1 | **连接状态指示**（对应 §三 A1-3） | 重连与指数退避（500 ms → 30 s 上限）已实现且有测试；但顶栏不绑 `RealSessionState`（grep 零命中），`ListStatus` 的 `Connected` 刻意不画 ⇒ 会话中段断线用户看不见 | 顶栏或会话中段出现持续连接态；断线 → 重连 → 恢复三态各有测试；真机拔网可复现 |
| T2 | **「重启 → 重连 → 快照恢复」端到端**（对应 §三 A2-3） | 读侧（`ui/attach` + snapshot）与写侧（`ui/submit`）各自有证据，闭环没人走过 | 一条 e2e：发消息 → 杀客户端 → 重启 → 同一会话消息仍在且 `last_seq` 连续；留下录屏或截图证据 |
| T3 | **真模型驱动的工具调用端到端**（对应 §三 A3-1 剩余半边） | 工具卡投影已接（`ToolCardProjectionTest` 12 例）；差一个真 model 模块把 `turn.tool_call` 发出来 | 真 core + 真 model 模块 + echo：模型自主调用四个工具、卡片在流中按序出现、审批卡绑到正确那张 |
| T4 | **单会话双 attach 收口**（对应 §八 D10） | 打开会话时 loader 与 chat 历史各 attach 一次，`ui/detach` 生产调用点为 0 ⇒ 同连接 2 sink、双投递、泄漏到连接结束 | 读侧两处合并为一次 attach；离开会话时 detach；判据：同一会话 `_register_sink` 调用次数 = 1，切走后 sink 归零 |
| T5 | **wasm 首次配对**（对应 §八 D8） | 浏览器读不到 `Set-Cookie` | 走 `POST /ui/session` 回包的 token 而非 cookie，或改用一次性 URL 换取 |
| T6 | **取消排队回合出网**（对应 §八 D6） | `ui/queue.cancel` 未接 | 点取消 → 发 `ui/queue.cancel` → core 回 `turn.cancelled` |
