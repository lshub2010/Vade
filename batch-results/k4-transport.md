# k4-transport 批次结果

## 第二部分：SPKI 指纹 / fp8 与 psk 命名拆分 / whitelist 自校验

### 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/identity/tls.py` | 新增 `spki_fingerprint_hex(cert_path)`：`cryptography` 解析 PEM，取公钥 DER `SubjectPublicKeyInfo`，`SHA-256(...)[:16]`，即 `tls=SPKI[:16]` pin（D78）。`cryptography` 缺失时抛 `VadeError("UNSUPPORTED", ...)`，明确不退回证书 DER。`cert_fingerprint_hex()` 保留但文档改为「整张证书 DER 的 SHA-256，仅供审计/兼容，不是 pin」 |
| `core/vade/identity/whitelist.py` | `_fp8()` 提升为公开 `fp8_hex()`（SHA-256[:16]），保留 `_fp8` 别名；`_load()` 对带 `public_key` 的条目自校验 `sha256(pub)[:16] == fingerprint`，不一致/非法 hex/非 32 字节一律丢弃（fail closed）；无 `public_key` 的旧条目原样加载。`add()` 复用同一校验并拒绝不匹配的 (fingerprint, public_key) |
| `protocol/core/src/identity.rs` | 新增 `Identity::psk_fingerprint()` / `psk_fingerprint_hex()`（BLAKE2s-256）与自由函数 `psk_fingerprint_of()`；`fingerprint()` / `fingerprint_of()` 保留为兼容别名；**`fingerprint_hex()` 名字不动**（uniffi 导出名/绑定 checksum 依赖它），内部委托给 `psk_fingerprint_hex()` |
| `core/tests/identity/test_spki_and_whitelist_selfcheck.py` | 新增单文件测试（9 条）：SPKI 值 = SHA-256(SPKI)[:16] 且 ≠ 证书 DER；缺 `cryptography` → UNSUPPORTED 而非 DER 回退；`fp8_hex` 与 `_fp8` 别名；whitelist load 接受一致条目、丢弃不一致/非法条目、保留无公钥旧条目；`add()` 拒绝不匹配、接受并持久化一致对 |

### 三个指纹的分工（防混淆）

| 名字 | 算法 | 用途 |
|---|---|---|
| `fp8_hex(pub)` / 二维码 `fp` | SHA-256(X25519 公钥)[:16] | core 身份指纹，人工核对 / setup 页 / `--print-fingerprint` |
| `spki_fingerprint_hex(cert)` / `tls=` | SHA-256(DER SubjectPublicKeyInfo)[:16] | TLS pin（D78），轮换证书不换钥匙时保持不变 |
| Rust `psk_fingerprint_hex()` | BLAKE2s-256(X25519 公钥) 全 64 hex | pair-code PSK 的 `core_pub_fp` 输入；uniffi 导出名仍为 `fingerprint_hex`（绑定 checksum 依赖，不可改） |

### 验证

- `python -m pytest core/tests/identity/test_spki_and_whitelist_selfcheck.py -q` → **9 passed**
- `python -m pytest core/tests/identity/ core/tests/transport/test_device_identity.py core/tests/transport/test_handshake_wiring.py -q` → **33 passed**（此前 `test_device_identity` 因环境缺 `protocol/core/python/libvade_protocol.so` 失败；用 `cargo build --features uniffi` 重新生成后通过）
- `cargo test --offline --lib`（protocol/core）→ **16 passed**，含新增 `identity::tests::psk_fingerprint_names_are_the_same_value`
- `cargo check --offline --features uniffi` → 通过；重新生成 `.so` 后 `vade_protocol.FfiIdentity.from_secret_hex(...).fingerprint_hex()` 可正常调用（说明 uniffi 方法名/checksum 未被破坏）
- 未跑全量 pytest（按要求）。

### 发现（需上游注意）

- `core/vade/crypto_gateway/session.py:30` 的 `fingerprint_of()` 同样是 `SHA-256(pub)[:16]`，与 whitelist 自校验一致；但它与 Rust `FfiIdentity.fingerprint_hex()`（BLAKE2s-256）算出的值**不同**。当前 core 侧 whitelist/session 全走 SHA-256，Rust 的 `fingerprint_hex` 仅作 PSK 输入使用。本次命名拆分（`fp8_hex` / `psk_fingerprint_hex`）就是为了让两者不再都叫 “fingerprint”；如果协议规范本意是 whitelist 也用 BLAKE2s，那是一个独立的兼容性决策，不在本棒擅自改。

### TODO（不在本棒文件范围）

1. **`core/vade/__main__.py:182`（另一棒负责）**：`--print-tls-fingerprint` 仍打印 `tls_module.cert_fingerprint_hex(cert)[:16]`（证书 DER，错值）。应改为 `tls_module.spki_fingerprint_hex(cert)`（该函数直接返回 16 hex，再 `[:16]` 也无害）。`print_tls_fingerprint` 的 help 文案仍写 "TLS certificate fingerprint"，建议同步为 SPKI。
2. `core/vade/admin/cli.py:140-151`：`print-tls-fingerprint` 子命令自己内联了一段 SPKI 计算，结果正确但与 `tls.spki_fingerprint_hex` 重复；后续可改为调用该函数，避免两处漂移。
3. `sdk/python/vade_sdk/transport.py:115` 的 `fingerprint_of()`（SHA-256[:16]）按命名拆分应改为 `fp8_hex()` + `fingerprint_of` 别名；该文件不在本棒允许修改清单内，留给 SDK/客户端棒处理。
4. `core/vade/RESULT.md:112` 仍写「SPKI pin 未实现」，已过时，由维护该文档的棒更新。
