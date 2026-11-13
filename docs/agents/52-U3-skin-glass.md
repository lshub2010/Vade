# U3 · `glass` 皮肤

| 项 | 值 |
|---|---|
| Agent 代号 | `u3-glass` |
| 波次 | W5 |
| 依赖 | **C-09 `Skin.kt` 冻结**、C0c 的版本矩阵 |
| 目录所有权 | `ui/composeApp/src/commonMain/kotlin/skin/glass/` |
| 产出 | **11 个组件**（D93：原 7 + `MessageBubble`/`AttachmentChip`/`ToolCallCard`/`InputBar`）+ 9 张截图 + 给 U4 的 `GLASS_SPEC.md` |
| 出口 | 独立验收：glass × 3 底栏 × 3 平台；Android 12- 不崩不白屏 |

## 一、一句话任务

做一套「毛玻璃」皮肤，但**不假装所有平台都能折射**：Android 13+ 有 AGSL 折射，Desktop/Web 只做模糊，Android 12- 降级为模糊 + 描边，三端都不能崩。

## 二、边界

| 做 | 不做 |
|---|---|
| **11 个组件** + glass token（新增 4 个只要求默认/禁用/错误三态） | 不改 `Skin.kt`、业务页面、构建文件 |
| Android AGSL 折射 + 降级 | 不做 miuix（U2） |
| Desktop/Web 模糊（不折射） | 不做 iOS 实现（U4），只给 spec |
| 性能保护与降级开关 | 不引第二个模糊库（用 U1 预置的 haze） |

## 二·五、接入方式（不改 U1 的文件）

```kotlin
// skin/glass/GlassSkin.kt
object GlassSkinEntry {
    fun register() = SkinRegistry.register("glass") { GlassSkin() }
}
```

- 与 U2 同一手法：只提供 `register()`，不改 `Skin.kt` / `SkinRegistry.kt` / 构建文件。
- Web 端不支持 `backdrop-filter` 时降级为不模糊，**不是不注册**——降级与否由 Skin 内部按平台判断。

## 三、必读

- `docs/design.md` §9.3（glass 矩阵）、§11（被否方案）
- `docs/requirements.md` A12 决议 2（只做模糊不折射）、FR-8.23（AGSL 降级）、AC-14/AC-15
- `spike/miuix/RESULT.md`（C0c 的 Web 能力结论）
- `ui/.../skin/Skin.kt`

## 四、跨端强度（按 D22，不许越界）

| 平台 | 模糊 | 折射 | 实现 |
|---|---|---|---|
| Android 13+ (API 33) | ✓ | ✓ | `haze` 取背景 + `RuntimeShader`(AGSL) 边缘折射 |
| Android 12- | ✓ | ✗ | `haze` 模糊 + 1dp 描边 + 高光渐变 |
| Desktop | ✓ | ✗ | Skia 模糊（`Modifier.blur` 或 haze 的 desktop 支持） |
| Web | ✓（尽力） | ✗ | 见 T3 的降级判定 |
| iOS | ✓ | ✓ | 系统 `TabView` 玻璃（U4 实现） |

## 五、任务分解

### T1 · tokens 与玻璃层（1.5 天）

```kotlin
object GlassTokens {
    val tintLight = Color.White.copy(alpha = 0.42f)
    val tintDark  = Color.Black.copy(alpha = 0.38f)
    val stroke    = Color.White.copy(alpha = 0.35f)   // 1dp
    val highlight = Brush.verticalGradient(...)        // 顶部高光
    val blurRadius = 24.dp
}
```

玻璃组件统一结构（避免每个组件各写一套）：

```kotlin
@Composable
fun GlassSurface(modifier: Modifier, shape: Shape, content: @Composable () -> Unit) {
    // 1. haze 背景采样 + 模糊
    // 2. tint 叠色
    // 3. 1dp 描边（内描边，避免尺寸抖动）
    // 4. 顶部高光渐变
    // 5. Android 13+ 可选：AGSL 折射（仅边缘 8dp 区域）
}
```

### T2 · 11 个组件（3 天；新增 4 个只做三态）

- `Button`：胶囊；主操作用 tint 更实的玻璃；危险操作用红色 tint（不让玻璃把红色冲淡）。
- `TextField`：玻璃底 + 内描边；聚焦时描边加亮。
- `Card`：玻璃面 + 内容区不透明（**文字底下必须有足够对比度**，玻璃只做边缘）。
- `ListItem`：行内透明，整块列表共享一层玻璃（不要每行一层，性能会崩）。
- `Switch`：轨道玻璃、滑块实心。
- `BottomBar`：三态；`FloatingBlur` = 玻璃 + 模糊；`Floating` = 玻璃不模糊；`Fixed` = 贴底玻璃。
- `ApprovalDialog`：**背景必须加深遮罩 + 面板高不透明度**。审批是安全关键路径，不能为了好看牺牲可读性。

**可读性硬指标**：正文与背景对比度 ≥ 4.5:1（用 WCAG 公式算，写进测试或交付说明）。

### T3 · 三端实现（2 天）

**Android**
- AGSL：`RuntimeShader` 采样 haze 输出，在边缘 8dp 做坐标扰动 + 轻微色散；中心区域不扰动（否则文字发虚）。
- 运行时探测：`Build.VERSION.SDK_INT >= 33` 且 shader 编译成功才启用；任何异常 → 关闭折射，只保留模糊。
- 降级路径必须有单测或 instrumented test（可用 fake shader 编译失败模拟）。

**Desktop**
- Skia 模糊；**不做折射**（D22）。窗口缩放时模糊半径不随 DPI 变化（用 dp 转换）。

**Web**
- 先试 DOM overlay + CSS `backdrop-filter`：仅当底栏能作为 DOM 元素叠在 canvas 上时可行；
- 若 Compose/Wasm 只能画在 canvas 内（大概率），用 haze/离屏快照模糊 + 半透明 tint 兜底；
- **无论走哪条，必须在交付说明里写明实际方案**，并说明与方案 §9.3「CSS backdrop-filter」的差异。不要静默改设计。

**iOS spec**
- `GLASS_SPEC.md`：tint alpha、blur radius、描边、高光、圆角、底栏三态的尺寸与安全区，附 3 张 Android/Desktop 截图供 U4 对照。
- 明确写：iOS 26+ 用系统 `TabView`，**不要自绘玻璃**（FR-8.21）。

### T4 · 性能与降级开关（1 天）

| 场景 | 要求 |
|---|---|
| 中端 Android 滚动聊天列表 | ≥ 50fps（`adb shell dumpsys gfxinfo` 或 Macrobenchmark） |
| 掉帧保护 | 连续 3 秒 < 45fps → 自动关闭折射（保留模糊），toast 提示一次 |
| 设置项 | 「性能模式」：关闭所有模糊，只留 tint + 描边 |
| 内存 | 背景快照不超过屏幕尺寸 × 2；不用时释放 |

### T5 · 截图（1 天）

9 张：3 平台 × 3 底栏，命名 `ui/shots/glass/<platform>-<bar>.png`；另加 2 张降级对照（Android 12- 模拟、性能模式），放 `ui/shots/glass/degraded-*.png`，并在 README 注明它们不对应任何 AC。

## 六、验收

```bash
./gradlew :composeApp:assembleDebug
./gradlew :composeApp:run
./gradlew :composeApp:wasmJsBrowserProductionWebpack
ls ui/shots/glass/*.png | wc -l      # 11（9 + 2 降级）
```

| 判据 | 说明 |
|---|---|
| 9 组合 | 不塌陷、不遮挡、文字不截断 |
| Android 12- | 无折射、无崩溃、无白屏（模拟器 API 31） |
| 对比度 | 正文 ≥ 4.5:1（附计算结果） |
| 性能 | 中端机 ≥ 50fps；掉帧自动降级生效 |
| 与 miuix 可区分 | 盲测能指出「半透明/模糊」差异 |
| Desktop/Web | 只有模糊，没有折射（截图 + 代码路径说明） |

## 七、禁止

- 禁止在 Desktop/Web 做折射（D22 明确不做）。
- 禁止为追求效果降低审批弹窗的不透明度。
- 禁止每行列表项各开一层模糊（性能必崩）。
- 禁止在 shader 编译失败时抛异常到 UI（必须降级）。
- 禁止改 `Skin.kt`、业务页面、构建文件。
- 禁止自绘 iOS 玻璃（U4 用系统控件）。

## 八、交付格式

按 §三，额外附：9+2 张截图、对比度计算、帧率数据、AGSL 降级测试、`GLASS_SPEC.md`、Web 实际方案与设计差异说明。
