# U1 · UI 骨架 + `material` 皮肤（G3）

| 项 | 值 |
|---|---|
| Agent 代号 | `u1-ui` |
| 波次 | W4（与 C4 / C5a 并行） |
| 决策基线 | **D87–D105（AMEND-22）**：4 个一级目的地、桌面双栏 + 可折叠右面板、C-09 扩到 11 个组件、模块页面走原生 schema（C-19 待冻结） |
| 依赖 | C0a 的 Kotlin 绑定、C0b 的 W-A/W-B 结论、C3 的事件表（C-06）、C4 的协议 API、**RFC-0003（注入分层，U1-0）** |
| 目录所有权 | `ui/shared/`、`ui/composeApp/src/commonMain/kotlin/core/`、`skin/material/`、`ui/composeApp/src/*Main/` |
| 产出 | Android / Desktop / Web 三端可聊天 + `material` 皮肤 + `Skin` 接口 + 4 个一级目的地骨架 + 模块页面外壳 |
| 出口 | **G3**；**C-09 `Skin.kt` 第 3 天冻结**、**C-10 `ProtocolClient.kt` 周末冻结** |

## 一、一句话任务

把 core 的事件流变成一个能用的聊天客户端：**4 个一级目的地（会话 / 活动 / 能力 / 我的）**、三端同源、断线能续、皮肤可换；并且把「皮肤」定成一个**组件工厂接口**，让 U2/U3 能在不碰业务页面的前提下接入。

## 二、决策基线

`docs/requirements.md` §8.22（AMEND-22）把 2026-11-14 的 19 条裁定落成 **D87–D105**：8 组修订 + 2 条新增（FR-8.78 / FR-8.79）。下表是这 19 条在 U1 里的施工口径——**冲突时以需求文档为准**，理由与备选见 `research/ui-decisions.md`，本节不复述。

| # | 议题 | 决定（决策号） | 落在哪 |
|---|---|---|---|
| 1 | 一级目的地 | **4 个**：会话 / 活动 / 能力 / 我的；活动页承载运行中 turn、待审批（带 badge）、最近失败（D87） | U1-a（见 2.1） |
| 2 | 手机会话详情 | **隐藏底栏**，全屏 + 顶栏返回；输入区 / IME / 浮动胶囊不叠压（D88） | U1-a / U1-b |
| 3 | 桌面布局 | **双栏（列表 + 详情）+ 可折叠右面板**（默认收起，`⌘\` 展开）；面板样式可自定义；可承载模块页面（D89） | U1-a / U1-h |
| 4 | 新建会话 | 列表顶栏右侧 **✎** + 桌面 **`⌘N`**；顶栏**可做成浮动胶囊式**（D90） | U1-a |
| 5 | 设置入口 | 手机「我的」tab；桌面 rail 底部头像 + **`⌘,`**；设置为全屏子页 / 独立窗口（D91） | U1-f |
| 6 | 消息形态 | 助手**全宽（≤760dp）** + 用户**右对齐气泡**（D92） | U1-b（`MessageBubble`） |
| 7 | C-09 组件数 | **7 → 11**：+`MessageBubble` / `AttachmentChip` / `ToolCallCard` / `InputBar`；**必须在冻结前落地**（D93） | U1-a（T2） |
| 8 | 工具卡默认态 | 成功折叠 / 失败自动展开 / `risk=high` 审批永远展开（D94） | U1-b（`ToolCallCard`） |
| 9 | 附件通道 | 独立 HTTPS **`POST /ui/blob`**（设备密钥鉴权、分片、断点续传）；附件字节不进 JSON-RPC 帧（D95） | U1-b（等 RFC-0004） |
| 10 | 图片存储 | **只存压缩图**（长边 2048 / ≤5MB）；UI 明示「原图未保留」；core 只存一个 `file_id`（D96） | U1-b |
| 11 | 语音转写 | **端上转写**，音频不出设备；桌面无系统 STT 时**隐藏**麦克风按钮（不报错）（D97） | U1-b / U1-g |
| 12 | UI 状态归属 | **真相只存 core**；客户端只持可丢弃派生状态（草稿 / 搜索索引 / 皮肤偏好）（D98） | U1-b / c / f |
| 13 | 技能管理 | **客户端原生技能页**，复用 FR-9.40 三步；WebView 连 admin 强制 Bearer（D99） | U1-e |
| 14 | Web 写权限 | **三端同源、可写**；补 CSRF / XSS 防护与安全评审；`READ_ONLY_SESSION` 只给显式只读会话（D100，废止 D76） | U1-g |
| 15 | 模型选择 | **会话级显式选择 > 全局默认 > `manifest.priority`**；列表按能力标签过滤（D101） | U1-f |
| 16 | 用量口径 | **两者并存**：审计按模块归因 + 会话累计与预算；同一份 usage 两种视图、可对账（D102） | U1-f |
| 17 | ask-user 状态 | 新增 **`waiting_input`**（C-06 `turn_states` 改枚举），走 RFC-0008（D103） | U1-d |
| 18 | 模块页面宿主 | manifest `ui.pages[]` + **原生声明式 schema**（4 原语、C-09 子集、3 类动作、禁脚本）；**新契约 C-19**（D104） | U1-h |
| 19 | 手机模块页入口 | 「能力」tab 内的「模块页面」入口；模块无页面则**不显示**入口（D105） | U1-h |

### 2.1 页面与导航

**四个一级目的地**（D87；三端语义与顺序一致，FR-8.77，不得出现第 5 个）：

| 目的地 | 承载 | U1 的硬约束 |
|---|---|---|
| **会话** | 会话列表（置顶 > 今天 > 昨天 > 7 天内 > 更早，组内按 `updated_at` 倒序）→ 会话详情 | 只有「列表 → 详情」两级；搜索是列表内覆盖层、不占一级；列表项动词集合三端一致（FR-8.26） |
| **活动** | 运行中 turn、待审批（badge）、最近失败、后台任务、子代理 | 审批跨端一致：弹窗关闭**只认** `turn.approval_resolved`（FR-8.53） |
| **能力** | 模块 / 技能 / 记忆；手机端「模块页面」入口在这里 | 模块无页面时不显示入口（FR-8.79）；技能页是**原生页**（D99） |
| **我的** | 外观（皮肤 × 底栏 × 明暗）、模型、设备、通知、关于 | 9 组合实时预览（FR-8.67）；设备吊销（FR-8.68）；E2E 标识双通道（FR-8.69） |

**四档断点**（FR-8.76；`≥1200dp` 的「三栏」= 双栏 + 右面板展开态，不是第三种布局）：

| 档位 | 容器 | 本任务要落的 |
|---|---|---|
| **<600dp** | 底栏 4 tab、单栏 | 详情隐藏底栏 + 顶栏返回；键盘弹起底栏隐藏、输入区贴 IME；返回按层级回退（FR-8.72） |
| **600–839dp** | NavigationRail 80dp；≥720dp 可「列表 + 详情」 | rail 项 = 4 个一级目的地。`miuix + floating` 在此档**保持底栏**（FR-8.20 例外，U2 验收，U1 不改语义） |
| **840–1199dp** | rail 72dp + 列表 300dp + 详情 | 真双栏；`⌘B` 折叠列表 |
| **≥1200dp** | 双栏 + 右面板展开态 | 右面板默认收起、`⌘\` 切换、宽度档位与承载组件可配、可放模块页面 |

- **桌面**：双栏 = 列表 + 详情；右面板收起时各断点的容器映射不变；开合不改变会话滚动位置。最小窗口 **960×640**，低于此降级单栏。快捷键：`⌘N` 新建、`⌘K` 命令面板、`⌘1–4` 切目的地、`⌘B` 折叠列表、`⌘,` 设置、`⌘\` 右面板、`Esc` 关覆盖层（FR-8.74）。
- **新建**：列表顶栏右侧 ✎ + 桌面 `⌘N`；**不做**底栏中央 FAB。顶栏允许做成**浮动胶囊式**——顶栏不是 C-09 组件，用 `Skin.Card` + `Skin.Button` 在 `shell/TopBar.kt` 里拼出「贴顶 / 胶囊」两形态，**不得**为此加第 12 个组件。
- **设置**：手机 = 「我的」tab；桌面 = rail 底部头像 + `⌘,`。打开后是全屏子页或独立窗口，**不是**弹层 / 抽屉 / Sheet。
- **消息形态**：助手全宽、正文最大 760dp；用户消息右对齐气泡；两态都由 `MessageBubble` 渲染（D92）。
- **模块页面**：桌面入口在右面板顶部；手机入口在「能力」tab 内（列表项 = 模块 + 页面标题）。一级目的地仍是 4 个。

## 三、边界

| 做 | 不做 |
|---|---|
| KMP 工程、三端 target、共享状态机 | 不做 `miuix` / `glass` 皮肤（U2/U3）；**不做三皮肤视觉精修**，U1 只交 `material` |
| `Skin` 接口 + `material` 实现 | 不做 iOS（U4）；不写任何 `iosMain` 的 `actual` |
| 4 个一级目的地骨架：会话 / 活动 / 能力 / 我的 | 不做配置页本体（C5a/C5b 的 `admin-ui/dist`） |
| 会话列表 + 详情 + 搜索（手机覆盖层 / 桌面 `⌘K`） | 不做消息树 / 版本翻页 / 完整分叉（只留位置，不做假功能） |
| 活动页 + 审批 + ask-user + 子代理 / 任务面板（协议未批时只做静态骨架） | 不做模块页面的 WebView 渲染（模块页一律原生 schema；WebView 只给 admin 配置页） |
| 能力页（模块 / 技能 / 记忆）+ 我的页（外观 / 模型 / 设备 / 通知 / 关于） | 不做第 5 个一级目的地（FR-8.77） |
| **模块页面宿主（原生声明式 schema，U1-h）** | 不执行模块提供的任意脚本 / 表达式（C-19） |
| **管理页 WebView 宿主 + 原生桥（T5b）** | 不写管理页的 React 代码 |
| 协议客户端封装（Flow） | 不改协议、不改 Rust、不改 `schema/` |

## 四、必读

- `docs/design.md` §9 全节（分层、Skin、三皮肤矩阵、Wasm 绑定）
- `docs/design.md` §7.10–7.12（WebView 约束、设备绑定协议）——T5b 必读
- `docs/requirements.md` FR-8.1–8.24（接入、外观、WebView 宿主）、**FR-8.26–8.79**（§8.21 客户端功能全集 + §8.22 AMEND-22 的 19 条裁定与修订）、A10/A11、AC-10–AC-15
- `docs/requirements.md` §8.22.4（C-09 修订 7 → 11）、§8.22.5（契约与 RFC 缺口）；`research/ui-decisions.md`（19 条的权威记录与理由）
- `docs/rfc/RFC-0003-prompt-injection-layers.md`（U1-0 的依据；状态 proposed，未批准前 U1-0 不开工）
- **RFC-0004（附件与多模态）/ RFC-0005（模型与用量）/ RFC-0007（子代理与后台任务）/ RFC-0008（交互式输入）**：按编号跟踪状态，未批准时只做降级路径（见 §九 风险）
- `schema/events.json`（C3）、`schema/rpc.json`（C2）、`docs/agents/01-contracts.md`（C-09 / C-10 的冻结时点与所有权）
- `docs/adr/ADR-001-web-protocol-binding.md`（C0b）：决定 Web 走 W-A 还是 W-B

## 五、批次与并行边界

U1 内部切成 **9 个交付切片**：`U1-0` + `U1-a` … `U1-h`。

> **这些不是 `status.md` 的批次行。** C4 的批次台账仍是 **23 个批次**、U1 仍是一行（出口 G3）；下表只是本任务书内的并行划分与验收单元。不要在 `status.md` / `00-orchestration.md` 的批次表里加行。

| 切片 | 范围 | 依赖 | 出口判据 |
|---|---|---|---|
| **U1-0** | 注入改造（**无 UI**）：技能目录消息化、工具冷热分离（`tools/search`）、stable / context 分段；默认 `prompt.injection=legacy` 可回退 | **RFC-0003 批准**（现 proposed）；目录与 C3b 交叉，开工前由编排者确认所有权 | 开关两侧都有测试；`legacy` 下旧行为不变；**不碰 `schema/`**（RFC 批准后才动 C-11 / C-05 / C-06 / C-18） |
| **U1-a** | 4 目的地骨架 + 四档断点 + 会话列表 + 新建（✎ / `⌘N`）+ 右面板壳 + **C-09 11 组件冻结** | 无（本批是其余切片的地基）；C3 事件表 | 三端一级目的地均为 4 个；底栏三态在 360dp 正确；右面板开合不丢滚动；`Skin.kt` 11 组件 + sha256 落档 |
| **U1-b** | 会话详情：消息流、Markdown、输入区、工具卡、附件条、图片全屏、停止 / 队列 | U1-a；C-09 组件 8–11；**RFC-0004（附件）** | 交互 1 / 2 / 5 通过（§8.2）；流式帧时间 P95 <24ms；失败工具卡自动展开 |
| **U1-c** | 搜索：手机全屏覆盖层 / 桌面 `⌘K` 命令面板 | U1-a；`session/search` 缺失时降级本地索引并标注「范围：已缓存」 | 标题命中高亮；从结果进会话再返回，搜索词与列表滚动位置保留 |
| **U1-d** | 活动页 + 审批 + ask-user + 子代理 + 任务面板 | U1-a；**RFC-0007 / RFC-0008** | 审批 60s 倒计时且只认 `turn.approval_resolved`；四类输入控件；任务进度 ≤2 次/秒；未批时只交静态骨架 |
| **U1-e** | 能力页（模块 / 技能 / 记忆） | U1-a；C5a admin API；C-06 `module.status` | 技能启停与审核复用 FR-9.40；记忆删除二次确认；模块卡片与 `module.status` 一致 |
| **U1-f** | 我的页（外观 / 模型 / 设备 / 通知 / 关于） | U1-a；**RFC-0005（模型与用量）** | 皮肤 × 底栏 × 明暗 9 组合实时预览；模型能力徽标；设备吊销；E2E 标识双通道 |
| **U1-g** | 无障碍 + 三端收口 | U1-a … U1-f；D100 的 CSRF 写路径 | 200% 字号不裁切；TalkBack 顺序；六个桌面快捷键；Web 写路径 + 防护提示（§8.4） |
| **U1-h** | **模块页面**（原生 schema 渲染）+ 桌面右面板承载 | U1-a；**新契约 C-19（待冻结）** | 4 原语 + C-09 子集 + 3 类动作；含脚本 / 未知动作 → `MANIFEST_INVALID` 拒渲染；无页面不显示入口；手机入口在「能力」tab |

**并行边界**（违反这条就会互相改文件）：

- **U1-a 必须先落地**——它是骨架，其余 7 个切片都从它的目录与 `Skin.kt` 出发。
- **U1-0 与 U1-a 可并行**：U1-0 不碰 `ui/`（注入改造在 core 的 prompt 层），U1-a 不碰 core。
- **U1-b / U1-c / U1-e / U1-f 在 U1-a 合入后 4 槽并行**；U1-b 最大，先开。
- **U1-d 等 RFC-0007 / RFC-0008 批准**；未批只做静态骨架与空态（见 §九）。
- **U1-h 等 C-19 冻结**；未冻结只做壳：入口、空态、拒渲染路径。
- **U1-g 最后做**：其余切片的功能面稳定后才收口无障碍与三端差异，它是 G3 的最后一道。
- **公共文件独占**：`Skin.kt`、`ProtocolClient.kt`、构建文件、`shell/` 由 U1-a 独占；后开的切片**不改**这些文件，需要改动就提 RFC/找 U1-a。

## 六、工程结构

```
ui/
├── settings.gradle.kts
├── shared/                       # KMP commonMain
│   └── src/commonMain/kotlin/
│       ├── state/SessionStore.kt  TurnQueueView.kt  ReconnectPolicy.kt
│       └── api/ProtocolClient.kt  Models.kt
├── composeApp/
│   └── src/
│       ├── commonMain/kotlin/
│       │   ├── core/screens/     # 业务页面，只准调 Skin
│       │   ├── core/destinations/ # U1-a：会话 / 活动 / 能力 / 我的 四个目的地
│       │   ├── core/modulepage/  # U1-h：C-19 schema 解析 + 4 原语渲染（禁脚本）
│       │   ├── skin/Skin.kt      # C-09
│       │   ├── skin/material/
│       │   ├── shell/BottomBar.kt
│       │   ├── shell/TopBar.kt   # 贴顶 / 浮动胶囊两形态（拼 Skin 原语，不进 C-09）
│       │   └── adminweb/         # T5b：expect 声明（必须在 commonMain 内）
│       │       ├── AdminWebView.kt   # expect fun AdminWebView(core, ...)
│       │       ├── DeviceKey.kt      # expect class DeviceKey（生成/签名/导出公钥）
│       │       └── Bridge.kt         # 三方法接口 + payload 模板校验（纯 Kotlin）
│       ├── androidMain/kotlin/.../adminweb/  # actual：WebView + Keystore/StrongBox
│       ├── desktopMain/kotlin/.../adminweb/  # actual：JCEF/WebView + 软件密钥
│       └── wasmJsMain/kotlin/.../adminweb/   # 只读，不生成密钥
├── admin-ui-dist/                # 构建期从 core 仓库同步进来的 admin-ui/dist
│                                 # 用 Gradle task 拷进各端 assets，不手改
└── ios/                          # U4 所有，U1 只留目录
```

**`admin-ui-dist` 的来源**：core 仓库的 `admin-ui/dist` 是构建产物且提交进仓库（C5a 保证）。U1 用一条 Gradle task（`syncAdminUiDist`）把它拷到 `androidMain/assets/admin/` 与 iOS bundle resources。**不要 fork 一份前端代码到 UI 仓库**——两份就会分叉，改了一边另一边不知道。CI 校验：`admin-ui-dist` 的 `sha256` 与 core 仓库 `dist` 一致。

## 七、任务分解

> 每个 T 后面标了它属于哪个切片（§五）。顺序仍是 T1 → T7；切片只是交付批次，不是新的编号体系。

### T1 · 工程骨架（1 天）· U1-a

- `shared`：`androidTarget`、`jvm("desktop")`、`wasmJs`（iOS target 留给 U4 加，避免 U1 被 Xcode 卡住）。
- `composeApp`：同上三 target；Android `minSdk 26`；Desktop JVM 17；Wasm 用 `wasmJs { browser() }`。
- 依赖：Compose MP、Kotlin 协程、kotlinx-serialization、`vade-protocol` 的 Kotlin 绑定（C1 产物）。
- **替 U2/U3 预留依赖**：按 C0c 的 `versions.lock.md` 把 `miuix` 与 `haze` 加进 `composeApp` 的 commonMain 依赖（版本锁死）。U2/U3 只写代码、**不改构建文件**；构建文件由你独占。
- **Web 路由**：读 ADR-001。W-A → 在 `wasmJsMain` 里 `external` 调 JS 包；W-B → 在 `wasmJsMain` 里用 TS/JS 实现 `ProtocolClient`。两条路的公共接口都是 C-10。

### T2 · `Skin` 接口（第 3 天必须冻结）· U1-a

`ui/composeApp/src/commonMain/kotlin/skin/Skin.kt`：

```kotlin
interface Skin {
    val id: String                       // "material" | "miuix" | "glass"
    val colors: ColorScheme
    val shapes: ShapeScheme
    val typography: TypeScale
    val motion: MotionSpec

    @Composable fun Button(text: String, onClick: () -> Unit, style: BtnStyle, enabled: Boolean = true)
    @Composable fun TextField(value: String, onValueChange: (String) -> Unit, placeholder: String? = null, error: String? = null)
    @Composable fun Card(modifier: Modifier = Modifier, content: @Composable () -> Unit)
    @Composable fun ListItem(leading: @Composable (() -> Unit)?, title: String, subtitle: String? = null, trailing: @Composable (() -> Unit)? = null, onClick: (() -> Unit)? = null)
    @Composable fun Switch(checked: Boolean, onCheckedChange: (Boolean) -> Unit)
    @Composable fun BottomBar(items: List<NavItem>, selected: Int, onSelect: (Int) -> Unit, style: BottomBarStyle)
    @Composable fun ApprovalDialog(request: ApprovalRequest, remainingSec: Int, onDecide: (Decision, Scope) -> Unit)

    // AMEND-22 / D93 新增的 4 个：冻结前必须一起进 C-09，缺一个 U2/U3 就要返工
    @Composable fun MessageBubble(message: MessageView, role: BubbleRole, maxWidth: Dp = 760.dp,
                                  actions: List<MessageAction> = emptyList(), onAction: (MessageAction) -> Unit = {})
    @Composable fun AttachmentChip(attachment: AttachmentView, onRetry: (() -> Unit)? = null, onRemove: (() -> Unit)? = null)
    @Composable fun ToolCallCard(call: ToolCallView, state: ToolCardState, onToggle: () -> Unit = {})
    @Composable fun InputBar(value: String, onValueChange: (String) -> Unit, mode: InputMode,
                             onSend: () -> Unit, onStop: () -> Unit, attachments: (@Composable () -> Unit)? = null)
}
```

- `BtnStyle`：`Primary / Secondary / Danger / Text`。
- `BottomBarStyle`：`Fixed / Floating / FloatingBlur`。
- **共 11 个组件**（AMEND-22 / D93：原有 7 个 + 下表的 8–11）；冻结后不得再加第 12 个（加就破坏 U2/U3 的并行边界），确实需要就提 RFC。
- **新增 4 个只要求「默认 / 禁用 / 错误」三态可渲染**，不要求全状态矩阵（hover / pressed / 加载 / 超长文本 / 深色逐项由 U2/U3 补）；`material` 也按这三态交付，不要在 U1 里把状态铺满。
- 冻结后只能加带默认值的可选参数。

| # | 组件 | 状态 | 皮肤职责 |
|---|---|---|---|
| 1–7 | `Button` / `TextField` / `Card` / `ListItem` / `Switch` / `BottomBar` / `ApprovalDialog` | 原冻结集 | 已有实现，语义不动 |
| 8 | `MessageBubble` | 新增 | 角色（user / assistant / system）、parts、正文最大宽度、右对齐气泡、「已压缩 / 模型未读取」角标、消息级动作条（D92） |
| 9 | `AttachmentChip` | 新增 | `file_id`、mime、状态：校验中 / 上传中 x% / 已上传 / 失败、重试与删除入口 |
| 10 | `ToolCallCard` | 新增 | **三态**：成功折叠 / 失败自动展开 / `risk=high` 审批永远展开；字段见 FR-8.51（D94） |
| 11 | `InputBar` | 新增 | 多行高度、发送 / 停止 / 取消排队、附件条插槽、语音按钮的可用态 |

**必须同时产出的注册表**（否则 U2/U3 的皮肤永远选不中）：

```kotlin
// skin/SkinRegistry.kt —— 你写，U2/U3 往里注册
object SkinRegistry {
    private val impls = mutableMapOf<String, () -> Skin>()
    private val placeholders = mutableMapOf<String, Skin>()   // 未安装时的兜底
    fun register(id: String, factory: () -> Skin) { impls[id] = factory }
    fun get(id: String): Skin = impls[id]?.invoke() ?: placeholders[id]
        ?: error("skin '$id' not installed and has no placeholder")
    fun installed(): Set<String> = impls.keys
}
```

- 你在 T2 里注册 `material` 的真实实现，并为 `miuix` / `glass` 提供**占位实现**（返回 material 的 token，按钮上打 `[miuix 未安装]` 角标）。
- U2/U3 各自在自己的包里调 `SkinRegistry.register("miuix") { MiuixSkin() }`，**不改你的文件**。没有这个 seam，U2/U3 被禁改业务页面又拿不到注册点，皮肤就是死代码。
- 设置页的皮肤选择器读 `SkinRegistry.installed()`，只列真装了的；未装的显示「安装」。

**防回退测试**（必须有，且必须真的会失败）：

```kotlin
@Test fun businessScreensDoNotImportMaterial3() {
    val files = File("src/commonMain/kotlin/core/screens").walkTopDown().filter { it.extension == "kt" }
    files.forEach { f ->
        assertFalse(f.readText().contains("androidx.compose.material3."), "${f.name} imports material3")
    }
}
```

### T3 · 共享状态机（2 天）· U1-a / U1-b

```
state/SessionStore.kt
  - apply(event): 按 seq 去重、排序；delta 追加；tool_call 建卡片；completed 收尾
  - replace(snapshot): 全量替换（attach 不覆盖时）
  - messages: StateFlow<List<Message>>
state/TurnQueueView.kt
  - running: Turn?; queued: List<Turn>; pendingApprovals: Map<String, ApprovalRequest>
state/ReconnectPolicy.kt
  - delays = [0.5,1,2,4,8,16,30]s ± 20% jitter; 成功后重置
```

规则：
- **UI 不推导状态**。`turn.state` 由 core 给，UI 只渲染。
- 客户端只持**可丢弃派生状态**（草稿 / 搜索索引 / 皮肤偏好 / 缩略图缓存）：清掉不影响正确性，也不得用它反推 core 记录（D98）。
- 同一 `seq` 重复到达直接丢弃；`seq` 跳跃（缺失）→ 触发一次 `attach(lastSeq)` 补齐，不要自己猜。
- `delta` 按 `turn_id` 累加；收到 `completed` 后冻结该 turn 的文本（防迟到的 delta 改了已完成的回复）。
- 审批：收到 `turn.approval_required` 入 `pendingApprovals`；收到 **`turn.approval_resolved`** 移除并关弹窗（多端一致，这是唯一关闭依据）。
- **导出行为契约向量**：把上述规则做成 `shared/src/commonTest/resources/state-vectors/*.json`（输入事件序列 → 期望的 messages/queue/approvals）。U4 若走 Swift 重写路径，必须用同一组向量验收。

### T4 · 协议客户端封装（2 天）· U1-a / U1-b

`api/ProtocolClient.kt`（C-10）：

```kotlin
interface ProtocolClient {
    suspend fun connect(cfg: ClientConfig): ConnectResult
    suspend fun attach(sessionId: String?, lastSeq: Long?): AttachResult
    suspend fun submit(sessionId: String, text: String, clientMsgId: String): SubmitResult
    suspend fun interrupt(sessionId: String, turnId: String)
    suspend fun cancelQueued(sessionId: String, turnId: String)
    suspend fun decide(requestId: String, decision: Decision, scope: Scope)
    suspend fun close()
    val events: Flow<ServerEvent>
}
```

- `ConnectResult` 要能区分失败原因：`TlsPinMismatch / AuthFailed / ProtocolMismatch / Timeout / Unreachable`，UI 给不同文案。
- 鉴权：
  - Android/Desktop：`device_key`，密钥存平台安全存储（Android Keystore；Desktop v1 用 `0600` 文件，README 标注这是妥协）；
  - Web：`browser_session`，不存私钥。首次打开显示「输入 8 位访问码」→ `POST /ui/session {pair_code, client_name}` → core 发 `__Host-ac_ui` cookie；WS 升级自动带 cookie（`withCredentials`），**这条通道没有 Noise，只有 TLS**，UI 上必须明确标注空心锁。
  - 访问码输入框要做的四件事：① 自动补 `-`（`青松A7K2M9` → `青松-A7K2M9`）；② `NFKC` 归一化后再提交（全角输入、大小写混输都要能过）；③ 粘贴时清理首尾空格与零宽字符；④ 失败提示分两种——`CODE_INVALID`（码错/已用）与 `CODE_EXPIRED`（超 180s），不要笼统说「配对失败」。
  - **不要自己实现码校验**，归一化以 C-15 `schema/pair-code.json` 为准，客户端只做展示层清理，判定一律在 core。
- 连接状态：`Disconnected / Connecting / Connected / Reconnecting`，暴露 `StateFlow` 给页面顶部状态条。

### T5 · 页面（2 天）· U1-a … U1-f、U1-h（T5c）

> T5b（设备绑定与原生桥，2 天）见下方，与本任务并行。

| 页面（目的地） | 内容 | 切片 |
|---|---|---|
| **会话 · 列表** | 新建（顶栏 ✎ / 桌面 `⌘N`）、切换、最近消息摘要、时间桶分组（置顶 > 今天 > 昨天 > 7 天内 > 更早）、列表项动词（置顶 / 重命名 / 归档 / 删除 / 导出 / 标记未读） | U1-a |
| **会话 · 详情（聊天页）** | 消息列表（用户 / 助手 / 工具卡片）、流式文本、输入框、发送 / 停止、队列条；手机隐藏底栏 + 顶栏返回 | U1-b |
| **会话 · 搜索** | 手机全屏覆盖层、桌面 `⌘K` 命令面板、结果高亮与跳转定位 | U1-c |
| 工具卡片 | 工具名、状态（运行 / 成功 / 失败 / 等待审批）、可折叠参数与结果摘要 | U1-b |
| 审批弹窗 | **完整参数**（等宽字体、可滚动）、risk 标识、倒计时、`仅本次` / `本会话` / `拒绝` | U1-d |
| **活动** | 运行中 turn、待审批（badge）、最近失败、后台任务、子代理；无记录时空态不空白 | U1-d |
| **能力** | 模块 / 技能 / 记忆；手机端「模块页面」入口（列表项 = 模块 + 页面标题） | U1-e / U1-h |
| **我的** | 皮肤选择（3 个）、底栏样式（3 个）、连接信息（地址、指纹、状态）、模型能力徽标、设备与通知、**「管理此 Core」入口**（见下） | U1-f |
| **模块页面** | C-19 声明式 schema 渲染：4 原语 + C-09 子集，**禁脚本** | U1-h |

- 所有控件走 `Skin.*`；页面里**不得**出现 `material3`。
- 语音：**端上转写**，音频不出设备；桌面无系统 STT 时**隐藏**麦克风按钮（不显示禁用态、不弹提示、不写错误日志，D97）。
- 皮肤/底栏切换**运行时生效**，用 `CompositionLocal` 提供当前 `Skin`，切换只换 Local，不重建 Activity/View 树。
- 空态：无模块时显示「core 已连接，但没有可用能力」，不是空白页；无会话时给新建引导；活动无记录时给「暂无活动」+ 说明。
- 「我的」里的设置项打开为**全屏子页 / 独立窗口**（D91），不做弹层；桌面另有 rail 底部头像与 `⌘,` 两个入口。

**「管理此 Core」入口（AMEND-17 重写，覆盖 AMEND-16 版本）**

v1.14 写的是「唤起系统浏览器」。那条被推翻了：浏览器拿不到第二个因子，只能单密码；而且手机上根本没有 SSH，非回环场景等于不可用。现在改成 **App 内 WebView**，系统浏览器降为只读备用。

- 设置页底部一个按钮 + 一行说明「管理此 Core（模块治理、技能、记忆）」。点击 → 打开**内置 WebView 页**，不是外部浏览器。
- **WebView 只加载 App 内置资源**：`app://admin/index.html`（iOS 自定义 scheme）/ `file:///android_asset/admin/index.html`（Android）。资源就是 C5a 构建出的 `admin-ui/dist`，**构建期打进 App 包**（不是运行时下载）。
- **绝不 `loadUrl("https://...")`**。release 构建里搜不到任何以 `http` 开头的 `loadUrl`，这条进 R3 检查清单。
- **注入的桥，只有三个方法**（名字固定，C5a 的前端按这个写）：

```ts
interface ACBridge {
  getDeviceInfo(): Promise<{deviceId: string; fp8: string; alg: "p256"|"ed25519"; hardwareBacked: boolean}>;
  sign(payload: string): Promise<{signature: string}>;   // 首次必须弹原生确认
  secureStore(op: "get"|"set"|"del", key: string, value?: string): Promise<string | null>;
}
// 另外注入只读上下文，不可被 JS 改写
window.__AC_HOST__ = {mode: "webview", core: {host, port, fp8}};
```

- `sign()` 的原生侧**先校验 payload 模板**：必须匹配 `^vade-admin-v2\n` + 32 字节 base64 + `\n` + 已知 origin，长度与格式都对才签。JS 传任意字符串一律拒绝——否则页面里一个 XSS 就能签任何东西。
- `mode` 决定前端行为：`webview` → 可写；`browser` → **管理面**只读（Wasm 端与系统浏览器打开管理页都是这个值）。**别把这条套到聊天面上**：D100 之后数据面（`__Host-ac_ui`）三端同源、可写，管理面（`__Host-ac_admin`）的 cookie 仍只读——两者不是同一个 cookie。
- **未绑定时**：WebView 打开的是一个原生「添加服务器」页（**扫码在原生层做，不在 WebView 里**），扫码 → 核对指纹 → 设密码 → 绑定完成 → 再进 WebView。绑定协议见方案 §7.12，实现挂在 U1 的 T5b。
- **禁止**：
  - 在模块里缓存管理面 token 或代填密码（token 由 WebView 页自己拿，原生只提供**签名能力**）；
  - 用 `shouldInterceptRequest` / `WebViewClient` 把 `/admin/api/*` 转发到别处；
  - `addJavascriptInterface(anyObject, "Native")` 暴露反射对象（Android 只允许一个 `@JavascriptInterface` 标注的 final 类）；
  - iOS `allowJavaScriptAccessFromFileURLs` / `allowUniversalAccessFromFileURLs`；
  - 在 `onReceivedSslError` 里调 `proceed()`。
- 文案不许写「安全登录」「已加密」这类没有主语的词。要写「管理页由 core 自己提供，与聊天界面不是同一个地址」。

### T5b · 设备绑定与原生桥（2 天，AMEND-17 新增）· U1-e（我的页入口）

这是「用户只有手机也能配」的落点，**不能省**。

| 平台 | 密钥存储 | 签名 |
|---|---|---|
| Android | Keystore `P-256`，优先 `StrongBox`（`setIsStrongBoxBacked(true)`，失败回落 TEE） | `Signature("SHA256withECDSA")` |
| Desktop | 软件密钥（P-256 或 `Ed25519` 软件实现），`hardware_backed=false` | —— |
| Wasm | **不生成密钥**（没有安全存储），只能 `mode=browser` 只读 | —— |

> **iOS 不在本任务**：`ui/ios/` 归 U4，U1 的边界明确写「不做 iOS」。iOS 的 WebView 宿主、Secure Enclave P-256、三方法桥、扫码绑定见 **U4 T6**。U1 只提供 `commonMain` 的 `expect` 声明与模板校验逻辑，U4 写 `actual`。**不要把 iOS 的 `actual` 写在 U1 里，也不要指望 U1 的 T5b 覆盖它**——U1 没有 iOS target。

- 私钥属性：`kSecAttrAccessibleWhenUnlockedThisDeviceOnly`（iOS）/ `setUserAuthenticationRequired(false)`（Android，因为后台刷新要签名；生物识别在 UI 层做二次确认）。
- **扫码**：CameraX 原生扫码，不在 WebView 里做。解析 `vade://bind?h=&p=&fp=&tls=&t=`（iOS 侧见 U4）。
- 绑定流程见方案 §7.12.3；实现要点：先验 `hello_sig`（用已 pin 的 `core_pub`），再生成 X25519 临时密钥、AES-256-GCM 加密，AAD 拼 `"vade-bind-v1" || fp8 || SHA256(ticket) || challenge || kex_pub`。
- 绑定成功后 `refresh_token` 进 Keychain / Keystore（加密存储），`access_token` 只留内存。
- **指纹核对 UI 是原生的、不可跳过的**：显示 `core.example.com` + 分组的大字 `3f9a 1c7e 5b2d 4a86`，用户点「一致，继续」才往下走。不做「默认选中已确认」。
- 卸载 App 后残留清理：首次启动检测 `bind_id` 不存在 → `deleteAll` 钥匙串条目。

### T5c · 模块页面宿主（U1-h，2 天，等 C-19）

**C-19 未冻结时只做壳**：入口、空态、拒渲染路径。不要自己发明 schema 字段——发明出来的字段冻结时一定对不上。

- 入口：桌面在右面板顶部；手机在「能力」tab 内（列表项 = 模块 + 页面标题）。模块没有 `ui.pages[]` 就**不显示入口**（不出现空 tab、空列表或占位页）。
- 渲染器放 `core/modulepage/`：schema → 组件树，只认 4 个原语（`column` / `row` / `tabs` / `list`）与 C-09 白名单子集（`Button` / `TextField` / `Card` / `ListItem` / `Switch`，加 `ToolCallCard` / `AttachmentChip` 的只读形态）。
- **禁**：`MessageBubble` / `InputBar` / `ApprovalDialog` / `BottomBar` 不得出现在模块页面；无 JS、无 `eval`、无 WebView、无内联事件。
- 动作只 3 类：`navigate`（会话 / 设置 / 模块页面内跳转）、`tools/call`（经 core 路由，`risk=high` 照走审批）、`ui/submit`（走现有提交通道）。
- 数据绑定**只读模块自身状态**；越界字段显示空值 + 一次性告警，**不泄露** core 数据。
- 前向兼容：未知原语或组件 → 该节点降级为占位并告警，页面其余部分继续渲染；含脚本 / 未知动作 → `MANIFEST_INVALID` **拒绝渲染**。
- 渲染超时 >5s → 可重试卡片，宿主不卡死；模块断连 → 显示「模块离线」而非白屏。
- 深链形态随 C-19 冻结确定；先按「模块 id + 页面 id」两级路由留位，不写死 URL 形态。

### T6 · `material` 皮肤 + 底栏三态（1.5 天）· U1-a / U1-b

- Material 3 原生组件包装成 `Skin` 实现；`BottomBarStyle.Fixed/Floating/FloatingBlur` 三态都要能渲染（`FloatingBlur` 在 Desktop/Web 用简单模糊，Android 用 `Modifier.blur`）。
- `MessageBubble` 两态（助手全宽 ≤760dp / 用户右对齐气泡）与 `ToolCallCard` 三态是本任务的截图重点，别只交「能显示」。
- `shell/TopBar.kt` 的「贴顶 / 浮动胶囊」两形态都用 `Skin` 原语拼（`Card` + `Button`），**不新增 C-09 组件**。
- 深色/浅色跟随系统（FR-8.17 可选，做了加分，不做不阻塞）。

### T7 · 三端构建与验收（1.5 天）· U1-g

```bash
./gradlew :composeApp:assembleDebug
./gradlew :composeApp:run
./gradlew :composeApp:wasmJsBrowserProductionWebpack   # 产物给 F1 托管
./gradlew :shared:allTests
```

## 八、验收（G3）

### 8.1 交付验收

| 用例 | 判据 |
|---|---|
| 三端聊天 | 各端连同一 core，发一条消息，看到流式回复与工具卡片 |
| 断线续传 | 飞行模式 30s 恢复 → 按 `last_seq` 补齐，无重复、无乱序（AC-11） |
| 多端一致 | 手机与桌面同时 attach，一端发消息，另一端 500ms 内看到（AC-10） |
| 重复 seq | 人为重放事件 → UI 不出现重复气泡 |
| 审批 | 两端同时弹窗，一端批准，另一端自动关闭 |
| 皮肤切换 | 运行中切 material → miuix 占位 → 不崩溃、不丢会话（U2 接入前用占位实现） |
| 浏览器 | 无 localStorage 私钥；cookie 会话可用且**可写**（带 CSRF token），审计记 `browser_session`；`READ_ONLY_SESSION` 只在显式只读会话（D100） |
| **WebView 只加载本地资源** | release 包里全局搜 `loadUrl("http` → 0 处；WebView 首屏 URL 是 `app://admin/`（iOS）/`file:///android_asset/admin/`（Android） |
| **桥只有三个方法** | `window.__AC__` 的 `Object.keys()` = `["getDeviceInfo","sign","secureStore"]`；从 JS 调 `sign("hello")` 被拒（模板不匹配），日志有 `bridge.sign.rejected` |
| **扫码绑定** | 全新设备扫码 → 指纹核对页 → 设密码 → 进管理页能看拓扑；`devices.json` 多一条 `alg=p256, hardware_backed=true`（真机，模拟器可 `false`） |
| **指纹不符即中止** | 把二维码 `fp` 改一位 → 页面显示「指纹不一致」，不发绑定请求 |
| **WebView 里没有密钥** | 用 Safari/Chrome 远程调试连 WebView，`window` 上搜不到任何私钥材料；`__AC__.getDeviceInfo()` 只返回 `deviceId/fp8/alg/hardwareBacked` |
| **Wasm 端管理面只读** | 浏览器版同一个入口（管理页）打开后 `__AC_HOST__.mode === "browser"`，管理面写操作按钮不出现（聊天面另按 D100） |
| **卸载清残留** | 卸载重装 App → 首次启动 Keychain/Keystore 里没有旧 `refresh_token` |
| **material 截图** | 9 张 `ui/shots/material/<platform>-<bar>.png`（Android/Desktop/Web × fixed/floating/floating+blur）。**F1 直接按这个路径 `ls`，你不交，AC-13 的 9 组合只有 6 张** |
| **SkinRegistry 可切换** | 三个皮肤在设置页都能被选中；未安装的 miuix/glass 走占位实现并显示「待安装」，不崩溃 |
| **4 个一级目的地** | 三端截图对照：一级目的地均为「会话 / 活动 / 能力 / 我的」且顺序一致；某端出现第 5 个即判不合格（FR-8.77） |
| **四档断点** | 360 / 700 / 900 / 1280dp 四张截图的容器类型与 §2.1 表逐行一致；`≥1200dp` 是双栏 + 右面板（FR-8.76） |
| **手机会话详情** | 详情页底栏不可见；IME 弹起时输入区完整、浮动胶囊不遮挡发送按钮（FR-8.72 / D88） |
| **桌面右面板** | `⌘\` 连按两次开合且会话滚动位置不变；改宽度档位 / 承载组件后重启保持（FR-8.26 R3） |
| **模块页面入口** | 装 2 个页面的模块 → 桌面右面板 2 个入口 / 手机「能力」tab 2 项；换成无页面模块 → 入口消失、无残留空白页（FR-8.79） |
| **模块页面拒渲染** | schema 含 `<script>` / `eval` / 未知动作 → 不执行任何代码，给 `MANIFEST_INVALID`；未知原语只降级该节点，其余仍渲染（FR-8.78） |
| **模块页面超时** | 页面渲染 >5s → 可重试卡片，宿主不卡死；模块断连 → 「模块离线」而非白屏（FR-8.79） |

### 8.2 交互验收（8 条，逐条挂 FR）

| # | 交互 | 判据 | FR | 切片 |
|---|---|---|---|---|
| 1 | **流式中断** | 点停止 → 3s 内停止、保留已生成文本、收到 `turn.interrupted` 后不再追加；显示「已停止 · 已生成 N 字」，**不显示红色错误** | FR-8.28 / 8.35 | U1-b |
| 2 | **队列** | 连发 3 条 → 显示「排队中 #1/#2/#3」；取消第 3 条立即移除且后续位置前移；当前 turn 中断后下一条自动开始 | FR-8.35 / 8.36 | U1-b |
| 3 | **审批** | `risk=high` → 弹层显示完整参数（脱敏后）、60s 倒计时；超时按拒绝处理并广播；另一端批准后本端 1 个 RTT 内关闭。显式只读会话点批准 → `READ_ONLY_SESSION` | FR-8.53 | U1-d |
| 4 | **ask-user** | 四类控件（`free_text` / `single_choice` / `multi_choice` / `confirm`）都能提交；超时后 turn 收到「取消」而不是空值；与审批同时出现时不互相覆盖、标题不同 | FR-8.54 / 8.55 | U1-d |
| 5 | **附件** | 选图 → 本地预览 100ms 内可见 → 上传进度 → 发送后消息里是 `file_id`；失败可重试，同 `sha256` 不重复上传；发送后标注「已压缩，原图未保留」 | FR-8.38 / 8.39 / 8.40 / 8.42 | U1-b |
| 6 | **断线** | 断网 10s 重连 → 按 `last_seq` 补齐，已见消息不重复渲染；超 replay 窗口 → 顶部提示「内容可能不完整，已重新同步」 | FR-8.34 / AC-11 | U1-b |
| 7 | **Web 写路径** | cookie 会话 + CSRF token 可发送 / 上传 / 批准，审计记 `browser_session`；缺 CSRF token → `ORIGIN_MISMATCH` / `PERMISSION_DENIED`；**显式只读会话**才返回 `READ_ONLY_SESSION`。注意：「cookie 一律只读」已被 D100 废止，别再按旧口径验 | FR-8.75 | U1-g |
| 8 | **无障碍** | 字体 200% 时输入区不遮挡发送按钮；TalkBack 顺序 = 标题 → 消息 → 输入区；流式播报 ≤3s 一次 | FR-8.73 / 8.72 | U1-g |

### 8.3 三端证据与截图

- **9 张 `material` 截图**：`ui/shots/material/<platform>-<bar>.png`（Android / Desktop / Web × fixed / floating / floating+blur）。F1 直接按这个路径 `ls`——你不交，AC-13 的 9 组合就只有 6 张。
- **三端页面截图**：`ui/shots/u1/<platform>/<page>.png`，每端 4 个一级目的地 + 会话详情 + 右面板展开（桌面 / Web）；手机详情那张必须能看出底栏已隐藏。
- **断点截图**：360 / 700 / 900 / 1280dp 各一张，容器类型对着 §2.1 的表逐行打勾。
- **交互的原始证据**：录屏或日志（seq 日志、上传分片日志、`bridge.sign.rejected`），每条交互一份；不接受「已通过」三个字。
- **无障碍证据**：200% 字号一张 + TalkBack 焦点顺序录屏（≥30s，覆盖 标题 → 消息 → 输入区）。

### 8.4 无障碍（FR-8.72 / 8.73 / 8.74）

- 对比度：正文 ≥4.5:1，大字 / 粗体 ≥3:1；`material` 先达标，U2/U3 同判据。
- 触控目标：Android ≥48×48dp、Web 实测 ≥44px；装饰性头像 `contentDescription=null`。
- 读屏语义顺序 = 视觉顺序；流式正文**不设** liveRegion，改用隐藏状态节点 ≤3s 播报一次（「正在生成，已 120 字」），完成时播报字数；审批 / 错误用 assertive。
- 「减少动画」开启后关闭光标闪烁、shimmer 与展开动画。
- 桌面：纯键盘可完成「新建 → 输入 → 发送 → 批准审批」；六个快捷键不与输入框编辑冲突；焦点环不可移除；960×640 不溢出。
- 手机：返回按层级回退（详情 → 列表 → 退出）且列表滚动位置保留；分屏 50/50 下输入区与发送按钮均 ≥48dp 可达。

### 8.5 已知缺陷的记法

- 依赖尚未批准 RFC 的判据（FR-8.39 上传端点、FR-8.42 content parts、FR-8.51 工具卡字段、FR-8.54 `waiting_input`、FR-8.78 / 8.79 的 C-19）在 F1 验收矩阵里一律记「**已知缺陷**」+ RFC 编号，**不得**标通过（沿用 §8.21.2 口径）。
- 降级路径本身也要可验：假上传不得显示假成功；静态骨架要有空态文案；C-19 未冻结时的拒渲染要给出 `MANIFEST_INVALID`。

## 九、风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| **C-09 冻结太早、组件没到 11 个** | U2/U3 返工，U1 接口不稳 | **D93 已拍板：冻结前必须落 11 个组件**（T2 的接口 + 4 个新签名 + 三态）；第 3 天冻结时把 `Skin.kt`、组件清单、sha256 一起交付；冻结后只准加带默认值的可选参数 |
| **RFC-0004（附件）未批** | U1-b 只能假上传 | U1-b 用本地假上传 + 本地预览 + `TODO(协议)`；消息里先塞本地占位 `file_id`；**不许**把假成功当验收通过，按 §8.5 记已知缺陷 |
| **RFC-0007（子代理 / 任务）未批** | U1-d 的活动页没有数据源 | U1-d 只做静态列表 + 空态 + 状态枚举占位，交互留桩；不接假事件流、不伪造进度 |
| **RFC-0008（ask-user）未批** | `waiting_input` 在 C-06 里不存在 | U1-d 的输入对话框只在本地 mock 下验控件形态；不写死 `waiting_input` 分支，未知状态按 `running` 渲染 + 降级提示 |
| **C-19 未冻结** | U1-h 无 schema 可依 | U1-h 只做壳：入口、空态、拒渲染（`MANIFEST_INVALID`）路径；解析器只留接口，字段等冻结 |
| 三皮肤组件 7 → 11 | U2 工期增加 | 新增 4 个只要求默认 / 禁用 / 错误三态，不做全状态矩阵；`material` 也按三态交付 |
| Web 可写（D100）引入 CSRF / XSS 面 | 安全返工 | 写路径必须 Origin + `Sec-Fetch-Site` + CSRF token 三件套；安全评审是合并门槛（U1-g） |
| 附件字节混进 ws 帧 | 帧超限（65519 字节） | 附件一律走 `POST /ui/blob`；「上传 8MB 时任何 ws 帧 <64KiB」进验收 |
| AGP 两开关 deprecated | 构建告警 | 沿用现状，不重构；拆模块留后续 ADR |

## 十、禁止

- 禁止业务页面直接 import `material3` / `miuix` / `glass`（只能 `Skin`）。
- 禁止把会话状态存在 UI 本地并当作真相（只做镜像，snapshot 覆盖）。
- 禁止在 Web 端存长期私钥（D15）。
- 禁止为了"看起来流畅"在 UI 侧伪造 delta（必须以 core 事件为准）。
- 禁止改 `schema/` 或 `protocol/`。
- 禁止在 U1 里实现 miuix/glass 的具体组件（那是 U2/U3 的目录）。
- **禁止 WebView 加载远程 HTML**（包括 `https://<core>:8444/admin`）；只加载 App 内置资源。这条是 §7.11 第 1 条，违反它等于把 XSS 升级成本地代码执行。
- **禁止模块转发 `/admin/*`**（HTTP 代理、端口映射、改 `fetch`、`shouldInterceptRequest` 中转，全禁）。
- **禁止在 JS 侧生成或持有任何密钥**；私钥只在 Keystore / Secure Enclave，签名只经 `__AC__.sign()`。
- **禁止在 `onReceivedSslError` 里 `proceed()`**；也不允许自定义 TrustManager 放行自签——首次信任走指纹核对，不走「忽略证书错误」。
- 禁止用「生物识别通过」代替设备签名：生物识别是 UI 层确认，不是第二因子。
- **禁止第 5 个一级目的地**（FR-8.77）；也不许把「活动」折进会话页——它在台账里是独立目的地。
- **禁止做消息树 / 版本翻页 / 分支切换入口**（§8.21.4）；位置可以留，假功能不行。
- **禁止用 WebView 渲染模块页面**，或执行模块提供的任意脚本 / 表达式 / 内联事件（C-19：只认 4 原语 + C-09 子集 + 3 类动作）。
- 禁止伪造模型列表与能力标签（无 `model/*` 时只显示只读徽标，FR-8.46）。
- 禁止把「已压缩」写成「原图可下载」——D96 只存压缩图，文案必须是「原图未保留」。
- 禁止为顶栏 / 右面板新增 C-09 组件：顶栏用 `Skin.Card` + `Skin.Button` 拼，右面板是容器不是组件。

## 十一、交付格式

按 `00-orchestration.md` §三，额外附：三端截图（`ui/shots/u1/` 与 `ui/shots/material/` 两套）、`Skin.kt` 与 `ProtocolClient.kt` 的冻结时间与 sha256、断线续传的 seq 日志、import 防回退测试输出、8 条交互的原始证据（录屏或日志）。

- **按切片提交**：`U1-0` / `U1-a` … `U1-h` 各自独立 commit（`feat(ui/u1-a): …`），回滚以切片为单位；不要把 9 个切片压成一个 commit，也不要把两个切片混进一个。
- **C-09 交付物**：`Skin.kt` 全文 + 11 个组件清单 + sha256 + 冻结时间；冻结后任何签名改动都要附 RFC 编号。
- **自检记录**：哪些判据因 RFC 未批按 §8.5 记了「已知缺陷」，逐条列出 RFC 编号与降级路径的验证方式。
