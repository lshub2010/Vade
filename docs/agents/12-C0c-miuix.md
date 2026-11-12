# C0c · 验证 Miuix on Compose Multiplatform（spike）

| 项 | 值 |
|---|---|
| Agent 代号 | `c0c-miuix` |
| 波次 | W0，与 C0a / C0b 并行 |
| 硬时限 | **3 个工作日** |
| 目录所有权 | `spike/miuix/` |
| 产出 | 三端可运行 demo + 截图 + `spike/miuix/RESULT.md` |

## 一、一句话任务

证明 Miuix 能在 Compose Multiplatform 上跑 Android / Desktop / Wasm 三端，并量出它带来的体积代价与 API 稳定性风险。

## 二、为什么先验

`miuix` 是 v1 必做皮肤（D23），且它不是 Material 的换色——按钮、列表项、对话框的形状逻辑完全不同。如果它只能在 Android 用，U2 的整个计划要改，越晚发现越贵。

## 三、必读

- `docs/design.md` §9.1–§9.3（分层、Skin 接口、三皮肤矩阵）
- `docs/requirements.md` A10（FR-8.14～8.20）、A11
- `docs/agents/01-contracts.md` §六

## 四、任务分解

### T1 · 最小工程（0.5 天）

```
spike/miuix/
├── settings.gradle.kts
├── build.gradle.kts
├── composeApp/
│   ├── build.gradle.kts        # androidTarget / jvm("desktop") / wasmJs
│   └── src/
│       ├── commonMain/kotlin/App.kt
│       ├── androidMain/
│       ├── desktopMain/
│       └── wasmJsMain/
```

版本矩阵必须记录到 `RESULT.md`（Kotlin / CMP / AGP / Miuix / haze 各是多少，从官方仓库取当前稳定版，不要用猜的版本号）。**锁定版本**，并在注释里写明锁定理由。

### T2 · 8 个组件渲染（1 天）

按 `Skin` 接口（C-09）将来要用的最小集合，逐个渲染并截图：

| # | 组件 | 关注点 |
|---|---|---|
| 1 | Button（主/次/危险） | 形状、圆角、按压反馈 |
| 2 | TextField | 聚焦态、清除按钮、错误态 |
| 3 | Card | 阴影/描边、嵌套圆角 |
| 4 | ListItem | 左右插槽、副标题、点击态 |
| 5 | Switch | 尺寸与动效 |
| 6 | BottomBar | 图标 + 文字 + 选中态 |
| 7 | Dialog / 确认弹窗 | 遮罩、按钮排布 |
| 8 | TopAppBar | 滚动联动（若支持） |

每张截图命名 `miuix-<platform>-<component>.png`，放 `spike/miuix/shots/`。

### T3 · 三端跑通（1 天）

| 端 | 验收 | 做不到时 |
|---|---|---|
| Android | 编译出 APK；有模拟器则安装截图 | 无模拟器 → 只做 `assembleDebug` 成功 + 标注「未真机验证」 |
| Desktop | `./gradlew :composeApp:run` 出窗口截图 | 无显示环境 → 用 `xvfb-run` 截图；仍不行则标注 |
| Wasm | `./gradlew :composeApp:wasmJsBrowserDevelopmentRun`，Playwright 打开截图 | 浏览器不可用 → 标注，但必须给出 `wasmJsBrowserProductionWebpack` 的体积数字 |

**Wasm 是这条 spike 的重点**：Miuix 依赖 Skia/Compose 的哪些能力、是否用了 Web 不支持的 API（如某些模糊效果），要在这里暴露。

### T4 · 体积与降级（0.5 天）

| 指标 | 期望 | 实测填这里 |
|---|---|---|
| Wasm 包 gzip（含 Miuix） | 设计预估 ~900KB 增量 | |
| Wasm 包 gzip（纯 Material 基线） | 对照组 | |
| Desktop 产物体积 | — | |
| APK 体积增量 | — | |
| `haze`/模糊在 Wasm 上是否生效 | 预期降级为无模糊 | |

若 Miuix 在 Wasm 上体积超 1.5MB 或直接编不过，给出**降级方案**：Web 端 `miuix` 皮肤退化为「Material + Miuix 配色/圆角 token」，并写明这会让 AC-13 的哪一项打折。

### T5 · 结论

`RESULT.md`：结论（三端可行 / 仅 Android+Desktop / 不可行）、版本矩阵、8 组件截图索引、体积表、API 风险（哪些 API 是 experimental、升级会断在哪）、对 U2 的工时建议。

## 五、验收

```bash
cd spike/miuix && ./gradlew :composeApp:assembleDebug          # Android
./gradlew :composeApp:run                                      # Desktop（或 xvfb-run）
./gradlew :composeApp:wasmJsBrowserProductionWebpack           # 出体积
ls spike/miuix/shots/ | wc -l                                  # ≥ 8
```

## 六、禁止

- 禁止改 Miuix 源码来"让它编过"——要记录上游问题与 workaround。
- 禁止只跑 Android 就宣布三端可行。
- 禁止用 `@OptIn` 掩盖所有 experimental API 而不记录（每个 OptIn 都要在结果里列出来）。

## 七、交付格式

`RESULT.md` + 截图目录 + 一份 `versions.lock.md`（版本矩阵与锁定理由）。
