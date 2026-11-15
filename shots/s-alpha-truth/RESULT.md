# s-alpha-truth · RESULT — `docs/ALPHA.md` 可信化校准（2026-11-15）

分支 `tmp/s-alpha-truth`（基于 `origin/dev_gingshan` @ `7941b93`），worktree `D:/HermesWorkspace/Vade/Vade-wt/s-alpha-truth`。
**未 push、未合并**；无 `--amend`/`--rebase`/`--reset`；只改了 `docs/ALPHA.md`、`docs/status.md`（U1 一行）与本目录。

---

## 0. 直答：现在到 alpha 0.0.1 还差几件事？

**阻断项 5 件 + 挂起决定 1 件**（对应 `docs/ALPHA.md` §七）：

| # | 差的事 | owner |
|---|---|---|
| B1 | **聊天写侧出网**：`ChatState.submit()` 纯本地，`ui/submit`/`ui/interrupt`/`approval/decide` 生产调用点为 0——真源下「发送」按钮不出网，流式/中断/审批全链条因此无法触发 | **无人**（客户端 tmp/ 分支无对应批次；需父代派棒） |
| B2 | **`SessionMetaHost` 挂载**：重命名/置顶/归档/删除/分组管理的宿主组件是死代码，适配器与工厂全在，只差组合根一跳 | **无人**（需父代派棒；与 B1 可同棒） |
| B3 | **模块页面生产三洞**：清单首拉无人发起 / 页面体无人拉 / 在线态恒离线（渲染链路本身已有真核 e2e 证据） | **无人**（需父代派棒） |
| B4 | **gitleaks 定稿**：history 扫描 0 泄漏已出，缺排除规则 + 私钥 grep 归档 | **无人**（需父代派棒，量小） |
| B5 | **配对引导**：三端都没有输入配对码的首次运行界面（桌面已有 `VADE_PAIR_CODE` env 兜底），双击产物连不上 core | **无人**（跨三端，量最大；需父代派棒） |
| — | **附件上传端到端**（N7）：RFC-0016 已 proposed，用户 2026-11-15 已拍板四项（绑设备/600s 可重放/收紧读面） | **待用户批准转实施**（批准后由父代排棒） |

非阻断里需要**用户资源**的：N1 代码签名（要证书）、N6 真·第二台无 JDK 机器（要机器）。`tmp/g-glass`（glass 皮肤）**在跑**（worktree 存活）。无 CI：无人认领，历史上未被立为阻断。

> owner 判定依据是仓内证据（分支名 / worktree / status.md），本棒看不到父代的在飞任务表；若父代已排队某棒，以父代记录为准。

## 1. 验到的（证据逐条实核）

### 1.1 `[x]` 条目引用的证据路径：37/37 `test -e` 全部在盘

主仓（2）：`docs/rfc/LEDGER.md` ✓；客户端仓（35）：`shots/w1-proto/smoke.md`、`shots/g-clean/{RESULT.md,scan-report.txt,isolated-launch-window.png}`、`shots/g-pack/RESULT.md`、`shots/w2-final2/{light,dark}.png`、`shots/w2-bottombar-fix/{phone-activity,phone-capabilities}.png`、`shots/c-inset/RESULT.md`、`shots/g-chatlive/RESULT.md`、`shots/g-module/RESULT.md`、`shots/g-modulewire/RESULT.md`、`shots/w-adapt/RESULT.md`、`shots/g-wasm/RESULT.md`、`shots/g-gitleaks/raw/`、`docs/QUICKSTART.md`、`deploy/desktop/{run.bat,run.sh}`、`gradle.properties`、`composeApp/build/compose/binaries/main/msi/vade-ui-0.1.0.msi`、`composeApp/build/outputs/apk/debug/composeApp-debug.apk`、以及 A1–A4 引用的 14 个源码文件（`VadeClient.kt`、`VadeTransport.kt`、`RealSessionState.kt`、`TopBar.kt`、`ChatState.kt`、`LiveTranscript.kt`、`SessionDetailScreen.kt`、`VadeErrorCategory.kt`、`VadeErrorMappingTest.kt`、`ModelFailure.kt`、`ModulePageRenderer.kt`、`ChatThread.kt`、`UiState.kt`、`App.kt`）✓。**没有一条引用不存在的路径。**

### 1.2 内容级复核（抽样实读，不止于存在性）

| 断言 | 复核方式 | 结果 |
|---|---|---|
| MSI 93,312,713 B | `stat` 实测 | ✓ 字节数与台账一致，未变 |
| 亮/暗截图 md5 不同 | `md5sum` 实测 | ✓ `497a6a71…` vs `365825fa…` |
| APK 22,789,790 B | `stat` 实测 | ✗ **已变**：现为 22,915,908 B（2026-11-15 构建），台账已改 |
| A5-1 的 app-image 154.7 MB | `ls build/…/app` | **目录已不在**（g-clean 当时产物），台账已标注历史口径 |
| `gradle.properties:33` 版本单一来源 | 实读 | ✓ 行号仍准 |
| `ProfileScreen.kt:248` 关于页版本 | 实读 | ✗ 行号漂移：版本渲染在 `:169`，已改 |
| `ChatThread.kt:516` ApprovalCardRow | grep+实读 | ✗ 漂移到 `:588`，已改 |
| `ChatState.kt:450` submit | grep+实读 | ✗ 漂移到 `:557`，已改 |
| `SessionDetailScreen.kt:94` 自带 fixtures | grep+实读 | ✗ **断言本身已过期**（见 §2-#9），已改 |
| `VadeClient.kt:96/509`、`RealSessionState.kt`、`VadeTransport.kt:58-120`、`UiState.kt:88/161`、`ModulePageRenderer.kt:333`、`ModelFailure.kt:127`、`VadeErrorMappingTest.kt:18`、`pairing.py:152` | 实读 | ✓ 全部仍准（RealSessionState `:28`→`:31` 微调） |
| `VadeClient.kt:110-145` 是「覆盖率守卫测试」 | 实读 | ✗ 表述不准：该区间是 `VadeMethod` 枚举；守卫测试本体是 `VadeContractCoverageGuardTest.kt`（desktopTest，已找到），已改 |
| A6-2「QUICKSTART §4 记的 368 是旧数，勿引用」 | 实读 | ✗ 该注记自身过期：QUICKSTART §4 已于 2026-11-15 刷新为 1118/0/1，已改 |

### 1.3 三档数字复数

逐条清点 §三 的 21 条验收句：`[x]` 8 条（A1-1、A5-1/2/3、A6-1/2/4/5）、`[ ]` 13 条、未做 0 条。**8+13+0=21 ✓**。13 条未达成项逐条核对后**每条都有实质已交付部分**（无整条空悬），故「部分达成」13 条成立；无一 条需要降级为「未做」。

### 1.4 门禁四跑（本 worktree 原始输出）

| 门禁 | 结果 |
|---|---|
| `pytest core/tests -q`（全量，538.51s） | **1164 passed, 1 failed**——唯一红 `admin/test_config_usage.py::test_the_evolve_total_survives_more_than_one_reader_page` 为冷 worktree 首跑一次性抖动：随后单跑 passed、整文件连跑 3 次全 2 passed（未改未 skip；与 `s-modules-impl` §7 记录的既有抖动同族），基线仍按 1164 |
| `tools/check_contracts.py` | `contracts: OK`（34 错误码、各 RFC append green） |
| `tools/check_ledger.py` | `ledger: OK (16 RFC rows; rpc 1.6.0 48 methods in 3 directions, events 2.0.0 34 entries, 34 error codes, appends 11+8+3)` |
| `docs/check_docs.py` | **合计 12 项，失败 0 项**（改后复跑仍 12/12） |

## 2. 修掉的（每条含原文）

> 原则：**历史事实保留**（估算、当时口径的数字加「当时口径」标注）；**现状句**按今天的代码实况改写；未做的保持未做，一条都没翻成 `[x]`。`[x]`/`[ ]` 旗标总数与分条归属**零变化**。

| # | 位置 | 原文（摘） | 改为 | 依据 |
|---|---|---|---|---|
| 1 | 头部状态行 | 「台账已校准（2026-11-14）」 | 「台账已校准（2026-11-15，tmp/s-alpha-truth 棒）」+ 本次方法说明 | 本次校准 |
| 2 | §一 | 「契约覆盖率守卫测试（`VadeClient.kt:110-145`）」 | 「`VadeContractCoverageGuardTest.kt`（desktopTest）对运行时契约逐方法比对 `VadeMethod` 枚举（`VadeClient.kt:110` 起）」 | 守卫测试本体在 desktopTest；枚举区间只是被比对对象 |
| 3 | §一 | 「UI 数据层默认仍走 fixtures（`App.kt:42` 默认 `FakeSessionSource`）」 | 「默认仍走 fixtures（`App.kt` 的 `source` 形参默认 `FakeSessionSource()`）」 | 行号漂移（`fun App(` 在 `:27`，`source` 形参约 `:43`）；语义不变 |
| 4 | §一 | （无） | 补一句：模块页面已有真核 e2e 证据；**但聊天写侧仍没出网** | `g-module-page` 已合并；`RealSessionSource.submit` 生产调用点 0 |
| 5 | §二 | 「现为服务端 1089 passed、客户端 588 tests」 | 「服务端 1164 passed、客户端 1122 tests」 | LEDGER §1 + g-module-page 门禁（§1.4） |
| 6 | A1-2 | 「（`BrowserPairing` 只能由代码注入）」 | 补「桌面还可用 `VADE_PAIR_CODE` 环境变量注入配对码（`SourceConfig.kt:19`）」+ 注入点写具体 | `SourceConfig.kt:19`、`AppShell.kt:525` 实读；结论不变仍 `[ ]` |
| 7 | A1-3 | 「`RealSessionState.kt:28`」 | 「`RealSessionState.kt:31` 的 `Reconnecting`」+ TopBar 补「grep 零命中」佐证 | 实读；顶栏未绑是真（保持 `[ ]`） |
| 8 | A2-1 | 「缺：`ChatState.submit()` 只改本地 `items`、不调用 source（`ChatState.kt:450`），窗口里的『发送』没有出网」 | 行号改 `:557`；补「`RealSessionSource.submit` 封装在 `RealSessionSource.kt:293`，**生产调用点 0**；真源下 `startAssistantTurn` 直接 return，没有任何代码把输入发成 `ui/submit`——**真源下发送不出网**」 | grep 全 commonMain：`uiSubmit` 仅定义与协议封装两处；`startAssistantTurn` 首行 `if (isRealSource) return` |
| 9 | A2-2 | 「缺：`applyLive` 在 `composeApp/src` 内只有定义与测试调用，聊天页未接事件流」 | 「读侧已接（`g-chat-real` 已合并）：`ChatThread` 挂 `collectLiveEvents()`+`loadHistory()`（`ChatThread.kt:151`），delta 经 `applyLiveEvent` 逐字进 Running 行；缺：**触发端**——发送不出网（A2-1），用户可见的流式流程今天走不通」 | ChatThread `:151-152` 实读 + `shots/g-chat-real/RESULT.md`（25 条测试 + 变异，门禁 923/1/1） |
| 10 | A2-3 | 「缺：聊天页不读 `state.messages`（`SessionDetailScreen.kt:94` 仍 `ChatState(session.id, preset)` 自带 fixtures），重启可见无从验证」 | 「读侧已接——真源时 `resolveChatState`（`:115`、`:322`）用真 turns/uploader，`loadHistory()` 拉 attach 快照行种子；`RealCoreUiE2ETest` 断言真核行上屏、假数据 id/串零出现；缺：『重启客户端仍可见』的显式端到端未做（发送侧断，闭环无人走过）」 | `resolveChatState` 实读；e2e 断言实读（`FIXTURE_IDS.none`、fixture marker absent） |
| 11 | A2-4 | 「缺：同上未接进 `ChatState`，UI 无真中断可验」 | 「`TurnInterrupted` 已在投影器处理、真源下随事件流上屏；缺：`ui/interrupt` 生产出口挂在发送链路上（A2-1），真中断无从触发」 | `LiveTranscript.project()` 实读；`ui/interrupt` 无生产调用点 |
| 12 | A2-5 | 「缺：聊天回合的 `turn.failed` 未经真事件流上屏（仍停在 `LiveTranscript.failureOf` 侧信道）」 | 「`turn.failed` 现在经真事件流把行染 error 色（`onFailed` → `MessageState.Failed`，`ChatThread.kt:392`）；缺：失败**原因**（code/message）与四类文案未上屏（`failureOf` commonMain 无调用点）」 | `onFailed` 实读；`grep failureOf` 仅定义处 |
| 13 | A3-1 | 「缺：**模块清单拉不到**——`ui_to_core` 34 个方法里没有『列模块』……工具卡只能由 fixtures 驱动」 | 「**模块清单通道已解决**（RFC-0015 登记 rpc 1.6.0 第 11 块 + core 实现 + 客户端 codec/订阅/组合根接线）；模块页面已有真核 e2e 证据（`RealCoreModulePageE2ETest`）；缺：**生产三洞**（§七 B3）+ **`turn.tool_call` 在 `LiveTranscript.project()` 被刻意忽略（`else -> Unit`），真会话仍无工具卡**」 | `schema/rpc.json` 1.6.0 实读；`de5250f`/`85851e0`/`d90cb56` 合并记录；`project()` else 分支注释实读 |
| 14 | A3-2 | 「缺：离线状态无真来源（同 A3-1 的模块清单/状态缺失）」 | 「真源下 `RealModulePageSource.status()` 硬返 `online=false`（诚实的『无数据源』，`:68`），生产上**恒显示『模块离线』**，即使模块在线」 | `RealModulePageSource.kt:68` 实读 + g-module-page 缺口表③ |
| 15 | A3-3 | 「`ChatThread.kt:516`」 | 「`ChatThread.kt:588`」+ 补 `VadeMethod.APPROVAL_DECIDE` 位置；「UI 从未调用 `approval/decide`」结论**保留**（复核为真） | grep：`approvalDecide` 仅枚举与本地戳记 |
| 16 | A4-1 | 「缺：注入行——`App()` 没有 metaActions 参数（`App.kt:42` 只有 `source`），动作仍打在 fixtures 上」 | 「组合根结构已通（`adaptersOf` 单点派生，`AppShell.kt:236`）；组名显示已接（`metaGroupsOf` → 两处 SessionsScreen）；缺：**`SessionMetaHost` 仍是死代码**（`g-activate` 复核结论，至今无生产挂载点）→ 重命名/置顶/归档/删除在界面上没有入口」 | `g-activate`/`g-meta-groups` 合并记录 + `AppShell.kt:270-273` 实读 + grep `SessionMetaHost(` 零生产挂载 |
| 17 | A4-2 | 「缺：同上未接线」 | 「组名显示已接（`session/group.list`）；组编辑 UI 与 `MetaGroupEditReal` 均在；缺：同 A4-1——`SessionMetaHost` 未挂载，生产界面上组管理不可用」 | 同上 |
| 18 | A5-1 | 「APK **22,789,790 B**」；「免安装 app-image **154.7 MB**（`shots/g-clean/RESULT.md` §1）」 | APK 改 **22,915,908 B**（2026-11-15 实测）；app-image 标「当时产物；当前 build 目录已清理，历史记录」 | `stat` 实测；`ls build/…/app` 为空 |
| 19 | A5-2 | 「`ProfileScreen.kt:248`」 | 「`ProfileScreen.kt:169`（`版本 ${VadeBuildConfig.VERSION}`）」 | 行号漂移 |
| 20 | A6-1 | 「服务端全量测试全绿（当前基线 1089）……`check_ledger.py` → `ledger: OK (14 RFC rows … rpc 1.5.0 47 methods … appends 10+8+3)`」 | 基线 **1164**（LEDGER §1 出处）+ 本棒复跑 1164 passed/1 failed（抖动注记）+ ledger 现值 `16 rows / rpc 1.6.0 48 methods / appends 11+8+3` | §1.4 四跑 |
| 21 | A6-2 | 「当前基线 588……588 tests / 0 failures（校准时的父代实跑口径，本次未复跑）……`QUICKSTART.md` §4 记的 368 是旧数，勿引用」 | 基线 **1122**（`g-module-page` 门禁 files=107 逐 XML 求和 1122/0/1；QUICKSTART §4 同日早前 1118/0/1 并自注「以实跑为准」，原「勿引用」注记删除）+ 三目标绿同证 + **e2e 预算 180 s + 可 grep verdict** 补充（`CoreBootDiagnostics.kt:51`、`shots/g-e2e-budget2/RESULT.md`：满载 8 轮全绿、81 轮 972 次起核全 READY） | g-module-page RESULT、QUICKSTART §4、CoreBootDiagnostics 实读 |
| 22 | A6-4 | 「亮/暗两帧 md5 不同」 | 补实测值 `497a6a71…` vs `365825fa…`（2026-11-15） | md5sum |
| 23 | §五 W1 | 「协议 34/34 有类型封装」 | 「协议 **35/35**（RFC-0015 `ui/modules` 登记进 rpc 1.6.0 后）」 | rpc.json 实读 |
| 24 | §五 W2 | 「部分完成：会话列表 / 搜索 / `UiState` 读源已接；**聊天流与动作适配器未接**（§七 B2、B3）」 | 「会话列表 / 搜索 / 组名 / 活动页 / 模型页 / 聊天**读侧**（历史+实时事件流）/ 模块清单与页面渲染链路已接；**聊天写侧（`ui/submit`/`ui/interrupt`/`approval/decide` 出网）与元数据动作挂载未接**（§七 B1、B2）」 | §2-#9/#16 同源证据 |
| 25 | §五 W4 | 「**未开始**（依赖 W2 收口）」 | 「**进行中**：`g-live` 实跑手册 + 两条真核 e2e 已在门禁跑通；系统性人工联调未做」 | `shots/g-live/PLAYBOOK.md`、`RealCoreUiE2ETest`/`RealCoreModulePageE2ETest` |
| 26 | §七 表头 | 「**5 项阻断、8 项非阻断。**」 | 「**5 项阻断、7 项非阻断**（N2 已解决，保留编号防断链）」 | B1 解除−1、新 B3 洞+1 → 仍 5；N2 解决 → 7 |
| 27 | §七 B1（旧） | 「**A3 模块清单**：`ui_to_core` 34 个方法里没有『列模块』……需新增契约」 | 划掉标「**已解除**（2026-11-15）：RFC-0015 `ui/modules` 登记 + core 实现 + 客户端接线全通」 | 契约 1.6.0 + `de5250f` + g-module-page e2e |
| 28 | §七 B2（旧→新 B1） | 「**聊天真接线**：`ChatState` 未调 `applyLive`、`submit()` 不调 source，聊天页仍 `ChatFixtures` → 流式 / 落库恢复 / 中断 partial 全部停在投影器与测试层」 | 改写为「**聊天写侧**」：读侧已接（历史/实时/落库投影），缺的是 `ui/submit`/`ui/interrupt`/`approval/decide` 出网 | §2-#8~#12 |
| 29 | §七 B3（旧→新 B2） | 「`MetaActionsReal` / `TurnGatewayReal` / `BlobUploaderReal` / `ModelActionsReal` 已合并，但 `App()`/`UiState` 无注入点（`App.kt:42` 只有 `source`），改名/置顶/分组/模型切换/附件上传仍打在 fixtures 上（`g-adapter-bundle` 在做）」 | 改写为「**元数据动作挂载**」：适配器/工厂/`adaptersOf` 全在，`SessionMetaHost` 死代码待挂载；模型切换已接、附件上传挪到 N7（RFC-0016） | `g-activate`/`g-meta-groups`/`g-chat-real` 已合并；`resolveChatState` 的 uploader 已接真适配器 |
| 30 | §七 新 B3 | （无此条） | 新增「**模块页面生产三洞**」（首拉/页面体/在线态，渲染链路已通的注记） | `shots/g-module-page/RESULT.md` 缺口表 + 今日 grep 复核（`.load()`/`fetchPage` 生产调用点 0、`status()` 硬编码未变） |
| 31 | §七 N2 | 「**Web 字体**：wasm 首帧中文豆腐块，字体就绪后重绘」 | 划掉标「**已解决**（2026-11-15，`g-font-bundle2`）：CJK 字体（`vade-sans-sc.woff2`，OFL 许可随包）打进 wasm 包——首帧即正确、0 gstatic、断网逐字节相同」 | `2f97c5d`/`9aea6d3` + `shots/g-font-bundle2/RESULT.md` + `resources/fonts/OFL-VadeSansSC.txt` 在盘 |
| 32 | §七 N4 | 「ask_user 卡片未合并：实现完成（37 条测试）在 `tmp/c-askuser`，未进 `main`」 | 结论**保留**，补「2026-11-15 复核：commonMain 仍只有协议层 `AskUser`/`InputRequired` 模型，无卡片组件」 | grep 实证 |
| 33 | §七 N7 | 「**`ui/blob.token` RPC 不存在**：……与设计稿的方法名不一致（有真适配器，契约缺条目）」 | 结论**保留** + 补「**RFC-0016 已 proposed**（2026-11-15 用户已拍板四项：绑设备/600s 可重放/收紧读面），实施未排」 | 主仓 log `fd491d5`/`7941b93` |
| 34 | §七 校准依据行 | 「`check_ledger.py` → `ledger: OK (14 RFC rows; rpc 1.5.0 47 methods, …)`」 | 全部刷新为 2026-11-15 四跑原值（§1.4） | 本棒复跑 |
| 35 | `docs/status.md` U1 行 | 「**已核实**：368 tests / 0 failures……**未完成**：UI 数据仍是 fixtures（`w-wire` 接线中）；ask_user 卡片未合并；模块页宿主（`g-module`）未落地」 | 刷新为 2026-11-15 复核值（1122/三目标/APK 新字节），「未完成」改为真实剩余项（聊天写侧 / SessionMetaHost / 模块三洞 / ask_user / 配对引导）；旧数字标「当时口径」 | U1 行的三处「未完成」与今日代码直接矛盾（w-wire、g-module 早已合并；fixtures 已非现状） |

## 3. 待核的（本棒无法闭环，列给父代）

| # | 事项 | 为什么待核 |
|---|---|---|
| 1 | A6-5 的「两台设备 `logcat -d -b crash` 中 `vade.ui` 命中 0 条」 | 引用的是 `shots/c-inset/RESULT.md` 当时记录；本棒未重建设备环境重放。文件在盘、报告自洽，采信为**当时口径** |
| 2 | A5-2 的「7 条测试含 `runComposeUiTest` 真渲染」（g-pack §3–4）、A5-3 的 23,721 zip 条目扫描 | 同上——历史报告在盘，未重放；版本单一来源与 `VadeBuildConfig` 渲染链路本棒已实读证实 |
| 3 | 客户端 1122 的门禁数字采信 `g-module-page` RESULT（其分支门禁 + 合并 `1112fff`），本棒**未重跑**客户端全量 | 客户端仓是共享单点、并行期间跑 gradle 会与在跑棒互抢（`vade-dev-loop` §八）：`gradle build/` 非并发安全。下一位复验者自己跑一次 `desktopTest --rerun` 即可闭环 |
| 4 | W4「系统性人工联调」的完成度 | `g-live/PLAYBOOK.md` 与两条 e2e 只证明「链路可跑通」；人工三端全链路联调无记录可查，标进行中 |
| 5 | 一次性抖动 `test_the_evolve_total_survives_more_than_one_reader_page` 的根因 | 本棒只做了定性（首跑红、单跑+3 连跑绿，冷 worktree 现象）；未挖根因（属代码棒范围，本棒禁改代码） |

## 4. 红线自查

- `git diff --name-only`：仅 `docs/ALPHA.md`、`docs/status.md`、`shots/s-alpha-truth/**` ✓
- 未改任何代码 / 契约 / 检查脚本；`docs/check_docs.py` 改后复跑 12/12 ✓
- 无 push、无 merge、无 amend/rebase/reset ✓
- `[x]` 旗标 8 条、`[ ]` 旗标 13 条，与改前完全一致——**没有任何未完成项被写成已完成** ✓
