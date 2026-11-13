# U2 · `miuix` 皮肤

| 项 | 值 |
|---|---|
| Agent 代号 | `u2-miuix` |
| 波次 | W5 |
| 依赖 | **C-09 `Skin.kt` 冻结**（U1 第 3 天）、C0c 的版本矩阵 |
| 目录所有权 | `ui/composeApp/src/commonMain/kotlin/skin/miuix/` |
| 产出 | **11 个组件**（D93：原 7 + `MessageBubble`/`AttachmentChip`/`ToolCallCard`/`InputBar`）+ 三端 9 张验收截图 |
| 出口 | 独立验收：miuix × 3 底栏 × 3 平台全部通过 |

## 一、一句话任务

用 Miuix 实现一套和 Material **质感明显不同**的皮肤：更方、更紧凑、分隔线而非阴影；三端都能跑，底栏三态都不塌。

## 二、边界

| 做 | 不做 |
|---|---|
| 实现 `Skin` 的 **11 个组件** + 4 组 token（新增 4 个只要求默认/禁用/错误三态） | 不改 `Skin.kt`（要改就 RFC） |
| 三端密度适配、模糊底栏 | 不改业务页面、不改构建文件（依赖 U1 已加） |
| 版本锁定与 API 变更隔离 | 不重写 Miuix 源码 |
| 9 张截图（3 平台 × 3 底栏） | 不做 glass（U3）、不做 iOS（U4） |

## 二·五、接入方式（不改 U1 的文件）

在 `skin/miuix/` 里提供一个注册入口，由 U1 的 `SkinRegistry` 调用：

```kotlin
// skin/miuix/MiuixSkin.kt
object MiuixSkinEntry {
    fun register() = SkinRegistry.register("miuix") { MiuixSkin() }
}
```

- **不要改 `skin/Skin.kt`、`SkinRegistry.kt`、构建文件**——它们归 U1。
- U1 会在 `SkinRegistry` 里留一个「按类名反射/显式 import 调 `register()`」的接缝；你只需保证 `register()` 可被无参调用且幂等。
- 不注册 = 设置页里永远选不到 miuix，皮肤等于没交付。

## 三、必读

- `docs/design.md` §9.2（Skin 接口）、§9.3（三皮肤矩阵）
- `docs/requirements.md` A10（FR-8.14–8.20）、A12、AC-13/AC-15
- `spike/miuix/RESULT.md`（C0c：版本矩阵、已知坑、体积）
- `ui/composeApp/src/commonMain/kotlin/skin/Skin.kt`（C-09）

## 四、设计语言（必须和 material 拉开差距）

| 维度 | material | miuix |
|---|---|---|
| 形状 | 大圆角（16–28dp）、无描边 | 小圆角（8–12dp）、1dp 描边/分隔线 |
| 层级 | 阴影 | 描边 + 背景色阶，几乎无阴影 |
| 密度 | 舒适（行高 56dp） | 紧凑（行高 48dp） |
| 按钮 | 填充为主 | 描边/文字为主，填充仅用于主操作 |
| 动效 | 标准 easing | 更短、更直接（120–180ms） |

不要直接把 Material 组件换色冒充 Miuix——审查会按「是否只换了颜色」判。

## 五、任务分解

### T1 · tokens（1 天）

```kotlin
object MiuixTokens {
    val light = ColorScheme(...)   // 背景/表面/主色/危险/文字三级
    val dark  = ColorScheme(...)
    val shapes = ShapeScheme(card = 12.dp, button = 8.dp, field = 8.dp, dialog = 16.dp)
    val type   = TypeScale(...)    // 比 material 小一号
    val motion = MotionSpec(fast = 120, normal = 180, easing = ...)
}
```

- 颜色只用语义名（`surface`, `onSurface`, `primary`, `danger`…），不散落硬编码色值。
- 深色模式必须一起做（同一套语义，不同值）。

### T2 · 11 个组件（3 天；新增 4 个只做三态）

| 组件 | 要点 |
|---|---|
| `Button` | 4 种 style；按压态用背景压暗，不用涟漪扩散到全屏 |
| `TextField` | 下划线/描边风格；聚焦时描边变色；错误态文案在下方 |
| `Card` | 描边 + 表面色；圆角 12dp；无阴影 |
| `ListItem` | 48dp 行高；左图标 20dp；右侧 trailing；分隔线只在列表内画（最后一项不画） |
| `Switch` | Miuix 原生；尺寸与 Material 不同，注意对齐 |
| `BottomBar` | 三态：Fixed / Floating(12dp 边距，胶囊) / FloatingBlur(haze) |
| `ApprovalDialog` | 完整参数区可滚动；按钮竖排（危险操作在下）；倒计时可见 |

- 优先用 Miuix 上游组件；上游没有的（如 ApprovalDialog）用 Miuix 的 Surface/Text/Button 组合，**不要**引 Material 组件。
- 所有组件必须有 `@Preview`（Desktop target 可跑），预览参数覆盖：默认/禁用/错误/深色。

### T3 · 三端适配（1.5 天）

| 端 | 处理 |
|---|---|
| Android | 原生密度；`FloatingBlur` 用 `haze`；检查导航栏 inset |
| Desktop | **密度补偿**：Miuix 按移动端设计，桌面用 `LocalDensity` 包一层（缩放 1.15–1.25，按实测调），否则控件过小 |
| Wasm | 体积核对（C0c 预估 ~900KB）；模糊效果不可用时降级为半透明 + 描边，不报错 |

桌面密度补偿要写成可配置常量，并在 README 记录取值依据（截图对比）。

### T4 · 验收截图（1 天）

`ui/shots/miuix/<platform>-<bar>.png`，共 9 张（路径以 `ui/` 为根，与 U1/U3 和 F1 的 `ls` 一致）：

| 平台 \ 底栏 | fixed | floating | floating+blur |
|---|---|---|---|
| Android | ✓ | ✓ | ✓ |
| Desktop | ✓ | ✓ | ✓ |
| Web | ✓ | ✓ | ✓ |

每张必须包含：聊天页（有消息 + 工具卡片）、底栏、输入框；**不能**只截一个空页面。

## 六、验收

```bash
./gradlew :composeApp:assembleDebug
./gradlew :composeApp:run
./gradlew :composeApp:wasmJsBrowserProductionWebpack
ls ui/shots/miuix/*.png | wc -l      # 9
./gradlew :shared:allTests           # U1 的 import 防回退测试仍绿
```

| 判据 | 说明 |
|---|---|
| 9 组合无塌陷 | 底栏不遮输入框、文字不截断、弹窗不被底栏压住 |
| 与 material 可区分 | 盲测：两张截图并排，能指出差异（形状/密度/层级） |
| 深色模式 | 9 张中至少 3 张为深色 |
| 运行时切换 | 不重启、不丢会话 |
| 体积 | Wasm 增量 ≤ C0c 预估的 120%，超出要说明 |

## 七、禁止

- 禁止只换颜色（审查第一眼就会看形状与密度）。
- 禁止在 `skin/miuix/` 里 import `androidx.compose.material3.*`。
- 禁止改 `Skin.kt`、业务页面、构建文件。
- 禁止 fork Miuix 源码；上游 bug 用 workaround + 记录。
- 禁止用 `@OptIn` 掩盖 experimental API 而不记录。

## 八、交付格式

按 `00-orchestration.md` §三，额外附：9 张截图（含 3 张深色）、与 material 的并排对比图、桌面密度取值说明、Wasm 体积对比、上游 API 变更风险清单。
