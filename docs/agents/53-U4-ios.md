# U4 · iOS 客户端（SwiftUI）

| 项 | 值 |
|---|---|
| Agent 代号 | `u4-ios` |
| 波次 | W6 前段（U1 的 C-10 冻结后） |
| 依赖 | C0a 的 Swift 绑定、C-10 的会话契约、C-09 的 `Skin` 语义、U3 的 `GLASS_SPEC.md` |
| 目录所有权 | `ui/ios/` |
| 产出 | 可扫码配对、可聊天、可审批、**能打开管理页并完成设备绑定**的 iOS App |
| 出口 | **G4**：真机跑通；iOS 26 底栏是系统玻璃；**管理页 WebView 与 P-256 绑定可用**（AMEND-18/A62） |

## 一、一句话任务

用 SwiftUI 做一个**看起来像 iOS 原生**的客户端：底栏在 iOS 26 上就是系统玻璃浮岛（不自绘）；协议仍然只有 Rust 那一份。

## 二、开工前 2 天的技术裁决（必须先做）

| 路径 | 做法 | 判定 |
|---|---|---|
| **A（优先）** | KMP `shared` 编译成 `Shared.framework`（iosArm64/iosSimulatorArm64），内部调 UniFFI Kotlin/Native 绑定 | C0a 只验证了 JVM，**Kotlin/Native + Rust 静态库**链路未验证。2 天内跑通则用 A |
| **B（降级）** | Swift 直接用 UniFFI Swift 绑定；`SessionStore` / `TurnQueueView` / `ReconnectPolicy` 用 Swift 重写 | 协议仍单份，但视图状态机多一份；必须用 U1 的契约测试向量对齐 |

**裁决必须写进 `ui/ios/DECISION.md`**：选哪条、为什么、验证命令、遗留风险。不要闷头做 A 两周然后说不行。

选 B 时，重写的三个类必须通过 U1 `shared/src/commonTest/resources/` 里的同一组 JSON 用例（seq 去重、delta 累加、snapshot 替换、重连退避序列），**行为一致才算过**。

## 三、目录

```
ui/ios/
├── Vade.xcodeproj
├── Vade/
│   ├── App.swift
│   ├── Core/            # ProtocolClient 封装、状态机（路径 A 则薄，B 则完整）
│   ├── Skin/Skin.swift  # 与 C-09 同构的协议
│   ├── SkinMaterial/  SkinMiuix/  SkinSystem/
│   ├── Screens/         # Chat / Sessions / Settings / Approval
│   └── Security/        # Keychain、证书 pin
├── Pairing/             # 扫码
└── DECISION.md
```

## 四、任务分解

### T1 · 协议接入（2 天）

- WebSocket：`URLSessionWebSocketTask`，二进制帧（`.data`）承载协议帧；
- 握手：调 Swift 绑定完成 IK/XX，密文收发在 Rust 侧；
- 证书 pin：`URLSessionDelegate.urlSession(_:didReceive:)`，比对 `SHA-256(SPKI)` 与配对时 pin 的指纹；不匹配 → 断开并提示「core 身份已变化」；
- ATS：自签/局域网需要 `NSAllowsLocalNetworking` + `NSLocalNetworkUsageDescription`（局域网发现提示）；不要用 `NSAllowsArbitraryLoads`。

### T2 · 安全存储与配对（2 天）

- **两把密钥，不要混**：
  1. **Noise 静态密钥（X25519，用于数据面 IK）**：`kSecClassGenericPassword` + `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`。它是 DH 密钥，Secure Enclave **不支持**，只能软件存放——这是允许的，因为它不承担管理面身份。
  2. **管理面签名密钥（P-256 ECDSA，用于 `/admin/api/login` 与绑定）**：`SecKeyCreateRandomKey` + `kSecAttrTokenIDSecureEnclave` + `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`，`kSecAttrIsPermanent=true`，**不可导出**（不实现 `SecKeyCopyExternalRepresentation` 的导出路径）。**不能用 GenericPassword 存它**——那等于把「硬件密钥」降级成可提取的软件密钥，D68 直接失效。
- 扫码：`AVCaptureSession` + `AVCaptureMetadataOutput`；解析
  `vade://pair?h=<host>&p=<port>&c=<code>&fp=<fp>`；
- 手动输入兜底（相机权限被拒时）：地址 + **8 位码**（2 汉字 + 6 位 base32，D52）+ 指纹。**不是 6 位**。
- 配对成功 → 存 `{host, port, fp, device_id}` 到 Keychain；之后走 IK。
- 指纹变更 → 红屏阻断，要求重新配对，**不允许「忽略一次」**。

### T3 · UI（3 天）

| 页面 | 要点 |
|---|---|
| 聊天 | 消息气泡、流式文本、工具卡片（可折叠）、输入框、滚动到底部 |
| 会话列表 | 新建/切换、最近消息、未读数 |
| 审批 | 完整参数（等宽、可滚动）、risk、倒计时、`仅本次/本会话/拒绝` |
| 设置 | 皮肤、底栏态、连接信息（地址、指纹短码、状态）、解除配对 |

- 所有控件走 `Skin` 协议；业务页面不直接 `import SwiftUI` 的 `Button` 样式？——这条在 Swift 里做不到完全隔离，改为：**页面只用 `Skin.button(...)` 等工厂方法**，CI 用 grep 检查页面里没有 `.buttonStyle(`。
- 动态字体（Dynamic Type）必须支持到 `.accessibility3` 不截断。

### T4 · 底栏与皮肤（3 天）

| 皮肤 | 实现 |
|---|---|
| `glass` | **iOS 26+：系统 `TabView`**（重编译即玻璃浮岛）；可选 `.tabBarMinimizeBehavior(.onScrollDown)`、`.tabViewBottomAccessory`；**不自绘** |
| `glass`（17–25） | `UnionTabView`（SPM，MIT）或 `FabBar`；按 `GLASS_SPEC.md` 对齐 tint/描边 |
| `material` | 自绘近似：圆角、填充按钮、标准 TabView |
| `miuix` | 自绘近似：小圆角、描边、紧凑密度（按 U2 截图对照） |

- 底栏三态：`fixed`（系统默认）/ `floating` / `floating+blur`。iOS 26 系统 TabView 本身是浮岛，`fixed` 用 `.tabViewStyle(.tabBarOnly)` 或自定义容器实现，需实测。
- 皮肤切换运行时生效（`@Observable` 环境对象），不重启。

### T5 · 真机验收（2 天）

```bash
xcodebuild -project Vade.xcodeproj -scheme Vade \
  -destination 'platform=iOS Simulator,name=iPhone 16 Pro' build
# 真机：扫码 → 聊天 → 审批 → 断网恢复
```

截图：iOS 26（或最新 SDK）玻璃底栏、iOS 17 降级底栏、审批弹窗、深色模式。共 ≥ 4 张。

### T6 · 管理页 WebView + 设备绑定（2 天，AMEND-18 新增）

U1 的 T5b 只覆盖 Android/Desktop（`ui/ios/` 归你，U1 明确「不做 iOS」）。这一块在 iOS 上没人做，必须在这里补齐。

```
ui/ios/Vade/Admin/
├── AdminWebView.swift   # WKWebView 加载 app://admin/index.html（包内资源）
├── DeviceKey.swift      # SecKeyCreateRandomKey + kSecAttrTokenIDSecureEnclave（P-256）
├── Bridge.swift         # WKScriptMessageHandler：getDeviceInfo / sign / secureStore
└── BindFlow.swift       # 扫码 → 验 fp8 + hello_sig → 设密码 → POST /admin/bind
```

- **只加载包内资源**：`loadFileURL(_, allowingReadAccessTo:)` 指向 bundle 的 `admin/`；**禁止** `load(URLRequest(url: https://<core>:8444/admin))`。`WKWebView` 的 `navigationDelegate` 对白名单外导航一律 `cancel`。
- **桥三方法**，`message.name` 白名单；禁止 `allowJavaScriptAccessFromFileURLs` / `allowUniversalAccessFromFileURLs`。
- `sign()` 先在原生校验模板（`vade-admin-v2\n` + 32B base64 + `\n` + 已知 origin），不匹配直接回错误；**首次登录必须弹原生确认**。
- **证书**：`urlSession(_:didReceive:completionHandler:)` 里只接受 `tls` pin 匹配的那张证书（`SecTrustEvaluateWithError` + SPKI 比对）；**禁止**无条件 `.useCredential`。
- **绑定**：解析 `vade://bind?h&p&fp&tls&t`，先算 `SHA-256(core_pub)[:16] == fp`，再验 `hello_sig`，再走 §7.12.3 的信封；`hardware_backed=true` 上报。
- refresh token 存 Keychain（`ThisDeviceOnly`），**不出原生层**；刷新由 `URLSession` 在原生侧代发。

## 五、验收（G4）

| 用例 | 判据 |
|---|---|
| 扫码配对 | 5 分钟内完成；错误码提示可读 |
| 聊天 | 流式回复 + 工具卡片；与桌面端看到同一会话 |
| 审批 | 弹窗显示完整参数；批准后桌面端弹窗同步关闭 |
| 断网 30s | 恢复后按 `last_seq` 补齐，无重复（AC-11） |
| iOS 26 底栏 | 系统玻璃浮岛（截图，非自绘） |
| iOS 17–25 | UnionTabView/FabBar 正常，不崩 |
| 指纹变更 | 阻断并要求重新配对 |
| 管理页 WebView | release 包里搜 `load(URLRequest` 对 `https://` 0 处；JS 调 `sign("hello")` 被拒；`POST /admin/api/skills/install` 用 Bearer 成功 |
| 设备绑定 | 扫码 → 验指纹 → 设密码 → `devices.json` 出现 `alg=p256`、`hardware_backed=true`；私钥 `SecKeyCopyExternalRepresentation` 返回错误（不可导出） |
| Keychain | 卸载重装后密钥不在（除非 iCloud 钥匙串，明确关闭同步） |

## 六、禁止

- 禁止自绘 iOS 26 玻璃（FR-8.21）。
- 禁止把私钥写 UserDefaults / 文件 / 日志。
- 禁止 `NSAllowsArbitraryLoads`。
- 禁止在指纹不匹配时提供「仍然连接」。
- 禁止为通过测试伪造流式（必须来自 core 事件）。
- 禁止改 `ui/shared/`（U1 所有）；需要共享逻辑就提 RFC。

## 七、交付格式

按 `00-orchestration.md` §三，额外附：`DECISION.md`、真机/模拟器截图、`xcodebuild` 输出、Keychain 存储路径说明（不贴密钥）、断网恢复的 seq 日志。
