# C0a · 验证 UniFFI 绑定（spike）

| 项 | 值 |
|---|---|
| Agent 代号 | `c0a-uniffi` |
| 波次 | W0，与 C0b / C0c 并行 |
| 硬时限 | **4 个工作日**（T1–T5 合计 4 天；其中 T4b Swift 可砍，砍则记「未验证」，此时 3 天可出结论） |
| 目录所有权 | `spike/uniffi/`（可写），其余只读 |
| 产出 | 可运行 demo + `spike/uniffi/RESULT.md` |

## 一、一句话任务

证明「Rust 协议核心能通过 UniFFI 被 Kotlin 与 Swift 调用，并跑通一次真实的 Noise 握手 + 一条加密消息 + 一次事件回调」，或者证明它不行并指出具体卡在哪。

## 二、边界

**做**：最小 crate、最小绑定、最小 demo。
**不做**：完整协议、帧编解码、重连、KAT、性能优化、CI 集成。这里写的代码**不会进主干**，验收后归档到 `spike/`。

## 三、必读

- `docs/design.md` §2.2（握手参数集）、§8（Rust 单实现 + UniFFI）、§9.4（Wasm 例外）
- `docs/agents/01-contracts.md` §六（版本规则）

## 四、任务分解

### T1 · 最小 Rust crate（0.5 天）

```
spike/uniffi/protocol-spike/
├── Cargo.toml
├── src/lib.rs
└── build.sh
```

`lib.rs` 必须包含这四类导出，因为它们是后续真实 SDK 的最小骨架：

```rust
#[uniffi::export]
pub fn version() -> String;                       // 同步、无参数

#[uniffi::export]
pub async fn handshake(addr: String) -> Result<String, SpikeError>;  // async + Result

#[uniffi::export]
pub async fn send_encrypted(addr: String, plaintext: Vec<u8>) -> Result<Vec<u8>, SpikeError>;

#[uniffi::export(callback_interface)]
pub trait EventSink: Send + Sync { fn on_event(&self, json: String); }  // 回调
```

错误类型用 `thiserror` + `#[derive(uniffi::Error)]`，验证错误能跨语言带 `code/message`。

### T2 · 真实握手（1 天）

用 `snow` 实现 `Noise_IK_25519_ChaChaPoly_BLAKE2s`：

- 在 crate 内起一个 **Rust 测试服务端**（`tokio::net::TcpListener`），握手后回显一条消息；
- `handshake()` 作为客户端完成 IK 握手；
- `send_encrypted()` 发一条密文、收一条回显；
- 断言：明文不出现在收到的字节里；篡改 1 字节后解密必须报错。

**关键验证点**：`snow` 的 `HandshakeState` 里带 `&mut [u8]` 缓冲，跨 FFI 不能持有生命周期。必须证明「状态机放在 Rust 侧的对象里，绑定层只拿 handle」这条设计可行（用 `Arc<Mutex<...>>` 包一层即可，但要在结果里写清开销）。

### T3 · Kotlin 绑定（0.75 天）

- `uniffi-bindgen generate --language kotlin`，JVM target；
- 写一个 `Main.kt`：`runBlocking { handshake("127.0.0.1:PORT") }`，收到事件回调打印；
- 验收命令：`./gradlew run` 输出 `HANDSHAKE_OK` 与回调 JSON。

### T4 · Swift 绑定（0.75 天）

- `--language swift` 生成 `.swift` + `.h` + `.modulemap`；
- 能编译则写 `main.swift` 用 `async/await` 跑同一流程；
- **若当前环境无 macOS/Xcode**：只做绑定生成 + 头文件与签名静态检查，结果里必须标注「未在 Apple 平台编译」，并附一份在 macOS 上 30 分钟内可跑完的 `run_swift.sh`。**不要把"生成了文件"写成"验证通过"。**

### T4b · Python 绑定（0.5 天）

C2 的 crypto-gateway 与 C4 的 SDK 都依赖它，**这条不能漏**。

- `--language python` 生成模块 + `cdylib`，打成 wheel；
- 干净 venv 里 `pip install` 后完成一次握手（对手方用 T2 的 Rust 测试服务端）；
- 验证：导入名、异常类型（能否拿到 `code`）、`Vec<u8>` 的零拷贝程度（至少记录是否有额外拷贝）。

若 UniFFI 的 Python 异步支持不可用，**不要自己写 async 包装**，直接在结果里写明「Python 侧只用同步 API，异步由 SDK 的线程池承担」，这是可接受方案。

### T5 · 结论（0.5 天）

`RESULT.md` 必须包含：

| 项 | 内容 |
|---|---|
| 结论 | 可行 / 有条件可行 / 不可行 |
| 实测 | 握手耗时（20 次中位数）、消息往返耗时、绑定产物体积 |
| 通过项 | T1–T4b 逐项 通过/未通过/未验证 |
| 卡点 | 具体报错、最小复现命令 |
| 对 C1 的约束 | FFI 边界上不能出现的类型（生命周期、泛型、trait object 等） |
| 对 C4 的约束 | Python 绑定是否可用、需要哪些 feature flag |

## 五、验收

```bash
cd spike/uniffi/protocol-spike && cargo test -q          # 含篡改与错误认证标签用例
cd spike/uniffi/kotlin && ./gradlew run                 # 输出 HANDSHAKE_OK
python -m venv /tmp/c0a && /tmp/c0a/bin/pip install dist/*.whl
/tmp/c0a/bin/python -c "from protocol_spike import handshake; print(handshake('127.0.0.1:9000'))"
cat spike/uniffi/RESULT.md                              # 五项齐全
```

## 六、禁止

- 不要为了跑通而关掉证书/签名校验。
- 不要把私钥写进源码或日志（spike 也用临时密钥）。
- 不要顺手实现完整协议（那是 C1 的活）。
- 不要引入 `snow` 之外的密码学实现做"对比实验"。

## 七、交付格式

按 `00-orchestration.md` §三。额外附：`RESULT.md` 全文 + `build.sh` 在新环境的实测输出。
