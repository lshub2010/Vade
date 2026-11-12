# C0b · 验证 Rust→Wasm（spike，决定 W-A / W-B）

| 项 | 值 |
|---|---|
| Agent 代号 | `c0b-wasm` |
| 波次 | W0，与 C0a / C0c 并行 |
| 硬时限 | **3 个工作日**，但 T1–T5 合计约 3.5 天——**T4 可砍**：T1（wasm-bindgen 跑通）出结论即可裁定 W-A/W-B；T2–T5 是打磨，超时不做 |
| 目录所有权 | `spike/wasm/` |
| 产出 | demo + `spike/wasm/RESULT.md` + `docs/adr/ADR-001-web-protocol-binding.md` |

## 一、一句话任务

证明「Rust 协议核心编译成 Wasm 后，能被 Compose/Wasm（Kotlin）调用，并通过 JS 侧的 WebSocket 完成一次握手」，或者证明它不行、必须走 W-B（TypeScript 重写协议）。

## 二、为什么这条必须先验

UniFFI 不支持 Wasm。若 W-A 不通，Web 端就要维护**第二份协议实现**（D21 已授权，但代价是双实现漂移风险）。这个决定不能等到 U1 做完才发现。

## 三、必读

- `docs/design.md` §9.4（W-A/W-B 两条路）、§8（协议单实现原则）
- `docs/requirements.md` D21、A12
- `docs/agents/01-contracts.md` §六

## 四、任务分解

### T1 · 编译可行性（0.5 天）

```
spike/wasm/protocol-wasm/
├── Cargo.toml          # crate-type = ["cdylib", "rlib"]
└── src/lib.rs
```

必须验证这些依赖在 `wasm32-unknown-unknown` 上能编：

| 依赖 | 关注点 |
|---|---|
| `snow` | 是否依赖 `getrandom`；`rand_core` 的 js feature 是否要开 |
| `chacha20poly1305` | 纯 Rust，预期可用 |
| `blake2` | 纯 Rust，预期可用 |
| `serde_json` | 可用，注意体积 |
| `wasm-bindgen` / `wasm-bindgen-futures` | 异步桥接 |

构建：`cargo build --target wasm32-unknown-unknown --release` + `wasm-bindgen --target web`。
需要 `getrandom` 时显式加 `features = ["js"]`，把这条写进结果——这是最容易卡住的一步。

### T2 · Rust ↔ JS（1 天）

导出三个函数，签名尽量贴近真实 SDK：

```rust
#[wasm_bindgen]
pub async fn handshake(ws_url: String, psk_hex: Option<String>) -> Result<String, JsValue>;
#[wasm_bindgen]
pub async fn encrypt(plaintext: Vec<u8>) -> Result<Vec<u8>, JsValue>;
#[wasm_bindgen]
pub extern "C" fn on_event(cb: js_sys::Function);
```

- **WebSocket 必须在 JS 侧**：Wasm 里不能开 TCP。用 `web-sys::WebSocket` 或直接由 JS 传入一个「发送/接收」回调对象。推荐后者：Rust 只管帧与密码学，传输交给宿主。这条如果成立，会显著简化 C1 的边界（协议核心 = 纯编解码 + 密码学，不含 IO）。
- 在浏览器/Node 里跑通：握手 → 发一条密文 → 收到回显 → 触发一次 JS 回调。

### T3 · Kotlin/Wasm 调用（1 天）

- 建最小 Compose/Wasm 工程（或直接用 Kotlin/Wasm 无 UI 工程），把 T2 的 JS 包用 `external` 声明引入；
- 验证 `@JsExport` / `external fun` 能否调用带 `Promise` 的导出；
- 验证 Kotlin 协程能 `await` 这个 Promise（`suspend` 桥接）；
- 记录 Kotlin 侧调用样板代码（C-10 会用到）。

**这一步是真正的风险点**：Rust→JS 通不代表 Kotlin/Wasm→JS 通。若 Kotlin/Wasm 侧无法稳定调用，即使 JS 能跑也算 W-A 不成立。

### T4 · 体积与时延（0.5 天）

| 指标 | 怎么测 |
|---|---|
| `.wasm` 原始体积 / gzip 体积 | `ls -l` + `gzip -9 -c | wc -c` |
| JS glue 体积 | 同上 |
| 握手耗时（wasm 内） | `performance.now()` 打点，20 次中位数 |
| 1KB 加解密耗时 | 同上 |

给出「加上协议核心后，Web 首屏预计增加多少 KB」的估算，并与 Miuix 的 ~900KB 预算并列，判断是否可接受。

### T5 · 裁决（0.5 天）

写 `docs/adr/ADR-001-web-protocol-binding.md`：

```markdown
# ADR-001 Web 端协议绑定
## 状态
已接受（W-A） / 已接受（W-B，降级）
## 背景
## 决策
## 证据
（T1–T4 的原始数字与命令输出）
## 后果
- W-A：协议 1 份；需维护 wasm 构建链；体积 +X KB
- W-B：协议 2 份；必须用 KAT 交叉验证；U1 需多写一层 TS
## 若选 W-B，W-A 的失败点
（精确到报错与最小复现）
```

## 五、验收

```bash
cd spike/wasm/protocol-wasm && ./build.sh          # 产出 pkg/
node spike/wasm/test.mjs                            # 输出 HANDSHAKE_OK + SIZE
cd spike/wasm/kotlin-wasm && ./gradlew wasmJsBrowserTest   # Kotlin 侧调用通过
test -f docs/adr/ADR-001-web-protocol-binding.md
```

## 六、禁止

- 禁止用「Node 里跑通」代替「Kotlin/Wasm 里跑通」——两者不是一回事。
- 禁止为了减小体积而砍掉密码学校验。
- 禁止把 WebSocket 实现塞进 Rust（用 `tokio-tungstenite` 在 wasm 上编不过，也别试）。
- 三天到点没通就写 W-B，不许申请延期。

## 七、交付格式

`RESULT.md` 含：结论、四张实测表、最小复现代码路径、对 C1 的边界建议（协议核心是否应做成无 IO 的纯库）。
