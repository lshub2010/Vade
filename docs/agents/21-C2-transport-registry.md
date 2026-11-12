# C2 · 传输层 + 加密网关 + 注册表

| 项 | 值 |
|---|---|
| Agent 代号 | `c2-registry` |
| 波次 | W2 |
| 依赖 | C1 的 Python wheel、`protocol/v1/frames.md`、KAT |
| 目录所有权 | `core/vade/transport/`、`crypto_gateway/`、`registry/`、`identity/`、`schema/`、`core/vade/{__main__,app,config,logging}.py` |
| 产出 | 可接受真实连接的 core 接入层 + 假模块夹具 |
| 出口 | 假模块能注册并调用；6 类非法 manifest 全部被拒且报错指向字段 |

## 一、一句话任务

让一个陌生进程能通过 WSS + Noise 连上 core、证明自己是谁、交一份 manifest，然后被 core 记住；同时让任何不合规的 manifest 在门口就被挡住。

## 二、边界

| 做 | 不做 |
|---|---|
| WS 接入、TLS、连接生命周期、心跳、背压 | 不做工具路由（C3） |
| 调 Rust 完成握手与加解密 | 不在 Python 里实现任何密码学 |
| manifest 校验、实例表、能力索引、健康度 | 不做审批、不做审计（C5a） |
| 配对码生成与消费、白名单、吊销 | 不做 admin 页面（C5a/C5b） |
| 冻结 `schema/manifest.schema.json` / `errors.json` / `rpc.json` | 不改 `protocol/`（C1 所有） |

## 三、必读

- `docs/design.md` §2.1–§2.5、§3（鉴权三模式、配对流程）、§7.3（状态判定）
- `docs/requirements.md` FR-2 / FR-3 / FR-4 / FR-6、A4（三态并发）、A7
- `docs/agents/01-contracts.md` 全节
- `protocol/v1/frames.md`（C1 产出）

## 四、已裁决的技术冲突

| 冲突 | 裁决 |
|---|---|
| 方案 §3 写「Ed25519 静态密钥对」，但 Noise_25519 需要 X25519 | **统一用 X25519 静态密钥**，不引入第二套密钥。双向认证由 Noise IK 完成（core 用白名单里的模块公钥解 msg1；模块用配对时拿到的 core 公钥验 msg2），不额外做签名。指纹 = `SHA-256(pub)[:16]` 的小写 hex。 |
| 需求 FR-3.3 原写「方向 4B + 计数器 8B 进 nonce」（AMEND-10 已回写） | 方向隔离改由 **Split 双密钥**承担，nonce 由 Noise 传输态维护，帧头 `counter` 只做重放检测。因此**接收端必须显式校验 `counter` 单调递增**——不校验，重放检测等于没有。 |
| 需求 FR-3.2 原写 `XChaCha20-Poly1305`（AMEND-10 已回写） | Noise 参数集只支持 96-bit nonce 的 `ChaChaPoly`，按 `ChaCha20-Poly1305` 实现；不要为了「和文档一致」去引 XChaCha。 |

## 五、任务分解

### T0 · 进程骨架与配置（0.5 天）

```
core/vade/
├── __main__.py     # argparse：--config / --bind / --admin-bind / --admin-allow-cidr / --print-fingerprint / --san；另有 admin subparser（见下）
├── app.py          # 生命周期：startup / shutdown / 组件装配
├── config.py       # core.toml + overrides.json + 环境变量 + 默认值，优先级：CLI > env > overrides > toml > 默认（C-18）
├── slots.py        # 【W2 只建空壳】四个扩展槽占位，W3 由 C3 填实现（所有权归 C3）
└── logging.py      # JSON lines，字段见 01-contracts §六
```

> **`admin` subparser（v1.18 新增）**：`__main__.py` 必须预置一个 `admin` 子解析器，`set_defaults(func=vade.admin.cli:main)`，把 `python -m vade admin <cmd>` 转发给 C5a 的 `core/vade/admin/cli.py`。C2 只负责这个入口存在并可被 import（C5a 未提交前，`import` 失败应给出「admin 模块未安装」而非崩栈）。`--print-setup-token` **不归 C2**（token 生命周期全归 C5a 的 `admin print-setup-token`），不要在这里实现。
>
> **`slots.py` 空壳（v1.18 新增）**：C2 在 W2 建 `core/vade/slots.py`，内容是四个槽的空列表 + `def install_slots(app): return app` 占位；这样 `app.py` 的 `from .slots import install_slots` 在 W2 就能跑，不必等 W3 的 C3。

**组件装配必须自动发现，不能硬编码名字表**——硬编码会漏批次，而且漏了还不报错：

```python
# app.py
INFRA = {"transport", "crypto_gateway", "registry", "identity"}

def install_components(app, strict: bool):
    """遍历 vade/ 下的所有子包，有 install(app) 就调用。按名字排序保证确定性。"""
    present = set()
    for m in sorted(pkgutil.iter_modules([str(Path(__file__).parent)]), key=lambda x: x.name):
        name = m.name
        if name.startswith("_") or name in INFRA:
            continue
        mod = importlib.import_module(f"vade.{name}")
        install = getattr(mod, "install", None)
        if callable(install):
            install(app)
            present.add(name)

    required = {"session", "policy", "audit"}
    missing = required - present
    if missing and strict:
        raise SystemExit(f"missing core components: {sorted(missing)}")
    if missing:
        log.warning("running WITHOUT %s — dev mode only", sorted(missing))
```

**组件的接入契约（所有批次必须遵守）**：包根 `__init__.py` 暴露 `def install(app) -> None`，不要在包里再放一个 `install.py`。

- 这条必须先定，否则两侧对不上：C3 在 `session/__init__.py` 写、C5a 在 `policy/__init__.py` 与 `audit/__init__.py` 写、C3b 在 `skill/__init__.py` 写、C3d 在 `events/__init__.py` 写。
- **不要用「有没有 `install.py` 文件」来判断组件是否存在**——那会让 `session`/`policy`/`audit` 在生产模式恒被判缺失，直接拒绝启动。这是第一版修复引入的错误，已改正。
- `install` 必须幂等。
- **禁止用固定的四元组**（`session/policy/audit/admin`）。C3b 的 `skill`、C3d 的 `events` 在 W4 才加入，硬编码会让它们的 `install(app)` 永不执行，而 `except ImportError: continue` 会把这件事静默吞掉——审查就是在这里抓到「C3b/C3d 等于白做」。
- `strict=True` 由 `[dev] allow_missing_components` 的反面决定：**生产缺组件拒绝启动**，不再 fail-open。
- 这串名字里**不允许出现任何业务模块**（`fs` / `shell` / …）；业务模块只走注册表。
- `--print-fingerprint` **只输出 core 身份指纹 `fp8`**（`SHA-256(core_identity_pub)[:16]`），与 setup 页、二维码 `fp` 同一个值。TLS 证书指纹是另一个命令 `--print-tls-fingerprint`（`SHA-256(SPKI)[:16]`）。**不要把两个值混在一个输出里**——用户拿它跟二维码对照时会对不上。
- 配置里的密钥只接受「环境变量名」，不接受字面量；`core.toml` 出现 `api_key = "sk-..."` 直接拒绝启动。

### 装配点总表（`app` 上的扩展位，谁 append、谁消费）

| 扩展位 | 生产者（append） | 消费者（遍历调用） | 用途 |
|---|---|---|---|
| `app.prompt_hooks` | C3b | C3 的 `assemble` | 填充 skill 段落、回填 `activated_skills` |
| `app.tool_failure_hooks` | C3b | C3 的 router | 工具失败 → 累加 skill 的 `fail_count` |
| `app.on_session_end` | C5a、C3d | C3 的 T6 会话结束路径 | 清审批放行、发 `session.closed` |
| `app.approval_port` | C5a | C3 的 `approval/decide` 分发 | 审批判定 |

**这张表是 C3 的交付物**：`app` 上必须先有这四个属性（默认空列表 / `None`），否则 C3b/C3d/C5a 的 `install` 会 `AttributeError`。C3 在 T0 就建好，不要等别人来加。

### T1 · identity 与 TLS（1 天）

```
core/vade/identity/
├── store.py        # 加载/生成 X25519 静态密钥
└── tls.py          # 自签 CA + 服务端证书 + 指纹
```

- 私钥文件 `data/core.key`，`0600`，内容为 32 字节原始种子；**目录权限 0700**。
- 首次启动生成 CA + 服务端证书，SAN 覆盖 `localhost`、`127.0.0.1`、`--san` 追加的主机名/IP；证书与 CA 存 `data/tls/`。
- 启动时把 **CA 指纹与 core 公钥指纹**打印到 stdout（只打印一次也可，但必须可重复查看：`python -m vade --print-fingerprint`）。
- 私钥对象不得有可读 `repr`；日志里出现 `[redacted]`。
- `identity/pairing.py`：配对码的唯一实现——`create(purpose, module_id=None, expected_fp=None, ttl=180)`、`consume(code, purpose)`、`revoke(code)`；只存 `argon2id(normalize(code))`，明文只在创建时返回一次。C5a 的邀请位与浏览器 `/ui/session` 都调用它，**不要各写一套**。

  **五个必须加的约束**（缺一个配对就能被滥用）：

  | 约束 | 为什么 |
  |---|---|
  | `purpose ∈ {module, browser}`，消费时必须匹配 | 同一个码既能配模块又能换浏览器 cookie，泄露一次等于拿到 30 天会话 |
  | `consume` 失败计数：同一码错 5 次即作废；每 IP 5 次/5 分钟 | 限流挡在线爆破；但真正的威胁是窥视后再消费，所以还要熵足够 |
  | 码长 **8 位中英混合**：2 汉字（256 字固定表）+ 6 位 base32 `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`，显示 `青松-A7K2M9` | `256² × 32⁶ ≈ 2^46`，远高于原 6 位数字的 `2^20`；汉字给口述一个锚点，降低「念错一位」的概率（AMEND-12/A43） |
  | **消费即删**：`DELETE ... WHERE code_hash=? AND purpose=?` 用 `rowcount` 判定 | 「标记 used」在并发下会被消费两次；`rowcount=0` 即失败，天然幂等 |
  | `module_id` 非空时，**已有同 module_id 且指纹不同 → 拒绝**，必须管理员先 revoke | 否则拿到一个码就能顶掉已受信模块 |

  归一化 `normalize(s)`：`NFKC` → 去 `-` `/` 空格 `·` → 字母段 `upper()`；汉字段原样。**HKDF 与 argon2 都必须用归一化后的串**，两边实现不一致就握手失败。`LANG=C` 或 stdin 非 UTF-8 时，`--print-pair-code` 额外输出一张 ASCII 二维码，不要把乱码糊到终端上。

- **XX 配对成功后的写入路径必须实现**（审查抓到的空洞）：`on_pairing_success(remote_static, module_id, instance_id)` → 把指纹写入 `identity/whitelist`（`(module_id, instance_id) → fp`）→ 落盘 0600 → 审计 `pairing_accepted`。不写这一步，配对只能走内存，重启即失，而「配置页删指纹」也无处可删。
- `identity/whitelist.revoke(fp)` 必须**关闭匹配该指纹的现有连接**，不只是拒绝下次握手。

### T2 · transport（1.5 天）

```
core/vade/transport/
├── server.py       # WSS 监听、连接接入
├── connection.py   # 单连接状态机
└── framing.py      # 调 crypto_gateway 的 seal/open，写帧
```

规则：

| 项 | 值 |
|---|---|
| 监听 | 默认 `127.0.0.1:8443`；`--bind 0.0.0.0` 才对外 |
| 数据面端口 | 用 ASGI（uvicorn + starlette）**只**承载 HTTPS（`/ui/session`、`/healthz`）与 WS 升级。**管理面不挂这里**——C5a 自己监听 `127.0.0.1:8444`（AMEND-12/A42）。不要为「省一个端口」把 admin 塞进数据面：`/app` 里跑的是模块代码，同源等于把管理面交给第三方 JS |
| 管理面与数据面的关系 | 两个独立 listener、两张独立证书配置（可共用同一张，但必须各自显式绑定）。C2 不 import `vade.admin`，C5a 不 import `transport` |
| `/ui/session` 的 CORS | 只对 `[server] ui_origins` 精确白名单回 `Access-Control-Allow-Origin` + `Allow-Credentials: true`，**禁 `*`**；启动时校验 `ui_origins` 的 host ∈ `[server] listen_hosts`（默认 `["127.0.0.1", "localhost"]`），否则拒绝启动——`SameSite=Strict` 在 cross-site 下不发送，会变成「能登录但请求全 401」的静默故障 |
| 双 host 监听 | 数据面一个 listener、两个 DNS 名（`127.0.0.1` 与 `localhost`）。作用：让 webui 用 `localhost:8080` 时 `ac_ui` 落在 `localhost` 域，与管理面的 `__Host-ac_admin`（`127.0.0.1` 域）host-only 隔离。**不要为这两个 host 起两个 listener** |
| 来源限制 | 数据面 `--bind 0.0.0.0` 时允许 LAN 连 `/ui/session`；管理面的来源限制**不归 C2 管**，由 C5a 自己的 listener 保证 |
| `--admin-allow-cidr` 的定位 | v1.12 起它**不是对外开关**，只是可选的收窄手段（白名单）。对外开关是 `--admin-bind`；允许对外后靠「密码 + 公钥」（已初始化）或「setup token」（未初始化）把住。两者叠加时取交集 |
| 握手超时 | 5s，超时关连接（防半开连接占坑） |
| 心跳 | 服务端每 10s 发 ping 帧；`2×interval` 无任何帧 → 判 offline 并关连接 |
| 背压 | 每连接发送队列上限 256 帧；满则关连接（code 1013），不无限缓冲 |
| 并发写 | **单 writer task + asyncio.Queue**，禁止多协程直接 `ws.send`（会交错帧） |
| **单 reader** | **单 reader task 串行调用 `open()`**。Noise 的 `TransportState` 不是并发安全的，两个协程同时解密会错乱 nonce 顺序，表现为「随机丢帧」——极难查。Rust 侧用 `&mut` 保证，Python 侧用单 task + 有界队列保证 |
| `/healthz` | 无鉴权 `GET /healthz → {ok, version, policy_mode}`；**不含任何配置、指纹、模块信息** |
| 帧上限 | 1 MiB 明文；超限直接关连接，不读入内存 |
| 关闭 | 先 `module/drain` 语义（停止路由新调用）→ 等在途 → close |

事件循环保护：seal/open 单条 < 2ms，**允许内联**；但必须加一个 loop lag 监控，P95 > 5ms 时改成 `asyncio.to_thread`。这条要写进代码注释和交付说明。

### T3 · crypto_gateway（1 天）

```
core/vade/crypto_gateway/
├── session.py      # 包装 protocol-core 的 Handshake / Transport
└── errors.py       # Rust error -> schema/errors.json
```

- 状态机：`NEW → HANDSHAKING → ESTABLISHED → DRAINING → CLOSED`。
- `NEW` 只接受 `type=0x01` 握手帧；收到 rpc/event 一律关连接（不允许先发业务后握手）。
- 握手模式判定：连接首帧带 `flags` 位标识 `IK` / `XX`；**IK 失败不得回退 XX**。
- **`browser_session` 通道**（设计 §3 的第三种鉴权，别漏）：
  - `POST /ui/session {pair_code, client_name}` → 校验配对码（与设备配对同一套，180s、一次性）→ 发 `__Host-ac_ui` cookie：`Secure + HttpOnly + SameSite=Strict + Path=/`（**不带 Domain**），30 天有效；返回 `device_id`。码先过 `normalize()`（NFKC / 去分隔符 / 字母 upper）再比对，否则全角输入会失败。
  - WS 升级时校验 `__Host-ac_ui`；有效则**跳过 Noise**，帧为纯文本 JSON-RPC（没有二进制帧头），身份登记为 `ui` 模块实例，`auth_mode=browser_session`，`concurrency=mirrored`。
  - 这条通道的信任边界**止于 TLS**，审计里必须标 `e2e=false`；不要假装它受 Noise 保护。
  - 吊销该 `device_id` → 立即关闭其 WS，并让 cookie 失效（服务端会话表删除）。
  - 配对码尝试限流：每 IP 5 次/5 分钟，失败写审计。
- 握手完成后把 `remote_static` 交给 registry 比对白名单；不匹配 → `rejected` 状态 + 审计一条 `handshake_rejected`。
- **rekey（FR-3.6 / 3.12）**：**按方向独立**维护 `gen` 与 `counter`（A→B、B→A 各一套）；达到 `24h / 1GiB / 2^20 帧` 任一阈值时，由**发送方**发 `type=0x06` 帧（帧体 `{new_gen}`，用**旧 key** 加密），自己只把**发送方向** `k` 换成 `HKDF(k,"rekey")`、counter 归零、该方向 `gen += 1`。
  - 接收方处理顺序：先用当前 key 解开 `0x06` → 校验 `new_gen`：`== 接收方向 gen + 1` 则 rekey 接收方向；`<= 当前 gen` 视为重复/过时，**忽略但不断连**；`> gen + 1` 断连。
  - 不能写成「双方各自 `rekey()` 两把都换、共用一个 `gen`」：双方同时到 24h 时会互发 `0x06{2}`，各自先把自己的 gen 加到 2，再收到对方的 `{2}` 就变成 `2 != 3`，直接断连。这是默认路径，不是边角。
  - `rekey` 帧解不开或 gen 不连续 → **立即断连**，绝不回退旧 key 继续通信（回退等于给降级攻击开门）。
  - `browser_session` 通道没有 Noise，**不参与 rekey**；这条路径本来就只有 TLS。
  - 24h 另起一次**重握手**刷新前向保密，rekey 不承担这个职责。
- 所有 `fatal` 错误（重放、认证失败）必须立即断连，不允许"忽略这一帧继续跑"。

### T4 · registry（2 天）

```
core/vade/registry/
├── manifest.py     # 校验（唯一实现）
├── table.py        # 实例表 + 能力索引
├── health.py       # 五态判定
└── api.py          # 供 C3/C5a 调用的只读查询面
```

**manifest 校验（顺序固定，遇错即返回，一次只报第一个错）**：

| # | 校验 | 错误信息必须含 |
|---|---|---|
| 1 | JSON Schema 合法（`schema/manifest.schema.json`） | JSON Pointer 路径，如 `/capabilities/tools/0/name` |
| 2 | `protocol` 主版本 = 1 | 对端版本与支持版本 |
| 3 | 工具名正则、长度 ≤ 64、description 非空 ≤ 512 | 字段值与规则 |
| 4 | `input_schema.type == "object"` 且是合法 schema | 实际 type |
| 5 | risk ∈ {low, medium, high}；timeout_ms ∈ [100, 600000] | 实际值 |
| 6 | 工具总数 ≤ 64，全局 ≤ 128；`module_id/tool` 唯一 | 冲突的工具 ID 与持有者 |

前向兼容：**未知字段忽略**；未知 capability 类型忽略并打 warning（FR-2.3）。

**实例表**：

| concurrency | 注册行为 | 路由行为 |
|---|---|---|
| `exclusive` | 同 module 第二实例 → `TOOL_CONFLICT` | 只此一个 |
| `pooled` | 允许 N 个（N ≤ max_concurrency） | 最少在途优先（C3 用） |
| `mirrored` | 允许 N 个 | 不参与负载，事件广播（C3/C5a 用） |

**健康五态**（`docs/design.md` §7.3 为准）：

- `online`：握手完成 + 最近 2×heartbeat 内有帧；
- `degraded`：连接在，但熔断打开或超时率 > 20%（熔断器在 C3，registry 只读取其状态）；
- `offline`：已配对但无连接；
- `rejected`：握手/白名单/版本失败；
- `pending`：邀请位（C5b 创建，registry 只存记录）。

`module/register` 返回 `{accepted, session_id, tool_names[]}`；拒绝返回 `MANIFEST_INVALID` + 可读原因，**不泄露其他模块的信息**。

**治理写接口**（C5a 的 L2 操作必须有落点，否则「停用」只改 UI 不改运行态）：

```python
# registry/api.py —— 与只读查询面分开，全部要求 admin 身份
async def set_enabled(module_id, instance_id, enabled: bool) -> None:
    """停用 = 停止路由 + 发 module/drain + 关连接 + 清订阅 + 清审批缓存。不是只从工具表移除。"""

async def set_concurrency_override(module_id, mode: Literal["exclusive","pooled","mirrored"]) -> None:
async def set_risk_override(module_id, tool, risk: Literal["low","medium","high"]) -> None:
    """只能上调。请求下调 → `RISK_DOWNGRADE_FORBIDDEN`；要下调必须改 core.toml 并重启。"""

async def disconnect(module_id, instance_id) -> None:
async def snapshot_for_admin() -> list[NodeView]:   # 含 endpoint / rtt / tool_count / kind / auth_mode
```

- `kind` 与 `auth_mode` 由**握手时**的 `role`、认证方式决定，不由模块自述：`role=ui` + `browser_session` → `kind=device/auth_mode=browser_session`；`role=ui` + Noise → `device`；`role=tool|evolve` → `module`。
- `snapshot_for_admin` 是 C5a 组装 `node.upsert` 的唯一数据源；C3 的 `module.status` 只作**触发信号**（告诉 admin「该重拉快照了」），不承载渲染字段。

**动态能力更新**（FR-2.13 / D27）：
- `module/capabilities.update` 全量提交，重走 T4 全部校验；
- 合法 → **原子替换**能力索引（构建新表再 swap 引用），`cap_epoch += 1`；
- 非法 → 保留旧 manifest，返回 `MANIFEST_INVALID`，旧能力继续可用；
- **risk 只能升不能降**：更新里任一工具的 risk 低于当前生效值 → 整份拒绝（不是只拒那一条），否则模块可以「先注册成 high 过审、再更新成 low 绕过审批」。新增工具一律先按 `high`，待管理员降级；
- 更新成功要**清空该模块所有会话的审批缓存**并广播既有事件 `module.status`（不新增事件名，C5a 收到后重拉快照）；
- C3 的 agent-loop 每轮开始时取一次快照，因此「下一轮生效」是自然结果。

### T5 · 假模块夹具（0.5 天）

`tests/fixtures/fake_module.py`：用 C1 的 Python wheel 连接、握手、注册一份可配置 manifest、响应 `tools/call`。必须支持：

```bash
python tests/fixtures/fake_module.py --core wss://localhost:8443 \
  --manifest tests/fixtures/manifests/good.json [--pair-code '青松-A7K2M9']
```

用于 C2/C3/C5a 的集成测试，**不要**把它做成正式模块（正式模块是 M 组）。

### T6 · schema 冻结（0.5 天）

- `schema/manifest.schema.json`：JSON Schema 2020-12，`additionalProperties: true`（前向兼容），必填项齐全。
- `schema/errors.json`：照 `01-contracts.md` §二，一字不差。
- `schema/rpc.json`：方法、参数、返回、错误码的机器可读表（供 C4 生成 SDK 常量、U1 生成 Kotlin 枚举）。
- **`schema/config.schema.json`（C-18，AMEND-19）**：`core.toml` 全量键、默认值、`hot|restart` 生效方式、所有者。这是 5 个模块共用的唯一默认值来源，**必须先冻结**（W2 中），否则 C3/C3b/M4a/M4b 会各自在 `config.py` 里拍数字。
  - 加载规则：未知键 → 拒绝启动（`UNKNOWN_CONFIG_KEY`），不是 warning。
  - 合并顺序：`CLI > env > data/config.overrides.json > core.toml > schema 默认值`。
  - `overrides.json` 由管理页写入，C2 只负责读取与校验；`config.changed` 审计载荷也定义在这个 schema 里。
  - **必须覆盖的段**（v1.18 补，此前遗漏导致「未知键拒绝启动」与正文矛盾）：`[core]` / `[tls]` / `[policy]` / `[audit]` / `[memory]` / `[evolve]` / `[compaction]` / `[skills]`，**另加** `[policy.tool_risk]`（FR-7.12 risk 裁决表）、`[server]`（`ui_origins` / `listen_hosts`，AMEND-12 CORS 与 host 校验）、`[dev]`（`allow_all`，fail-closed 开关）三组。定义见方案 §7.9.1。
- **`tools/check_contracts.py`（合并门禁第 2 条的工具，v1.18 明确归 C2）**：校验 `schema/*.json` 与代码生成的 manifest 一致、契约版本号与 `01-contracts.md` 登记一致。C5a 补 admin schema 的校验分支。没有这个脚本，门禁第 2 条永远红。

## 六、验收

```bash
pytest core/tests/transport core/tests/registry -q          # 全绿

# 正常注册
python tests/fixtures/fake_module.py --core wss://localhost:8443 \
  --manifest tests/fixtures/manifests/good.json &
sleep 2 && curl -sk https://localhost:8443/healthz           # ok

# 六类非法 manifest 必须逐条被拒，且 message 指向字段
for f in tests/fixtures/manifests/bad-*.json; do
  python tests/fixtures/fake_module.py --core wss://localhost:8443 --manifest $f \
    | grep -E "MANIFEST_INVALID.*(/capabilities|protocol|tools)"
done

# 重放：重发上一帧 → 连接被关
pytest core/tests/transport/test_replay.py -q

# 私钥不进日志
grep -rE "core.key|BEGIN .*PRIVATE" core/vade/ && exit 1 || true
```

必须有的集成测试：
1. TLS 校验失败（错误指纹）→ 连接被拒，错误可读。
2. IK 用未登记公钥 → `rejected`，registry 里出现该记录（供拓扑显示）。
3. `exclusive` 第二实例 → 拒。
4. `pooled` 三实例 → 全部登记，能力索引 3 条。
5. `capabilities.update` 非法 → 旧工具仍可调用（回归）。
6. 心跳超时 → `offline`，但记录保留（拓扑仍显示灰虚线）。
7. `browser_session`：正确配对码 → 拿到 cookie → 无 Noise 的 WS 可 `ui/attach`；**同一码第二次使用失败**；错误码 5 次后锁定；吊销后连接立即断开。
8. 配对码归一化：同一码的 `青松-A7K2M9` / `青松a7k2m9` / `青松 A7K2M9` / 全角 `Ａ７Ｋ２Ｍ９` 四种写法，首次都能消费成功；第二次全部失败。
9. 数据面与管理面**不共享 listener**：`--bind 0.0.0.0` 时 `:8443/admin` 返回 404（不是 403，是根本不存在这条路由）。

## 七、禁止

- 禁止在 Python 里 `import nacl` / `cryptography` 做协议加解密（TLS 证书生成除外）。
- 禁止「先注册后校验」或「校验失败但放行以便调试」。
- 禁止把 manifest 原文透传给 admin API（C5a 只能拿白名单字段）。
- 禁止用 `json.loads` 后的 dict 直接当内部状态（可变共享会出竞态），用不可变 dataclass。
- 禁止改 `schema/errors.json` 的既有 code 语义。

## 八、交付格式

按 `00-orchestration.md` §三，额外附：
- 六类非法 manifest 的**原始报错文本**（每条一行）
- 一次真实握手的时序日志（含握手耗时）
- `schema/*.json` 的 sha256（冻结凭据）
