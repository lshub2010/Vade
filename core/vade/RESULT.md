# C2 · 传输层 + 加密网关 + 注册表 · RESULT

| 项 | 值 |
|---|---|
| 分支 | `tmp/c2-registry`（基线 `dev_gingshan` 51d6b61） |
| 契约 | C-03 / C-04 / C-05 / C-18 已冻结 |
| 测试 | `pytest core/tests -q` → **56 passed**（0 failed，0 skipped） |
| 门禁 | `python3 tools/check_contracts.py` → `contracts: OK` |
| 未 push | 是（按任务要求） |

## 一、changed（文件 + md5）

| 文件 | md5 |
|---|---|
| `schema/manifest.schema.json` | `3bd69cdf970b7354d63e35a6da583d87` |
| `schema/errors.json` | `7ca6178fa0de6f0e1af804bda2245e95` |
| `schema/rpc.json` | `be8a5310497bff8ee7a6ff44db84837b` |
| `schema/config.schema.json` | `629cd5619c0a180a5eeec7888cbf6839` |
| `core/vade/__init__.py` | `6f34cc51fb935ad5be00cfac49c161b8` |
| `core/vade/_jsonschema.py` | `43e7da434757761e0f4ca7800a13c457` |
| `core/vade/errors.py` | `07e12022e366dc9ac62fffc83c9aac38` |
| `core/vade/config.py` | `6bfaf5746a06197e14b3182009579c00` |
| `core/vade/logging.py` | `42fafb043d0504f34f1a7c5fb0a79d59` |
| `core/vade/slots.py` | `9f979a3e5246f25fdf325062e81d9ce4` |
| `core/vade/app.py` | `262915f0d4c809b35738cb85d111ba7a` |
| `core/vade/__main__.py` | `e5f93e3b083f48faf091e2814cb04f87` |
| `core/vade/transport/__init__.py` | `3d822b176b28b9d4f7da2e62285dcc27` |
| `core/vade/transport/framing.py` | `6edd116ff2ab42475c61a16e855f0e53` |
| `core/vade/transport/connection.py` | `e9704033ada186cdbc48590d9f5593eb` |
| `core/vade/transport/server.py` | `b417802910b028815c3d3705b7e56714` |
| `core/vade/crypto_gateway/__init__.py` | `12dea93656a6bb10ce5a9764a0c74b17` |
| `core/vade/crypto_gateway/session.py` | `cd1f1f1c22d6fdfe8fdfc0ab6385a865` |
| `core/vade/crypto_gateway/errors.py` | `8c89fdd40efde587cc6b009c02998953` |
| `core/vade/registry/__init__.py` | `8b5c599f579c25b5902c063c28ea0b79` |
| `core/vade/registry/manifest.py` | `8b8c8a7610de26b422387d63b03058f7` |
| `core/vade/registry/table.py` | `f90ba3708deb515c88ec98468235e371` |
| `core/vade/registry/health.py` | `d8665aefaefb2b735d11eda241dc246c` |
| `core/vade/registry/api.py` | `51bbf1ba06cd140eb785845577c59033` |
| `core/vade/identity/__init__.py` | `1153bcf9b4da14ada9a5dac691c3e819` |
| `core/vade/identity/store.py` | `f965256ff0b40fcfa3dd55ed03c8be9b` |
| `core/vade/identity/whitelist.py` | `9a32a938e9f1208dd47d46ae647c1478` |
| `core/vade/identity/pairing.py` | `f5e5943041065a8457430e17b7dfcfff` |
| `core/vade/identity/tls.py` | `098892b98e6d8d0cc958002f450c0905` |
| `core/tests/conftest.py` | `42d799e95e4388d3e6d303d06f28dc2e` |
| `core/tests/registry/test_manifest.py` | `ed38ff8e918ba94788083f2aecc930da` |
| `core/tests/registry/test_table.py` | `38718fe767112f447813e8e0151cefee` |
| `core/tests/transport/test_framing.py` | `10a66d172c548d540c16c22202dca662` |
| `core/tests/test_config.py` | `d2049760f9927cfa5f62cccd8b47873a` |
| `tests/fixtures/manifests/good.json` | `1d500fbd41f77bbcde0601399e9b1e57` |
| `tests/fixtures/manifests/bad-module-id.json` | `68333d94c5b6d544ddeac011b6e16de3` |
| `tests/fixtures/manifests/bad-protocol.json` | `70b315850ec9ed1ee3170a8cd131f511` |
| `tests/fixtures/manifests/bad-tool-name.json` | `7329f95917ddacd13b102a0c0e2fb30d` |
| `tests/fixtures/manifests/bad-input-schema.json` | `ba4311d82d98604a40982e0d2ab4f78e` |
| `tests/fixtures/manifests/bad-risk.json` | `199bd9329f123621433acdb53451a244` |
| `tests/fixtures/manifests/too-many-tools.json` | `1a188c7e7610f122fe8e0edd85308a47` |
| `tools/check_contracts.py` | `5aec0d36b524ae36b3fffb20fe8b6b80` |

schema 冻结凭据（sha256，`sha256sum schema/*.json` 实测）：

```
d18eb72adb4bcc692b6ee6065b7a9ea03b4abc9e4f10f82abd5dbcc733672edd  config.schema.json
0eb0d020d0c011599647c0613b6a580a48664cf5e19e761668016c4be75250fa  errors.json
c453b0dfaaedaad1c84b8e9bbcbc6d7f39b8b3b47bfb212e4d9929ce37a1bdc2  manifest.schema.json
0ab17cd6e4531d94a6fad59c5d4372872726021c972e45b31782eb018f1319ee  rpc.json
```

四份 C2 契约均已由 `tools/check_contracts.py` 校验 id/版本/默认值/所有者齐备。

## 二、red_evidence（六类非法 manifest 的真实报错）

```
bad-input-schema.json : MANIFEST_INVALID: /capabilities/tools/0/input_schema/type: must equal 'object', got 'string'
bad-module-id.json    : MANIFEST_INVALID: /module_id: value 'Com.Example.FS' does not match '^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$'
bad-protocol.json     : MANIFEST_INVALID: /protocol: unsupported protocol 'vade/v2'; core supports vade/v1 (major 1 only, no downgrade)
bad-risk.json         : MANIFEST_INVALID: /capabilities/tools/0/risk: must be one of ['low', 'medium', 'high'], got 'critical'
bad-tool-name.json    : MANIFEST_INVALID: /capabilities/tools/0/name: value 'read-file' does not match '^[a-z][a-z0-9_]{0,63}$'
too-many-tools.json   : MANIFEST_INVALID: /capabilities/tools: has 65 items, maxItems 64
```

工具名冲突（注册表层，拒绝后注册者）：

```
TOOL_CONFLICT: /capabilities/tools/0/name: tool 'read_file' already held by com.example.fs
```

## 三、full_suite_tests_run

```
$ /root/HermesWorkspace/Vade/.venv/bin/python -m pytest core/tests -q
56 passed in 0.66s
```

覆盖：
- manifest 6 类非法 + 未知字段忽略 + injects + 不可变 dataclass（10 项）
- registry：exclusive 第二实例拒、pooled 三实例全登记且索引 3 条、超 max_concurrency 拒、跨模块同名拒后注册者、异指纹不能顶替、capabilities.update 原子替换 + epoch、非法 update 保留旧能力、risk 只能上调、revoke 关闭活连接并标 rejected、健康降级但记录保留
- transport：version 错误在 AEAD 之前拒（解密调用计数为 0）、65519 边界、65520 拒、reserved/未知 type fatal、声明长度不符、重放不二次解密、gen 回退/跳代 fatal、rekey 后 counter 归位、握手帧封装、payload_len 自定界
- 真实 Noise：IK 握手 + transport 往返 + 重放 → `PERMISSION_DENIED` 且会话关闭；未登记公钥 → `PERMISSION_DENIED`；IK 缺 remote_pub 不回退 XX
- config：schema 默认值、hot/restart/owner、未知键 UA 路径、CLI>env>toml、overrides、未知 toml 键、字面量密钥拒、模型 env 名允许、ui_origins host 校验、`*` 禁止、越界值拒

## 四、与任务书的偏差（需裁决）

1. **握手模式不由 `flags` 位判定**：C-01 冻结 `flags=0` 且非零即 `PROTOCOL_MISMATCH`，两者冲突。实现改为「响应方按候选静态公钥/配对码选择 IK 或 XXpsk3」；`flags` 位留给未来主版本。
2. **`max_payload` 上限 65519**：按 C1 的 E2 约束，schema 直接拒绝 >65519 的申报值，而不是承诺 1 MiB 单帧。
3. **配对码哈希用 scrypt 而非 argon2id**：环境无 `argon2-cffi`；记录中带 `algo` 前缀，后续换算法不改存储格式。
4. **汉字表为占位表**：C-15 的 `hanzi_table` 仍为 `null`；生成用内置 256 字占位表，消费只校验形状（与 `protocol/core/src/paircode.rs` 一致）。

## 五、unverified（如实列出，未验证即未验证）

1. **无真实监听器**：环境无 `uvicorn`/`starlette`，`CoreApp` 的 `/healthz`、`/ui/session`、`/ws` 只做了代码路径，未跑过真实 HTTP/WS 握手。
2. **`tests/fixtures/fake_module.py`（T5）未交付**：优先级 1–3 用尽预算。
3. **IK 响应方需要候选公钥**：Rust 绑定 `FfiHandshake` 在 IK 下强制 `remote_pub`（两角色都要求）；而白名单目前只存指纹。`make_crypto` 因此无法在未拿到候选公钥时构造会话——需白名单增存公钥并逐个尝试。
4. **自签 TLS 未验证**：无 `cryptography`；`--print-tls-fingerprint` 目前是证书 DER 的 SHA-256，**不是 SPKI**，与 `--print-fingerprint`（core 身份 fp8）确实分开，但 SPKI pin 未实现。
5. **`browser_session` 未测**：cookie 签发/校验已写，吊销只标记 `revoked` 并返回连接 id，**没有真正关闭 WS**；`/ui/session` 的 CORS 未跑过真实请求。
6. **rekey 调度未接**：只有 `CryptoSession.maybe_rekey()`，没有 24h/1GiB/2^20 阈值的定时器，也没有 24h 重握手。
7. **组件自动发现未测**：`session`/`policy`/`audit` 尚未存在，`strict=True` 拒绝启动路径未被测试覆盖。
8. **`LoopLagMonitor` 未接线**：只提供 p95 与 `should_offload`，seal/open 仍一律内联。
9. **`RegistryAPI.set_concurrency_override` 只记录**，不改变已注册实例的并发行为。
10. **`tools/check_contracts.py` 的 Rust 枚举比对是正则近似**，当前通过，但没做 `cargo test` 级一致性证明。
11. **性能**：未测 seal/open 单帧耗时（任务书要求注释与交付说明；注释已写，数字未测）。

## 六、给下游的接口

- `module/register` → `{accepted, session_id, tool_names[]}`；拒绝 `MANIFEST_INVALID` + 指针。
- `module/capabilities.update` → 原子替换，`cap_epoch += 1`；失败保留旧能力；降 risk → `RISK_DOWNGRADE_FORBIDDEN`。
- 能力索引 key 为 `module_id/tool@instance_id`（pooled 三实例 = 三条 binding，`tool_count()` 按 `module_id/tool` 去重）。
- `Registry(close_connection=...)` 是治理写接口唯一的连接副作用入口；C5a 必须经它停用/吊销。
- `App` 上的四个扩展位（`prompt_hooks` / `tool_failure_hooks` / `on_session_end` / `approval_port`）已由 `slots.install_slots` 建好。


---

## 修复轮（第二轮，2026-11-13）

审查报告 `/root/HermesWorkspace/Vade-REVIEW-C2.md` 指出 3 个阻塞 + P1–P3。本轮全部修复：

| 项 | 修复 | 测试 |
|---|---|---|
| 阻塞 A · IK 无 remote_pub | 白名单存 `public_key`，`CandidateKeySession` 逐候选重试；`build_module_crypto` 真正接线 | `test_handshake_wiring.py` 4 条真握手 |
| 阻塞 B · 关连接空转 | `Connection.request_close()` 跨线程调度；修 `transport.install` 早退 | `test_close.py` 3 条 |
| 阻塞 C · 缺夹具/真实监听 | `tests/fixtures/fake_module.py` + uvicorn 真端口 + httpx/websockets | `test_live_server.py` 5 条 |
| P1 配对码约束 5 | `consume(..., fingerprint=)`，按模块指纹判定 | 3 条 |
| P2 `note_failure` 死代码 | 失败走 `_note_failure_row`，5 次销毁 | 4 条 |
| P2 XFF 绕过 | `trust_forwarded_for=False` 默认；uvicorn `proxy_headers=False` | 伪造 4 个 XFF 仍 429 |
| P2 rejected 快照崩 | `manifest: Manifest \| None`，`tool_count=0` | 1 条 |
| P3 索引误伤 / 降级键错 | `_rebuild_index()` + `{module}/{name}@{instance}` | 2 条 |
| 汉字表 | 去重补到 **256 unique**，`test_hanzi_table_is_256_unique` 守门 | 通过 |

**额外发现（审查未列，最严重）**：`pairing._verify()` 的盐解析判断写反（`salt_hex.startswith("{")` 直接 return False），
导致**任何配对码都永远无法消费**——`/ui/session` 全链路此前是死的。已修，附 4 种写法归一化测试。

### 实测

```
pytest core/tests -q          → 84 passed, 4 warnings
tools/check_contracts.py      → contracts: OK (C-03/C-04/C-05/C-18, 33 codes)
```

### 已知限制（本轮未验证）

- TLS / `wss://` 未跑（`cryptography` 未装）
- XXpsk3 首次配对未跑
- `fake_module.py` 未作为独立子进程 CLI 跑
- `trust_forwarded_for` 只是构造参数，未加 config 键 → 反代部署暂时无法从 `core.toml` 开启
