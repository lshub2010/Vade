# RESULT · tmp/s-alpha-truth2 — docs/ALPHA.md §三 21 条可信台账 + §七 清零 + §八 已知缺陷清单

- 基点：主仓 `dev_gingshan` @ `f24313a`（worktree `D:/HermesWorkspace/Vade-wt/alpha-truth2`，分支 `tmp/s-alpha-truth2`）
- 客户端参考仓：`D:/HermesWorkspace/Vade-client` @ `3fda055`（**只读**，本轮仅在其上跑测试/扫描，未改任何 tracked 文件，跑后 `git status --porcelain` 非 untracked 变更 = 0）
- 代码改动：**零**（只改 `docs/ALPHA.md`）；文档 commit **`8ca0542`**（本报告第二个 commit）
- 未 push、未 merge、无 amend/rebase/reset
- 本报告不含任何 token / 密钥 / 配对码；gitleaks 报告一律 `--redact`

## 0. 一句话结论

§三 21 条按「实现在仓 + 可重跑验证 + 本轮亲跑」三条件逐条判活：**[x] 8 → 17**（新勾 9 条：A1-2 / A2-1 / A2-2 / A2-4 / A2-5 / A3-2 / A3-3 / A4-2 / A6-3），**[ ] 13 → 4**（A1-3 / A2-3 / A3-1 / A4-1，各写明差什么）；§七 阻断表改口径为 **0 阻断 / 7 非阻断**（B1/B2/B4/B5/B6 已解除、B3 假阳性撤销，编号全保留）；新增 **§八 alpha 已知缺陷清单（D1–D9）**。门禁三件套全绿；§三 证据路径逐条 `test -e` **59/59 在盘**。

## 1. 本轮亲自跑过的验证（原始输出）

### 服务端（worktree @ f24313a；补 gitignored `vade_protocol.dll` 后）

```
$ /d/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe -m pytest core/tests -q
1203 passed, 9 warnings in 133.68s (0:02:13)

$ python docs/check_docs.py            （改后复跑）
合计 12 项，失败 0 项。

$ python tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete, RFC-0005 append green, RFC-0010 session metadata green, RFC-0012 subagent family green, RFC-0014 session search green, RFC-0016 blob token green, RFC-0003 layers green, event gate green)

$ python tools/check_ledger.py
ledger: OK (16 RFC rows; rpc 1.7.0 49 methods in 3 directions, events 2.0.0 34 entries, 34 error codes, appends 12+8+3; impl paths verified, statuses verified, acceptance tables recounted)
```

注：rpc 契约现为 **1.7.0 / 49 方法**、ledger appends **12+8+3**——旧文档页脚写的 1.6.0/48、11+8+3 已过期，本轮一并修正。

### 客户端（@ 3fda055，三门禁串行）

```
$ GRADLE_USER_HOME=D:/Hermes/tools/gradle-home JAVA_HOME=D:/Hermes/tools/jdk21 \
  gradle :composeApp:desktopTest --rerun
BUILD SUCCESSFUL in 2m 58s
  逐 XML 求和（composeApp/build/test-results/desktopTest/TEST-*.xml）：
  files=151 tests=1442 failures=0 errors=0 skipped=1
  skipped = vade.ui.protocol.VadeClientSmokeTest.attachSubmitAndLiveEventsAgainstARealCore（需 VADE_SMOKE_PORT 真核，既有 env 门）

$ gradle :composeApp:assembleDebug      → BUILD SUCCESSFUL in 37s
  composeApp-debug.apk = 22,845,270 B
$ gradle :composeApp:compileKotlinWasmJs → BUILD SUCCESSFUL in 38s
```

### gitleaks（两仓，本轮亲跑；口径 = 工作区 0 命中）

```
$ gitleaks dir <主仓 worktree>  --config .gitleaks.toml --redact …
  exit=0, findings=0
$ gitleaks git <主仓 worktree> --log-opts=--all …
  exit=0, findings=5   ← 全部为 core/tests 夹具旧提交（test_audit.py / test_args_redaction.py / test_passwords.py）存量，工作区现行文件 0
$ gitleaks dir <客户端> --config .gitleaks.toml --redact …
  exit=0, findings=0
$ git grep -nE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"   # 工作区
  主仓 0 行；客户端 0 行
$ git grep -nE … $(git rev-list --all)                    # 全历史（存量口径）
  1200 行（历史重写明确不做，与 B4 台账一致）
```

### 证据路径在盘清点

```
§三 59 个文件路径 token 逐条 test -e（主仓 worktree / 客户端仓按前缀解析）：
59/59 在盘；§八 引用的 shots 证据（b6-approval / g-chat-send / g-pair-first-run / g-module-page 等）亦全部在盘。
唯一失效路径（旧 A6-3 文本里的客户端 .venv …/hpke.pyi，venv 已不存在）随 B4 定稿文本重写一并移除。
```

### 其他复核

```
$ md5sum shots/w2-final2/{light,dark}.png   （客户端）
  497a6a71b0c35cf57c9bbcc6e1af8b84  light.png
  365825fa525bfb450e834dd7c3629320  dark.png     ← 两图确实不同
$ ls composeApp/build/compose/binaries/main/msi/vade-ui-0.1.0.msi → 93,312,713 B（与 A5-1 原记录同值）
```

## 2. 逐条判定表（21 条）

| 条号 | 旧态 | 新态 | 证据（文件 + 符号，客户端路径省略 `vade-client: composeApp/src/…` 前缀） | 跑的验证 + 结果 | 若不勾，差什么 |
|---|---|---|---|---|---|
| A1-1 attach | [x] | [x] 保留 | 客户端 `commonMain/…/protocol/VadeClient.kt` `attachInternal`；证据 `shots/w1-proto/smoke.md` | `RealCoreUiE2ETest.realCoreSessionTitlesRenderInAppAndFixturesStayAway`（真核 + 真 attach），本轮套内绿 | — |
| A1-2 配对 | [ ] | **[x]** | 客户端 `commonMain/…/destinations/PairingCard.kt`；`data/SourceConfig.kt` `needsFirstRunPairing`/`PairingRuntime`；服务端 `core/vade/identity/pairing.py` `PairingStore.create`；守卫 `PairingFirstRunTest.enteredPairCodeHasNoPersistenceSink` | `PairingFirstRunTest`(10) + `PairingCardUiTest`(6)，本轮套内绿；真核截图 `shots/g-pair-first-run/` 5 图 | —（B5 已解除） |
| A1-3 重连+状态 | [ ] | [ ] | `protocol/VadeClient.kt` `backoffDelayMs`；`data/RealSessionState.kt` `Reconnecting`；横幅 `destinations/ListStatusBanner.kt` `ListStatus` | `backoffDelayMs`/`Reconnecting` 有测试（本轮套内绿）；`TopBar.kt` grep `RealSessionState` 零命中 | 顶栏/会话中段连接状态指示（横幅只反映列表首读三态，Connected 刻意不画） |
| A2-1 ui/submit | [ ] | **[x]** | `chat/ChatState.kt` `submit()`→`submitOverWire`；`chat/ChatTurns.kt` `ChatTurnGateway.submit`；`data/TurnGatewayReal.kt` `submit` | `ChatSendWireTest.realSubmitSendsExactlyOneUiSubmitWithTheBubblesClientMsgId` + `TurnGatewayRealTest` 帧断言 + `fakeSourceSubmitAndStopNeverTouchTheGateway`，本轮套内绿；真核冒烟 `shots/w1-proto/smoke.md` | —（B1 已解除） |
| A2-2 流式 | [ ] | **[x]** | `chat/ChatThread.kt` `collectLiveEvents()`；`chat/LiveTranscript.kt` `apply`（(turn_id, part_index) 追加） | `LiveTranscriptTest.deltasReplayTheRealE2eWireSentenceAndUsage` / `twoDeltasOnOnePartAccumulateInsteadOfOverwriting` / `outOfOrderDeltasBufferUntilTheGapFills` 等，本轮套内绿 | —（触发端已由 B1 接通） |
| A2-3 落库重启 | [ ] | [ ] | `destinations/SessionDetailScreen.kt` `resolveChatState`；`chat/ChatState.kt` `loadHistory()` | `RealCoreUiE2ETest`（快照行上屏）本轮套内绿 | 「重启→重连→快照恢复」显式端到端未做 |
| A2-4 中断 | [ ] | **[x]** | `chat/ChatState.kt` `stop()`→`interruptOverWire`；`data/TurnGatewayReal.kt` `interrupt`；`chat/LiveTranscript.kt` `onInterrupted`（partial 保留 + wire `partial_chars` 优先） | `ChatSendWireTest.realStopSendsExactlyOneInterruptAndDedupesDoubleClicks` + `interruptGatewayErrorBecomesVisibleState` + `LiveTranscriptTest.interruptKeepsPartialTextAndCountsCodePoints` / `interruptPrefersTheWirePartialChars`，本轮套内绿 | —（B1 已解除） |
| A2-5 四类错误 | [ ] | **[x]** | `data/VadeErrorCategory.kt` `categoryOf`（34→4 冻结表）；`chat/ChatState.kt` `makeFailureReadable` | `VadeErrorMappingTest`（34 穷举）+ `ChatSendWireTest.turnFailedShowsReadableCopyAndRetryGoesFromOneToTwoCalls` / `turnFailedMapsTheOtherErrorBucketsAndTheUnknownCodeFallback` / `submitGatewayErrorBecomesVisibleStateOnTheBubble`，本轮套内绿 | — |
| A3-1 工具卡 | [ ] | [ ] | 模块页 e2e `desktopTest/…/e2e/RealCoreModulePageE2ETest.kt`；**缺口**：`commonMain/…/chat/LiveTranscript.kt` `project()` 对 `turn.tool_call` `else -> Unit` | `RealCoreModulePageE2ETest` 本轮套内绿（模块页部分）；`project()` 源码核实仍忽略 tool_call | `turn.tool_call` 投影进会话流 + 真模型驱动工具调用端到端（model 模块非真） |
| A3-2 离线提示 | [ ] | **[x]** | `data/VadeAdapters.kt`（`RealModulePageSource(client, moduleRegistry)`）；`module/RealModulePageSource.kt` `status()` 读 registry；`module/ModulePageRenderer.kt` `BannerRow` | `ModulePageStatusBannerTest.statusComesFromTheRegistryNotFromAConstant` / `anOfflineModuleStillDrawsTheOfflineBanner` / `anOnlineModuleRendersThePageWithoutTheOfflineBanner` + `RealCoreModulePageE2ETest`（在线零横幅），本轮套内绿 | —（B3 撑销后「恒离线」已不存在） |
| A3-3 审批卡 | [ ] | **[x]** | `chat/ChatThread.kt` `ApprovalCardRow`；`chat/ChatState.kt` `onApprovalRequired`/`decideApproval`；`data/RealSessionSource.kt` `decideApproval`（唯一 `client.approvalDecide` 出网点）；`request_id` 取 `turn.approval_required` | `ApprovalWireTest`(20，判据 `approveClickSendsExactlyOneApprovalDecideWithVerbatimParams`) + `ApprovalDecideWireTest`(8)，本轮套内绿；变异自验见 `shots/b6-approval/RESULT.md` | —（B6 已解除；卡在时间线尾部记 §八 D2，不属本句验收范围：本句只要求内联、不弹独立窗口） |
| A4-1 会话元数据 | [ ] | [ ] | `shell/UiState.kt` `groups()`；`shell/AppShell.kt` `metaGroupsOf`；`meta/SessionMetaOverlay.kt` `SessionMetaOverlayHost`（AppShell 三处挂载）；`data/MetaActionsReal.kt` | `MetaActionsRealTest`(14) + `SessionMetaOverlayTest`(9) + `SessionMetaLongPressUiTest`(4) + `SessionMenuTest`/`MetaWireTest`，本轮套内绿 | 新建会话：`shell/UiState.kt` `newSession()` 仍只建本地草稿、不出网（真源无会话创建出口，§八 D4） |
| A4-2 分组管理 | [ ] | **[x]** | `meta/SessionMetaOverlay.kt` 编辑弹层；`meta/SessionMetaReal.kt` `MetaGroupEditReal`（group.create/update/delete/assign） | `meta/GroupFlowTest.kt` `createGroupAppendsWithServerShapeAndWhitelistedDefaults` / `iconAndColorCanBeSetTogetherWithoutClobbering` / `deletingGroupKeepsSessionsAndUntagsThem` / `createWithBlankNameIsRejectedWithoutCallback` + `MetaIconWhitelistTest`/`MetaColorTest`，本轮套内绿 | —（B2 已解除） |
| A5-1 分发产物 | [x] | [x] 保留 | 客户端 `deploy/desktop/run.bat`/`run.sh`；MSI/APK 实存 | 本轮 `assembleDebug` 亲跑（APK 22,845,270 B）；MSI 93,312,713 B 在盘同值 | — |
| A5-2 版本号 | [x] | [x] 保留 | 客户端 `gradle.properties` `vadeVersion=0.1.0-alpha.1` → 生成 `VadeBuildConfig` → `destinations/ProfileScreen.kt`「版本 ${VadeBuildConfig.VERSION}」 | `ui/about/AboutPageTest.kt` 本轮套内绿 | — |
| A5-3 干净启动 | [x] | [x] 保留 | `vade-client: shots/g-clean/scan-report.txt`、`isolated-launch-window.png`（本轮 test -e 在盘） | 历史扫描证据复核在盘（环境隔离口径，§七 N6 不变） | — |
| A6-1 服务端门 | [x] | [x] 更新 | `docs/rfc/LEDGER.md` §1 | 本轮 pytest **1203 passed / 133.68s** + 三门禁原始输出见 §1（基线 1164→1203 随夹具批次增长） | — |
| A6-2 客户端门 | [x] | [x] 更新 | 客户端 `desktopTest/…/e2e/CoreBootDiagnostics.kt` `CoreBootBudget.READY_TIMEOUT_MS=180_000L` | 本轮 desktopTest --rerun **151/1442/0/0/1** + assembleDebug + wasm（§1）；基线 1122→1442 | — |
| A6-3 gitleaks | [ ] | **[x]**（口径=工作区 0） | 主仓/客户端 `.gitleaks.toml`（4 条路径级排除、零 allowlist）；证据 `shots/s-gitleaks-final/`（两仓） | 本轮两仓工作区 dir 扫描 **0/0** + 私钥 grep 工作区 **0/0**（§1；全历史存量 5/1200 如实记录，历史重写不做） | —（B4 已解除；句式已改写为「工作区 0 命中」口径） |
| A6-4 截图 | [x] | [x] 保留 | `vade-client: shots/w2-final2/{light,dark}.png`、`w2-bottombar-fix/phone-{activity,capabilities}.png` | 本轮 md5 复核 light≠dark + 在盘 | — |
| A6-5 Android 验收 | [x] | [x] 保留 | `vade-client: shots/c-inset/`（api37-* / api30-* 共 5 图 + RESULT.md） | 本轮逐路径 test -e 在盘 | — |

新勾 9 条全部满足：①实现在仓可指认（文件+符号）②可重跑验证（具名测试/命令/证据路径）③本轮亲自复跑（原始输出见 §1）。四条未勾均写明缺口并挂到 §八 缺陷清单。

## 3. §七 改写与 §八 新增

- **§七**：表头口径改为「**0 项阻断、7 项非阻断**（B1/B2/B4/B5/B6 已解除、B3 判假阳性撤销、N2 已解决——全部保留编号防断链）」。B1 两行、B2、B4、B5、B6 全部按既有删除线风格补「已解除 + 证据指针」；B1 第二行删去「未解除的一半」（B6 已解除）；B6 行补「遗留（非阻断）：审批卡插在时间线尾部 → §八 D2」。
- **§八（新增）alpha 已知缺陷清单（D1–D9，可带缺陷发布）**：
  - D1 真源下普通工具卡不显示（`LiveTranscript.project()` 忽略 `turn.tool_call`；审批卡例外）——绕：能力页/模块页看状态。
  - D2 审批卡插在时间线尾部而非事件原位——绕：当「最新待办」处理。
  - D3 会话中段无连接状态指示（顶栏不绑 `RealSessionState`，横幅仅列表首读三态）——绕：重连自动进行。
  - D4 「新建会话」只建本地草稿（`newSession()` 不出网）——绕：在已有会话聊。
  - D5 「重启仍可见」闭环无端到端——绕：快照读侧有 `RealCoreUiE2ETest` 佐证。
  - D6 取消排队回合不出网（`ui/queue.cancel` 未接）——绕：等完成或 ■ 中断。
  - D7 发送失败气泡无专用重试钮——绕：编辑重发。
  - D8 wasm 首次配对不可用（浏览器读不到 `Set-Cookie`）——绕：桌面/Android 配对。
  - D9 Android extras 键名文档漂移（`vade.source` vs 裸键 `source`）——绕：用裸键。
  每条含「用户会看到什么」+「怎么绕」+ 出处；N1/N3–N8 存量留在 §七 不重复。

## 4. 过期断言修正（相对 f24313a 的旧 ALPHA.md）

1. §一「聊天写侧（发送/中断/审批）仍没出网（§三 A2、§七 B1）」→ 已全部出网，改写。
2. §二/A6-1/A6-2 测试基线 1164 / 1122 → 1203 / 1442（本轮实测）。
3. 页脚契约口径 rpc 1.6.0 / 48 方法、appends 11+8+3 → 1.7.0 / 49、12+8+3。
4. A6-3 的「该项未定稿，不得计为通过」与失效路径 `vade-client: .venv/…/hpke.pyi` → B4 定稿口径 + 本轮 0 命中证据。
5. A3-2 的「`RealModulePageSource.status()` 硬返 `online=false`、恒显示离线」→ registry 已注入（B3 撑销），横幅三测 + e2e 判活。
6. §五 W2「只剩 approval/decide 出网」、W3「配对引导未做」→ 两波已完成；W4 补「0 阻断，剩系统性人工联调」。
7. 校准汇总 8+13+0 → 17+4+0；文件头状态行改 tmp/s-alpha-truth2。

## 5. 未做 / 边界

- 未动任何代码、契约、客户端仓（客户端仅跑测试/扫描；跑后 tracked 零变更）。
- 全历史 gitleaks/私钥存量（5 / 1200）按 B4 口径保留，历史重写明确不做。
- `VadeClientSmokeTest`（需 `VADE_SMOKE_PORT`）维持既有 skip，未为本轮单独起真核补跑——A1-1/A2-1 的真核证据由 `RealCoreUiE2ETest`（套内真跑）与 `shots/w1-proto/smoke.md` 承担。
