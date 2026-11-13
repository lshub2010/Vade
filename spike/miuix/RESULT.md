# C0c · Miuix on Compose Multiplatform — 实测结论

| 项 | 值 |
|---|---|
| Agent | `c0c-miuix`（W0） |
| 分支 | `tmp/c0c-miuix` |
| 实测主机 | Windows 11 x86_64，16 核 / 16 GB，JDK 21.0.13 (Temurin)，Gradle 8.10.2（本工程矩阵）/ 9.6.1（Android 验证矩阵），本工程无 wrapper |
| 实测日期 | 2026-11-13 |
| 结论 | **三端可行**：Desktop（无头 + 7 张真实窗口图）、Wasm（构建通过）、Android（升 AGP 9.5.1 + Gradle 9.6.1 + 两个开关后出 APK） |

---

## 一、结论（TL;DR）

1. **Desktop：通过，且本轮补上了真实窗口截图。** 两条链路都跑通：
   - 无头：`:composeApp:screenshot`（`ImageComposeScene`，480×420，`headless=true`）出 8 张 `shots/miuix-desktop-*.png`；
   - 真实窗口：`java -cp <printRuntimeClasspath>` 起 `MainKt`，PowerShell 置顶 + `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` + `CopyFromScreen` 截 7 张 **466×893** 的 `shots/miuix-window-*.png`（button / textfield / card / listitem / switch / topappbar / bottombar）。
   - 截图脚本已固化到仓库：`tools/capture-window.ps1`（原来只在临时目录里，会被清掉）。

2. **Wasm：构建通过，体积无增量。** 两端一起做 `wasmJsBrowserProductionWebpack`，BUILD SUCCESSFUL in 2m16s。Miuix 版 app.wasm gzip **936,611 B**，纯 Material3 对照组 **1,013,216 B** → **Miuix 反而小 76,605 B（−74.8 KiB）**。设计预估的「Wasm +约 900 KB」**不成立**；真正的大块是 `skiko.wasm` 3,328,940 B gzip，两端完全相同（同一个 hash 文件名），与皮肤无关。**浏览器运行时仍未验证**。

3. **Android：当前仓库矩阵（AGP 8.8.2 / Gradle 8.10.2 / compileSdk 36）不可构，但升矩阵后已验证出 APK。** 把构建链换成 **AGP 9.5.1 + Gradle 9.6.1 + `compileSdk = 37` + `android.builtInKotlin=false` + `android.newDsl=false`** 后：`BUILD SUCCESSFUL in 2m 59s`，APK **13,575,772 B**（`aapt2 dump badging`：`compileSdkVersion=37` / `minSdkVersion=24` / `targetSdkVersion=36`）。失败链与风险见 §八。

4. **所以「三端可行」的准确表述是**：Miuix 自身的 API/变体覆盖 Android、Desktop、Wasm（0.9.4 发布矩阵里三端都有独立变体，三端源码编译全部通过），Android 的阻塞**完全在构建工具链**（AGP 版本 + 两个 AGP 9 兼容开关），不是 Miuix 缺端、也不是 Miuix 不支持 Android。

5. **方法学警告**：本次 Wasm 体积对照是「纯 M3 画的同一套界面」vs「Miuix 画的同一套界面」，**不是**「M3 应用 + 追加 Miuix」的边际成本。v1 要三皮肤共存（material + miuix + glass），那时 Miuix 的边际增量会大于本测的负值，需在 U2 里另测一次。

---

## 二、版本矩阵

已锁定；来源与锁定理由逐条见 [`versions.lock.md`](./versions.lock.md)。Android 那一行是**本仓库现状**，括号里是**已验证可行**的另一套。

| 组件 | 版本 | 关键约束 |
|---|---|---|
| Kotlin (KGP) | **2.4.20** | Miuix 0.9.4 的 `.module` 要求 `kotlin-stdlib 2.4.20`；低版本 Kotlin 读不了 2.4 metadata |
| Compose Multiplatform | **1.12.0** | Miuix 0.9.4 直接依赖 `org.jetbrains.compose.foundation:foundation:1.12.0`（精确版本） |
| Miuix | **0.9.4** | Maven Central `latest=0.9.4`、`lastUpdated=2026-09-20T08:24:05Z`（已核对 `maven-metadata.xml`） |
| AGP | **8.8.2**（本仓库现状，**Android 不可构**）/ **9.5.1 + Gradle 9.6.1**（**已验证可行**，见 §八） | 8.8.2 受 Gradle 8.10.2 封顶（AGP 8.9+ 需 Gradle 8.11.1）；8.x 末版 8.13.2 **也不行**（androidx 要求 AGP ≥ 9.1） |
| Gradle | **8.10.2**（现状，本工程无 wrapper）/ **9.6.1**（AGP 9.5.1 要求） | — |
| JDK | **21.0.13** (Temurin) | Gradle 启动 JVM；两套矩阵共用 |
| compileSdk | **36（现状）/ 37（Miuix 要求）** | Miuix 四个 AAR 全部 `minCompileSdk=37`；SDK 里只有带小版本号的 `platforms/android-37.0` |
| minSdk / targetSdk | **24 / 36** | 升级矩阵后 APK 实测 `sdkVersion=24`、`targetSdkVersion=36` |
| `androidx.navigationevent` | 1.1.2（传递） | 仅 Google Maven 有，仓库配置必须留 `google()` |
| `material3-window-size-class` | **1.12.0-alpha03**（传递） | Miuix 拉进来的 **alpha** 依赖，见 §五 |

---

## 三、截图索引（15 张，按性质分三类）

`spike/miuix/shots/` 下共 15 个 PNG。**三类证据的强度不同，引用时不要混用**：只有 `miuix-window-*` 是「进程真的开了一个窗口、屏幕上真的画成这样」。

### 3.1 真实窗口（7 张，466×893）→ `miuix-window-*.png`

链路：`java -cp <runtimeClasspath> vade.spike.miuix.MainKt --section=<slug>` → PowerShell 枚举该 pid 的 `SunAwtFrame` → `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` 取可见边框（`GetWindowRect` 会多带 8 px 隐形缩放边）→ `CopyFromScreen`。脚本：`tools/capture-window.ps1`。

| # | 文件 | 组件 | 字节 | 截到的实际内容 |
|---|---|---|---|---|
| 1 | `shots/miuix-window-button.png` | Button（主/次/危险/禁用 + TextButton） | 16,266 | 5 个按钮全在：主操作蓝胶囊、次操作灰胶囊、删除红、禁用灰化、文字按钮；无裁切 |
| 2 | `shots/miuix-window-textfield.png` | TextField | 18,651 | 两个填充式圆角输入框；第一个浮动 label「会话名称」+ 值「本地会话」，第二个空值显示 label；可见文字未溢出 |
| 3 | `shots/miuix-window-card.png` | Card（含嵌套） | 18,823 | 修复后完整：卡片标题 + 两行说明 + 嵌套外层 + 嵌套内层；**卡片自身表面与页面同色**（见 §五 R5） |
| 4 | `shots/miuix-window-listitem.png` | ListItem（`BasicComponent`） | 14,323 | 两行：标题 / 副标题 + 右侧 `›` 与 `Switch`；分隔线与 Card 底色不可见 |
| 5 | `shots/miuix-window-switch.png` | Switch | 16,901 | 行内开关（开，蓝）+ 独立开关（关）；形状比 Material 更细长 |
| 6 | `shots/miuix-window-topappbar.png` | TopAppBar | 18,112 | 浅灰底 + 大标题「Vade」+ 副标题「Miuix · Desktop」；无分隔线 |
| 7 | `shots/miuix-window-bottombar.png` | BottomBar（`NavigationBar`） | 13,232 | 3 项；**点击第三项后**选中态从「聊天」切到「设置」，标题文字变深加粗。这条同时证明窗口内点击链路是通的 |

> 7 张图里组件都完整可见。第 7 张是「点击后」的状态，不是初始态 —— 文件名按组件命名，不体现点击。

### 3.2 无头渲染（7 张，480×420，`ImageComposeScene` + `Density(1f)`）→ `miuix-desktop-*.png`

| 文件 | 字节 | 备注 |
|---|---|---|
| `miuix-desktop-button.png` | 8,441 | 与窗口图同构，尺寸差异来自 480×420 vs 466×893 的布局空间 |
| `miuix-desktop-textfield.png` | 11,134 | 同上 |
| `miuix-desktop-card.png` | 10,994 | **本轮重渲**（Cards 修复后），四行文字完整 |
| `miuix-desktop-listitem.png` | 6,504 | 同上 |
| `miuix-desktop-switch.png` | 9,721 | 同上 |
| `miuix-desktop-bottombar.png` | 5,138 | 初始态（首项选中），没有点击 |
| `miuix-desktop-topappbar.png` | 10,166 | 同上 |

### 3.3 内容表面渲染（1 张）→ `miuix-desktop-dialog.png`

| 文件 | 字节 | 性质 |
|---|---|---|
| `miuix-desktop-dialog.png` | 8,149 | ⚠️ **内容表面渲染，不是真弹窗**。`Shot.kt` 对 dialog 段绕过 `OverlayDialog`，直接画 `windowDimming` 遮罩 + `surface` 圆角卡 + 标题/摘要/两个按钮（`App.kt` 的 `DialogSurfaceShot()`）。它证明「这套文案与按钮排布在这套主题下长这样」，**不证明 OverlayDialog 能弹出来** |

> 为什么必须这样：`OverlayDialog` 在 Desktop 上根本不绘制 —— 真实窗口里点「打开确认弹窗」后，进程的窗口列表仍只有一个 `SunAwtFrame`（没有新的 popup 窗口），遮罩和弹窗都不出现；`ImageComposeScene` 同样拿不到。见 §五 R6、§十 P4。

---

## 四、体积与降级（T4 表，逐格填）

| 指标 | 设计期望 | 实测 | 结论 |
|---|---|---|---|
| Wasm 包 gzip（含 Miuix） | — | `app.wasm` **936,611 B** + `composeApp.js` 99,134 B = **1,035,745 B（1.01 MiB）** | — |
| Wasm 包 gzip（纯 Material 基线） | 对照组 | `app.wasm` **1,013,216 B** + `baselineApp.js` 99,156 B = **1,112,372 B（1.06 MiB）** | — |
| **Miuix 相对基线的增量** | **+约 900 KB** | **−76,605 B gzip（−74.8 KiB / −76.6 KB）**，raw **−151,350 B**；连 js 一起算 −76,627 B | **预估不成立，方向都反了**：Miuix 版更小 |
| Desktop 产物体积 | — | **未测**（没跑 `packageMsi`/`createDistributable`） | 缺口 |
| APK 体积 | — | **13,575,772 B**（debug，AGP 9.5.1 矩阵；`sha256 ac7f518841d9fb393550e45199383f73f12371bbcdbcb4f03fcb42368cd22547`） | 有绝对值；**无基线对照**（`baselineApp` 只有 wasm 目标，没有 android 目标），增量仍属缺口 |
| `haze`/模糊在 Wasm 上是否生效 | 预期降级为无模糊 | **未验证**：本 spike 完全没接 `haze`，也没在浏览器里跑过 | 缺口，见 §五 R4 |

### 4.1 原始数字（`{app}/build/kotlin-webpack/wasmJs/productionExecutable/`）

| 产物 | `:composeApp`（M3 + miuix-ui） | `:baselineApp`（纯 M3） | 差 |
|---|---|---|---|
| 应用 wasm（raw） | 3,159,707 B | 3,311,057 B | **−151,350 B** |
| 应用 wasm（gzip -9） | 936,611 B | 1,013,216 B | **−76,605 B** |
| 主 JS（raw / gzip） | 529,119 / 99,134 B | 529,342 / 99,156 B | −223 / −22 B |
| `skiko.wasm`（raw / gzip） | 8,640,316 / 3,328,940 B | 8,640,316 / 3,328,940 B | **0（同一文件，hash 名同为 `bfa5198…wasm`）** |
| 全量 gzip（wasm + js + skiko） | 4,364,685 B（4.16 MiB） | 4,441,312 B（4.24 MiB） | −76,627 B |

`skiko.wasm` 占 Miuix 版全量 gzip 的 **76.3%** —— Web 端体积的支配项是 Compose/Skia 运行时，换皮肤在噪声级别。gzip 数字用 `gzip -9` 在本机对产物重算过，与构建日志一致；raw 数字直接来自文件长度。

### 4.2 为什么是负增量（别误读）

- `:composeApp` 虽然声明了 `compose.material3`，但界面**全部**用 Miuix 组件画 → Skia/wasm 链接时把 M3 的大部分实现 DCE 掉了；`:baselineApp` 是纯 M3 全量用到 8 类组件，保留的 M3 代码更多。净效果是「Miuix 组件集 < 被裁掉的 M3 组件集」。
- 所以**不能**得出「Miuix 免费」。v1 要同时带 material + miuix + glass 三套皮肤，Miuix 的真实边际成本 = 本测的差值 **加上**「M3 无法被 DCE 的那部分」。U2 必须在一个**已经含 Material 皮肤**的工程里复测一次，别直接引用本表的 −76 KB。
- 结论仍然有用且更乐观的一条：**Miuix 在 Wasm 上编得过、且不引入任何新的重型运行时**（`skiko.wasm` 与基线逐字节相同），不存在任务书 T4 说的「超 1.5 MB 就降级」触发条件。

---

## 五、API 风险

### 5.1 代码里所有 `@OptIn`（逐个列出，无一遗漏）

在 `spike/miuix/` 全树 grep `@OptIn`，共 6 处（按文件）：

| 位置 | 注解 | 性质 | 升级会断在哪 |
|---|---|---|---|
| `composeApp/build.gradle.kts:23` | `org.jetbrains.kotlin.gradle.ExperimentalWasmDsl` | 构建脚本级，KGP 的 wasmJs DSL | KGP 把 wasmJs 从 experimental 提升时只是多余；若 DSL 改名（`moduleName`→`outputModuleName` 已发生过一次）会直接编译失败 |
| `baselineApp/build.gradle.kts:11` | 同上 | 同上 | 同上 |
| `composeApp/src/wasmJsMain/.../main.kt:11` | `androidx.compose.ui.ExperimentalComposeUiApi` | **运行时 API**：`ComposeViewport(document.body!!)` | CMP 每次 minor 都可能动 `ComposeViewport` 签名；1.13/1.14 升级要重新对 |
| `composeApp/src/desktopMain/.../Shot.kt:16` | `androidx.compose.ui.ExperimentalComposeUiApi` | `ImageComposeScene`（仅测试/截图用） | 同上；这条只在 spike 工具里，不进产品 |
| `baselineApp/.../BaselineApp.kt:37` | `ExperimentalMaterial3Api` | M3 的 `TopAppBar`（对照组） | 只影响对照组 |
| `composeApp/src/commonMain/.../App.kt` | **无 `@OptIn`** | ✅ Miuix 用到的组件（Button/TextField/Card/Switch/NavigationBar/NavigationBarItem/TopAppBar/OverlayDialog/BasicComponent/TextButton/**Surface**）在 0.9.4 上**全部是稳定 API，不需要 OptIn** | 这是本次最正面的一条：换皮肤不会引入 experimental 依赖面 |

> 注意：`App.kt` 不用 `@OptIn` 是事实，不是我们偷偷加了再删 —— 上面 6 处是唯一存在的 OptIn。U2 若要用 Miuix 的其它组件（如 blur/`haze` 集成），需重新审一遍是否引入新的 experimental。

### 5.2 依赖图风险

- **R1 · `minCompileSdk=37` 是硬门槛，但不是死路（已解）。** 四个 Android AAR 一致：`miuix-ui` / `miuix-core` / `miuix-shader` / `miuix-squircle`，全部 `minCompileSdk=37, minCompileMinorSdk=0, minAndroidGradlePluginVersion=1.0.0`。0.9.2 / 0.9.3 同样 37 → **降版本无解**。解法是升 AGP 9.5.1（§八）；注意 **AGP 8.13.2 也不够**，因为 androidx compose 1.12.0 的 AAR 要求 AGP ≥ 9.1（原文见 §八 8.2）。
- **R2 · 传递依赖里有 alpha。** `miuix-ui:0.9.4` 的 metadata 列出 `org.jetbrains.compose.material3:material3-window-size-class:1.12.0-alpha03`、`com.materialkolor:material-color-utilities:5.0.1`、`androidx.navigationevent:navigationevent-compose:1.1.2`。alpha 传递依赖不受我们锁版本保护，U2 上线前要把它们进 lockfile 或 `resolutionStrategy` 钉死。
- **R3 · Miuix 自带宽度的平台矩阵不含 iosX64 / macosX64。** 0.9.4 发布的 Kotlin 变体：`androidJvm`、`jvm`(desktop)、`wasmJs`、`js`、`iosArm64`、`iosSimulatorArm64`、`macosArm64`。**x86_64 的 iOS 模拟器与 Intel Mac 桌面没有变体** —— 三端（A/D/W）不受影响，但 U4（iOS）与 Intel Mac 开发者会撞上，提前记档。
- **R4 · `haze` 未验证。** 本 spike 没引 `haze`，`floating+blur` 底栏完全没做。Miuix 0.9.4 自带 `miuix-shader` 且**有 wasmJs 变体**（由 `miuix-squircle` → `miuix-shader` 传递进来，wasm 编译通过），说明上游对 wasm 的 shader 路径是支持的；但**运行时**（浏览器里是否真出模糊/圆角 squircle）没验证。U2 的 T3「模糊不可用则降级」仍需要在真浏览器里定夺。
- **R5 · Card 的「首行被裁」是 showcase 的排版 bug，已修；但 Card 默认没有表面色/描边，这条是真的。** 旧结论说「首行上沿被裁半行 + 卡片几乎不可见」，实测把两件事拆开了：
  1. **裁切是假象**：`Cards()` 把两个 `Card` 直接放在 section 的 `Box` 里，两个 Card 叠在同一位置，第二张 Card（带 `padding(top=12.dp)`）的表面正好盖住第一张的首行与整段说明。给 `Cards()` 加一层 `Column`（`Arrangement.spacedBy(12.dp)`）后，真实窗口图与无头图都完整显示四行文字。**改的是 `composeApp` 的演示排版，Miuix 源码一行未动。**
  2. **表面色是真的没有**：对 `miuix-window-card.png` 取样，卡片内部 `(200,60)`、左内边 `(30,60)`、右内边 `(450,60)` 与页面 `(460,400)` **全是 `#FFFFFF`**，没有描边、没有阴影、没有 1 px 边界。即 `lightColorScheme()` 下 `Card` 的 `surface` 与 `background` 同色。**U2 §四要求「描边 + 表面色、无阴影」必须靠覆写 `CardDefaults`/`CardColors` 自己实现**，不能指望 Miuix 默认值 —— 否则会做出「一片白」的皮肤。深色主题未测，不能假设它在那里就有对比度。
- **R6 · `OverlayDialog` 在 Desktop 上根本不绘制（不是 headless 的锅）。** 早前以为是 `ImageComposeScene` 不画 popup 层，本轮用真实窗口证伪了这个归因：点「打开确认弹窗」后，`dump` 出来该进程仍然**只有一个 `SunAwtFrame`**，没有新窗口（点击前后的截图里也既无遮罩也无弹窗）。也就是说 Miuix 的 dialog 走 popup 窗口路径，而 Compose Desktop 这条路径在这里**没有创建窗口**；无头渲染拿不到是同一个原因，不是第二个 bug。
  - 后果：**「8 组件全部截到」在 Desktop 上无法靠真弹窗完成**。本轮给 dialog 的是一张**内容表面渲染**（§三.3），文档与提交说明里都标注了「非真弹窗」。
  - 对 U2 的直接影响：`ApprovalDialog` 不要建立在 `OverlayDialog` 之上 —— 要么在页面内自建 `Box` + 遮罩 + 圆角卡（可截、可控、跨端一致），要么等上游修 Desktop popup。这条算进 U2 的 T2 预算。

---

## 六、验收命令与实际状态

任务书 §五 的四条，逐条对照（**不要照抄成全部通过**）：

| 命令 | 实际结果 |
|---|---|
| `./gradlew :composeApp:assembleDebug` | ⚠️ **本仓库矩阵（AGP 8.8.2 / Gradle 8.10.2 / compileSdk 36）仍失败**于 `:composeApp:checkDebugAarMetadata`（Miuix AAR 要 compileSdk 37）。**升 AGP 9.5.1 + Gradle 9.6.1 + compileSdk 37 + 两开关后 ✅ 通过**，出 APK 13,575,772 B（§八）。无 wrapper：本机用 PATH 上的 `gradle` |
| `./gradlew :composeApp:run` | ✅ **目的达成，命令形式不同**：没跑 `gradle run`，而是 `gradle :composeApp:printRuntimeClasspath` + `java -cp … vade.spike.miuix.MainKt --section=<slug>` 起同一个 `MainKt`，再用 `tools/capture-window.ps1` 截真窗口。**7 张 466×893** |
| `./gradlew :composeApp:wasmJsBrowserProductionWebpack` | ✅ **通过**（两端一起，2m16s）；产物体积见 §四。dev server / 浏览器打开**未做** |
| `ls spike/miuix/shots/ \| wc -l` | ✅ **15**（8 张无头 + 7 张真实窗口；dialog 那张是内容表面，见 §三.3） |

**按目的计 4/4**：Desktop 有真实窗口图、Wasm 有构建与体积、Android 有 APK（升级矩阵下）、截图数量达标。
**按命令字面计 3.5/4**：`gradle run` 用等价的 `java -cp` 代替；`assembleDebug` 在仓库现状矩阵下仍是红的（红的原因已定位并给出已验证解法）。

---

## 七、对 U2 的工时建议（`51-U2-skin-miuix.md`）

### 7.1 前置阻塞（不属 U2 目录，但决定 U2 能不能收工）

| 事项 | 归属 | 建议工时 | 说明 |
|---|---|---|---|
| 构建链升级：**AGP 9.5.1 + Gradle 9.6.1 + `compileSdk = 37` + `android.builtInKotlin=false` + `android.newDsl=false`** | U1 / 构建负责人 | **0.5–1 天** | 不做则 U2 的 `assembleDebug` 永远红，Android 的验收截图无从谈起。**这条已在 §八 实测走通并出 APK**，改动只有 4 行（根 `build.gradle.kts` 的 AGP 版本、`composeApp/build.gradle.kts` 的 `compileSdk`、`gradle.properties` 的两个开关 + `suppressUnsupportedCompileSdk=37`），`settings.gradle.kts` 不动 |
| ~~AGP 8.13.2 + Gradle ≥ 8.14.4 就够~~ | — | — | ❌ **旧结论已推翻**：AGP 8.13.2 在 `checkDebugAarMetadata` 被 androidx compose 1.12.0 的 AAR 挡住（要求 AGP ≥ 9.1.0），`BUILD FAILED in 2m 45s`。别再试 8.x 末版 |
| `material3-window-size-class:1.12.0-alpha03` 等传递 alpha 依赖钉版本 | U1 | 0.25 天 | R2 |
| **AGP 10 的迁移预留** | U1 / 架构 | U2 之后单排 | 两个开关在 AGP 10 会移除（构建日志原文：`It will be removed in version 10.0 of the Android Gradle plugin`）。到期只能拆 `androidApp` 模块 + `com.android.kotlin.multiplatform.library`。现在不做，但要在 U1 的路线图里留一行 |

### 7.2 U2 自身

| 任务 | 计划 | 建议 | 依据（本 spike 实测） |
|---|---|---|---|
| T1 tokens（1d） | 1 d | **1 d**（可压到 0.5） | `MiuixTheme(colors = lightColorScheme())` 现成，工作主要是覆写语义色 + 深色 + 形状。**必须覆写 `CardDefaults`/`CardColors`**（R5：默认 surface 与 background 同为 `#FFFFFF`）；深色没做，是本 spike 的缺口 |
| T2 七个组件（3d） | 3 d | **5 d** | 5 个薄包装（Button/TextField/Card/ListItem/Switch，各 0.5 d ≈ 2.5 d）；`BottomBar` 三态（fixed / floating 胶囊 / floating+blur）**1.5 d**，本 spike 只做了 fixed 且没接 haze；`ApprovalDialog` **1 d**，上游 `OverlayDialog` 在 Desktop 上不绘制（R6），必须自建 in-window 遮罩 + 卡片，且要自己保证可截图 |
| T3 三端适配（1.5d） | 1.5 d | **2 d** | Desktop 密度补偿 0.5 d（480×420 / 466×893 两套渲染器可直接拉 1.15/1.25 比对）；Wasm 体积核对 **0 d**（实测无增量，只需抄 §四）；Android inset + 密度 0.5 d（前置升级做完才能开工）；模糊降级 0.5 d（haze 在 wasm 完全未验证，R4） |
| T4 九张验收截图（1d） | 1 d | **1 d**（比上版降 0.5） | 截图链路已经现成：`tools/capture-window.ps1` 一条命令一张真实窗口图，`Shot.kt` 支持只渲一个 section。剩下的成本是「聊天页 + 工具卡片 + 底栏」三个组合场景的构造，不是工具 |
| **合计** | **6.5 d** | **9 d**（+ 前置 0.5–1 d） | 超出计划的 2.5 d 主要来自 BottomBar 三态、ApprovalDialog 自建（R6），不是 Miuix 难用 |

### 7.3 需要改判据的两条

- `51-U2` §六「**体积**：Wasm 增量 ≤ C0c 预估的 120%」→ 本测把「预估 900 KB」证伪了。建议改成**绝对值**判据：`app.wasm + 主 JS` 的 gzip 总量 **≤ 1.15 MB**（当前 1.01 MiB，留 ~12% 余量给真实页面），并要求 U2 在含 Material 皮肤的工程里报一次相对增量（§4.2）。
- `12-C0c` §T3「Desktop：`./gradlew :composeApp:run` 出窗口截图」→ 建议把**无头渲染 + 真实窗口截图**两条路都写进任务书：CI 无显示器时用 `ImageComposeScene`，组件级验收用 `tools/capture-window.ps1`；并明确「popup 类组件（dialog/sheet）必须另证能在目标平台弹出」，否则会像本次一样出现「截到了按钮、没截到弹窗」。

---

## 八、Android 后续验证（AGP 9.5.1 + Gradle 9.6.1，已验证出 APK）

> 这一节回答一个问题：**Android 到底能不能构，需要什么。** 结论：能，但要动构建矩阵，且不是「随便升个 8.x 末版」就行。

### 8.1 成功组合（可复制）

| 项 | 本仓库现状 | 验证时改成 |
|---|---|---|
| AGP | `8.8.2` | **`9.5.1`**（根 `build.gradle.kts` 的 `id("com.android.application") version …`） |
| Gradle | `8.10.2`（PATH） | **`9.6.1`**（`gradle-9.6.1/bin/gradle`，无 wrapper 改动） |
| `compileSdk` | `36` | **`37`**（`composeApp/build.gradle.kts` 的 `android { compileSdk = … }`） |
| `gradle.properties` | `android.suppressUnsupportedCompileSdk=36` | **`=37`** |
| AGP 9 兼容开关 | 无 | **`android.builtInKotlin=false` + `android.newDsl=false`** |
| `settings.gradle.kts` | — | **不动** |

结果（原始日志 `build-B2.log`）：

```
BUILD SUCCESSFUL in 2m 59s
```

产物与 `aapt2 dump badging`：

```
composeApp/build/outputs/apk/debug/composeApp-debug.apk   13,575,772 B
sha256 ac7f518841d9fb393550e45199383f73f12371bbcdbcb4f03fcb42368cd22547
package: name='vade.spike.miuix' versionCode='1' versionName='0.0.1'
         platformBuildVersionCode='37' compileSdkVersion='37'
minSdkVersion:'24'
targetSdkVersion:'36'
```

同一套矩阵下 Desktop 与 Wasm 也仍然编得过（`:composeApp:desktopMainClasses` + wasm production 链路，`BUILD SUCCESSFUL in 27s`）—— 升级不会把另外两端搞坏。

### 8.2 三条失败链（按踩到的顺序，原文录入）

**① AGP 8.13.2 + Gradle 8.14.4（`BUILD FAILED in 2m 45s`）** —— 卡在 AAR metadata，不是 compileSdk：

```
Dependency 'androidx.compose.ui:ui-graphics-android:1.12.0' requires Android Gradle plugin 9.1.0 or higher.
This build currently uses Android Gradle plugin 8.13.2.
...（共 11 个 androidx 1.12.0 AAR 同样报，含 material3-window-size-class-android:1.5.0-alpha22）
```

→ **「AGP 8.13.2 就够」的旧结论到此作废。** 8.x 线再怎么升都过不了 androidx 1.12 的 AGP 下限。

**② AGP 9.5.1 + Gradle 9.6.1 裸跑（`BUILD FAILED in 1m 2s`）** —— KMP 插件不兼容：

```
The 'com.android.library' (or 'com.android.application') plugin is not compatible with the
'org.jetbrains.kotlin.multiplatform' plugin since AGP 9.0.
Solution:
  - [Recommended] Replace the 'com.android.library' plugin with the
    'com.android.kotlin.multiplatform.library' plugin
  - Or set the Gradle property 'android.builtInKotlin=false' and 'android.newDsl=false'
    to temporarily bypass this issue.
```

→ 官方推荐路径是 `com.android.kotlin.multiplatform.library`，但**那是 library 插件，我们的 `composeApp` 是 application 模块**，直接换不掉；只能走两个开关的临时绕行。

**③ 只开 `android.builtInKotlin=false`（`BUILD FAILED in 7s`）** —— 还不够：

```
The 'org.jetbrains.kotlin.multiplatform' plugin with `androidTarget()` enabled is not compatible
with AGP's 9.0 new DSL (`android.newDsl=true` is enabled by default).
```

→ **必须两个开关一起关**。少一个都停在配置阶段。

### 8.3 风险（写进 U1 路线图，不在 U2 处理）

- **两个开关是 deprecated 的临时绕行。** 构建日志原文：`The option setting 'android.builtInKotlin=false' is deprecated. … It will be removed in version 10.0 of the Android Gradle plugin.` `android.newDsl=false` 同样。**AGP 10 一到就得拆 `androidApp` 模块**（shared 用 `com.android.kotlin.multiplatform.library`，app 壳单独一个 Android 模块）。
- **Gradle 9.6.1 自己也报警**：`Deprecated Gradle features were used in this build, making it incompatible with Gradle 10` —— 升级路径上还有第二处要还的债。
- **`compileSdk = 37` 依赖 SDK 里的 `platforms/android-37.0`**（带小版本号的目录，仓库里没有裸 `android-37`）。AGP 9.5.1 能解析它，**AGP 8.8.2 不能**（§十 P2 的三次绕行都是这个原因）。CI 上装 SDK 要装 `android-37.0`，别按 `android-37` 找。
- **只有 debug APK，没有运行验证**：没装模拟器/真机，没截图，minSdk 24 的运行时行为未知。13.5 MB 是 debug 未裁剪的数字，release + R8 后的体积未测。

---

## 九、本机适配清单（已改在构建文件里，记录在此）

首轮跑在 aarch64 Linux 8 核 / ~5 GB 的机器上；本轮 Windows 11 16 核 / 16 GB。改动只在 `spike/miuix/` 内：

| 文件 | 改动 | 原因 |
|---|---|---|
| `gradle.properties` | 删 `android.aapt2FromMavenOverride=/usr/lib/android-sdk/build-tools/debian/aapt2` | aarch64 Linux 专有 hack（Google 只发 x86_64 aapt2）；Windows 上 AGP 自带 aapt2 可用 |
| `gradle.properties` | `-Xmx2g` → `-Xmx4g`、`MaxMetaspaceSize 768m` → `1g`、`workers.max 2` → `4`、`parallel false` → `true`、`kotlin.daemon.jvmargs 1536m` → `2g` | 本机内存 3 倍于首轮，放开并行；首轮是「两台其他 agent 的 daemon 抢内存」才压到 2g/2 workers |
| `gradle.properties` | 删 `kotlin.compiler.execution.strategy=in-process` | 该值是为「daemon 起不来」设的兜底，本机不需要；留着会让所有编译进 Gradle daemon，反而慢 |
| `gradle.properties` | 删「KGP refuses to wire the npm/webpack graph until [useAndroidX] is on」这条解释 | 与实际因果不符（`android.useAndroidX` 只影响 Android 变体的 androidx 依赖解析），随手清理 |
| `gradle.properties` | 重写 Wasm 工具链那段注释 | 原文声称根 build 脚本设了 `NodeJsRootExtension.download=false`，**代码里没有这段**；已改为与实测一致的描述（见 §十 P5） |
| `composeApp/build.gradle.kts` | `TargetFormat.Deb` → `TargetFormat.Msi` | jpackage 在 Windows 只能出 Msi/Exe；spike 实际不打包，只需配置阶段能过 |
| `composeApp/build.gradle.kts` | 注释补 `moduleName` → `outputModuleName`（KGP 2.4.20 已无 `moduleName`） | 记录踩过的坑 |
| `settings.gradle.kts` | `include(":baselineApp")` | 体积对照组（纯 Material3，同一套工具链） |
| `settings.gradle.kts` | 保留 `maven.aliyun.com` 镜像在前 | 本机到 repo1 慢；`androidx.navigationevent` 只在 Google Maven 有，`google()` 不能删 |
| `local.properties` | **从版本库删除**（原本被跟踪，内容是 `sdk.dir=/usr/lib/android-sdk`） | 主机路径不该进库；本机 SDK 走 `ANDROID_HOME=D:/Hermes/tools/android-sdk` |
| `.gitignore`（新增） | `local.properties` / `.gradle/` / `build/` / `node_modules/` / `kotlin-js-store/` | 防止再被误提交 |
| **本轮** `composeApp/.../App.kt` | `Cards()` 的两张 Card 包进 `Column`（间距 12 dp） | 修 showcase 重叠 —— R5 的「首行被裁」真因 |
| **本轮** `composeApp/.../App.kt` | `Dialogs()` 的 `show` 默认 `false` + 注释按实情重写；抽 `ApprovalActions()`；新增 `DialogSurfaceShot()` | `OverlayDialog` 在 Desktop 不绘制（R6），dialog 截图改走内容表面 |
| **本轮** `composeApp/.../Shot.kt` | dialog 段绕过 `OverlayDialog`；新增「只渲一个 section」的可选第二参数 | `ImageComposeScene` 同样拿不到 popup 层；整轮重渲太慢 |
| **本轮** `tools/capture-window.ps1`（新增） | 真实窗口截图链路固化（置顶 + DWM 边框 + `CopyFromScreen`） | 原来只存在于临时目录，会被清理；本文要能复现 |
| **本轮** `shots/*` | 15 张 PNG（7 真实窗口 + 7 无头 + 1 内容表面） | 见 §三 |
| **未改** | Miuix 依赖版本、任何 Miuix 源码 | 任务书 §六禁止 |

> 遗留不实注释（本次未动，留给下次）：`settings.gradle.kts:2` 仍写「use `/opt/gradle/gradle-8.10.2/bin/gradle`」，那是 Linux 路径；本机是 PATH 上的 `gradle` 8.10.2。

---

## 十、踩坑（按踩到的顺序）

**P1 · `:composeApp:assembleDebug` → `checkDebugAarMetadata` 失败（阻塞，已解）**

```
Dependency 'top.yukonga.miuix.kmp:miuix-ui-android:0.9.4' requires libraries and applications
that depend on it to compile against version 37 or later of the Android APIs.
:composeApp is currently compiled against android-36.
```

`miuix-core-android:0.9.4` 同样报。已解包 AAR 核对四个模块的 `META-INF/com/android/build/gradle/aar-metadata.properties`，全部 `minCompileSdk=37` / `minCompileMinorSdk=0`。解法见 §八。

**P2 · 在 AGP 8.8.2 上三次绕行全部失败（原文录入，避免后人重踩）**

a. `compileSdk = 37`：
```
Failed to find Platform SDK with path: platforms;android-37
```
本机 `D:/Hermes/tools/android-sdk/platforms/` 只有 `android-36` 和 **`android-37.0`**（带小版本号的目录），SDK 仓库里没有裸 `android-37`。

b. `compileSdkVersion("android-37.0")`：
```
Unsupported value: android-37.0. Format must be one of:
android-31 / android-31-ext2 / android-T / vendorName:addonName:31
```
AGP 8.8.2 的平台名解析器根本不认识 `major.minor` 形式。

c. 用 NTFS 目录联接（junction）把 `platforms/android-37` 指向 `android-37.0`：
```
Observed package id 'platforms;android-37.0' in inconsistent location
'...\platforms\android-37' (Expected '...\platforms\android-37.0')
```
SDK 元数据按目录名反查，junction 只会让包管理器认为「位置不一致」，仍然找不到 `android-37`。

→ **结论：AGP 8.8.2 用不了 API 37（带小版本号的平台）。** 升 AGP 9.5.1 后同一个 `platforms/android-37.0` 能被正常解析（§八 已实测）。

**P3 · 构建被 kill 后 `:kotlinWasmToolingSetup` 假故障**

```
Cannot access output property 'destination' ...
Failed to create MD5 hash for file content
```
不是代码问题，是上一次被中断的 Gradle/Kotlin daemon 仍占着 tooling 输出文件。解法：`gradle --stop` + 删掉 `build/` 后重跑，一次过。

**P4 · `OverlayDialog` 在 Desktop 上不创建 popup 窗口** —— 点击触发按钮后进程窗口列表仍只有一个 `SunAwtFrame`，遮罩/弹窗都不出现；`ImageComposeScene` 同源不画。见 §五 R6、§三.3。

**P5 · Node 工具链：`gradle.properties` 那句注释是错的，而且比它写的更绕**

实情：工程里**没有任何** `NodeJsRootExtension.download = false`（`build.gradle.kts` 里不存在这段代码）。KGP 保持默认 `download = true`，自己往 `$GRADLE_USER_HOME/nodejs` 下了一份 **node-v26.2.0-win-x64**（还有 `yarn-v1.22.22`）—— **webpack 跑的不是 PATH 上的系统 node 22.23.2**。npm registry 是用户侧 `~/.npmrc` 钉的 `https://registry.npmmirror.com`（并配了 `127.0.0.1:7897` 代理），不在库里。注释已按这个实情改写。

**P6 · 本机 Gradle 用户目录不在默认位置** —— `GRADLE_USER_HOME=D:/Hermes/tools/gradle-home`。查依赖版本要去那里翻缓存，`~/.gradle` 里没有。

**P7 · 「Card 首行被裁」被误判成 Miuix 的 `insideMargin`** —— 真因是 showcase 里两个 Card 在 `Box` 里重叠（`Cards()` 少了 `Column`），第二张 Card 的表面盖住了第一张的首行与整段说明。判据：真实窗口与无头渲染出现**同一处裁切** → 说明是布局代码而不是渲染器；修 `composeApp` 后两者同时消失。并存的另一条真问题（Card 表面与页面同色）用像素取样单独证实，见 R5。

**P8 · AGP 9 需要两个开关，缺一不可** —— 只关 `builtInKotlin` 会停在 `android.newDsl=true is enabled by default`（§八 8.2 ③）。两个都关之前，官方推荐的 `com.android.kotlin.multiplatform.library` 对 application 模块不适用。

---

## 十一、复现命令（本机当前状态）

```bash
cd spike/miuix
source D:/Hermes/tools/env/vade-env.sh     # JAVA_HOME / ANDROID_HOME / GRADLE_USER_HOME / gradle 8.10.2

# 1) Desktop 无头渲染（8 张 480x420；第二参数可只渲一个 section）
gradle :composeApp:screenshot
# 等价的手动版：java -Djava.awt.headless=true -cp "$(cat cp.txt)" vade.spike.miuix.ShotKt shots dialog

# 2) Desktop 真实窗口截图（7 张 466x893）
gradle :composeApp:desktopMainClasses :composeApp:printRuntimeClasspath -q | tail -1 > cp.txt
for s in button textfield card listitem switch topappbar bottombar; do
  powershell -NoProfile -ExecutionPolicy Bypass -File tools/capture-window.ps1 \
    -Java "$JAVA_HOME/bin/java.exe" -ClassPathFile cp.txt \
    -Section $s -OutFile shots/miuix-window-$s.png -NoDesktopShot
done

# 3) Wasm 生产构建 + 体积（两端一起，2m16s）
gradle :composeApp:wasmJsBrowserProductionWebpack :baselineApp:wasmJsBrowserProductionWebpack
for f in composeApp/build/kotlin-webpack/wasmJs/productionExecutable/*.{wasm,js}; do
  echo "$(stat -c %s "$f") $(gzip -9 -c "$f" | wc -c) $f"; done

# 4) Android：本仓库矩阵（AGP 8.8.2）必失败，只用来复现 P1
gradle :composeApp:assembleDebug

# 5) Android：已验证矩阵（AGP 9.5.1 + Gradle 9.6.1）
#    改 4 处：根 build.gradle.kts AGP=9.5.1 / composeApp compileSdk=37 /
#    gradle.properties 加 android.builtInKotlin=false + android.newDsl=false
#    并把 android.suppressUnsupportedCompileSdk 改成 37
D:/Hermes/tools/gradle-9.6.1/bin/gradle :composeApp:assembleDebug
ls -l composeApp/build/outputs/apk/debug/composeApp-debug.apk   # 13,575,772 B
D:/Hermes/tools/android-sdk/build-tools/36.0.0/aapt2 dump badging \
  composeApp/build/outputs/apk/debug/composeApp-debug.apk | head -3
```

---

## 十二、未验证 / 缺口（交付即承认，别当已完成）

- ⚠️ Android：**有 debug APK（13,575,772 B，AGP 9.5.1 矩阵），但没有模拟器/真机截图**，没有运行验证；release/R8 体积未测；APK 体积无基线对照。
- ⚠️ Android 构建矩阵：`9.5.1 + 两开关` 是**临时绕行**，AGP 10 会移除这两个开关；正式迁移（拆 `androidApp` 模块）未做。
- ❌ Desktop：未跑 `packageMsi`/`createDistributable`，桌面产物体积缺失；未测深色主题。
- ❌ Wasm：没有浏览器运行时验证（未跑 dev server、未开 Playwright、无 wasm 截图）；`ComposeViewport` 真机挂载成功与否未知。
- ❌ `haze` / 模糊底栏：完全未接。
- ⚠️ Dialog：**只有内容表面渲染（`shots/miuix-desktop-dialog.png`），没有真弹窗**；`OverlayDialog` 在 Desktop 不绘制（§五 R6）。
- ❌ 交互态：TextField 聚焦/清除/错误态、Switch 动效、TopAppBar 滚动联动未验证（BottomBar 点击切换已验证，见 `miuix-window-bottombar.png`）。
- ⚠️ Card 表面色/描边（R5）：已确认默认无对比度，未验证自定义 `CardColors` 的效果。
