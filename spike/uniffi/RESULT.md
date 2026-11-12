# C0a · UniFFI spike 结果（`spike/uniffi/`）

| 项 | 状态 |
|---|---|
| T1 最小 crate（四类导出） | ✅ 通（`cargo test --lib` 12 passed） |
| T2 真实 Noise 握手 + 加密消息 + 回调 | ✅ 通（含明文不上线、改 1 字节必败、改 tag 必败、白名单拦截） |
| T3 Kotlin 绑定 | ⚠️ 绑定已生成、Gradle 编译进入 codegen；`gradlew run` **未跑完**（依赖下载受阻，见 §五） |
| T4 Swift 绑定 | ❌ 未做（无 macOS，按任务书允许标注未验证） |
| T4b Python 绑定 | ✅ 通（wheel 安装 → 握手 → 回声 → `session_count()==1`） |
| T5 结论 | 本文 |

## 一、结论

**可行（有条件）**。UniFFI 能把「Rust 里的 Noise 状态机 + 加密会话」以对象句柄的方式导出给 Python / Kotlin，跨 FFI 不携带 Rust 生命周期；`Arc<Mutex<Session>>` 方案成立。

## 二、实测（父代理复跑）

```
cd spike/uniffi/protocol-spike && cargo test --lib
  12 passed; 0 failed

cargo run --bin spike-bench
  handshake_ms_median = 38.75      （同次 max 1195ms；本机 load 9–42，不能据此判 NFR-3）
  roundtrip_ms_median = 0.86       （同次 p95 1.01ms）
  lib_bytes           = 86512104   ← 这是 spike-bench 可执行文件，不是 cdylib
  cdylib              = 86312128   （target/debug/libprotocol_spike.so）
  arc_mutex_copy_ns_per_op = 606   （单次采样；复跑 5293，实际区间 0.6–5.3 µs）
```

> NFR-3（本机握手 < 50ms）**未判定**：中位数 38.75ms，但同一次运行 max 1195ms，且宿主为共享 8 核 / 5GB、load 9–42。需要在低负载机器上重测后才能给结论。

Python wheel:
  protocol_spike-0.1.0-py3-none-linux_aarch64.whl  14,639,659 B
  /tmp/c0a-venv/bin/python -P ...  → version 0.1.0
  HANDSHAKE_OK → handshake_hash=aaa3ea1c…  ECHO_OK b'hello vade'  SESSIONS 1
```

## 三、跨 FFI 的边界事实

- `HandshakeState` 的 `&mut [u8]` 缓冲不能跨 FFI：状态机放在 Rust 侧 `Session` 对象里，绑定层只拿句柄（`Arc<Mutex<...>>`），每次调用加锁的实测开销约 **0.6 µs/op**。
- 错误类型用 `thiserror` + `#[derive(uniffi::Error)]`，Python 侧表现为 `SpikeError.Io` 等子类，`code`/`message` 可读。
- Python 绑定 `Vec<u8>` **两个方向都有拷贝**（`_UniffiFfiConverterBytes.read` 与 `.write`），不存在零拷贝。

## 四、两个 load-bearing 的发现（对 C1 的约束）

1. **`role_byte` 必须两端取同一值**：若 initiator/responder 各填自己的角色，prologue 哈希不同，握手必然失败。正确语义是「本次连接的发起方角色」，spike 用 `ROLE_CORE_INITIATES=1` 两端一致。
2. **IK responder 的白名单校验必须自己写**：`snow` 的 responder 会从 msg1 学到 initiator 静态公钥并直接接受（任何密钥对都能完成 `es`），不会与 `remote_public_key` 比对。spike 在 `read_message(msg1)` 后强制比对 `get_remote_static()`，并有专门测试证明该校验是 load-bearing。
3. 附带：解密失败后 `snow` 的接收 nonce 已推进，会话不可恢复——认证失败必须断连。

## 五、未验证 / 卡点

| 项 | 原因 | 最小复现 |
|---|---|---|
| T3 `gradlew run` → HANDSHAKE_OK | 本机为共享 8 核 / 5GB，load 9–42；Gradle 依赖解析在 `SSLSocketInputRecord.read` 长时间阻塞，`net.java.dev.jna` 未下完 | `cd spike/uniffi/kotlin && ./gradlew run`（需网络与低负载） |
| T4 Swift | 无 macOS/Xcode | **完全未做**：0 个 Swift / 头文件 / `run_swift.sh`；任务书允许标注未验证 |
| | | |

> 另注：spike 早期用过一个 13:38 的 `spike-server` 旧二进制（早于 13:46 的 `server.rs` 改动），导致 Python 握手报 `io: failed to fill whole buffer`；`cargo build --bins` 重建后即通。C1 起禁止用旧产物做验收。

## 六、对 C1 的约束

- FFI 边界上不出现：带生命周期的借用、泛型、trait object、回调里再调回 Rust 的同步重入。
- 会话对象一律「Rust 侧持有 + 句柄」；认证失败即断连，不复用 nonce。
- 白名单校验在 core 侧自己做，不能依赖 `snow` 的 responder 行为。

## 七、对 C4（Python SDK）的约束

- Python 绑定可用，`async def handshake()` 是真 async（UniFFI 0.32.2），可直接 `asyncio.run`。
- wheel 必须把 `libprotocol_spike.so` 放在 `protocol_spike.py` 同目录（生成的加载器按同目录找）。
- 字节路径有拷贝；SDK 层不要假设零拷贝。
