# C1 · Rust 协议核心

| 项 | 值 |
|---|---|
| Agent 代号 | `c1-protocol` |
| 波次 | W1（C0 三路结论之后） |
| 依赖 | C0a 的 FFI 约束、C0b 的「协议核心不做 IO」结论 |
| 目录所有权 | `protocol/core/`、`protocol/v1/` |
| 产出 | `protocol-core` crate + KAT + `protocol/v1/frames.md` + Python wheel |
| 出口 | **G1 的前置**：KAT 与负向用例全绿，绑定可被 Python 调用 |

## 一、一句话任务

用 Rust 实现**唯一一份**协议核心：帧编解码、Noise 握手、密文收发、重放防护。它不含网络 IO，不含业务，不含任何模块知识。

## 二、必须遵守的边界

| 做 | 不做 |
|---|---|
| 帧编解码、Noise IK/XX、AEAD、计数器、KAT | 不开 socket、不连 WS、不做重连（那是 SDK 的事） |
| 暴露 FFI 友好的同步/异步 API | 不暴露生命周期、泛型、trait object 到 FFI 边界 |
| 错误码映射到 `schema/errors.json` | 不自造错误码 |
| 生成 Python / Kotlin / Swift 绑定 | 不写 UI、不写业务逻辑 |

**协议核心是纯库**：输入字节，输出字节。这条如果破了，Wasm 端就没法复用（C0b 已验证）。

## 三、必读

- `docs/design.md` §2 全节（帧、握手、manifest、命名空间、RPC、seq）
- `docs/design.md` §3（三种鉴权模式）、§8（UniFFI）
- `docs/requirements.md` FR-2 / FR-3 / FR-4、NFR-2/3/6
- `docs/agents/01-contracts.md` §二、§六

## 四、已裁决的技术冲突（不要再改）

| 冲突 | 裁决 |
|---|---|
| 方案 §2.1 写「nonce = direction(4B) \|\| counter(8B)」，但 `snow` 的 `TransportState` 自己维护 nonce，且不暴露流量密钥 | **采用 snow 原生 nonce**（96-bit，前 4 字节 0，后 8 字节小端计数器）。方向隔离靠 Noise 的 Split 产生**两把独立密钥**（c2m / m2c），比把方向写进 nonce 更强：反射帧用另一把密钥解密必然失败。帧头 `counter` 只用于重放检测，不参与 AEAD。 |
| 握手用 `snow`，但需要固定向量做 KAT | 握手是随机化的（ephemeral 每次新生成），**不做全握手 KAT**。KAT 覆盖：帧编解码、AEAD 原语（固定 key/nonce/pt→ct）、配对码 PSK 的 HKDF 派生。跨实现互操作（W-B 的 TS）用**在线互通测试**兜，不靠握手 KAT。 |
| `XX + PSK` 的 PSK 位置 | 用 `Noise_XXpsk3_25519_ChaChaPoly_BLAKE2s`，PSK 在第三条消息混入；PSK = `HKDF-SHA256(ikm=normalize(pair_code) || core_pub_fp, salt="vade/pair/v1", len=32)`。`normalize()` 由 C-15 定义（`NFKC` → 去 `-`/空格 → 字母 upper），**C1 只消费不实现自己的版本**，KAT 里带 4 组归一化等价输入。 |

以上三条必须写进 `protocol/v1/frames.md` 的「实现说明」，否则审查会按旧方案打回。

## 五、任务分解

### T1 · crate 骨架（0.5 天）

```
protocol/core/
├── Cargo.toml            # workspace，features: uniffi, python
├── src/
│   ├── lib.rs
│   ├── error.rs          # ProtocolError -> code/message/retriable
│   ├── frame.rs
│   ├── handshake.rs
│   ├── transport.rs
│   ├── identity.rs
│   └── ffi.rs            # #[cfg(feature = "uniffi")]
├── tests/
│   ├── frame_kat.rs
│   ├── negative.rs
│   └── interop_rust.rs
└── benches/aead.rs
```

`#![forbid(unsafe_code)]`（FFI 层除外，用 `#[cfg]` 隔离）。所有 `pub` API 必须能跨 FFI：只用 `Vec<u8>`、`String`、`u64`、`bool`、`Option<T>`、自定义 `#[derive(uniffi::Error)]`。

### T2 · 帧编解码（0.5 天）

```
| ver(1) | type(1) | flags(1) | rsv(1) | payload_len(4,BE) | generation(4,BE) | counter(8,BE) | payload |
```

- `payload_len` = **明文长度**；密文长度 = `payload_len + 16`；帧总长 = `16 + payload_len + 16`。
- `type`：`0x01` handshake / `0x02` rpc / `0x03` event / `0x04` heartbeat / `0x05` close / **`0x06` rekey**。
- `generation`：握手成功置 `1`，每次 rekey `+1`；`rekey` 帧本身用**旧 key** 加密，帧体 `{new_generation}`。接收端按 `(generation, counter)` 字典序判定单调递增，generation 回退 → `fatal`。
- 握手帧的 `payload` 是 Noise 原始消息（明文），`counter = 0`；其余帧 `payload` 是密文。
- `ver != 1` → `PROTOCOL_MISMATCH`，**在解密之前就拒**，不得先解密再判断。
- `payload_len > limits.max_payload`（默认 1 MiB）→ `PAYLOAD_TOO_LARGE`，**不分配对应大小的缓冲**（防内存放大）。
- 声明长度与实际字节不一致 → `INVALID_ARGUMENT`，不允许「按实际长度宽容解析」。

### T3 · 握手（1 天）

```rust
pub enum Role { Initiator, Responder }
pub enum Mode { Ik, XxPsk }

pub struct Handshake { /* 内部持有 snow::HandshakeState */ }

impl Handshake {
    pub fn new(mode: Mode, role: Role, local: &Identity,
               remote_pub: Option<&[u8; 32]>, psk: Option<&[u8; 32]>) -> Result<Self>;
    pub fn write(&mut self, payload: &[u8]) -> Result<Vec<u8>>;
    pub fn read(&mut self, msg: &[u8]) -> Result<Vec<u8>>;
    pub fn is_finished(&self) -> bool;
    pub fn remote_static(&self) -> Option<Vec<u8>>;   // XX 配对时取模块公钥
    pub fn into_transport(self) -> Result<Transport>;
}
```

- 参数集：`Noise_IK_25519_ChaChaPoly_BLAKE2s` / `Noise_XXpsk3_25519_ChaChaPoly_BLAKE2s`。
- `prologue = b"vade/v1" || role_byte`（role: initiator=1 / responder=2）。**prologue 必须两边一致**，否则握手失败——这是防跨协议混淆的第一道。
- IK 下 `remote_pub` 必填；缺失 → `INVALID_ARGUMENT`，不允许退回 XX（禁止降级）。
- XX 结束后 `remote_static()` 必须返回对方静态公钥，供 core 登记。
- 握手消息超过 64KB → 拒绝。

### T4 · 传输态（1 天）

```rust
pub struct Transport { /* snow::TransportState + 两个方向的 last_counter */ }

impl Transport {
    pub fn seal(&mut self, dir: Direction, gen: u32, counter: u64, plaintext: &[u8]) -> Result<Vec<u8>>;
    pub fn open(&mut self, dir: Direction, gen: u32, counter: u64, ciphertext: &[u8]) -> Result<Vec<u8>>;
    pub fn rekey(&mut self) -> Result<u32>;   // 返回新的 generation
}
```

规则：
- `seal` 要求 `counter == last_sent + 1`（首帧为 1）；否则 `INVALID_ARGUMENT`。**不允许跳号**，避免接收端无法判定丢失。
- `open` 要求 `counter > last_recv`；`<=` 一律判 `PERMISSION_DENIED` 并返回 `fatal: true`（调用方必须断开）。**不做滑动窗口**：WS 是可靠有序传输，乱序只可能是攻击或实现错误。
- `counter` 到 `u64::MAX - 1024` 时返回 `UNSUPPORTED`，提示调用方重握手（FR-3.3）。
- `rekey()` 只换对称密钥、不动 ephemeral，返回新 generation；调用方负责发 `0x06` 帧并在成功后把本方向 counter 置 0。**rekey 不刷新前向保密**，24h 另起重握手（FR-3.12）。
- `dir` 参数只用于内部选择发送/接收状态，**不参与 nonce 构造**（见第四节）。
- 单条 1KB 加解密 P95 < 2ms（NFR-2），用 `benches/aead.rs` 记录。

### T5 · FFI 与绑定（1 天）

- `features = ["uniffi"]` 导出：`Identity`、`Handshake`、`Transport`、`Frame`、`ProtocolError`。
- 异步：`Handshake` 本身同步（纯内存），**不要在 Rust 侧假装异步**；异步留给 SDK 的 IO 层。C0a 已验证 async 可行，但这里不需要。
- 生成物：
  - Python wheel：`protocol/core/python/` + `make wheel`，导入名 `vade_protocol`；
  - Kotlin：`make kotlin` 输出 `protocol/core/dist/kotlin/`；
  - Swift：`make swift` 输出头文件与 `.swift`。
- Python wheel 必须能在干净 venv 里 `pip install dist/*.whl` 后完成一次握手（对手方是 `tests/` 里的 Rust 测试服务端）。

### T6 · 测试与 KAT（1 天）

`protocol/v1/kat.json` 结构：

```json
{
  "version": 1,
  "frame": [{"name":"rpc-1kb","type":2,"gen":1,"counter":1,"payload_len":1024,"hex":"..."},
            {"name":"rekey-frame","type":6,"gen":1,"counter":99,"payload_len":4,"hex":"..."}],
  "aead":  [{"name":"chacha20poly1305-1","key":"...","nonce":"...","pt":"...","ct":"..."}],
  "hkdf":  [{"name":"pair-psk","pair_code":"青松-A7K2M9","normalized":"青松A7K2M9","core_fp":"...","psk":"..."},
            {"name":"pair-psk-fullwidth","pair_code":"青松－Ａ７Ｋ２Ｍ９","normalized":"青松A7K2M9","core_fp":"...","psk":"<同上，必须相等>"}]
}
```

**负向用例（必须真的会失败，不是断言 true）**：

| 用例 | 期望 |
|---|---|
| 版本字节改成 2 | `PROTOCOL_MISMATCH`，且未进入解密 |
| 翻转密文任意 1 字节 | `PERMISSION_DENIED`（认证失败） |
| 重发同一 counter | 拒收，`fatal` |
| counter 回退 | 拒收，`fatal` |
| generation 回退 | 拒收，`fatal`（且不得回退旧 key） |
| rekey 后旧 key 的帧 | 拒收，`fatal` |
| 把本端发出的帧喂给 `open` | 失败（方向密钥不同） |
| IK 用错误静态公钥 | 握手失败 |
| XX 用错误配对码 | 握手失败 |
| 声明长度 2MB | `PAYLOAD_TOO_LARGE`，内存不增长（用 `dhat` 或 `/proc/self/statm` 断言） |
| 截断帧（少 1 字节） | `INVALID_ARGUMENT` |
| 握手消息 65KB | 拒绝 |
| 未完成握手就 `into_transport` | `INVALID_ARGUMENT` |

再加 `cargo-fuzz` 一个 target 喂 `decode_frame`，跑 10 分钟不 panic。

### T7 · 协议文档（0.5 天）

`protocol/v1/frames.md`：帧图、字段语义、握手时序（IK 1-RTT / XX 2-RTT）、错误码映射、KAT 说明、**第四节三条裁决**、以及「本实现不含 IO」的声明。

## 六、验收

```bash
cd protocol/core && cargo test --all-features      # 全绿，含负向
cargo clippy -- -D warnings
cargo bench --bench aead                            # P95 < 2ms/1KB
python -m venv /tmp/v && /tmp/v/bin/pip install dist/*.whl
/tmp/v/bin/python -c "import vade_protocol as p; print(p.version())"
test -f protocol/v1/frames.md && test -f protocol/v1/kat.json
```

## 七、禁止

- 禁止自研密码学原语（Noise 用 snow，AEAD 用 RustCrypto）。
- 禁止把私钥 `Debug` 打印出来（`impl Debug` 必须打 `[redacted]`）。
- 禁止为了通过测试放宽「重放即断开」。
- 禁止把 IO 塞进 crate（哪怕 feature gate）。
- 禁止改 `protocol/v1/` 的语义而不升版本。

## 八、交付格式

按 `00-orchestration.md` §三，额外附：
- KAT 文件 diff（新增向量要说明来源：是实测生成还是标准向量）
- `cargo bench` 原始输出
- Python wheel 的 `pip install` + 握手日志
- 三条裁决在文档中的落点（章节号）
