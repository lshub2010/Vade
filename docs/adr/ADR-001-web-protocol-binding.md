# ADR-001 · Web 端协议绑定：W-A（Rust→Wasm）还是 W-B（TypeScript 重写）

| 项 | 值 |
|---|---|
| 状态 | **accepted** |
| 日期 | 2026-11-12 |
| 决策者 | C0b 验证 + 编排者 |
| 关联 | D21、A12、`docs/agents/11-C0b-wasm.md`、`spike/wasm/RESULT.md` |

## 背景

协议核心是 Rust 单实现（D2/D19），但 UniFFI 不支持 Wasm。Web 端只有两条路：

- **W-A**：Rust → `wasm32-unknown-unknown` + `wasm-bindgen`，协议仍是同一份实现；
- **W-B**：TypeScript 再写一份协议（D21 已授权，代价是双实现漂移）。

## 证据（C0b spike，Node/V8）

- `cargo build --target wasm32-unknown-unknown --release` + `wasm-bindgen --target web` 成功；
- `node test.mjs`：**8 PASS / 0 FAIL / exit 0**，含真实 WebSocket（loopback）上的 `IKpsk2` 握手 144 B、AEAD 加密往返、JS 回调；
- `gradle wasmJsNodeDevelopmentRun`（Kotlin/Wasm）：**BUILD SUCCESSFUL**，输出 `KOTLIN_WASM_START` → `KOTLIN_WASM_OK`，证明 Kotlin/Wasm 能调用返回 Promise 的 JS 模块；
- 产物：wasm 207,148 B（gzip 95,008 B），glue 30,900 B；
- 关键边界成立：**Rust 侧不含任何 socket 代码**，`handshake(transport, psk)` 只调用宿主 `send()` 并 `await` 宿主 `recv()`。

## 决策

**采用 W-A 为主路线**。T3 已实测通过（见上），以下三条作为落地约束：

1. **C-10（Kotlin 绑定面）可以按 W-A 定稿**：T3 已实测 `KOTLIN_WASM_OK`；遗留的 payload 回显问题由 U1 在接入时查清；
2. 传输仍在宿主（JS WebSocket），Rust 侧不含 IO —— 这条边界已在 C0b 验证；
3. W-B 仍保留为兜底：若 U1 实装时发现 Kotlin/Wasm 互操作有无法绕开的限制，按 D21 转 W-B，并在此 ADR 追加「superseded」记录。

## 后果

**对 C1**

- 协议核心 = 纯编解码 + 密码学，**不含 IO**；传输（WebSocket）由宿主实现；
- `wasm32` 需要显式打开 `getrandom` 的 `js` feature（`rand 0.8 → getrandom 0.2`、`rand 0.9 → getrandom 0.3` 两代并存）；
- 两个 spike 的 `snow` 版本不一致（C0a 0.10.0 / C0b 0.9.x），C1 必须统一后再冻结 C-01/C-02。

**对 U1/U3**

- Web 端调用带 `Promise` 的导出函数，Kotlin 侧需要 `suspend` 桥接；样板代码见 `spike/wasm/kotlin-wasm/`。

**风险**

- 体积：wasm 207 KB + glue 31 KB（gzip 后约 95 KB），可接受但需在 U1 实测首屏；
- 时延数据噪声大（同机 14× 抖动），只到「毫秒级、非瓶颈」的量级结论；
- 浏览器未验证，仅 Node/V8。
