# C1 · 协议核心结果（`protocol/`）

| 项 | 状态 |
|---|---|
| 帧格式 / 握手 / 错误码（C-01） | ✅ 冻结：`protocol/v1/frames.md` |
| KAT（C-02） | ✅ 生成：`protocol/v1/kat.json`，sha256 `e154100b…` |
| 单测 | ✅ **58 passed / 0 failed**（15 lib + 7 frame_kat + 10 interop + 26 negative） |
| clippy | ✅ `--all-features --all-targets -D warnings` 干净 |
| C-15 `schema/pair-code.json` | ⚠️ 已补，状态 `frozen-partial`（256 汉字表待入库） |
| Python 绑定（T5） | ✅ 生成 `protocol/core/python/vade_protocol.py`；`version()=0.1.0`、`protocol_version()=1`；wheel 未打 |
| `cargo test --all-features` | ⚠️ 未跑完（编译超时 400s，非失败） |
| bench / wasm32 / 三语言绑定 / fuzz | ❌ 未验证 |

## 一、实测（父代理复跑）

```
cd protocol/core && cargo test
  running 15 tests   → ok. 15 passed
  running 7 tests    → ok. 7 passed
  running 10 tests   → ok. 10 passed
  running 26 tests   → ok. 26 passed
  sha256(protocol/v1/kat.json) = e154100b75a5c8e95705350fdfaceb2d547d4166d27e7fd7a0c2e6d8eabe6feb
```

负向用例（26 条）覆盖：错版本、错 role byte、错 prologue、改 1 字节密文、改 tag、重放、counter 回滚、generation 回滚/跳变、rekey 后旧密钥、反射、未登记 static key、错配对码、2 MiB 声明长度（带 RSS 断言）、截断、65 KB 握手消息、提前 `into_transport`。

## 二、三条裁决（写进 `frames.md` §7）

1. **nonce 交给 `snow`**：方向隔离靠 Noise Split 的双密钥，帧头 `counter` 只做重放检测——因此显式单调校验是唯一重放防线，不能省。
2. **不做全握手 KAT**：握手随机化，KAT 覆盖帧编解码 + AEAD 原语 + PSK 派生；跨实现互通靠在线互通测试。
3. **XXpsk3 的 PSK 在第三条消息混入**，派生式见 `frames.md` §6。

## 三、三条勘误（与任务书/设计的偏差）

- **E1：帧头是 20 字节**，不是任务书写的 16（`1+1+1+1+4+4+8`）。所有长度断言按 20 算。
- **E2：单帧明文上限 65519 B**，不是 `limits.max_payload` 承诺的 1 MiB。
- **E3：snow 0.10 传输模式下，AEAD 失败不推进接收 nonce**（`cipherstate.rs::decrypt_ad` 实测），与 C0a spike 的结论相反；`fatal` 策略保留，测试钉住真实行为。

## 四、对下游的硬约束

- **C2**：不要按 1 MiB/帧设计；>65519 B 需要 RPC 层分片（v1 未定义）。
- **C2/UI**：重放窗口必须实现，否则「nonce 交给 snow」的代价会变成真实漏洞。
- **W-B（若启用）**：用 `kat.json` + 在线互通测试对齐，不要自己造向量。

## 五、未验证（如实记录）

| 项 | 原因 |
|---|---|
| `cargo test --all-features` | 编译超时 400s（该机 8 核/5GB 高负载），非已知失败；clippy --all-features 已证明 uniffi 路径可编译 |
| `cargo bench --bench aead` | 未跑，NFR-2/NFR-3 无实测数字 |
| wasm32 构建 | 未验证；C0b 的 `getrandom/js` 注意事项仍适用于使用方 |
| Kotlin/Swift 绑定 | 未做（Python 已生成并冒烟，见下） |
| `cargo-fuzz` 10 分钟 | 未做 |
| 256 汉字表 | 未入库，`normalize()` 只校验后 6 位 |
