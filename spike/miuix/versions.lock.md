# C0c · 版本矩阵与锁定理由

配套：[`RESULT.md`](./RESULT.md)。本文件只解决一件事：**为什么是这几个版本，动哪个会炸什么。**

实测主机：Windows 11 x86_64 / 16 核 / 16 GB / JDK 21.0.13 (Temurin) / Gradle 8.10.2（PATH，无 wrapper）/ `GRADLE_USER_HOME=D:/Hermes/tools/gradle-home`。实测日期 2026-11-13。

## 一、锁定表

| 组件 | 锁定值 | 来源（怎么确认的） | 锁定理由 |
|---|---|---|---|
| **Kotlin / KGP** | `2.4.20` | `miuix-ui-0.9.4.module` 的变体依赖里写死 `org.jetbrains.kotlin:kotlin-stdlib:2.4.20` | Miuix 0.9.4 的 klib/metadata 是 Kotlin 2.4 编的。KGP 只能读**不高于自己**的 metadata 版本：低于 2.4 会直接报 "class file was compiled with a newer version of Kotlin"。上浮到 2.5 需重跑三端（未验证） |
| **Compose Multiplatform** | `1.12.0` | `miuix-ui-0.9.4.module` 依赖 `org.jetbrains.compose.foundation:foundation:1.12.0`（精确版本，不是区间） | CMP 插件版本与 `compose.foundation` 运行时必须同源；插件给 1.11/1.13 会和 Miuix 拉进来的 1.12.0 运行时混版，症状是 `NoSuchMethodError` 或 `@Composable` 调用约定不一致 |
| **Miuix** | `0.9.4` | Maven Central `maven-metadata.xml`：`<latest>0.9.4</latest>`、`<release>0.9.4</release>`、`<lastUpdated>20260920082405</lastUpdated>`；已发布版本 `0.9.0 / 0.9.1 / 0.9.2 / 0.9.3 / 0.9.4-rc01 / 0.9.4` | 0.9.4 是当前最新**稳定**版（2026-09-20 发布）。不取 `0.9.4-rc01` |
| **AGP** | `8.8.2` | 工程 `build.gradle.kts`；Google Maven 上 8.x 线末版是 `8.13.2`，9.x 线已到 `9.5.1` | **被迫锁定**：Gradle 8.10.2 能带的最高 AGP 是 8.8.x（AGP 8.9+ 要求 Gradle ≥ 8.11.1）。这个锁是 Android 构建失败的直接原因，见 §三 |
| **Gradle** | `8.10.2` | `gradle --version` → `Gradle 8.10.2`，`Launcher JVM: 21.0.13 (Temurin)` | 主机镜像自带、工程无 wrapper（`settings.gradle.kts` 顶部有注释说明）。不引入 wrapper 是为了让 spike 跟着镜像走 |
| **JDK** | `21.0.13` Temurin | `java -version`；Gradle launcher 同 | 与 Kotlin 2.4 / AGP 8.8 的支持区间一致；桌面端 Skiko 用 21 无问题 |
| **compileSdk** | `36` | `composeApp/build.gradle.kts`：`compileSdk = 36` + `android.suppressUnsupportedCompileSdk=36` | ⚠️ **不够**：Miuix 的 Android AAR 要求 37，见 §三。本机 `platforms/` 只有 `android-36` 与 `android-37.0`，后者 AGP 8.8.2 不认 |
| **minSdk / targetSdk** | `24` / `36` | 同上 | v1 的最低机型覆盖；与 Miuix 的 24 下限一致 |

### 1.1 Miuix 0.9.4 在本工程里实际解析出的传递依赖

| 坐标 | 版本 | 备注 |
|---|---|---|
| `top.yukonga.miuix.kmp:miuix-core` | 0.9.4 | 与 miuix-ui 同版本，必须一起升 |
| `top.yukonga.miuix.kmp:miuix-squircle` | 0.9.4 | → 再拉 `miuix-shader` 0.9.4 |
| `androidx.navigationevent:navigationevent-compose` | **1.1.2** | 只在 Google Maven 有 → `settings.gradle.kts` 里 `google()` 不能删 |
| `org.jetbrains.compose.material3:material3-window-size-class` | **1.12.0-alpha03** | ⚠️ **alpha**。不受我们的 CMP 1.12.0 锁定保护，U2 前需钉死（RESULT.md §5.2 R2） |
| `com.materialkolor:material-color-utilities` | 5.0.1 | 用于从种子色生成配色 |

### 1.2 Miuix 0.9.4 发布的 Kotlin 变体（平台覆盖实测）

`androidJvm` · `jvm`(desktop) · `wasmJs` · `js` · `iosArm64` · `iosSimulatorArm64` · `macosArm64`

**没有** `iosX64` / `macosX64`。→ 三端（Android/Desktop/Wasm）不受影响；Intel Mac 桌面与 x86_64 iOS 模拟器无变体，U4 与用 Intel Mac 的开发者会被挡住。

## 二、为什么不能各自动一下「小版本」

- `kotlin-stdlib` 与 KGP 必须同版：库的 metadata 版本由 kotlinc 写入，KGP 只向上兼容。单独把 stdlib 提到 2.5 而 KGP 留 2.4 会在依赖解析里被降回 2.4.20（KGP 强制对齐）。
- `compose.*` DSL 与 `org.jetbrains.compose.*` 运行时必须同源：插件负责生成资源/入口，运行时是 Miuix 侧的传递依赖，混版最容易表现为「编译过、运行时 `NoSuchMethodError`」。
- `miuix-ui` / `miuix-core` / `miuix-squircle` / `miuix-shader` 四个 artifact **版本必须一致**：跨版本混用它们的 internal API 不稳定（同一个 `.module` 里以 `requires 0.9.4` 约束，Gradle 会强制对齐，但手写 `force` 会破坏这一点）。

## 三、硬约束：**Miuix 0.9.2 ～ 0.9.4 全部要求 `compileSdk ≥ 37`**（本轮最重要的实测结论）

解包 Gradle 缓存里的 Android AAR，读 `META-INF/com/android/build/gradle/aar-metadata.properties`：

| AAR | `minCompileSdk` | `minCompileMinorSdk` | `minAndroidGradlePluginVersion` |
|---|---|---|---|
| `miuix-ui-android-0.9.4` | **37** | 0 | 1.0.0 |
| `miuix-core-android-0.9.4` | **37** | 0 | 1.0.0 |
| `miuix-shader-android-0.9.4` | **37** | 0 | 1.0.0 |
| `miuix-squircle-android-0.9.4` | **37** | 0 | 1.0.0 |

- 0.9.4 的四个 AAR 是本轮直接解包核对的（缓存在 `$GRADLE_USER_HOME/caches/modules-2/files-2.1/top.yukonga.miuix.kmp/`）。
- **0.9.2 / 0.9.3 的 AAR 也核过，同样是 `minCompileSdk=37` + `minCompileMinorSdk=0`** → **降版本不能解**，不要浪费时间试 0.9.1/0.9.2。

### 3.1 后果链

1. `compileSdk = 36` → `:composeApp:checkDebugAarMetadata` 失败，没有 APK。
2. `compileSdk = 37` → `Failed to find Platform SDK with path: platforms;android-37`（本机只有 `android-37.0`，SDK 仓库无裸 `android-37`）。
3. `compileSdkVersion("android-37.0")` → `Unsupported value: android-37.0. Format must be one of: android-31 / android-31-ext2 / android-T / vendorName:addonName:31`（AGP 8.8.2 不解析 major.minor 平台名）。
4. NTFS junction `platforms/android-37` → `android-37.0` → `Observed package id 'platforms;android-37.0' in inconsistent location ... (Expected '...\android-37.0')`，仍然找不到。

→ **AGP 8.8.2 + Gradle 8.10.2 这条组合无法使用 API 37。**

### 3.2 升级候选（**未在本机验证**，仅列证据边界）

| 候选 | 证据 | 风险 |
|---|---|---|
| **AGP 8.13.2** + Gradle 8.14.4 + `compileSdk = 37` | 本机实测：平台能解析（仅警告 tested up to 36.1），但 `checkDebugAarMetadata` 失败——`androidx.compose.ui:ui-android:1.12.0` 与 `material3-android:1.5.0-alpha22` 要求 **AGP ≥ 9.1.0** | **不足**：CMP 1.12.0 的 androidx 传递依赖已把下限抬到 9.1，8.x 末版救不回来 |
| **AGP 9.5.1 + Gradle 9.6.1** + `compileSdk = 37` + `android.builtInKotlin=false` + `android.newDsl=false` | 本机实测：`BUILD SUCCESSFUL in 2m59s`；`composeApp-debug.apk` **13,575,772 B**；`aapt2 dump badging` 显示 compileSdkVersion 37 / minSdk 24 / targetSdk 36 | **已验证可行**（Desktop/Wasm 同组合仍可构）。两个开关已 deprecated，AGP 10 移除，届时需拆 androidApp 模块 |
| 换 Miuix 更老版本 / fork 去掉 AAR metadata | — | **禁止**（任务书 §六禁止改 Miuix 源码让它编过）。且 0.9.2/0.9.3 同样 37，向下没有出路 |

**给审核人的一句话**：Android 的红不是「Miuix 不支持 Android」，是「Miuix 用 API 37 编译，我们的 AGP 8.8.2 提供不了 API 37」。升到 **AGP 9.5.1 + Gradle 9.6.1**（带 `android.builtInKotlin=false` / `android.newDsl=false`）就会绿——已在 scratch 副本验证出 APK；但换 AGP 会牵动全工程构建，不属 C0c 范围，留给 U1 开工前统一改矩阵。

## 四、改动本矩阵前的检查清单

1. Kotlin / CMP / Miuix 三者要么一起升，要么都不动；只升一个 100% 出 metadata 或方法签名问题。
2. 升 Miuix 前先看新版本 `.module` 里 `foundation` 与 `kotlin-stdlib` 的 `requires`，这两个决定 Kotlin/CMP 能留多少。
3. 升 AGP 前先看它的 Gradle 下界（本机 Gradle 是镜像自带，换 AGP 通常要同时换 Gradle）。
4. 每次都要重跑：`:composeApp:screenshot`（桌面渲染）+ 两端 `wasmJsBrowserProductionWebpack`（体积）+ `assembleDebug`（Android）。三者全绿才算矩阵可用。
5. `material3-window-size-class:1.12.0-alpha03` 这类传递 alpha 不进我们的锁，升 Miuix 时要重新扫一遍依赖树。
