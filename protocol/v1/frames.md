# Vade 协议 v1 · 帧格式、握手与错误码（C-01）

| 项 | 值 |
|---|---|
| 契约号 | **C-01** |
| 版本 | **1.0（冻结）** |
| 冻结时间 | 2026-11-12（W1 末） |
| 所有者 | C1（`protocol/core/`、`protocol/v1/`） |
| 消费者 | C2（transport/registry）、C4（Python SDK）、U1（Kotlin 绑定）、F1（集成）、W-B（TS/Wasm 兜底） |
| 实现 | `protocol/core`（crate `vade-protocol-core`，lib `vade_protocol`） |
| 机器可读向量 | `protocol/v1/kat.json`（C-02） |

> **冻结的含义**：本文件里的字节布局、字段语义、错误码与判序规则不得超过。新增字段只能以「保留位 + 升版本」的方式做；语义变更必须提 RFC（`docs/agents/01-contracts.md` §七）。消费方按本文件实现即可与 Rust 核心互通——不需要读 Rust 源码。

---

## 1. 帧格式

### 1.1 帧图

```
 偏移  0        1        2        3
      +--------+--------+--------+--------+
      | ver(1) | type(1)| flags(1)| rsv(1) |
      +--------+--------+--------+--------+
   4  | payload_len (4, BE)                |
      +------------------------------------+
   8  | generation (4, BE)                 |
      +------------------------------------+
  12  | counter (8, BE)                    |
      +------------------------------------+
  20  | body ……                            |
      +------------------------------------+
```

- **帧头固定 20 字节**（`HEADER_LEN = 20`）。
- `body` 长度：`Handshake` 帧 = `payload_len`；其余类型 = `payload_len + 16`（Poly1305 tag）。
- 帧总长 = `20 + payload_len`（握手）或 `20 + payload_len + 16`（加密）。
- 1 MiB 的 `payload_len` 能表达 4 GiB，但**单帧物理上限是 65519 字节**，见 §7.3。

### 1.2 字段语义

| 字段 | 宽度 | 语义 | 违规时 |
|---|---|---|---|
| `ver` | 1 | 协议主版本，恒为 `1`。**不协商、不降级**。 | `PROTOCOL_MISMATCH`，**解密之前拒绝** |
| `type` | 1 | 帧类型，见 §2 | `PROTOCOL_MISMATCH` |
| `flags` | 1 | v1 冻结为 `0`（保留） | `PROTOCOL_MISMATCH` |
| `rsv` | 1 | v1 冻结为 `0`（保留） | `PROTOCOL_MISMATCH` |
| `payload_len` | 4 | **明文**长度（不是密文长度）。这是本协议唯一的长度前缀。 | 与实际字节不符 → `INVALID_ARGUMENT`；超限 → `PAYLOAD_TOO_LARGE` |
| `generation` | 4 | 密钥代数。握手成功置 `1`，该发送方向每次 rekey `+1`。握手帧为 `0`。 | 回退 → `PERMISSION_DENIED`(fatal)；跳代 > +1 → `PROTOCOL_MISMATCH`(fatal) |
| `counter` | 8 | **本方向**帧序号，从 `1` 起严格连续。握手帧为 `0`。 | 重复/回退 → `PERMISSION_DENIED`(fatal) |
| `body` | 变长 | 握手帧：Noise 消息明文；其余：ChaCha20-Poly1305 密文（含 tag） | tag 不验 → `PERMISSION_DENIED`(fatal) |

**`payload_len` 就是「4 字节大端长度帧」**：它是第 5–8 字节的大端长度前缀，语义与 length-prefixed framing 完全一致，只是前面还有 4 字节控制头、后面跟 8 字节代数/计数。字节流传输（WS 之外的管道/TCP 回退）用同一字段自定界：

```
读满 20 字节 → 解析帧头 → wire_len = 20 + payload_len (+16) → 再读 wire_len-20 字节 → 解一帧
```

**不得**另立外层长度前缀；同一段字节流上出现两种长度语义就是实现 bug。

### 1.3 校验顺序（硬要求）

接收端**必须**按此顺序判，顺序本身就是安全属性：

1. 读满 20 字节 → `ver`、`type`、`flags`、`rsv` 合法性；
2. `payload_len ≤ limits.max_payload` 且 `≤ 65519`（加密帧）/ `≤ 65535`（握手帧）——**此步之前不得按 `payload_len` 分配任何缓冲**；
3. 声明长度与实际字节数一致（严格相等，不容忍多余或不足）；
4. `(generation, counter)` 重放判定（§4.2）；
5. 只有以上全过，才调用 AEAD 解密。

第 1 步失败时**一次 AEAD 都不能调用**。C1 的 `tests/negative.rs::wrong_version_is_rejected_before_decryption` 用「receive counter 未动 + 原帧仍可解」证明这一点，不是断言一个 bool。

---

## 2. 帧类型

| 值 | 名称 | body | generation / counter | 说明 |
|---|---|---|---|---|
| `0x01` | `handshake` | Noise 握手消息**明文** | `0 / 0` | 握手期间唯一允许的帧 |
| `0x02` | `rpc` | 密文 | `≥1 / ≥1` | JSON-RPC 2.0 载荷 |
| `0x03` | `event` | 密文 | 同上 | core → 模块事件 |
| `0x04` | `heartbeat` | 密文（允许空明文） | 同上 | 空明文也带 16 字节 tag |
| `0x05` | `close` | 密文 | 同上 | 有序关闭，body 为 `{code,message}` JSON |
| `0x06` | `rekey` | 密文 | 同上 | body 为 4 字节大端 `new_generation`，**用旧 key 加密**（§4.3） |

未知 `type` 一律 `PROTOCOL_MISMATCH`，不忽略——帧层没有 manifest 那种前向兼容。

---

## 3. 握手

### 3.1 参数集（固定，禁止协商）

```
Noise_IK_25519_ChaChaPoly_BLAKE2s       常规连接（已配对，1-RTT）
Noise_XXpsk3_25519_ChaChaPoly_BLAKE2s   首次配对（2-RTT，PSK 在第 3 条消息混入）
```

- 密码学全部由 `snow` 提供，**不自研任何原语或状态机**。
- 版本统一：C0a 用 `snow 0.10.0`、C0b 用 `0.9.x`；**C-01 冻结为 `snow = "=0.10.0"`**（理由见 §7.1）。
- 前向保密：每连接新生成 ephemeral X25519；长期密钥只参与认证 DH，不加密 payload。

### 3.2 prologue

```
prologue = b"vade/v1" || role_byte      （共 9 字节）
role_byte = 0x01  本次连接由 core 发起
            0x02  本次连接由 module 发起
```

- **两端必须发送同一个 `role_byte`**：它描述「谁拨号」，不是「我是谁」。C0a 实测：两端各填自己的角色 → transcript 哈希不同 → 握手必然失败。
- prologue 不一致 → 握手失败（`PERMISSION_DENIED`，fatal）。这是防跨协议混淆的第一道闸。
- 字节级向量在 `kat.json` 的 `prologue` 段。

### 3.3 IK 时序（1-RTT）

```
module(initiator)                                core(responder)
  msg1  -> e, es, s, ss        (含 module 静态公钥，明文帧 type=0x01)
                                ① read(msg1) 后用 get_remote_static() 比对白名单
  msg2  <- e, ee, se
  ② read(msg2) 校验 core 静态公钥
  之后双方各持 k_c2m / k_m2c（Split 两把独立密钥）
```

- 发起方读完 msg2 即 `is_finished()`；响应方**写完 msg2** 才算完成（`snow` 的语义，绑定时不要按「读完 msg1 就完成」实现）。
- IK 缺 `remote_pub` → `INVALID_ARGUMENT`，**禁止退回 XX**。

### 3.4 XXpsk3 时序（2-RTT，首次配对）

```
module(initiator)                                core(responder)
  msg1  -> e
  msg2  <- e, ee, s, es        ② 发起方在此看到 core 静态公钥
  msg3  -> s, se, psk          ① 响应方读完 msg3 才看到 module 静态公钥
```

- PSK 在 msg3 混入：任何一方 PSK 不同，msg3 的解密就失败（`PERMISSION_DENIED`，fatal）。
- 两端 `remote_static()` 都必须能取到对方静态公钥，供 core 登记（FR-4）。
- 未完成握手调用 `into_transport()` → `INVALID_ARGUMENT`。

### 3.5 白名单校验（**必须自己写**）

`snow` 的 IK responder 从 msg1 学到发起方静态公钥后**直接接受**，不会与 `remote_public_key()` 比对——任何密钥对都能走完 `es`。因此：

- 响应方**必须**在 `read(msg1)` 成功后立即取 `get_remote_static()` 与白名单里的公钥比对，不等、不拖到传输态；
- 比对失败 → `PERMISSION_DENIED`(fatal)，且**不得**再写 msg2；
- 发起方同样在 `read(msg2)` 后比对（防止 core 被替换）；
- `into_transport()` 里再做一次兜底比对（`tests/negative.rs::ik_with_an_unlisted_static_key_fails` 证明这道检查是 load-bearing 的）。

### 3.6 握手消息上限

单条握手消息 > **65535 字节** → `PAYLOAD_TOO_LARGE`，在交给 `snow` 之前拒（Noise 协议本身的 `MAXMSGLEN`）。

---

## 4. 传输态

### 4.1 nonce 与 counter 是两套东西

| | 谁维护 | 范围 | 是否重置 |
|---|---|---|---|
| AEAD nonce | `snow::TransportState` | 96-bit（4 字节 0 ‖ 8 字节小端计数） | **不重置**，rekey 只换 key |
| 帧头 `counter` | 本 crate | u64，每方向独立 | rekey 后归 `0`，下一帧从 `1` 开始 |

- 帧头 `counter` **不参与 AEAD**；它只做重放检测与断线定位。
- 方向隔离靠 Split 出的两把密钥（c2m / m2c），不靠 nonce 里的方向位：反射帧用另一把密钥解密，tag 必然失败。
- 推论（实现者必读）：帧是有序且不可丢的——**丢一帧会导致后续所有帧的 AEAD nonce 错位**。WS 保证有序可靠，所以这不是问题；但任何「跳过/丢弃」逻辑都不许加在传输层。

### 4.2 重放规则

接收端维护 `(recv_generation, recv_counter)`，判定**严格字典序**：

| 条件 | 结果 |
|---|---|
| `gen < recv_generation` | `PERMISSION_DENIED`，**fatal**（代数回退，不得回退旧 key） |
| `gen > recv_generation + 1` | `PROTOCOL_MISMATCH`，**fatal**（跳代） |
| `gen == recv_generation && counter <= recv_counter` | `PERMISSION_DENIED`，**fatal**（重放/回退） |
| `gen == recv_generation + 1 && counter >= 1` | 接受（对端已 rekey） |
| `counter == 0`（加密帧） | `PROTOCOL_MISMATCH`，fatal |

- **不做滑动窗口**：WS 是有序可靠传输，乱序只可能是攻击或实现错误。
- 发送端 `counter` 必须**严格连续**（首帧 `1`），跳号 → `INVALID_ARGUMENT`；这是为了让接收端能判定丢失。
- 重放判定在 AEAD **之前**：被拒的帧不消耗任何密码学状态。

### 4.3 rekey

| 项 | 规定 |
|---|---|
| 帧 | `type=0x06`，body = 4 字节大端 `new_generation`，**用旧 key 加密**（否则对端不知道该用哪把解） |
| 方向性 | **按发送方向各自独立**：`gen` 是本端发送方向的代数；收到对端 `0x06` 只推进**接收方向**，不动自己发送方向的 gen |
| 计数器 | rekey 后本方向 `counter` 归 `0`，下一帧 `1` |
| 幂等 | 收到 `new_generation <= recv_generation` → **忽略该帧但不断连**（对端可能只是重复/双方同时触发）；`> recv_generation + 1` → 断连 |
| 失败 | rekey 帧解不开 → 立即断连，**不回退旧 key**（回退等于给降级攻击留门） |
| 与重握手 | rekey **不刷新前向保密**（ephemeral 未变）；24h 另起重握手（FR-3.12） |

实现注意：`snow 0.10` 提供 `rekey_outgoing()` / `rekey_incoming()` 这一对**按方向**的接口，正好对应上表；**不要**用「同时 rekey 两把」的写法，那会在双向并发时把对端在途帧解错。

### 4.4 计数耗尽

`counter > u64::MAX - 1024`（即 `MAX_COUNTER`）→ `UNSUPPORTED`，提示调用方重握手（FR-3.3）。留 1024 帧余量，让调用方在耗尽前还有操作空间。

---

## 5. 错误码映射

错误码取自 `schema/errors.json`（C-04），**不得自造**。`retriable` 是码的属性（C-04 表），`fatal` 是本次错误的属性（本层新增，C-04 不覆盖）：`fatal=true` 表示调用方**必须断开连接**。

| 条件 | code | fatal | retriable |
|---|---|---|---|
| `ver != 1` / 未知 `type` / `flags!=0` / `rsv!=0` / 加密帧 `counter==0` / 跳代 / rekey body 非 4 字节 | `PROTOCOL_MISMATCH` | ✅ | ❌ |
| 握手认证失败（含 prologue 不一致、白名单不符、PSK 不符） | `PERMISSION_DENIED` | ✅ | ❌ |
| AEAD tag 不验 | `PERMISSION_DENIED` | ✅ | ❌ |
| 重放 / counter 回退 / generation 回退 | `PERMISSION_DENIED` | ✅ | ❌ |
| `payload_len` 超限（含 > 65519 加密帧、> 65535 握手帧） | `PAYLOAD_TOO_LARGE` | ✅ | ❌ |
| 声明长度与实际字节不符 / 截断 / 尾部多余字节 / 参数非法（缺 `remote_pub`、缺 PSK、未完成握手进传输态、方向传反、发送跳号） | `INVALID_ARGUMENT` | ❌ | ❌ |
| `counter` 超过 `MAX_COUNTER` | `UNSUPPORTED` | ❌ | ❌ |
| 内部不可能发生（内置参数解析失败、锁中毒、snow 返回值越界） | `INTERNAL` | ❌ | ❌ |

规则：
- `message` ≤ 300 字，**不回显密钥、不回显对端字节**；认证失败统一一句话，不做区分（区分即 oracle）。
- 人读信息只描述「哪一步失败」，不描述「为什么」。

---

## 6. 配对码与 PSK 派生（C-15）

```
normalize(code):
    NFKC 归一化
    → 去掉 '-'、空格（含 U+3000）、'\t'、'·'(U+00B7)、'・'(U+30FB)
    → 逐字符 to_uppercase（与 locale 无关）
    结果必须是 8 个字符（2 汉字 + 6 base32）

PSK = HKDF-SHA256(
        ikm  = utf8(normalize(code)) || BLAKE2s256(core_static_pub)   // 32 字节原始摘要，不是 hex
        salt = "vade/pair/v1"
        info = ""                                                     // 冻结为空串
        L    = 32)
```

- base32 字母表：`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（剔除 `I O 0 1`）。
- NFKC 是关键：`青松－Ａ７Ｋ２Ｍ９`（全角）与 `青松-A7K2M9` 必须算出同一个 PSK，否则手机输入法会静默产生不同密钥。
- `info` 为空是**冻结决定**（设计与 FR-4.9 都没规定 info）；以后加 info 属于破坏性变更，走 RFC。
- 本实现的归一化只校验「8 字符 + 后 6 位在 base32 表内」，**未校验前 2 个汉字是否在 C-15 的 256 字表内**（该表尚未入库，见 `RESULT.md` 已知限制 U3）。
- 4 组等价输入的 PSK 向量在 `kat.json` 的 `hkdf`/`normalize` 段，四者 `psk` 必须完全相等。

---

## 7. 实现说明（必读，三条裁决 + 三条勘误）

### 7.1 裁决一：用 `snow` 原生 nonce，方向隔离靠 Split 双密钥

方案 §2.1 原写 `nonce = direction(4B) || counter(8B)`，但 `snow::TransportState` 自己持有 nonce 且**不暴露流量密钥**——按原方案实现只能 fork snow 或自研对称态，两者都违背「不自研密码学」。

**裁决**：nonce 交给 `snow`（96-bit，前 4 字节 0、后 8 字节小端计数）；方向隔离由 `Noise Split` 的两把独立密钥承担（比把方向写进 nonce 更强：反射帧必然解不开）；帧头 `counter` 只做重放检测与断线定位。

**代价（如实记录）**：帧头 `counter` 与 AEAD nonce 不再字面对应，所以 §4.2 的显式单调校验是**唯一**的重放防线，省掉它等于没有重放防护。

### 7.2 裁决二：不做全握手 KAT，改为「原语 + 编解码 + 派生」KAT

握手是随机化的（ephemeral 每次新生成），全握手 KAT 需要固定 RNG 注入，收益低、维护贵。**裁决**：KAT 覆盖帧编解码、AEAD 原语（固定 key/nonce/pt→ct）、配对码 PSK 派生；跨实现互通（W-B 的 TS）用**在线互通测试**兜底，不靠握手向量。

### 7.3 裁决三：XXpsk3 的 PSK 位置与派生

用 `Noise_XXpsk3_25519_ChaChaPoly_BLAKE2s`，PSK 在第三条消息混入；派生式见 §6。C1 是 C-15 规则的参考实现，`kat.json` 是其他实现的判据。

### 7.4 勘误 E1：帧头是 20 字节，不是 16

C1 任务书 T2 写「帧总长 = `16 + payload_len + 16`」，但同一行的字段表是 `1+1+1+1+4+4+8 = 20`，`design.md` §2.1 的字段图也是 7 个字段。**以字段表为准：`HEADER_LEN = 20`**。16 字节装不下 `flags`、`rsv` 与 `payload_len` 三者。所有长度断言（含 `kat.json` 的 `frame_len`）按 20 计算。

### 7.5 勘误 E2：单帧明文上限是 65519，不是 1 MiB

`limits.max_payload`（manifest 默认 1 MiB）是**逻辑**上限；Noise 单条消息上限 65535 字节，减去 16 字节 tag 后，**单帧明文最多 65519 字节**。本实现在帧头解析阶段就按 `min(limits.max_payload, 65519)` 拒绝，超过 → `PAYLOAD_TOO_LARGE`，不分配缓冲。

**影响**：C2 在 manifest 校验时不能承诺「1 MiB 单帧」；> 65519 的载荷必须由 RPC 层分片（v1 未定义分片，属于 C4/C2 的待办），或把 `limits.max_payload` 下调到 65519 以内。这是本波次发现的**跨批次约束**，已登记在 `RESULT.md` 的「交付给其他批次」。

### 7.6 勘误 E3：认证失败后 nonce 未推进（snow 0.10 实测）

C0a/C0b 的结论「认证失败后 nonce 已推进、会话不可复用」在 **snow 0.10 的传输态上不成立**：`cipherstate.rs::decrypt_ad` 只有在解密成功后才 `self.n += 1`，失败时 nonce 原封不动（该结论适用于握手态 / 0.9.x）。

**但策略不变**：AEAD 失败仍然 `fatal = true`，调用方必须断连。理由：tag 不验意味着链路上有人改字节或对端实现坏了，调用方无法区分；继续用同一条链路等于把「可重试」当成「可信」。`tests/negative.rs::flipped_ciphertext_byte_is_rejected` 把这个实测行为与策略都钉住了。

### 7.7 `snow` 版本统一

`=0.10.0`。理由：C0a 用它在 UniFFI 边界上跑通（Kotlin/Swift/Python 都能拿到对象句柄），且 0.10 才有**按方向**的 `rekey_outgoing()/rekey_incoming()`（0.9.x 只有整态 rekey，做不出 §4.3 的方向性）。C0b 的 0.9.x 结论其余部分（无 IO、字节进出、wasm 可行）继续有效，版本以本文件为准。

---

## 8. KAT（C-02）

文件：`protocol/v1/kat.json`，根字段 `version: 1`、`protocol: "vade/v1"`。

| 段 | 内容 | 判据 |
|---|---|---|
| `frame[]` | 9 条完整帧（含 1 条握手帧、1 条 rekey 帧、1 条空明文、1 条大 counter） | `hex` 解码后逐字段比对，再 `encode()` 必须逐字节还原 |
| `aead[]` | 4 条 ChaCha20-Poly1305（空/1B/16B/1KB，AAD 为空） | `encrypt(key,nonce,pt) == ct`，`decrypt` 回原文，翻 tag 必败 |
| `hkdf[]` | 4 条配对码输入（规范形/全角/小写/中点分隔） | 四条 `psk` 必须字节相等；`normalize` 与 `core_fp` 也必须对上 |
| `normalize[]` | 4 条归一化等价 | `normalize(input) == expected` |
| `prologue[]` | 2 条 prologue 字节 | 必须等于 `b"vade/v1\x01"` / `b"vade/v1\x02"` |

- **全部向量由实现生成，无手写值**：`cargo run --features kat-gen --bin gen-kat`。生成器先把每条向量的帧自己解码一遍再写出（自校验），`source` 字段恒为 `"generated"`。
- 帧的 `body` 是**真 AEAD 输出**（KAT key、固定 nonce、`pattern(n)` 明文），因此第二实现既能验帧结构、也能验解密与明文内容。
- 未覆盖：完整握手（随机化，见 §7.2）。
- 校验命令：`cargo test --test frame_kat`。

---

## 9. 「本实现不含 IO」声明

`protocol/core` 是**纯库**：输入字节、输出字节。它不打开 socket、不建 WS、不读文件、不做重连、不做定时器。

- 帧的读取边界由调用方负责：读 20 字节 → `FrameHeader::peek` → 按 `wire_len()` 读满 → `Frame::decode`。
- 握手消息的收发由调用方负责：`Handshake::write/read` 只吃/吐字节。
- 重连、心跳、超时、并发全部是 SDK（C4）与 core（C2）的事。

这条边界是 C0b（Wasm）能复用同一份实现的**唯一**原因；破了它，Web 端就得重写一遍密码学。

---

## 10. 变更规则

| 变更 | 允许？ |
|---|---|
| 新增 `flags` 语义 / 新 `type` | 否，必须升主版本 |
| 放宽 `payload_len` 上限 | 否（受 Noise 65535 物理约束） |
| 新增可选 JSON 字段（RPC 层） | 是，向前兼容 |
| 改 prologue 前缀 / role_byte | 否，等于换协议 |
| 改 KAT 向量 | 只能由 `gen-kat` 重新生成并在 PR 里附 diff 与原因 |
| 错误码新增 | 先改 C-04（C2），本文件跟随 |
