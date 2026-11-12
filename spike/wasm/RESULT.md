# C0b · Rust→Wasm spike 结果（`spike/wasm/`）

| 项 | 状态 |
|---|---|
| T1 编译可行性 | ✅ 通（`cargo build --target wasm32-unknown-unknown` + `wasm-bindgen --target web`） |
| T2 Rust ↔ JS（真实 WebSocket 握手 + AEAD 往返 + 回调） | ✅ 通，`node test.mjs` **8 PASS / 0 FAIL / exit 0**，连续 3 次 |
| T3 Kotlin/Wasm 调用 | ❌ 未验证（本机无法解析依赖，见 §九） |
| T4 体积与时延 | ⚠️ 体积已测；时延数字噪声过大，只能给量级（§五） |
| T5 ADR-001 | ❌ 未写（不在本次修复范围内） |

## 一、结论

**W-A 在 JS 侧成立**：Rust 协议核心编成 Wasm 后，在 Node（与浏览器同一个 V8）里完成了
一次真实的 Noise `IKpsk2` 握手，并且：

- **传输完全在宿主**：Rust 侧没有任何 socket 代码，`handshake(transport, psk)` 只调用
  宿主给的 `send(bytes)`，再 `await` 宿主返回的 `recv()` Promise。这条边界成立，可以直接
  作为 C1 的核心设计（协议核心 = 纯编解码 + 密码学，不含 IO）。
- 握手用的字节真的走了一条 `ws` 的 WebSocket（loopback server + client），不是内存里对传。
- 握手完成后 AEAD 加密 → 服务端回显 → 解密 → 触发 JS 回调（`handshake.done` /
  `frame.received`）全链路通。

## 二、验收命令与实测输出

```bash
cd spike/wasm/protocol-wasm && ./build.sh    # 产出 pkg/
cd spike/wasm && node test.mjs
```

```
PASS selftest_in_module {"ok":true,"roundtrip":"vade-spike","pong":"pong","ct_len":26}
PASS psk2_roundtrip pt=psk-frame m1=96B m2=48B
PASS psk_mismatch_rejected rejected=true initiator_established=false
EVENT handshake.done {"ok":true,"pattern":"IKpsk2","local_static":"5d7c...3005","handshake_bytes":144}
PASS HANDSHAKE_OK {"ok":true,"pattern":"IKpsk2","local_static":"5d7c...3005","handshake_bytes":144}
HANDSHAKE_MS 1558.21
EVENT frame.received {"bytes":10}
PASS ECHO_OK ct=26B
PASS tamper_rejected
PASS js_callbacks_fired events=["handshake.done","frame.received"]
AEAD_1KB_MS 1.170 (1000-byte payload, 2000 iterations)
SIZE wasm=207148B wasm_gzip=95008B glue=30900B total=238048B
PASS no_stray_errors last_error=decrypt failed: decrypt error
RESULT OK
EXIT=0
```

`last_error` 里残留的 `decrypt failed: decrypt error` 是 §2.2 故意做的篡改测试留下的，
不是 stray error——`no_stray_errors` 检查的是**服务端**异常（`serverError`），为 null。

## 三、修复前的真实报错（红线证据）

父代理实测到的现象，原样复现：

```
$ cd spike/wasm && node test.mjs
node:internal/modules/run_main:111
    triggerUncaughtException(
    ^
handshake read failed: input error
Node.js v26.12.1
EXIT=1
```

一条 PASS 都没有：脚本第一屏的 `selftest()` 就抛了。把 stdout 重定向到文件（管道会丢
缓冲）后确认输出确实为空，问题在 `selftest()`：

```
selftest() -> THREW handshake read failed: input error | last_error=handshake read failed: input error
```

## 四、根因（三条，按重要性排序）

### R1 · `pkg/` 是从**过期的 .wasm** 生成的（主因）

`build.sh` 里 `cargo build` 用默认 target 目录，而进程环境里有一个**别的 spike 泄漏出来的**
`CARGO_TARGET_DIR`：

```
CARGO_TARGET_DIR=/root/HermesWorkspace/Vade/spike/uniffi/protocol-spike/target
```

后果：`cargo build` 把产物写到 uniffi 的 target 下，而 `wasm-bindgen` 读的是
`./target/wasm32-unknown-unknown/release/protocol_wasm.wasm`——那个文件停在 **13:30**，
`src/lib.rs` 是 **14:12**。所以 `pkg/` 一直是「老代码 + 新 JS」，`read_handshake` 在旧版
返回 `Option<Vec<u8>>`（完成时 `undefined`），新版返回 `Result<Vec<u8>>`，两者 ABI 对不上。

**证据**：`pkg/protocol_wasm.js` 里生成出来的注释写着
*"Returns the reply to send, or `undefined` once the handshake completes"*，
而当前 `lib.rs` 的签名无此语义；同时 `cargo build` 只打印 `Finished`、不打印 `Compiling`，
artifact mtime 一直不动。

**修复**：`build.sh` 显式 `--target-dir "$TARGET_DIR"`（默认 `target`），并对产物做存在性
检查。修复后第一次构建出现 `Compiling protocol-wasm v0.1.0`，耗时 1m31s，产物从 611 592 B
（无 LTO/strip 的旧物）变成 206 723 B。

### R2 · `write_handshake()` 没有把状态推进到 transport mode

Noise 的 `IK` / `IKpsk2` 里，**responder 是靠「写 msg2」完成握手的**，不是靠读。原实现只在
`read_handshake()` 里做 `into_transport_mode()`，于是 responder 永远
`is_established() == false`，后续 `decrypt()` 一律 `not established`：

```
responder.read_handshake(m1) -> OK len=0
responder.write_handshake    -> OK len=48
responder.established=false          <-- 这里应该是 true
initiator.encrypt            -> OK len=20
responder.decrypt            -> THREW not established
```

**修复**：`Peer::write_handshake` 写完消息后同样检查 `is_handshake_finished()` 并
`into_transport_mode()`，失败时写 `last_error`（`handshake write failed: ...`）。

### R3 · `test.mjs` 自身两处错误假设（不是实现问题，但会让验收永远红）

**(a) `psk_mismatch_rejected` 断言错了时机。** 原测试只做
`responder.read_handshake(m1)` 就期望抛错。但 `IKpsk2` 的 PSK token 在 **msg2**（responder
的消息）里，msg1 与 PSK 无关——数学上不可能在 msg1 阶段发现 PSK 不一致。
改为走完整个交换，断言**安全性质**：持错 PSK 的一方必须抛错且不进入 transport mode。
篡改/错 key 仍然被拒，只是拒绝点从「msg1」变到「msg2」，这与 Noise 规范和 WireGuard 一致。

**(b) 篡改测试会毒化共享 session，导致后面的时延循环崩。** 原顺序是先 `decrypt(tampered)`
（预期失败），再用同一个 SESSION 跑 2000 次 `decrypt(encrypt(pt))`。Noise 在认证失败时
**仍会推进接收 nonce**（防 nonce 复用），于是收发 nonce 错位一格，循环第一次就
`decrypt failed: decrypt error` 未捕获退出。真实实现里这也意味着**认证失败必须断链**。
修复：时延基准跑在**独立的一对 Peer** 上，不动 WebSocket 那条已经「被烧掉」的 session。

## 五、实测数据

### 5.1 体积（`node test.mjs` 输出 + `ls`）

| 产物 | 原始 | gzip -9 |
|---|---|---|
| `protocol_wasm_bg.wasm` | 207 148 B (202 KB) | 95 008 B (93 KB) |
| `protocol_wasm.js`（glue） | 30 900 B | — |
| 合计（首屏需传输） | 238 048 B | ~126 KB |
| `wasm-b64.js`（spike 专用 base64 内联，**不要**带进生产） | 275 663 B | — |

对比 Miuix 的 ~900 KB 预算：协议核心 gzip 后约 **93 KB**，占 10% 左右，可接受。

### 5.2 握手

| 指标 | 值 |
|---|---|
| 握手字节数 | 144 B（msg1 96 B + msg2 48 B） |
| 耗时（3 次运行） | 98 ms / 1 368 ms / 1 345 ms |

⚠️ 抖动 14×，同一台机器上不可用于容量判断：这台是共享/受限的 4.14 内核盒子，AEAD 数字
同步抖动（0.30 / 1.66 / 1.52 ms）。**结论只到「毫秒级、不是瓶颈」**，正式预算要在目标
浏览器/真机上重测。

### 5.3 AEAD（1 KB payload，2000 次 encrypt+decrypt）

| 运行 | 每次 |
|---|---|
| #1 | 0.300 ms |
| #2 | 1.657 ms |
| #3 | 1.522 ms |

### 5.4 功能检查（`node test.mjs`，8 项）

| 检查 | 结果 | 说明 |
|---|---|---|
| `selftest_in_module` | PASS | 模块内自检（IK 无 PSK 双向 AEAD） |
| `psk2_roundtrip` | PASS | IKpsk2 双端 roundtrip，`pt=psk-frame` |
| `psk_mismatch_rejected` | PASS | 错 PSK 走到 msg2 被拒，initiator 未建立 |
| `HANDSHAKE_OK` | PASS | 走真实 WebSocket 的握手，pattern=IKpsk2 |
| `ECHO_OK` | PASS | 密文经 socket 回显后解密回 `hello vade` |
| `tamper_rejected` | PASS | 翻转 1 bit 必须解密失败 |
| `js_callbacks_fired` | PASS | `handshake.done` + `frame.received` 都到 JS |
| `no_stray_errors` | PASS | 服务端无异常 |

## 六、对 C1 的边界建议

1. **协议核心做成无 IO 的纯库**：`Peer { write_handshake / read_handshake / encrypt /
   decrypt }`，加一个 `handshake(transport, psk)` 的便利封装。Transport 只有三个方法
   `remoteStaticKey() / send(bytes) / recv() -> Promise<bytes>`，宿主负责 WebSocket。
   这条已被 T2 证明可行，也让 Kotlin 侧只需要实现一个三方法的对象。
2. **`write_handshake` 和 `read_handshake` 都必须处理「握手完成」**——这是 R2 踩出来的坑，
   单测要覆盖 responder 侧（只测 initiator 会漏）。
3. **认证失败即断链**：Noise 失败后 nonce 已前移，session 不可继续用（R3b）。C1 的连接层
   要把 `decrypt` 的 Err 当成 fatal。
4. **`IKpsk2` 的错 PSK 在 msg2 才可见**，配对/重连逻辑不要假设「msg1 就能验证对端」。

## 七、构建环境的坑（复现必读）

- `getrandom` 两个大版本都要显式开 feature：`getrandom 0.2 = ["js"]`、
  `getrandom 0.3 = ["wasm_js"]`，否则 `generate_keypair()` 在 wasm 上 panic。
- **`CARGO_TARGET_DIR` 泄漏会静默产出陈旧 `pkg/`**（R1）。`build.sh` 已改为强制
  `--target-dir`；CI 里也应 unset 该变量。
- `[profile.release] strip = true` 会让 wasm-bindgen 报
  `target_features custom section is missing`（只是 warning，产物可用）。

## 八、未验证 / 风险（逐条）

1. **T3 Kotlin/Wasm 未验证**（见 §九）。这是任务书里认定的真正风险点：Rust→JS 通不代表
   Kotlin/Wasm→JS 通。本次只跑到 JS 侧，W-A 的最终裁决仍取决于 T3。
2. **T4 时延数字不可信**：抖动 14×，只能在目标设备上重测。
3. **浏览器未验证**：只在 Node/V8 跑过。`WebSocket`/`atob`/`crypto.getRandomValues` 的
   浏览器行为未实测；`pkg/wasm-b64.js` 是 spike 权宜（生产应走 streaming instantiate）。
4. **ADR-001 未写**（T5，不在本次范围）。
5. **只验证了 loopback**：没有丢包/重连/分片（>64 KB 帧）场景。

## 九、T3 现状

`kotlin-wasm/` 里已有完整的最小工程：`@JsModule` + `external fun` 引入 wasm-bindgen 的 ES
模块、`awaitJs()` 用 `suspendCancellableCoroutine` 把 JS Promise 桥成 Kotlin 协程、
Kotlin lambda 作为 Rust→UI 回调、以及一个把 base64 wasm 嵌入的 `vade-bridge` 适配层。
运行命令（无 wrapper，用系统 Gradle 8.10.2）：

```bash
cd spike/wasm/kotlin-wasm && /opt/gradle/gradle-8.10.2/bin/gradle wasmJsNodeDevelopmentRun
```

**实测结论：未验证。** 两次尝试（后台 900 s、前台 230 s）都卡在 Gradle 的插件/依赖解析：

```
To honour the JVM settings for this build a single-use Daemon process will be forked.
EXIT=124   （timeout 230s，之后无任何构建进展）
```

原因（已确认，不是代码问题）：

- `~/.gradle/caches/modules-2/files-2.1` **不存在**，本机没有任何 Kotlin/Wasm 工件缓存；
- 网络只有 `https://maven.aliyun.com/repository/public` 可达（返回 404，说明链路通），
  `https://repo1.maven.org/maven2/` 8 s 超时不通；而 `settings.gradle.kts` 的
  `pluginManagement` 里 gradlePluginPortal / mavenCentral 排在 aliyun 之后，Gradle 会
  依次尝试并长时间挂起。

要在能联网的机器上给出 T3 结论，只需：

```bash
cd spike/wasm/kotlin-wasm
/opt/gradle/gradle-8.10.2/bin/gradle wasmJsNodeDevelopmentRun   # 期望输出 KOTLIN_WASM_OK
```

**未做的事**：没有把「工程已写好」当成「Kotlin 能调用」的证据。T3 仍然决定 W-A 是否成立
（任务书 §六：禁止用 Node 跑通代替 Kotlin/Wasm 跑通）。
