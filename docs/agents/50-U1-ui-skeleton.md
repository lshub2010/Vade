# U1 · UI 骨架 + `material` 皮肤（G3）

| 项 | 值 |
|---|---|
| Agent 代号 | `u1-ui` |
| 波次 | W4（与 C4 / C5a 并行） |
| 依赖 | C0a 的 Kotlin 绑定、C0b 的 W-A/W-B 结论、C3 的事件表（C-06）、C4 的协议 API |
| 目录所有权 | `ui/shared/`、`ui/composeApp/src/commonMain/kotlin/core/`、`skin/material/`、`ui/composeApp/src/*Main/` |
| 产出 | Android / Desktop / Web 三端可聊天 + `material` 皮肤 + `Skin` 接口 |
| 出口 | **G3**；**C-09 `Skin.kt` 第 3 天冻结**、**C-10 `ProtocolClient.kt` 周末冻结** |

## 一、一句话任务

把 core 的事件流变成一个能用的聊天客户端：三端同源、断线能续、皮肤可换；并且把「皮肤」定成一个**组件工厂接口**，让 U2/U3 能在不碰业务页面的前提下接入。

## 二、边界

| 做 | 不做 |
|---|---|
| KMP 工程、三端 target、共享状态机 | 不做 `miuix` / `glass` 皮肤（U2/U3） |
| `Skin` 接口 + `material` 实现 | 不做 iOS（U4） |
| 聊天页、会话列表、设置页、审批弹窗 | 不做配置页本体（C5a/C5b 的 `admin-ui/dist`） |
| **管理页 WebView 宿主 + 原生桥（T5b）** | 不写管理页的 React 代码 |
| 协议客户端封装（Flow） | 不改协议、不改 Rust |

## 三、必读

- `docs/design.md` §9 全节（分层、Skin、三皮肤矩阵、Wasm 绑定）
- `docs/design.md` §7.10–7.12（WebView 约束、设备绑定协议）——T5b 必读
- `docs/requirements.md` FR-8.1–8.24、A10/A11、AC-10–AC-15
- `schema/events.json`（C3）、`schema/rpc.json`（C2）
- `docs/adr/ADR-001-web-protocol-binding.md`（C0b）：决定 Web 走 W-A 还是 W-B

## 四、工程结构

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
│       │   ├── skin/Skin.kt      # C-09
│       │   ├── skin/material/
│       │   ├── shell/BottomBar.kt
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

## 五、任务分解

### T1 · 工程骨架（1 天）

- `shared`：`androidTarget`、`jvm("desktop")`、`wasmJs`（iOS target 留给 U4 加，避免 U1 被 Xcode 卡住）。
- `composeApp`：同上三 target；Android `minSdk 26`；Desktop JVM 17；Wasm 用 `wasmJs { browser() }`。
- 依赖：Compose MP、Kotlin 协程、kotlinx-serialization、`vade-protocol` 的 Kotlin 绑定（C1 产物）。
- **替 U2/U3 预留依赖**：按 C0c 的 `versions.lock.md` 把 `miuix` 与 `haze` 加进 `composeApp` 的 commonMain 依赖（版本锁死）。U2/U3 只写代码、**不改构建文件**；构建文件由你独占。
- **Web 路由**：读 ADR-001。W-A → 在 `wasmJsMain` 里 `external` 调 JS 包；W-B → 在 `wasmJsMain` 里用 TS/JS 实现 `ProtocolClient`。两条路的公共接口都是 C-10。

### T2 · `Skin` 接口（第 3 天必须冻结）

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
}
```

- `BtnStyle`：`Primary / Secondary / Danger / Text`。
- `BottomBarStyle`：`Fixed / Floating / FloatingBlur`。
- **共 11 个组件**（AMEND-22 / D93：原有 7 个 + `MessageBubble` / `AttachmentChip` / `ToolCallCard` / `InputBar`）；冻结后不得再加第 12 个（加就破坏 U2/U3 的并行边界），确实需要就提 RFC。
- 冻结后只能加带默认值的可选参数。

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

### T3 · 共享状态机（2 天）

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
- 同一 `seq` 重复到达直接丢弃；`seq` 跳跃（缺失）→ 触发一次 `attach(lastSeq)` 补齐，不要自己猜。
- `delta` 按 `turn_id` 累加；收到 `completed` 后冻结该 turn 的文本（防迟到的 delta 改了已完成的回复）。
- 审批：收到 `turn.approval_required` 入 `pendingApprovals`；收到 **`turn.approval_resolved`** 移除并关弹窗（多端一致，这是唯一关闭依据）。
- **导出行为契约向量**：把上述规则做成 `shared/src/commonTest/resources/state-vectors/*.json`（输入事件序列 → 期望的 messages/queue/approvals）。U4 若走 Swift 重写路径，必须用同一组向量验收。

### T4 · 协议客户端封装（2 天）

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

### T5 · 页面（2 天）

> T5b（设备绑定与原生桥，2 天）见下方，与本任务并行。

| 页面 | 内容 |
|---|---|
| 聊天页 | 消息列表（用户/助手/工具卡片）、流式文本、输入框、发送/停止、队列条 |
| 工具卡片 | 工具名、状态（运行/成功/失败/等待审批）、可折叠参数与结果摘要 |
| 审批弹窗 | **完整参数**（等宽字体、可滚动）、risk 标识、倒计时、`仅本次` / `本会话` / `拒绝` |
| 会话列表 | 新建、切换、最近消息摘要 |
| 设置页 | 皮肤选择（3 个）、底栏样式（3 个）、连接信息（地址、指纹、状态）、**「管理此 Core」入口**（见下） |

- 所有控件走 `Skin.*`；页面里**不得**出现 `material3`。
- 皮肤/底栏切换**运行时生效**，用 `CompositionLocal` 提供当前 `Skin`，切换只换 Local，不重建 Activity/View 树。
- 空态：无模块时显示「core 已连接，但没有可用能力」，不是空白页。

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
- `mode` 决定前端行为：`webview` → 可写；`browser` → 只读（其实 Wasm 端和系统浏览器都是这个值）。
- **未绑定时**：WebView 打开的是一个原生「添加服务器」页（**扫码在原生层做，不在 WebView 里**），扫码 → 核对指纹 → 设密码 → 绑定完成 → 再进 WebView。绑定协议见方案 §7.12，实现挂在 U1 的 T5b。
- **禁止**：
  - 在模块里缓存管理面 token 或代填密码（token 由 WebView 页自己拿，原生只提供**签名能力**）；
  - 用 `shouldInterceptRequest` / `WebViewClient` 把 `/admin/api/*` 转发到别处；
  - `addJavascriptInterface(anyObject, "Native")` 暴露反射对象（Android 只允许一个 `@JavascriptInterface` 标注的 final 类）；
  - iOS `allowJavaScriptAccessFromFileURLs` / `allowUniversalAccessFromFileURLs`；
  - 在 `onReceivedSslError` 里调 `proceed()`。
- 文案不许写「安全登录」「已加密」这类没有主语的词。要写「管理页由 core 自己提供，与聊天界面不是同一个地址」。

### T5b · 设备绑定与原生桥（2 天，AMEND-17 新增）

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

### T6 · `material` 皮肤 + 底栏三态（1.5 天）

- Material 3 原生组件包装成 `Skin` 实现；`BottomBarStyle.Fixed/Floating/FloatingBlur` 三态都要能渲染（`FloatingBlur` 在 Desktop/Web 用简单模糊，Android 用 `Modifier.blur`）。
- 深色/浅色跟随系统（FR-8.17 可选，做了加分，不做不阻塞）。

### T7 · 三端构建与验收（1.5 天）

```bash
./gradlew :composeApp:assembleDebug
./gradlew :composeApp:run
./gradlew :composeApp:wasmJsBrowserProductionWebpack   # 产物给 F1 托管
./gradlew :shared:allTests
```

## 六、验收（G3）

| 用例 | 判据 |
|---|---|
| 三端聊天 | 各端连同一 core，发一条消息，看到流式回复与工具卡片 |
| 断线续传 | 飞行模式 30s 恢复 → 按 `last_seq` 补齐，无重复、无乱序（AC-11） |
| 多端一致 | 手机与桌面同时 attach，一端发消息，另一端 500ms 内看到（AC-10） |
| 重复 seq | 人为重放事件 → UI 不出现重复气泡 |
| 审批 | 两端同时弹窗，一端批准，另一端自动关闭 |
| 皮肤切换 | 运行中切 material → miuix 占位 → 不崩溃、不丢会话（U2 接入前用占位实现） |
| 浏览器 | 无 localStorage 私钥；cookie 会话可用 |
| **WebView 只加载本地资源** | release 包里全局搜 `loadUrl("http` → 0 处；WebView 首屏 URL 是 `app://admin/`（iOS）/`file:///android_asset/admin/`（Android） |
| **桥只有三个方法** | `window.__AC__` 的 `Object.keys()` = `["getDeviceInfo","sign","secureStore"]`；从 JS 调 `sign("hello")` 被拒（模板不匹配），日志有 `bridge.sign.rejected` |
| **扫码绑定** | 全新设备扫码 → 指纹核对页 → 设密码 → 进管理页能看拓扑；`devices.json` 多一条 `alg=p256, hardware_backed=true`（真机，模拟器可 `false`） |
| **指纹不符即中止** | 把二维码 `fp` 改一位 → 页面显示「指纹不一致」，不发绑定请求 |
| **WebView 里没有密钥** | 用 Safari/Chrome 远程调试连 WebView，`window` 上搜不到任何私钥材料；`__AC__.getDeviceInfo()` 只返回 `deviceId/fp8/alg/hardwareBacked` |
| **Wasm 端只读** | 浏览器版同一个入口打开后 `__AC_HOST__.mode === "browser"`，写操作按钮不出现 |
| **卸载清残留** | 卸载重装 App → 首次启动 Keychain/Keystore 里没有旧 `refresh_token` |
| **material 截图** | 9 张 `ui/shots/material/<platform>-<bar>.png`（Android/Desktop/Web × fixed/floating/floating+blur）。**F1 直接按这个路径 `ls`，你不交，AC-13 的 9 组合只有 6 张** |
| **SkinRegistry 可切换** | 三个皮肤在设置页都能被选中；未安装的 miuix/glass 走占位实现并显示「待安装」，不崩溃 |

## 七、禁止

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

## 八、交付格式

按 `00-orchestration.md` §三，额外附：三端截图、`Skin.kt` 与 `ProtocolClient.kt` 的冻结时间与 sha256、断线续传的 seq 日志、import 防回退测试输出。
