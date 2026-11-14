# Vade Agent Runtime（Vade）技术方案 v1.19

| 项 | 值 |
|---|---|
| 产品名 | **Vade Agent Runtime**（中文「偕行」），简称 **Vade**；包名 / URI scheme / 协议版本用 `vade` |
| 版本 | v1.19（v1.18 + AMEND-21/22：客户端功能全集 52 条；19 条 UI/协议裁定——4 目的地、桌面双栏+可折叠右面板、C-09 扩到 11 组件、附件走 `POST /ui/blob`、只存压缩图、端上转写、Web **数据面**可写（D100 废止 D76，管理面 cookie 仍只读）、会话级模型优先、`waiting_input`、模块 UI 页面 C-19 待冻结） |
| 对齐需求 | `requirements.md` v1.0 + AMEND-01～23 |
| 状态 | **已确认**，可进入阶段 3 拆任务 |
| 变更触发 | AMEND-02（多实例/多端）、AMEND-03（队列/中断）、AMEND-04（皮肤/底栏、T1）、AMEND-08（扩展点/知识库）、AMEND-09（记忆/自进化）、AMEND-12（三面分离/配对码）、AMEND-13（管理面鉴权/rekey）、AMEND-14（setup token）、AMEND-15（token 轮换）、AMEND-16（记忆/技能管理）、AMEND-17（WebView 反转/设备绑定/去 SSH）、AMEND-18（审查修正：setup 门禁/绑定签名/票顺序）、AMEND-19（自进化节流/记忆硬顶/配置面全量化）、AMEND-20（文档一致性修订）、AMEND-21（客户端功能全集 52 条）、AMEND-22（19 条 UI/协议裁定；D76 废止）、AMEND-23（七份 RFC 批准；C-04 `BUDGET_EXCEEDED` / C-05 `ui/tool.call` / C-06 2.0.0 待实施） |

## 0. 本版相对 v1.0 的实质变化

| # | v1.0 | v1.1 | 原因 |
|---|---|---|---|
| 1 | 自研 4 步握手 | **Noise_IK / Noise_XX，用 `snow` 库** | 自研握手是最高风险项，改为成熟实现 + 固定参数集 |
| 2 | 注册表仅 `module_id` | `module_id + instance_id + concurrency` | 多实例（AMEND-02） |
| 3 | 只有请求-响应 | 增加 **core→UI 事件流 + seq + replay** | 多端同步（AMEND-02） |
| 4 | 会话隐式在内存 | 显式 `session_id` + attach/detach + 快照 | 多端 attach（AMEND-02） |
| 5 | 鉴权单一 | 三模式：`module_key` / `device_key` / `browser_session` | 原生端与浏览器安全模型不同 |
| 6 | 无队列 | FIFO turn 队列 + 中断 + 协作取消 | AMEND-03 |
| 7 | 无审批通道 | 统一 `approval/*`，超时拒绝，多端任一生效 | FR-7 |
| 8 | 无外观层 | 皮肤 × 底栏正交，运行时切换 | AMEND-04 |
| 9 | 多语言 SDK 各自实现 | **Rust 单实现 + UniFFI 绑定** | 消除协议漂移 |
| 10 | UI 技术栈未定 | T1：Compose MP + iOS SwiftUI | AMEND-04/A11 |
| 11 | Web 绑定风险未兜底 | W-A 优先，C0 三天不通降级 W-B | AMEND-05/A12 |
| 12 | glass 跨端强度未定 | Desktop/Web 只模糊不折射 | AMEND-05/A12 |
| 13 | 皮肤为可选项 | **三皮肤全部进 v1** | AMEND-05/A12 |
| 14 | 串行开发 | 并行子代理 + 独立审查 + 原代理返工 | AMEND-05/A13 |
| 15 | 只有 core 与 UI | **增加 6 个业务模块，模块组并行开发** | AMEND-06/A14 |
| 16 | 批次 B0–B8 | 重编 **C / M / U / F** 四组，里程碑 G1–G5 | AMEND-06/A14 |
| 17 | 配置页只有列表 | 升级为**蓝图式拓扑画布**（状态色连线 + 地址 + 可编辑） | AMEND-07/A19 |
| 18 | 编辑无边界的风险 | 分 L1/L2/L2.5 三层，**明确不做 L3 部署编排** | AMEND-07/A20 |
| 19 | 配置页无构建步骤 | 构建期 Node，产物入库，运行期零 Node | AMEND-07/A21 |
| 20 | 管理面与模块 app 同端口同源 | **三者分离**：数据面 8443 / 管理面 8444（默认回环，可显式对外）/ 模块 app 自托管 | AMEND-12/A42 |
| 21 | 6 位纯数字配对码 | **8 位中英混合**（2 汉字 + 6 位 base32），消费即删 | AMEND-12/A43 |
| 22 | 配置页一次性 token | **密码 + 远程公钥（Ed25519 挑战签名）**，core 不签发任何证书 | AMEND-13/A45 |
| 23 | 只 rekey 不刷新前向保密 | **rekey 管单 key 用量，24h 另起重握手**；帧头加 `generation` | AMEND-13/A46 |
| 24 | setup 只回环 + 30min 硬超时 | **双通道**：回环免 token / 非回环凭 `setup_token`；取消硬超时，改 24h 告警 + 启动期 fail-closed | AMEND-14/A47 |
| 25 | 云端 setup 无入口 | setup token（`acs_` + 32B，`data/setup.token`，消费即删）；首选仍是临时 IP 白名单 | AMEND-14/A48 |
| 26 | setup token 长期有效 | **每次启动未初始化就换新**；env 钉住则不换；已初始化清残留 | AMEND-15/A49 |
| 27 | 记忆/技能只能手工管 | **进配置页**：技能可安装/卸载/启停/审核，记忆可浏览/晋升/归档；**均不可编辑原文** | AMEND-16/A50 |
| 28 | 配置页只能在浏览器里开 | `ui` 模块只**唤起系统浏览器**；WebView 降级；**禁止模块代理 `/admin`** | AMEND-16/A51 |
| 29 | 公钥只能本机登记 | 本机 CLI **或**已登录会话内上传（**step-up 重输密码**，上限 5 把） | AMEND-16/A52 |
| 30 | 首次必须走 setup 页面 | `core admin init --password-stdin` 可**跳过 setup**，适合容器/编排 | AMEND-16/A53 |
| 31 | 技能装完即 `active` | **安装一律先落 `pending/`**，人工批准才进工具表与 prompt | AMEND-17/A54 |
| 32 | 配置页只许唤起系统浏览器 | **反转为自研 App 内 WebView**（系统浏览器降为只读）；WebView 只加载 App 内置资源，core 只给 JSON | AMEND-17/A55 |
| 33 | 公钥由管理员自己 `ssh-keygen` | **App 生成设备密钥（进 Secure Enclave / StrongBox）**；core 公钥由用户扫码/粘贴导入 App，不经云端 | AMEND-17/A56 |
| 34 | SSH 隧道是首次配置主路径 | **删除**。首次绑定 = 扫码/输 token + 设备密钥，全程手机可完成 | AMEND-17/A57 |
| 35 | setup 页直接渲染含 token 的二维码 | **先验 token 再渲染**：非回环 GET 只回 `requires_token`，解锁后才下发二维码与指纹；二维码里是一次性绑定票，不是 setup token | AMEND-18/A58 |
| 36 | 绑定问候（`kex_pub` / `challenge`）无签名 | **core 身份密钥对问候整体签名**，App 用已 pin 的 `core_pub` 验签；信封 AAD 覆盖 `challenge` 与 `kex_pub` | AMEND-18/A59 |
| 37 | 票在验证签名前就被删除 | 改为**先验后删**：解密、验签、限额全过才在同一事务里 `DELETE ... WHERE consumed_at IS NULL` | AMEND-18/A60 |
| 38 | 设备公钥只承诺进安全区，无任务承接 | **iOS 由 U4 承接**；`core_identity.key` 生成明确归 C5a；设备密钥 P-256 三端落地 | AMEND-18/A61 |
| 39 | 自进化只在会话末跑一次 | **轮次级 `turn_review`（每 10 轮）+ 会话末兜底**；短会话（<6 轮）不调 LLM | AMEND-19/D80 |
| 40 | 记忆按条数限容 | **字符硬顶（默认 4200，可配）+ 写入即报错**；条数只作结构上限，两者先到先限 | AMEND-19/D79 |
| 41 | 教训混在“事实”里 | 独立 `lesson`：四段式 `when/do/pitfall/verify`，**单次即可入 `agent:lessons`**但标 `unverified`；失败轨迹只产 lesson、不产 skill | AMEND-19/D81/D82 |
| 42 | 配置项散落、无 schema | `core.toml` 全量 schema（C-18）；**热生效项进配置页、重启项只读**；新增 `[memory]` / `[compaction]` 两段 | AMEND-19/D85/D86 |

## 1. 系统全景

```
┌─ 模块侧（任意语言 / 任意机器）──────────────────────────┐
│  工具模块     记忆模块     模型模块      UI 模块         │
│  (py/rs/go)   (py)        (py)         (Compose/Swift)  │
└──────┬───────────┬───────────┬──────────────┬──────────┘
       │           │           │              │
       └───────────┴─────┬─────┴──────────────┘
                         │  WSS（应用层 Noise 加密）
              ┌──────────▼───────────┐
              │   core (Python)      │
              │ ┌──────────────────┐ │
              │ │ transport  (WS)  │ │
              │ ├──────────────────┤ │
              │ │ crypto-gateway   │ │ ← Rust 扩展（Noise/AEAD）
              │ ├──────────────────┤ │
              │ │ registry         │ │
              │ ├──────────────────┤ │
              │ │ router           │ │
              │ ├──────────────────┤ │
              │ │ session-store    │ │
              │ ├──────────────────┤ │
              │ │ agent-loop       │ │
              │ ├──────────────────┤ │
              │ │ policy/approval  │ │
              │ ├──────────────────┤ │
              │ │ audit            │ │
              │ ├──────────────────┤ │
              │ │ admin-ui         │ │ ← 唯一内置 UI
              │ └──────────────────┘ │
              └──────────┬───────────┘
                         │
                    [ LLM 提供方 ]
```

**core 的 10 个主组件（外加 `agent-loop` 的一个子模块 `skill-loader`），没有一个是「知道某个具体模块存在」的。**

| 组件 | 职责 | 依赖谁 |
|---|---|---|
| `transport` | WS 接入、连接生命周期、心跳超时 | 无 |
| `crypto-gateway` | 握手、AEAD 加解密、重放窗口 | Rust 扩展 |
| `registry` | manifest 校验、实例表、健康度、能力索引 | transport |
| `router` | 工具名解析、实例选择、超时/熔断、取消 | registry |
| `session-store` | 会话、turn、队列、seq、replay buffer | 无 |
| `agent-loop` | 思考-行动-观察循环、终止条件、**prompt 组装**（含 skill 注入） | router + session-store + llm-adapter + skill-loader |
| `skill-loader` | 扫描本地 skill、按需激活、渐进注入、审计留名（loop 的子模块，不单独计数组件） | 无 |
| `policy` | risk 分级、审批挂起、放行规则 | session-store |
| `audit` | 追加写、按天分表、不可改 | 无 |
| `admin-ui` | 模块治理页 + **拓扑画布**（唯一内置 UI） | registry + audit |
| `llm-adapter` | OpenAI 兼容调用，可被 model 模块替换 | 无 |

> 关键约束：`agent-loop` **不认识**任何工具名。它拿到的是 router 给的「当前可用工具快照」，调的是 `router.call(tool_id, args)`。
>
> 与 FR-1.1 的「8 个组件」对应关系：`session-store` 属于 Agent 循环的状态部分，`llm-adapter` 属于模型接入的默认实现，两者都不是新增职责；需求侧仍按 8 个逻辑组件计。

> **审计 kind（agent-loop / policy 写入，v1.19 登记）**：`turn_completed`、`turn_interrupted`（被用户中断的 turn **不得**记成 completed）、`tool_call`（工具已真实执行但 turn 被中断时带 `aborted: "interrupted"`）、审批决策（`approval.*`）。新增或改名 kind 必须同时更新本节与 `docs/status.md` 的口径，否则审计口径会与代码漂移。

## 2. 协议 v1

### 2.1 帧格式

```
 0        1        2        3
+--------+--------+--------+--------+
| ver(1) | type(1)| flags(1)| rsv(1) |
+--------+--------+--------+--------+
| payload_len (4, BE)                |
+------------------------------------+
| generation (4, BE)                 |
+------------------------------------+
| counter (8, BE)                    |
+------------------------------------+
| ciphertext  (payload_len + 16 tag) |
+------------------------------------+
```

- `ver`：协议主版本，当前 `1`。不匹配 → 立即断开，首帧里就定死，**禁止协商降级**。
- `type`：`0x01` handshake / `0x02` rpc / `0x03` event / `0x04` heartbeat / `0x05` close / `0x06` rekey。
- `generation`：密钥代数，握手成功为 `1`，每次 rekey `+1`。接收端要求 `(generation, counter)` **字典序单调递增**；generation 回退一律 `fatal`（不回退旧 key，见 §2.2.2）。这一字段是为了让 `counter` 在 rekey 后归零时不触发重放误判。
- `counter`：本方向单调递增，重复或回退 → 判为重放，断开。
- AEAD：**ChaCha20-Poly1305**，nonce 由 Noise 传输态维护（96-bit，Noise 规范：前 4 字节 0，后 8 字节小端计数器）。
  - **方向隔离靠密钥，不靠 nonce**：Noise Split 产生两把独立密钥（c2m / m2c），不共享。把收到的帧原样反射回去必然解不开——这比「方向写进 nonce」更强。
  - 帧头 `counter` 只用于**重放检测与断线定位**，不参与 AEAD nonce。

#### 2.1.1 为什么改了 nonce 方案（v1.5）

原方案写 `nonce = direction(4B) || counter(8B)`，但 `snow` 的 `TransportState` 内部持有 nonce 且**不暴露流量密钥**。要按原方案做，只能 fork snow 或自己重写 Noise 对称态——两者都违背「不自研密码学状态机」。

因此：握手与传输都交给 `snow`，方向隔离由 Split 的两把密钥承担。安全性不降：反射帧用错误的密钥解密，认证标签必然失败。代价是帧头 `counter` 与 AEAD nonce 不再字面对应，所以接收端必须**显式校验 `counter` 单调递增**，否则重放检测形同虚设（见 C1 任务书 T4）。

**与需求原文的对齐（AMEND-10 已回写）**：
- FR-3.2 原写 `XChaCha20-Poly1305`，已改为 `ChaCha20-Poly1305`——Noise 参数集固定为 `ChaChaPoly`（96-bit nonce），XChaCha 的 192-bit nonce 与 Noise 规范不兼容。
- FR-3.4 原写「长期密钥仅用于签名」，已改为「长期 X25519 只参与认证 DH」——Noise 没有签名步骤，前向保密由每连接新生成的 ephemeral 保证。
- FR-3.3 原写「方向 4B + 计数器 8B 进 nonce」，已改为「方向靠 Split 双密钥、nonce 由 Noise 维护、counter 只做重放检测」，理由见上。



### 2.2 握手

用 `snow`（Rust Noise 实现），参数集固定：

```
Noise_IK_25519_ChaChaPoly_BLAKE2s     ← 已配对模块的常规连接（1-RTT）
Noise_XX_25519_ChaChaPoly_BLAKE2s     ← 首次配对（2-RTT）
```

- **不自己实现密码学状态机**。上一版方案写「4 步自研握手」，风险过高，本版推翻。
- `IK` 的前提是 core 已持有模块静态公钥（白名单），模块也预置了 core 公钥（配对时下发）。
- `XX` 只用于首次配对：core 端显示 **8 位中英混合配对码**，模块端输入，配对码经 HKDF 混入 PSK。
- 具体参数：`Noise_XXpsk3_...`，PSK 在第三条消息混入；`PSK = HKDF-SHA256(ikm = normalize(pair_code) || core_pub_fp, salt = "vade/pair/v1", len = 32)`。
- `prologue = b"vade/v1" || role_byte`（initiator=1 / responder=2），两边不一致即握手失败——防跨协议混淆。
- 握手完成后再发 `module/register`，**注册信息在密文通道内传输**。

前向保密：`snow` 的 ephemeral 每连接新生成，`IK` 下 long-term 只用于认证不做加密。

### 2.2.1 配对码规格（v1.10 改）

| 项 | 规格 |
|---|---|
| 形态 | **2 个汉字 + 6 位 base32**，共 8 个有效字符；显示为 `青松-A7K2M9` |
| 汉字表 | 固定 256 字表（`identity/hanzi.py`，全表随代码入库）。取《现代汉语常用字表》前 600 中筛选：剔除生僻字、异体字、同音难辨（`己/已/巳`）、视觉相近（`未/末`）、以及需要选字的二级字 |
| base32 表 | `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（32 字符，剔除 `I O 0 1`） |
| 熵 | `256² × 32⁶ ≈ 2^46`。对比：原 6 位数字 = 2^20，8 位纯数字 = 2^27 |
| 归一化 | `NFKC` → 转半角 → 去 `-`、空格、`·` → 中文段原样比对、字母段 `upper()`。**归一化在 HKDF 与哈希之前做，两边实现必须一致**（写进 C-01 测试向量） |
| 有效期 | **180s**，显示倒计时；到期自动作废并让二维码置灰 |
| 一次性 | `consume()` 用 `DELETE ... WHERE code_hash=? AND purpose=?` 的 **rowcount 判定**，返回 0 即失败。**不是标记 used**——标记型一次性在并发下会被用两次 |
| 落盘 | 只存 `argon2id(normalize(code))`，明文仅在创建响应体里出现一次，不进日志、不进审计、不进二维码以外的地方 |
| 失败处理 | 同一码错 5 次立即作废；每 IP 5 次 / 5 分钟；两条都写审计 |
| 用途绑定 | `purpose ∈ {module, browser}`，与码一起存，消费时校验；码不跨用途复用 |

**为什么加中文**：不是为了加密强度（英文也能做到同等熵），而是**降低口述与手抄错误**。「2 个汉字」给人的锚点远比 6 个随机字符强，念一遍就能记住，走「管理员念、对方输入」这条路径时出错率显著低于纯字母数字。8 个字符里的 6 位 base32 仍承担了 2^30 的熵，汉字段主要承担可读性。

**代价（如实记录）**：中文需要输入法，命令行参数与 shell 环境可能编码不一致。因此：

1. `--pair-code` 接受任意编码，客户端统一按 UTF-8 解析，并用同一套 `normalize()`（NFKC 是关键——否则全角字符与兼容汉字会算出不同 PSK）；
2. 二维码是**主路径**，手输是兜底；
3. 终端环境非 UTF-8（`LANG=C`）时，`vade --print-pair-code` 额外输出一张 ASCII 二维码，避免直接打印乱码。

### 2.2.2 密钥协商与轮换：完整时序（回答「是不是先非对称再对称、隔段时间换密钥」）

是，但「非对称加密」这个说法要精确一点——**不是用非对称算法去加密数据，而是用它协商出一把对称密钥**。数据从头到尾只用对称加密。

```
① 每次连接开始，双方各自生成一对临时 X25519 密钥（ephemeral，用完即弃）
② 交换公钥，各自算出同一个共享秘密 ss = X25519(我的临时私钥, 你的临时公钥)
   —— 这一步是 DH，不是「加密」。私钥本身不传，ss 也不传
③ 把 ss 交给 HKDF-SHA256，连同握手 transcript 哈希，派生两把对称密钥：
      k_c2m = HKDF(ss, "vade/v1 c2m")   ← core→模块
      k_m2c = HKDF(ss, "vade/v1 m2c")   ← 模块→core
   方向隔离靠这两把独立的 key，不靠 nonce 里的方向位
④ 之后所有帧：ChaCha20-Poly1305(k_方向, nonce=Noise 计数器, plaintext)
   —— 到这里非对称算法已经退出，只剩对称加密
```

三个要点：

1. **每次连接都重新生成 ephemeral**，所以每条连接的前向保密是独立的：今天抓到的包，明天拿到长期私钥也解不开。
2. **长期 X25519 私钥只参与认证 DH，不参与派生加密密钥**（Noise IK 里它是 `ee/es/se` 中的一个输入，混进 `ss` 之后就无法单独剥离）。它不做签名，Noise 也没有签名步骤。
3. **密钥只在内存里**，不落盘、不进日志、不进 core dump（`prctl(PR_SET_DUMPABLE, 0)`，Rust 侧密钥结构 `zeroize` on drop）。

**轮换（rekey）**

| 项 | 决定 |
|---|---|
| 触发条件 | 连接存活 > **24h**，或单方向发送 > **1 GiB**，或帧数 > **2^20**，三者先到者 |
| 谁触发 | 发送方；发一个 `type=0x06 rekey` 控制帧，帧体用**旧密钥**加密（否则接收方不知道该用哪把解） |
| 做什么 | 双方各自 `k = HKDF(k, "rekey")`，**只换对称密钥**，不重新握手、不换 ephemeral |
| 计数 | rekey 后 `counter` 归零，但**必须记录 generation**（`(generation, counter)` 二元组），否则归零会被重放检测当成回退 |
| **方向性** | `gen` **按发送方向各自独立**：A→B 与 B→A 各维护一个 `gen`/`counter`。收到对端的 `0x06` 只 rekey**接收方向**，不动自己发送方向的 `gen` |
| **同时触发** | 双方都到 24h 时会各自发一张 `0x06{gen+1}`。因为是按方向独立，A 收到 B 的帧时，A 的**接收方向** gen 仍是旧值，校验通过；两个方向各自前进一代，不会互相打断 |
| **幂等** | 收到 `new_gen <= 接收方向当前 gen` → 视为重复或过时，**忽略该帧但不断连**（对端可能只是重传）；只有 `new_gen > gen + 1` 才是跳代，断连 |
| 失败处理 | rekey 帧解不开 → 立即断连，不回退到旧 key 继续用（回退等于给降级攻击留门） |
| 与重握手的关系 | rekey **不提供新的前向保密**（ephemeral 没变）。要新的前向保密必须重握手；生产建议 24h 主动重握手一次，rekey 只是防止单条连接用同一把 key 太久 |

**为什么不用「每隔一段时间重握手」代替 rekey**：重握手要 1–2 个 RTT，且要重新走认证；对一个已经活跃的会话来说，代价与中断风险都不划算。但反过来，只 rekey 不重握手会让 ephemeral 长期存活——所以两者都做：**rekey 管「单 key 用量」，重握手管「前向保密刷新」**。

数字上的余量：ChaCha20-Poly1305 的 nonce 是 96-bit（Noise 实际只用 64-bit 计数器），2^20 帧远远没到危险线。阈值定这么保守，是因为**换 key 的成本几乎为零，而算错的成本是灾难性的**。

### 2.3 manifest

```json
{
  "module_id": "com.example.fs",
  "instance_id": "fs-01",
  "version": "1.0.0",
  "protocol": "vade/v1",
  "concurrency": "pooled",
  "capabilities": {
    "tools": [{
      "name": "read_file",
      "description": "读取指定路径的文本文件，返回 UTF-8 内容",
      "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
      "risk": "low",
      "idempotent": true,
      "timeout_ms": 10000
    }],
    "prompts": [{"id": "fs.system", "content": "..."}],
    "events": ["fs.changed"]
  },
  "limits": {"max_concurrency": 4, "max_payload": 1048576},
  "injects": ["session_id"],
  "role": "tool"
}
```

**校验规则（不合法即拒注册，回可读原因）**：

| 校验 | 规则 | 拒绝理由示例 |
|---|---|---|
| 工具名 | `^[a-z][a-z0-9_]{0,63}$` | `read-file` 含非法字符 |
| description | 非空，≤ 512 字 | 空描述（模型无法判断何时调用） |
| input_schema | 合法 JSON Schema，`type: object` | schema 根不是 object |
| risk | 模块**申报** `low` / `medium` / `high`；core 按 `[policy.tool_risk]` 裁决，**只能上调不能下调** | 申报值**不在枚举内** → 拒；值合法但未登记 → **接受并升为 `high`**（不是拒），二者别混 |
| 工具数 | 单模块 ≤ 64，全局 ≤ 128 | 超过上限，模型选择质量会崩 |
| 重名 | `module_id/tool` 唯一；同一 `tool` 名跨模块 → **拒绝后注册的** | 已存在 `fs/read_file` |
| `module_id` / `instance_id` | `^[a-z][a-z0-9._-]{2,127}$`，禁 `/`、`#`、控制符 | 含 `#` 会破坏 `module_id#instance_id` 的节点 id 格式 |
| `injects` | 只能是 `session_id`；**只有声明了才注入**，未声明的一律不注入 | 声明了但 core 未实现该注入源 |
| `role` | `tool` / `evolve` / `ui`，缺省 `tool`；`evolve` **只认 core 配置里的指纹白名单**，不看模块自述 | 自封 evolve |

### 2.4 工具命名空间

| 层 | 形式 | 例 |
|---|---|---|
| 协议内部 ID | `module_id/tool_name` | `com.example.fs/read_file` |
| 暴露给 LLM | `{prefix}__{tool_name}` | `fs__read_file` |
| 前缀生成 | 取 `module_id` 最后一段，冲突时加序号 | `fs`、`fs2` |

LLM 函数名只允许 `[a-zA-Z0-9_-]`，所以 `/` 和 `.` 必须映射。

### 2.5 RPC 方法表

**模块 → core**

| 方法 | 参数 | 返回 |
|---|---|---|
| `module/register` | manifest | `{accepted, session_id, tool_names[]}` |
| `module/heartbeat` | `{load, inflight}` | `{ok, server_time}` |
| `module/event` | `{topic, payload}` | 无（通知） |
| `events/subscribe` | `{topics[], redact?}` | `{accepted[], rejected[]}` —— 白名单校验，见 §16.7 |
| `skill/proposal` | `{name, action, content, base_revision?, derived_from}` | `{accepted, path}` / `{ok:false, error}` |
| `module/drain` | — | core→模块，见下 |

**core → 模块**

| 方法 | 参数 | 返回 |
|---|---|---|
| `tools/call` | `{call_id, tool, args, deadline_ms}` | `{ok, result}` / `{ok:false, error}` |
| `tools/cancel` | `{call_id}` | `{cancelled: bool}` |
| `prompts/get` | `{id}` | `{content}` |
| `events/deliver` | `{topic, session_id, seq_from, seq_to, payload}` | `{ok}` —— 仅向已订阅且在白名单内的模块推送 |
| `module/drain` | `{grace_ms}` | `{accepted}` |

**UI 模块 → core**

| 方法 | 参数 | 说明 |
|---|---|---|
| `ui/attach` | `{session_id?, last_seq?}` | 无 session_id 则新建；有 last_seq 则增量补发 |
| `ui/detach` | `{session_id}` | 主动断开订阅 |
| `ui/submit` | `{session_id, content[], client_msg_id}` | 入队，返回 `{turn_id, queue_pos}` |
| `ui/interrupt` | `{session_id, turn_id}` | 中断当前 turn |
| `ui/queue.cancel` | `{session_id, turn_id}` | 取消排队项 |
| `approval/decide` | `{request_id, decision, scope}` | `scope`: `once` / `session` |

**core → UI（事件流）**

| 事件 | 载荷 |
|---|---|
| `session.attached` | `{session_id, from_seq, snapshot?}` |
| `turn.queued` | `{turn_id, pos}` |
| `turn.started` | `{turn_id}` |
| `turn.delta` | `{turn_id, text}`（增量） |
| `turn.tool_call` | `{turn_id, call_id, tool, args_digest}` |
| `turn.tool_result` | `{turn_id, call_id, ok, summary}` |
| `turn.approval_required` | `{turn_id, request_id, tool, args, risk, expires_at}` |
| `turn.approval_resolved` | `{turn_id, request_id, decision, by, scope}` —— **多端关闭弹窗的唯一依据**，缺了它其余端会一直转圈 |
| `turn.completed` | `{turn_id, usage}` |
| `turn.failed` | `{turn_id, code, message}` |
| `turn.interrupted` | `{turn_id, by}` |
| `queue.updated` | `{pending[], running?}` |
| `module.status` | `{module_id, instance_id, online, version}` |

> **第一版禁止模块反向调用 core 的工具**。否则一个装了 `read_file` 的模块可以借道去调另一个模块的 `shell_exec`。
>
> `events/deliver` 是 core 主动推、模块被动收，不构成「模块反向调用 core」——模块只能声明兴趣（`events/subscribe`），推什么、推给谁由 core 决定。

### 2.6 seq 与 replay

- `seq` per-session 单调递增，**core 分配**，不由客户端协商。
- replay buffer：最近 **1000 条** 或 **24 小时**，先到为准。
- `ui/attach(last_seq=N)`：
  - buffer 覆盖 N → 从 N+1 起补发，然后转实时
  - 不覆盖 → 返回 `snapshot`（当前消息树 + 队列状态），`from_seq` = 快照点
- 客户端必须**按 seq 去重**：重连时可能收到重叠片段。

### 2.7 扩展点：三层，不要混用

系统有三个能「往上加东西」的地方，能力差别很大，混用会同时丢治理和丢体验：

| 层 | 形态 | 谁管 | 能否执行 | 进拓扑 | 进审计 | 审批 |
|---|---|---|---|---|---|---|
| **module** | 独立进程 + manifest + 协议 | core 注册表 | 是（`tools/call`） | 是，一实例一节点 | 是，工具级 | 按 risk |
| **prompt 片段** | manifest 里的 `capabilities.prompts` | 模块自带 | 否，纯文本 | 否 | 否 | — |
| **skill** | 本地目录 `SKILL.md` + 资源 | core 的 `skill-loader` | **否**，只能用已有工具 | 否 | 是，只记名字 | 跟随它借用的工具 |

#### 2.7.1 判定「是不是模块」的三条硬标准

任一不满足，就不是模块：

1. 有没有 core 签发的 `module_key`（走过 Noise 握手）；
2. 有没有进注册表（有 `module_id + instance_id`）；
3. 能不能被**别的模块**通过 router 路由到。

按这三条，`mcp-bridge` 背后的上游 MCP server **不是模块**——它不握手、不注册、不能被路由。它是 bridge 的外部依赖，和 `fs` 连的那个磁盘目录同一性质。因此 FR-5.7「禁止模块间直连」没有被破坏：两个 bridge 实例之间没有边，一个 bridge 和它上游之间也不是模块间的边。

> 这条判定标准会被反复问到，所以写成规则而不是举例：**「看起来像连着的」不等于「连着的」。**

#### 2.7.2 skill 规格

一个 skill 是一个目录：

```
<data_dir>/skills/kb-guide/
├── SKILL.md            # 必需，frontmatter + 正文
├── references/         # 可选，被正文引用时才读
└── scripts/            # 可选，人跑的；模型要跑得走 shell 模块
```

```yaml
---
name: kb-guide
description: 项目知识库的查询与写入规范
when_to_use: 用户问到项目历史决策、架构约定，或需要写入长期结论时
triggers: ["知识库", "之前定过", "决策记录"]     # v1 只做子串匹配
requires_tools: ["mcp-bridge__s_kb_recall", "mcp-bridge__s_kb_remember"]
---
正文（≤ 8KB）：什么时候查、查完怎么引用、写入前要确认什么。
```

加载与注入规则：

| 项 | 规则 |
|---|---|
| 扫描 | 启动时扫 `skills/*/SKILL.md`；文件变更靠 fs watch 或手动 reload，不做热更 |
| 渐进式披露 | 默认**只注入 frontmatter**（name / description / when_to_use）；正文在激活后才注入 |
| 激活 | `@skill-name` 显式提及，或 `triggers` 子串命中；v1 **不做语义匹配**（要 embedding，依赖 model） |
| 上限 | 同时激活 ≤ 3 个；正文 ≤ 200 行 / 8KB，超出截断标 `truncated`，读全文靠 `fs` |
| 依赖缺失 | `requires_tools` 里的工具不全 → 标 `unavailable`，**不注入**，日志 warning |
| 审计 | `turn.completed` 带 `skills: ["kb-guide"]`，**只记名字，不记正文** |

安全约束：

- skill **只能来自本地文件**，不接受模块注册、不从网络下载——否则等于给模块开了一条 prompt 注入通道。
- 加载时扫一遍 `sk-` 前缀、`BEGIN PRIVATE KEY`、超长 base64，命中只告警不改写（可能误报）。
- `scripts/` **不自动执行**。模型要跑它，只能走 `shell` 模块，风险等级等于 shell，这是刻意的。

#### 2.7.3 组装管线：谁能往 prompt 里写字

`agent-loop` 每轮拼一次 prompt，顺序固定，**每个位置只有一个写入者**，否则两批并行开发必然互相覆盖：

```
persona → tool_block → skill_catalog → skill_bodies → session_memory → context
```

```python
@dataclass(frozen=True)
class PromptParts:
    persona: str
    tool_block: str
    skill_catalog: str = ""          # C3b 填：目录行（name + description + 状态）
    skill_bodies: str = ""           # C3b 填：已激活正文
    session_memory: str = ""         # M4a 的 session 层，由 core 读后填
    user_message: str = ""           # 当轮用户原文，供 activation 匹配
    activated_skills: tuple[str, ...] = ()   # C3b 回填，供审计
    context: str = ""
```

三条硬规则：

1. **`user_message` 必须进 `PromptParts`**。skill 激活靠 `@mention` 与 `triggers` 子串，hook 拿不到用户原文就无从匹配。
2. **`activated_skills` 是回传通道**。loop 不 import skill 包，但要把它写进 `turn.completed.skills`；hook 在返回 parts 时一并回填这个字段，loop 只读不判断。
3. **`session_memory` 由 core 填，不由模块填**。`tools/call` 里因此需要 `session_id`（core 侧填充、模块侧只读），且**只有 manifest 声明了 `injects: ["session_id"]` 的模块才收到它**——否则等于给所有模块发会话标识。

不遵守这三条的后果很具体：skill 永远不激活、审计里看不到用了哪个 skill、会话记忆永远进不了上下文。


#### 2.7.4 为什么 skill 不能自带执行通道

三条，任一条都足以否掉：

1. **不可审计**：core 只能看到「调了 shell」，看不到「这是 skill 里的脚本」，事后追不到是哪段编排出的问题。
2. **权限借道**：skill 声明 `exec` 就等于绕过 risk 分级——本来该审批的高危动作，包一层 skill 就变成「skill 的常规操作」。
3. **不可治理**：没法按 skill 粒度禁用、没法在拓扑上体现、没法回答「这个 skill 现在生效吗」。

要执行，就做模块。要说明书，就写 skill。**中间没有第三种东西。**

#### 2.7.5 上游 MCP 的定位（知识库的推荐路径）

外置知识库不注册进 core，走 `mcp-bridge` 上游：

```
知识库 MCP server ──MCP(stdio/HTTP)──> mcp-bridge ──Vade 协议──> core
        ↑ 不是模块                ↑ 是模块，可能多实例
```

好处是零 core 改动：工具自动进模型工具列表，有 risk、有审计、有拓扑。代价是治理精度受 bridge 限制，所以：

- **一个上游拆一个 bridge 实例**（`instance_id = mcp-bridge-<server>`），故障与健康度各自独立，拓扑上是多个节点；
- 上游没有 risk 概念，靠 `tool_risk` 映射显式声明（`recall = low`、`forget = high`），**不靠猜**；
- 侧栏三层树：`bridge → 上游 server → 工具`，画布上仍只画 bridge 节点（见 D35）。

## 3. 鉴权三模式

| 模式 | 适用 | 凭据 | 传输层 | 端到端加密 |
|---|---|---|---|---|
| `module_key` | 服务端模块 | X25519 静态密钥对 | WSS | ✅ Noise |
| `device_key` | 桌面 / 移动原生端 | 设备独立密钥对，存系统钥匙串 | WSS | ✅ Noise |
| `browser_session` | 浏览器 / Flutter-free Web | HttpOnly cookie | TLS | ❌ 止于 TLS |

> 密钥类型说明（v1.5）：原表写 Ed25519，但 `Noise_25519` 需要 X25519。**不引入第二套密钥**——身份认证由 Noise IK 本身完成（core 用白名单里的模块公钥解第一条消息，模块用配对时拿到的 core 公钥验第二条），比额外签一次名更少出错。指纹 = `SHA-256(公钥)[:16]` 小写 hex。

**为什么浏览器不存私钥**：localStorage / IndexedDB 里的私钥挡不住 XSS，存了就是「看起来安全」。宁可明确写「浏览器端信任边界止于 TLS」，也不给假的安全感。

浏览器会话怎么建立：管理员在配置页生成 8 位配对码（`purpose=browser`，180s、一次性），浏览器 `POST /ui/session` 换取 `__Host-ac_ui` cookie；WS 升级凭 cookie，**跳过 Noise**，帧为纯文本 JSON-RPC。这条通道在审计里必须标 `e2e=false`，拓扑连线上是空心锁。

> **写权限（D100 / FR-8.75，v1.19）**：`browser_session` 默认**可写**（发送、上传、批准、删除）。写方法必须带与会话绑定的 CSRF token（double-submit）+ `Origin` / `Sec-Fetch-Site` 同源校验，Web 壳 CSP 禁 `unsafe-inline` / `unsafe-eval`；只有**显式只读会话**才返回 `READ_ONLY_SESSION`。**管理面 `:8444` 的 `__Host-ac_admin` 仍是只读（§7.10），两者不是一回事**，不得混为一谈。

### 3.1 「端到端加密」到底覆盖哪几段（必须说清，不能笼统回答）

| 链路 | 是否 E2E | 靠什么 | 中间节点能看到明文吗 |
|---|---|---|---|
| core ↔ 模块 | **是** | Noise IK + ChaCha20-Poly1305，密钥由每连接 ephemeral ECDH 派生，**不依赖 TLS** | 不能。反代、LB、抓包、TLS 终止点都只看到密文 |
| core ↔ 原生端（桌面/移动） | **是** | 同上，`device_key` 模式 | 不能 |
| core ↔ 浏览器 | **否** | 仅 TLS + cookie | 能。TLS 终止在哪，明文就在哪 |
| mcp-bridge ↔ 上游 MCP server | **否** | 上游自己的传输（多为 stdio 或它自己的 HTTPS） | 能。bridge 进程内是明文；只有回传 core 那一段加密 |
| core 进程内部 | 不适用 | 同进程内存 | — |

三条必须写进文档、不许含糊的结论：

1. **E2E 保护的是「传输中的消息」，不保护「端点本身」。** 模块被入侵、core 被入侵，或者有人在模块进程里 `strace`，Noise 一点用都没有。它是防中间人和防落盘抓包，不是防提权。
2. **静态存储不加密。** `data/core.key` 是 0600 的明文种子，会话库 SQLite 是明文。没有 TPM / Keychain 时，「加密存储」只能靠全盘加密，core 不做假的密钥包裹层。这点在风险表里明说（§12）。
3. **bridge 是加密边界的终点。** 知识库内容从上游 MCP 出来到 bridge 进程为止是明文；从 bridge 到 core 才进 Noise。所以「接了知识库 = 知识库内容全程加密」是错的，别这么宣传。


配对流程（原生端）：

```
配置页点「添加设备」 → 生成一次性配对码 + 二维码
                        QR = vade://pair?h=<host>&p=<port>&c=<code>&fp=<core_pub_fp>
手机扫码 → 本地生成密钥对 → 走 Noise_XX + 配对码 PSK → core 记录公钥指纹
之后连接 → Noise_IK，设备长期 X25519 密钥参与认证 DH（无签名步骤）
```

吊销：配置页删指纹 → 立刻踢连接 → 该设备下次握手失败。

## 4. 会话与多端同步

**单一真相源在 core，UI 只是视图。** 手机和电脑不需要互相认识。

```
session
├── id, created_at, title
├── messages[]          # 已确认的历史
├── turns[]             # 执行单元
│   ├── id, state, started_at
│   └── events[]        # 该 turn 的过程事件
├── queue[]             # FIFO，单执行
└── seq_counter
```

**多端 attach 后看到的完全一致**，因为它们看的是同一个 seq 流，不是各自维护的状态。

## 5. turn 队列与中断

```
submit → [queued] → [running] → [done]
              ↓          ↓
        queue.cancel  interrupt
              ↓          ↓
         [cancelled]  [interrupted]
```

| 规则 | 内容 |
|---|---|
| 并发度 | 每会话同时**只有 1 个 running**，其余排队 |
| 入队 | 按 core 收到顺序，`client_msg_id` 幂等去重 |
| 中断 | `interrupt(turn_id)` → core 向正在执行的模块发 `tools/cancel` → 标记 `interrupted` → **立即释放队列** |
| 取消排队 | 只对 `queued` 有效，直接移除并广播 `queue.updated` |
| 模块不支持取消 | core 不等待，标记该工具结果作废，turn 照常推进为 `interrupted` |
| 审批阻塞 | 只卡**队首**，后续消息可入队；UI 显示「等待确认」，不显示「执行中」 |

## 6. 审批通道（HITL）

```
policy 判定 risk=high
   ↓
广播 turn.approval_required {turn_id, request_id, tool, args, risk, expires_at} 给所有 attached UI
   ↓
第一个 approval/decide 生效 → 广播 turn.approval_resolved {turn_id, request_id, decision, by, scope}
        （其余端撤销弹窗；没有这一条，它们会一直转圈）
   ↓
执行 or 拒绝
```

| 规则 | 内容 |
|---|---|
| **risk 谁说了算** | **core，不是模块。** 模块在 manifest 里的 `risk` 只是申报值，core 用 `[policy.tool_risk]` 表裁决，规则三条：① 只能上调不能下调；② 未在表中登记的 `(module_id, tool)` 按 `high`；③ `shell` / `fs.write` / `fs.delete` / 任何 `forget` 类工具**硬编码下限 high**，配置也改不了 |
| **谁能批** | 只有 `device_key`（原生端，私钥在系统钥匙串）与 **loopback 管理员**。`browser_session` **只能查看、不能批准 high**——浏览器是 XSS 可控环境，让它批准等于把 HITL 交给攻击者。`ui` 模块只能渲染，不能代替人决策 |
| 默认超时 | 60s（`[policy] approval_timeout_s` 可配），超时 = **拒绝**（fail-closed，不是 fail-open） |
| 决策范围 | `once` 只本次；`session` 缓存 key 是 **`(session_id, tool_id, args_digest)`**，不是 `(session_id, tool_id)`——否则批一次 `ls` 就等于批了 `rm -rf /`。通用执行器（`shell`）**只允许 `once`** |
| 缓存失效 | `module/capabilities.update`、停用/重新启用、risk 变更，三者任一发生即清空该会话的审批缓存 |
| 无人在线 | **立即拒绝**（`no_approver`），不等到 60s，也不挂起等设备上线。**不做 TTY 交互**——systemd 下 stdin 可能是 TTY，会卡死队列 |
| 审计 | `request_id`、`tool`、`args_digest`、谁批的（`device_id` 或 `admin`）、耗时、决策、`scope` |
| 参数展示 | 显示**完整参数**，不是摘要。摘要会让用户批准自己没看清的东西 |
| fail-closed | 生产模式下 policy / audit 组件缺失 → **拒绝启动**。`[dev] allow_all=true` 才允许 `AllowAll`，启动时打红字告警，且 `/healthz` 暴露 `policy=allow_all` |

## 7. 配置页（core 唯一内置 UI）

### 7.1 形态与路由

**先说结论：管理面、数据面、模块 app 是三个不同的 origin，不是一个端口上的三个路径前缀。**

| 面 | 默认监听 | 内容 | 谁能访问 |
|---|---|---|---|
| **数据面** `:8443` | `127.0.0.1:8443`，`--bind` 才对外 | `wss` 模块接入、`POST /ui/session`、`GET /healthz` | 模块与已配对客户端（LAN 视配置） |
| **管理面** `:8444` | `127.0.0.1:8444`，**`--admin-bind 0.0.0.0` 允许对外** | `GET /admin`（浏览器只读壳）、`/admin/api/*`、`/admin/api/stream` | **已绑定设备（App 内 WebView）全功能**；浏览器**只读**；模块 app 一律 403 |
| **模块 app** | 模块自己定（webui 默认 `:8080`） | 对话界面静态资源 + 管理页 WebView 宿主 | 由 `ui` 模块自己决定 |

| 项 | 决定 |
|---|---|
| 鉴权 | **密码（Argon2id）+ 设备密钥（P-256 ECDSA 挑战签名；Ed25519 仅软件密钥）**；会话用 Bearer，不用 cookie（§7.12）。浏览器直连只有密码 → **管理面只读**（数据面浏览器会话按 D100 可写，见 §7.1.3 注） |
| 实时 | **SSE** `/admin/api/stream`（单向推送状态；布局写回走普通 `PATCH`，故不需要双向 WS） |
| 前端 | React + React Flow + TypeScript，源码 `admin-ui/src`，构建产物 `admin-ui/dist` **提交进仓库**；同一份 `dist` 被 core 静态托管（浏览器只读）**和**打进 App 包（WebView 全功能） |
| 运行时 | core 只做静态托管，**部署不需要 Node**；CI 校验 `dist` 与 `src` 一致 |
| CORS | **管理面不返回任何 `Access-Control-Allow-*` 头**。浏览器天然阻断跨源读取，这个「缺省」本身就是防线 |
| CSP | `default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'` |
| 边界 | 模块治理 + 技能治理 + 记忆治理；**不含对话界面、不含 `core.toml` 编辑、不含拉起模块进程**（FR-9.2 / §7.9） |

#### 7.1.1 为什么必须不同源（v1.10 修正）

v1.9 及以前把 `/admin`、`/app`、`/ui` 全 mount 在同一个 8443 上，理由是「少起一个端口少一套 TLS」。这个理由站不住：

1. **`/app` 里跑的是模块代码，不是 core 代码。** webui 是 `ui` 类型模块（FR-8.1），它的 XSS 漏洞等同于「任意第三方 JS 在管理员已登录的 origin 上执行」。同源意味着它可以直接 `fetch('/admin/api/nodes')` 读拓扑、读审计、发吊销请求，`HttpOnly` 完全挡不住——`HttpOnly` 只防读 cookie，不防「借用 cookie 发请求」。
2. **CSRF token 也同源，一样能读。** 双提交 cookie 在「攻击者已在同源执行 JS」的假设下毫无意义。同源 + 第三方代码 = 没有任何前端隔离手段可用。
3. **管理面的暴露面应该比数据面小一个数量级。** 数据面要接受模块连接，可能开在 `0.0.0.0`；管理面永远只该在本机。绑在同一个 listener 上，等于让「为了模块接入而开的端口」把管理面一起带出去。

分离之后，浏览器把两个 origin 当作两个安全域：模块 app 的 JS 读不到 admin 的 DOM、localStorage，也读不到 admin 的响应体（CORS 缺省阻断）。

#### 7.1.2 分离之后必须补的三个细节

> **v1.15 前置说明（A55）**：下面关于 cookie 的三条只适用于**浏览器只读模式**。主路径是 App 内 WebView，走 **Bearer 会话**（access token 存内存、refresh 存钥匙串），**完全不依赖 cookie**——于是「cookie 不区分端口」「`__Host-` 只在 HTTPS 可设」「WebView cookie jar 与系统浏览器隔离」这一整类坑在主路径上不存在。cookie 那套只留给「手机不在手边、临时用别人电脑看一眼」的降级场景。

**① cookie 不区分端口 —— 这是最容易漏的一条。**

cookie 的作用域是 `(domain, path)`，**不含端口**。所以 `127.0.0.1:8080` 的页面照样会把 `127.0.0.1:8444` 设的 cookie 带上。仅靠「不同端口」并不能隔离 cookie。因此：

- 管理 cookie 用 `__Host-` 前缀：`__Host-ac_admin`，`Secure; HttpOnly; SameSite=Strict; Path=/`，**不带 `Domain`**（`__Host-` 强制这三条，浏览器会拒绝不合规的 Set-Cookie）。
- 管理面**硬校验 Origin**：`/admin/api/*` 的每个请求，`Origin` 必须等于 `https://127.0.0.1:8444`（或以 `--admin-origin` 显式声明的那一个），不匹配直接 `403 ORIGIN_MISMATCH`；缺失 `Origin` 的非导航请求（curl、脚本）放行但要求 `Authorization` 而非 cookie。再叠一层 `Sec-Fetch-Site: same-origin` 校验（现代浏览器必带）。
- 写操作仍要 `X-CSRF-Token`（双提交），作为 Origin 校验失效时的兜底。
- **推荐部署（host 隔离，比只靠 Origin 校验更硬）**：管理面固定 `127.0.0.1:8444`，数据面同时接受 `127.0.0.1` 与 `localhost` 两个 DNS 名（同一个 listener，不额外开端口），webui 固定用 `localhost:8080` 访问。于是 `__Host-ac_admin` 落在 `127.0.0.1` 域、`__Host-ac_ui` 落在 `localhost` 域，**host-only cookie 天然不互通**。
  > 注意这里的对应关系：**webui 的 host 必须等于它访问数据面时用的 host**（都是 `localhost`），否则 `SameSite` 会把 `ac_ui` 拦掉；admin 的 host 则必须是**另一个**。三者不是「三个都不同」，而是「admin 单独一个，webui 与数据面共用另一个」。

**② `/app` 不再由 core 托管。**

- 默认：core **不挂载** `/app`，webui 静态资源由 `ui` 模块自己 serve（模块本来就能独立运行，FR-5.2）。
- 可选：`core --serve-ui <dir> --ui-listen 127.0.0.1:8080`，**显式开启**才生效，且强制独立端口。启动时打印「内置 webui 宿主已启用，origin=http://127.0.0.1:8080（与管理面不同源）」。这个开关存在只是为了降低本地试用门槛，生产不建议开。
- 修订 FR-8.9：原写「webui 由 core 托管静态资源（同源）」，改为「由 `ui` 模块自托管；core 可提供显式开启的独立端口宿主，默认关闭」。

**③ `/ui/session` 留在数据面，因此需要 CORS（且只对它需要）。**

浏览器在 `:8080` 的页面里 POST 到 `:8443/ui/session`，是跨源请求：

- 数据面**只**对 `[server] ui_origins` 白名单回 `Access-Control-Allow-Origin`（精确匹配，禁 `*`，必需 `Access-Control-Allow-Credentials: true`）。
- 管理面**永远不**回 CORS 头。
- `__Host-ac_ui` cookie 必须 `SameSite` 判定的坑：`127.0.0.1:8080 → 127.0.0.1:8443` 属于 **same-site**（same-site 不含端口），`Strict` 仍会发送；但 `localhost:8080 → 127.0.0.1:8443` 是 **cross-site**，`Strict`/`Lax` 都不会发送。所以约束是：**webui 页面与它访问的数据面必须同 host、仅端口不同**，否则 `__Host-ac_ui` 要退化成 `SameSite=None`，那等于放弃 CSRF 防护。启动时校验 `ui_origins` 里的 host ∈ `[server] listen_hosts`，不一致直接拒绝启动（不给「能跑但会静默失效」的中间态）。

  这三条合起来是一张固定表，不是三套可选方案：

  | 谁 | host | 端口 | cookie | 与服务端的关系 |
  |---|---|---|---|---|
  | 管理面 | `127.0.0.1` | `8444` | `__Host-ac_admin` | 独立 origin |
  | 数据面 | `localhost`（也被 `127.0.0.1` 访问） | `8443` | 无 cookie，走 Noise | 模块接入 |
  | 模块 app（webui） | `localhost` | `8080` | `__Host-ac_ui`（设在 `localhost`） | 跨 origin、same-site |


#### 7.1.3 管理面鉴权：密码 + 设备密钥（v1.11 引入；v1.15 换设备密钥；v1.16 收紧只读判定）

**没有一次性 token 了。** 一次性 token 打在 stdout 上，实际使用中会被截屏、进 CI 日志、被 `docker logs` 收走，而且它只有一个因子。改成密码 + 设备密钥。

| 凭据 | 来源 | 需要 | 权限 |
|---|---|---|---|
| **Bearer**（App 内 WebView / 原生端） | 任意 | **密码 + 设备密钥挑战签名** | **可写**。两个因子缺一不可；密码泄露而没有那台设备的私钥仍然进不来 |
| **Bearer**（本机 CLI / 脚本） | 回环 + `X-AC-Client: cli` | 仅密码 | **可写**。本机已是受信环境；这个头只为把 `curl` 与浏览器区分开，浏览器不发它 |
| **cookie**（浏览器·**管理面**） | **任意，含回环** | 仅密码 | **只读**：能看状态/拓扑/审计/记忆列表，不能改配置、不能装技能、不能管设备（§7.10）。数据面 `__Host-ac_ui` 按 D100 可写（带 CSRF token，见 §7.1.3 注） |
| 无（首次配置） | — | 回环免 token；非回环先验 setup token | 只能完成 setup / 绑定（§7.1.4） |

> **权限由凭据类型决定，不由来源决定。** `Authorization: Bearer` 才能写，`__Host-ac_admin` cookie 在**管理面**一律只读——包括在 core 本机上开的浏览器（它拿不到 `X-AC-Client: cli` 那条路径）。这条是为了让「只读」成为一个可以静态判定的规则（看头就知道），而不是「看来源 IP 猜」。本机用户要写配置，用 App（会拿 Bearer）或 CLI。

**密码**

- 算法 `Argon2id`，参数 `m=64MiB, t=3, p=1`，随机 16B salt；哈希串按 PHC 格式存 `data/admin/admin.json`（0600），**参数随哈希一起存**，将来调参不影响老密码。
- 最短 **12** 字符；不强制大小写/符号组合（强制组合只会逼出 `Passw0rd!`），改为拒绝常见弱密码表前 10k 条。
- 比较用 `hmac.compare_digest`，**不区分「密码错」与「无此用户」**——管理面只有一个账户，但错误码必须统一为 `AUTH_FAILED`，避免泄露「是否已初始化」。
- 改密码需要旧密码；忘记密码走本机 `core admin reset-password`（要求对 `data/` 有写权限，等价于已是宿主用户，远程无法利用）。
- 日志、审计、错误响应里**永远不出现密码**，连长度都不记。

**设备密钥（core 不签发任何东西）**

先明确一句，避免和「core 持有私钥」混淆：

> **core 只持有自己的三把私钥**（TLS 证书私钥 / Noise 静态私钥 / **管理面身份私钥 Ed25519**），**不充当 CA，不签发证书，不生成设备的密钥对**。设备密钥对由 App 在设备上生成，私钥进系统安全区、**永不导出**；交给 core 的只有公钥。

**core 身份密钥（v1.15 新增，绑定流程的前提）**

`data/admin/core_identity.key`：Ed25519，首次启动生成，`0600`，**永不变**。`fp8 = SHA-256(pub)[:16]` 就是用户在 App 里核对的那串字。

为什么不用 TLS 证书指纹当 pin：TLS 证书会轮换（ACME 90 天，或换域名），pin 一断，所有已绑定设备全部失效——用户看到的是「突然要求重新配对」。把「服务端身份」和「TLS 传输安全」解耦，pin 才能长期稳定。TLS 仍然要校验（§7.11 第 7 条），但它不再是身份锚点。

`core --print-fingerprint` 打印的就是这把的 `fp8`，与二维码、setup 页大字显示的**同一个值**。

**算法选 P-256，不是 Ed25519 —— 这是硬件决定的，不是偏好。**

| 平台 | 硬件密钥库 | 支持的签名算法 |
|---|---|---|
| iOS | Secure Enclave | **仅 P-256（ECDSA）** |
| Android | StrongBox / TEE（Keystore） | EC P-256 普遍可用；Ed25519 要 Android 13+ 且依赖厂商实现 |
| 桌面/CLI | 无硬件 | 都可以 |

v1.14 写的是「用 `ssh-keygen -t ed25519` 生成」，那条在「用户有电脑」的前提下成立，一旦要求「私钥不能导出、必须进安全硬件」，Ed25519 就不可用了。所以：

- **主路径**：`ECDSA P-256 + SHA-256`，`hardware_backed=true`（`Secure Enclave` / `StrongBox` / `TEE`），私钥 `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` / `setUserAuthenticationRequired(false)`（管理面要能后台刷新，不做生物识别强绑；生物识别在 UI 层做二次确认）。
- **兼容路径**：`Ed25519`，只用于**软件密钥**（桌面 CLI、救援、CI）。core 两种都验。
- 设备表 `data/admin/devices.json`（`0600`），条目带 `alg` 字段（`p256` / `ed25519`），签名校验按 `alg` 分派，**不靠猜**。`core admin trust-key` 从 `~/.ssh/*.pub` 导入时转成同样的结构，`alg=ed25519`、`hardware_backed=false`。
- ECDSA 签名必须校验 **low-S**（拒绝 `s > n/2`），否则签名可延展——同一条 challenge 能被改出一个「不同但有效」的签名，破坏一次性语义。

**三条登记路径**

| 路径 | 前提 | 场景 | 私钥在哪 |
|---|---|---|---|
| **① App 绑定**（主路径） | 有 setup token / 绑定票，且已导入 core 公钥 | 首次配置、加新手机 | 设备安全区，不可导出 |
| **② 已登录会话内加设备** | 现有设备已登录 + **step-up 重输密码** | 换手机、加平板 | 同上 |
| **③ 本机 CLI**（救援） | 能在 core 主机执行命令 | 全丢设备、初始化、救援 | 用户自己的文件 |

② **不是匿名入口**：上传前必须先通过现有因子登录，再重输一次密码。攻击者只有公钥没有密码进不来；只有密码没有已绑定设备，非回环也进不来（②要求本来就是已登录态）。

- **step-up 必须做**：会话被借用（WebView 里的 XSS、遗留标签页）时，攻击者能发请求但不知道密码，种不进设备。这是这条路径唯一的安全承重点。
- 每账户上限 **5 台设备**，超出拒绝而不是自动淘汰（淘汰哪台由人决定）。
- 存储 `{device_id, label, alg, pubkey, fp8, hardware_backed, added_at, added_from_ip, last_used_at}`；`device_id = SHA-256(pubkey)[:16]`，UI 只显示 `label + fp8 + 是否硬件`。
- 吊销：`POST /admin/api/devices/{id}/revoke` 或 `core admin revoke-device --id`，立即失效并踢掉该设备换发的所有会话。
- 新设备**立即生效**，不留冷静期。代价如实说：密码泄露 + 攻击者能登录时，他可以种一台长期设备。所以 `/settings` 必须把「所有设备 + 添加时间 + 最后使用时间 + 来源 IP + 硬件/软件」摊开给人看。
- **软件密钥必须标出来**：`hardware_backed=false` 在列表里显示为黄色标签「软件密钥，可被提取」。不阻止使用（桌面没有安全区），但不允许它悄悄混在硬件密钥里。

**登录时序（已绑定设备）**

```
1. POST /admin/api/login/challenge
   → {challenge_id, challenge: base64(32B), expires_at,
      devices:[{device_id, label, fp8, alg}]}
   challenge 存服务端：60s、一次性、绑定 (challenge_id, 来源 IP /24, 设备表版本)
   **不绑 TLS session id** —— HTTP/2 连接池、TLS 1.3 resumption、LB 终止都会让它
   要么每次不同（登录随机失败），要么所有客户端相同（等于没绑）。

2. App 原生层用设备私钥签名（不经过 WebView 的 JS，见 §7.11）：
   sig = sign("vade-admin-v2\n" || challenge || "\n" || admin_origin)

3. POST /admin/api/login {challenge_id, password, device_id, signature}
   → 先验密码（常数时间），再验签名
   → 两者都过：有设备签名（或回环 + X-AC-Client: cli）→ access_token(15min) + refresh_token(30d)
     浏览器来源（无签名、无该头）→ 只设 __Host-ac_admin cookie（**管理面**只读），响应体不含 token
   → 任一失败：401 AUTH_FAILED，challenge 立即作废

4. 回环来源可省略 device_id/signature（但仍必须密码）
```

签名对象里带 `admin_origin` 是为了防「把 A 站点的 challenge 拿到 B 站点用」；固定前缀 `vade-admin-v2` 是域分离（v1→v2 因为签名算法从 Ed25519 换成 P-256，前缀换掉可以杜绝旧签名被重放）。

对外绑定时，`admin_host` **必须显式配置**成实际访问用的 `host:port`（默认只有 `127.0.0.1:8444`）；`Origin` 与 `Host` 都按这个值严格比对。用 IP 直连就把 IP 写进去，用域名就写域名，**不要留空表示「接受任意」**。

**限流与锁定**

| 维度 | 阈值 | 动作 |
|---|---|---|
| 同 IP 密码失败 | 5 次 / 5 分钟 | 该 IP 锁 5 分钟（`429`，不告诉还剩几次） |
| 全局密码失败 | 20 次 / 5 分钟 | **关闭登录入口 15 分钟**，写 `admin_lockout` 审计；已登录会话不受影响 |
| challenge 请求 | 30 次 / 分钟 / IP | 只挡刷 nonce，不锁账户 |
| 签名失败 | 计入密码失败同一计数 | 不区分失败类型，防探测「密码对了但钥匙不对」 |

**「浏览器拿不到私钥」这条约束仍然成立，但结论反了（v1.15 反转）**

浏览器没有裸签名 API（只有 WebAuthn，那是另一套信任模型）——这句话没错。v1.14 由此推出「所以走 SSH 隧道，让 SSH 承担公钥因子」，这个推论错在**把开发者的环境当成了用户的全部环境**：手机上根本没有 SSH 客户端，云主机也不一定有可用的 SSH 入口。一个「必须先有电脑才能配手机」的方案，在「用户只有手机」这个真实场景下等于不可用。

**先把那个反问正面答了：设备被入侵的话，浏览器不也被入侵了吗？**

是。**在「设备已经失守」这个维度上，WebView 和系统浏览器没有本质差别**——两者的进程隔离、沙箱、渲染引擎都是同一套，都存在被提权的可能。所以不能拿「更安全」当 WebView 的理由，那是假的。

WebView 真正的收益是另外三条，都跟「设备失守」无关：

| 收益 | 具体是什么 | 浏览器为什么做不到 |
|---|---|---|
| **能力** | 原生层调 Secure Enclave / StrongBox 做签名，**私钥不可导出**；签名前可以弹原生确认框（显示 origin + 摘要），JS 无法伪造 | 浏览器里 JS 拿不到私钥，也没有裸签名 API；WebAuthn 是另一套凭据模型，不能复用设备密钥 |
| **可控** | 加载什么资源、能连哪些 origin、证书怎么 pin、cookie jar 是否独立，全部由我们的代码决定 | 浏览器里这些是用户设置与厂商策略，我们说了不算 |
| **身份** | 每次打开都是**同一台设备**，有稳定的设备身份，双向同意才有意义 | 浏览器没有设备身份，每次都是匿名会话，只能靠 cookie 区分 |

反过来，WebView 也有一个浏览器没有的**新增风险**，必须靠工程约束抵消：**加载远程 HTML + 暴露 JS 桥 = XSS 直接升级成本地代码执行**。这条在 §7.11 用硬约束封死（只加载 App 内置页面，core 只给 JSON，桥接白名单 + 原生确认）。

**三条路径，按推荐顺序**

| 路径 | 做法 | 因子 | 权限 |
|---|---|---|---|
| **① App 内 WebView（主路径）** | 自研 `ui` 模块内嵌 WebView 打开管理页；页面资源随 App 包分发；签名由原生层完成 | 密码 + 硬件设备密钥 | **全功能** |
| ② `core admin proxy`（开发者） | 本机跑代理，本地 `9444` → 远程 `8444`，代理用本地软件密钥代签 challenge | 密码 + 本地软件密钥 | 全功能 |
| ③ 系统浏览器（降级） | 直接开 `https://<core>:8444/admin` | **仅密码** | **只读** |

- ① 覆盖「只有手机」的全部场景：首次绑定扫码即可（§7.12），不需要电脑、不需要 SSH、不需要 `ssh-keygen`。
- ② 只给脚本、CI、桌面运维用。它要求本机能跑 Python 且持有软件密钥——不假装这是给普通用户的。
- ③ **明确降级为只读**：能看状态、拓扑、审计、记忆列表、技能列表，**不能写**。不渲染「看起来能点但点了 403」的按钮——按只读模式渲染，顶部一条「当前为只读模式，请用 App 打开以进行配置」。这条比「给个用不了的双因素表单」诚实。
- **SSH 隧道从主路径删除**，降为「开发者可选」。理由写进 §11 被否方案：它把「配置」这件事和「有一台装了 SSH 客户端的电脑」绑定，而这个前提在目标用户里不成立。

**为什么不用 mTLS**：mTLS 要 core 当 CA 签发客户端证书，或至少维护一份证书信任库——这直接违反「core 不签发」。而挑战签名只依赖一把裸公钥，没有证书格式、SAN、有效期、吊销列表这一整套负担。代价是公钥登记必须带外完成，这恰好也是我们想要的（不允许远程自助）。

**为什么这里可以引入 P-256（与「不引入第二套密钥」不冲突）**：模块与设备链路的身份认证由 Noise IK 完成，那里 **X25519 只做 DH、不做签名**，所以不需要签名密钥。管理面是 HTTP，没有 Noise，认证只能靠挑战-响应签名，而 X25519 不是签名算法。**两个场景不同，不是重复建设**：一套用于通道建立（X25519），一套用于 HTTP 认证（P-256/Ed25519）。

#### 7.1.4 首次配置（setup）：回环直配 / 非回环凭 setup token

**setup 模式**（`data/admin/admin.json` 不存在时）。v1.12 改了准入规则：原来「只回环 + 30 分钟硬超时」在云端不成立——容器在公网上，管理员在本地，根本没有回环可用；硬超时还会让「先起容器、过两天再配」的流程每次都要 `reset`。

**两条通道，按来源分流：**

| 来源 | 准入 | 理由 |
|---|---|---|
| 回环（`127.0.0.1` / `::1`） | 直接进 setup 页面，**不要 token** | 能打回环说明已经在宿主上，等价于已有 root |
| 非回环 | 必须提交 **setup token** | 公网上「谁先打开谁当管理员」，必须有带外凭据 |

- `GET /admin/setup`：未初始化时 200。**响应体按来源分流，这是 v1.16 的修正**：

  | 来源 | 返回 | 为什么 |
  |---|---|---|
  | 回环 | `{setup_required: true, requires_token: false, bind_url, fp8}` | 已在宿主上，等价 root |
  | 非回环、未解锁 | `{setup_required: true, requires_token: true}` —— **只有这两个字段，没有 `bind_url`、没有 `fp8`、没有 token** | 见下 |
  | 已初始化 | **302 → `/login`**（不是 404，避免死页面） | —— |

  > **为什么非回环 GET 不能下发二维码（v1.16 修的漏洞）**：v1.15 让 setup 页无条件渲染含 `t=<setup_token>` 的二维码。只要 8444 对外，任何扫到或抓到这张页面的人都能拿到票，用自己的设备密钥 `POST /admin/bind`、**顺手设一个初始密码**，完成接管。这不是「二维码被拍照」的弱风险，是「打开网页就拿到」。所以入口闸必须放在**二维码之前**。

- `POST /admin/setup/unlock`：`{setup_token}` → 验过则设 `__Host-ac_setup`（10 分钟、HttpOnly、SameSite=Strict，只用于 setup 路由）并返回 `{bind_url, fp8, expires_at}`。**未解锁的会话再调 `GET /admin/setup` 仍只拿到 `requires_token: true`。**
- `POST /admin/setup`：`{setup_token?, password, pubkey?}`。设置密码（两次输入）+ 可选粘贴第一把公钥。**不提供「跳过密码」**。`pubkey` 是给「在电脑上操作」的场景留的；手机路径走 `POST /admin/bind`（§7.12），不需要填这个字段。

**setup 页同时是「绑定入口」——这是「用户只有手机」的关键落点（v1.15 新增，v1.16 加门禁）**

云端容器跑起来后，用户手上只有手机。这时让他手打 47 字符的 token 是不现实的——但**不现实不等于可以不要**。所以顺序是「先证明你拿到了 token，再给你二维码」：

```
手机浏览器 → GET /admin/setup（只看到「请输入访问令牌」）
           → 粘贴 token（来源见下）→ POST /admin/setup/unlock
           → 页面出现二维码 + 大字 fp8
           → App 扫 → 核对指纹 → 设密码 → 完成
```

```
vade://bind?h=<host>&p=<port>&fp=<SHA256(core_identity_pub)[:16]>&tls=<SHA256(SPKI)[:16]>&t=<bind_ticket>
```

- **二维码里的 `t` 是一次性 `bind_ticket`（10 分钟），不是 `setup_token`。** 渲染二维码时由 core 现场签发，与浏览器表单用的 token 分离：二维码被拍照最多泄露一张 10 分钟的一次性票，不泄露那个还能解锁 setup 表单的凭据。
- **手机怎么拿到 setup token**：① 云控制台看容器日志（core 启动时打 `setup token written to data/setup.token (acs_4f2a…)`，中段打码，只有 `--print-setup-token` 或 `cat` 给全文）；② 云厂商的 Web 终端 / `docker exec`；③ 临时 IP 白名单——把手机出口 IP 加白后，该来源视为已解锁，**不需要 token** 就能看到二维码。三条都不要求「面前有一台电脑」，最差也只需要手机上的云控制台 App。
- 二维码只在**已解锁的 setup 页面**渲染，**且只在未初始化或仍有有效绑定票时存在**，其余时候不出现。
- App 扫码一次拿到三样东西：core 地址、**core 公钥指纹**、绑定票。用户在 App 里核对 `fp`（与 `core --print-fingerprint` 或页面大字显示的一致）后确认，即完成「客户端同意」。
- **core 同意**由绑定票本身表达：票是 core 自己签发的一次性凭据，验过即成立（§7.12）。
- 手输兜底仍然保留：`host` + `fp` + **票（26 字符）** 三个字段可分别粘贴。二维码是体验优化，不是唯一路径——扫码失败时不能卡死。
- 二维码里**不含** core 公钥原文，只含指纹；公钥原文在绑定握手第一步由 core 下发、用指纹校验。真正的防线不是二维码保密（它会被拍照），而是**绑定票一次性 + 用票换到的会话绑定了设备私钥**。
- **指纹核对在「手机 + 自签 + 没有第二块屏」时是锚不住的**：二维码和页面大字都来自同一条 TLS 会话，能替换 `kex_pub` 的攻击者也能同时替换这两处显示。所以这个组合下必须靠另外两条之一：① 用可信证书（ACME）；② 用临时 IP 白名单（攻击者不在白名单里，连页面都拿不到）。文档要明写这条限制，不要让用户以为「核对了指纹就万事大吉」。
- setup 期间 `/admin/api/*` 业务接口**全部 404**，唯一例外是 `/admin/api/health`（只回 `{ok:true}`，供探活，不含任何状态）。
- 成功顺序：**先原子写 `admin.json`（tmp + `rename`）→ 再 `unlink` token 文件**。反过来会出「删了 token 但写密码失败 → 只能 `reset`」的死局；先写后删即使崩在中途，残留 token 也因已初始化而不被接受。
- 已初始化后，`/admin/setup` 不再读 token 文件；**即使文件残留也不认**（判定只认 `admin.json` 是否存在）。

**跳过 setup：编排注入密码（推荐给容器/自动化）**

setup 页面本质是「第一次交互」。如果部署流程本身就能提供密码，这个交互是多余的：

```bash
echo "$ADMIN_PASSWORD" | core admin init --password-stdin --pubkey-file ./admin.pub
# 写 data/admin/admin.json，删 data/setup.token，退出 setup 模式；重启后直接 /login
```

- 必须在 core 停止状态下执行；运行中执行返回「请先停 core」。否则会出现「页面还在 setup、磁盘已经初始化」的分裂状态。
- `--pubkey-file` 可选，但**对外部署时强烈建议带**：一次把首台设备配好，省掉「先登录、再去设置页加设备」这一轮。
- 安全性：能 `exec` 进容器、能读 stdin 的人，本来就等价于 root。这条路径不额外扩大攻击面。
- 密码规则与页面一致（≥12 字符、拒绝弱密码表），不因为走 CLI 就放宽。

有了这条，云端部署就不必在公网上开 setup 窗口；`setup token` 退化为「既不能 exec 注入、也没有安全组可用」时的兜底。

**取消 30 分钟硬超时。** 原来那条的理由是「不要长期开着一个无密码入口」，但入口现在不是无密码的：回环只有本机可达，非回环必须有 256 bit 的 token。替代措施是**可观测**而不是**自毁**：

- 未初始化状态下，每 24h 在日志里警告一次 `admin plane still uninitialized (uptime=<h>)`。
- `core status` 把 `admin.initialized=false` 标黄。
- 对外绑定时若拿不到任何可用 token（见下）→ **拒绝启动**，不允许「开了公网端口但没有准入凭据」的中间态。

**setup token 的形状与保管**

| 项 | 决定 |
|---|---|
| 格式 | `acs_` + 32 字节随机数的 base64url（43 字符），共 47 字符 |
| 强度 | 256 bit，爆破无意义；限流只为压日志噪音，不是安全边界 |
| 存储 | `data/setup.token`，`0600`，目录 `0700` |
| 生成 | **每次启动、只要仍未初始化，就生成新 token 覆盖旧文件**（D59）；`--print-setup-token` 才把明文打到 stdout |
| 默认输出 | stdout 只打 `setup token written to data/setup.token (acs_4f2a…)`，**中段打码** |
| 覆盖 | 环境变量 `VADE_SETUP_TOKEN`：**仅当文件不存在时生效**，之后写回文件；用于 Secret 注入 |
| 消费 | setup 成功后 `unlink`；已初始化时启动顺手删除残留；`core admin reset` 重新生成；`core admin print-setup-token` 本机读 |
| 传输 | **只允许 POST body**。禁止 query string（会进 access log、Referer） |
| 比较 | `hmac.compare_digest`；不受理截断、大小写、前后空格 |
| 失败 | 统一 `401 SETUP_UNAUTHORIZED`，**不区分**「没带」「带错」「已过期」 |

`VADE_SETUP_TOKEN` 走环境变量有代价：`/proc/<pid>/environ`、`docker inspect`、部分编排系统的事件日志都会带出来。所以**文件是默认路径，env 只是显式覆盖**——顺序写死成：

```
① 文件 data/setup.token 存在        → 用它（推荐：K8s Secret 挂成文件、docker -v 挂卷）
② 否则 VADE_SETUP_TOKEN 非空   → 用它，并写回文件
③ 都没有                            → 生成新 token 写文件
```

②之后写回文件是刻意的：让「注入一次」和「cat 一次」两条路径都能用。

**每次启动轮换（D59）**，只在「无 env」时成立：

```python
def ensure_setup_token(data_dir, env_value, initialized):
    p = data_dir / "setup.token"
    if initialized:
        if p.exists():
            p.unlink()                     # 顺手清残留，不报错
        return None
    if env_value:                          # 用户把值钉在环境里 → 不轮换
        atomic_write(p, env_value, 0o600)
        return env_value
    tok = "acs_" + b64url(os.urandom(32))  # 每次启动都是新的
    atomic_write(p, tok, 0o600)
    return tok
```

为什么轮换：上一轮留的是「文件在就沿用」，于是卷被 `docker cp` / 备份 / 误挂载复制出去之后，那个 token 永久有效——而它恰好是「谁能当管理员」的钥匙。轮换成「重启即换代」，把凭据的有效期压到一次进程生命周期。

代价如实写：**setup 未完成前不要重启容器**，重启后必须重新 `cat` 一次。这条进 runbook，不藏着。

一个必须配套的约束：**同一个 `data/` 只允许一个 core 进程**（`flock data/core.lock`）。两个进程同时跑，会互相覆盖 token，谁 cat 到的都不算数。

**云端 setup 的 TLS 前提（必须让用户看见）**

token 和密码都在 TLS 里传。自签证书被中间人替换时，攻击者能同时拿到**两个因子**——这比远程登录更严重，因为 setup 一次就等于永久接管。

- 云端**优先用可信证书**（ACME / 云负载均衡托管证书）。有可信证书就不需要核对指纹。
- 只有自签时才核对 TLS：`GET /admin/setup` 显示 SPKI 指纹 `SHA-256(SPKI)[:16]`，与 `core --print-tls-fingerprint` 对照。**这与身份指纹 `fp8` 是两个值**，不要混（见下）。
- 身份指纹 `fp8 = SHA-256(core_identity_pub)[:16]`，由 `core --print-fingerprint` 打印，与二维码、页面大字是同一个值。**它不用于 TLS 校验**，只用于「我要连的是哪台 core」。
- 更硬的替代方案：**先用安全组/防火墙把自己的出口 IP 临时加白**，setup 完再撤。token 是兜底，不是唯一手段。这条写进部署清单，因为它的攻击面比 token 小一个数量级。

**登录阶段沿用同一套 TLS 判断**：管理面没有 Noise，密码是在 TLS 里传的；TLS 被替换，密码就是送出去的。自签证书首次访问时浏览器必然告警，**这不是可以点「继续」就过去的提示**。登录页与 setup 页同样固定显示 `SHA-256(服务端证书)[:16]`。此外：

- core 侧同时校验 `Origin` 与 `Host`：`Host` 必须是显式配置过的 `admin_host`（默认 `127.0.0.1:8444`），防 DNS rebinding。
- 设置 HSTS（`max-age=31536000`，不带 `includeSubDomains`、不带 `preload`）。

### 7.2 拓扑数据模型

画布不是独立的图数据库，它是**注册表的投影**。core 侧只维护两样东西：注册表的实时状态，加一张布局表。

```
node = {
  id:            "com.example.fs#a1b2",     // module_id + instance_id
  kind:          "module" | "device" | "core" | "pending",
  module_id:     "com.example.fs",
  instance_id:   "a1b2",
  label:         "文件系统",                 // 别名，L1 可改
  capabilities:  ["tools", "prompts"],       // 类型，决定图标与分组
  state:         "online"|"degraded"|"offline"|"rejected"|"pending",
  endpoint:      "wss://10.0.1.7:9101",      // 只到这里，不含任何凭据
  protocol:      "vade/v1",
  version:       "1.0.0",
  auth_mode:     "module_key"|"device_key"|"browser_session",
  rtt_ms:        18,
  last_seen:     1762526000,
  x: 320, y: 180,                            // L1 布局
  revoked:       false
}

edge = { from: "core", to: node.id, state, since, reason? }
```

**edge 的 `from` 永远指向 core。** 模块之间没有边——它们之间不允许直连（FR-5.7）。画布上若出现两条模块之间的线，那是 UI bug，不是拓扑。

#### 7.2.1 节点粒度：默认模块级（D35）

一个模块可能带 4～16 个工具（`mcp-bridge` 甚至几十个）。若按工具级渲染，画布会变成 30+ 个小方块的毛线团，运维看不出「哪些机器连上来了」——而后者才是这张图的唯一目的。

| 决定 | 内容 |
|---|---|
| **默认粒度** | 一个 `(module_id, instance_id)` = 一个节点，不展开工具 |
| **工具位置** | 节点右侧属性面板列出 `tool_names[]`，可折叠；画布上只显示 `tool_count` |
| **展开方式** | 双击节点 → 侧栏展开工具列表（仍是列表，不生成子节点） |
| **不做** | 不做工具级节点、不做嵌套子图、不做模块间边 |
| **例外** | `mcp-bridge` 工具数 > 20 时同样只显示计数，侧栏分页 |

理由：拓扑回答的是「连接健康度与信任关系」，工具清单回答的是「这个模块能干什么」，两者受众相同但阅读场景不同。混在一张图上，两边都读不清。

工具级视图排在 v2 的「能力目录」页，不进 v1（与 D32 的边界一致：配置页只管连接，不管能力编排）。

### 7.3 状态判定与推送

| 状态 | 判定 | 触发 |
|---|---|---|
| `online` | 握手完成 + 最近 `2×heartbeat_interval` 内有心跳 | 注册 / 心跳恢复 |
| `degraded` | 连接在，但熔断打开或超时率 > 20% | 熔断器状态变化 |
| `offline` | 已配对但无连接；或连接断开未重连 | 断连 / 心跳超时 |
| `rejected` | 握手认证失败、版本主号不匹配、被吊销后仍尝试连接 | 握手失败 |
| `pending` | 邀请位节点，尚无对应连接 | 管理员创建邀请位 |

`degraded` 是我加的第五个状态——只有「在线/离线」两态会把「连上了但一直超时」显示成绿色，那是最误导运维的一种显示。

推送为 **增量事件**（`node.upsert` / `edge.state` / `node.remove`），带 `seq`；浏览器断线重连后带 `last_seq` 补齐，与 UI 模块的 replay 机制复用同一套序号逻辑。

### 7.4 编辑的三层语义

| 层 | 能做什么 | 生效方式 | 审计 |
|---|---|---|---|
| **L1 布局** | 拖位置、改别名、分组、折叠 | 立即（仅布局） | 不需要 |
| **L2 治理** | 启用/停用、吊销、断开重连、覆盖并发模型、改 risk 覆盖、看权限范围、生成配对码 | 立即影响运行态 | **必须** |
| **L2.5 邀请位** | 画布投放占位节点 → 出二维码与配对码 → 模块接入后原地转实心 | 等待态 | 必须 |
| **L3 编排** | 在画布上新建模块并由 core 拉起进程 | — | **不做** |

**L3 不做的理由**：core 不知道模块在哪台机器、用什么方式启动、依赖什么环境；一旦 core 承担拉起职责，它就从「消息中枢」变成「集群调度器」，用户必须把 SSH 凭据或 K8s token 交给它——这与「私钥只在 core」的安全模型方向相反，且把故障域从「一个模块挂了」扩大到「编排器挂了全挂」。跨机部署继续交给 compose / k8s，配置页只负责**把已经连上来的东西管好**。

L2 的破坏性操作（停用、吊销）确认弹窗必须算出**影响面**，不是问「确定吗」：

> 停用 `com.example.fs` 将移除 4 个工具（`read_file` / `write_file` / `list_dir` / `search_files`），当前会话不受影响，下一轮对话起模型将看不到它们。

### 7.5 出参字段白名单（硬约束）

`/admin/api/*` 不允许把注册表内部结构直接 `json.dumps` 出去。所有出参经 `to_public_view()` 转换，字段白名单固定：

```
node: id, kind, module_id, instance_id, label, capabilities, state,
      endpoint, protocol, version, auth_mode, rtt_ms, last_seen,
      x, y, revoked, tool_count, tool_names[]
```

明确**不在**白名单里的：私钥、公钥原文（仅允许短指纹）、会话密钥、PSK、配对码明文（创建时一次性返回，之后只存 `argon2id(normalize(code))`）、上游 MCP 的认证头、`manifest` 原文（其 `extensions` 可能藏凭据）。

`endpoint` 只保留 `scheme://host:port`：查询串与 userinfo 一律剔除（`wss://user:pass@host` 这种 URL 能带凭据）。

对应测试（**必须是真的会失败的用例**）：

```python
def test_admin_api_has_no_secrets():
    for path in ["/admin/api/nodes", "/admin/api/audit", "/admin/api/stream"]:
        body = client.get(path).text
        assert not re.search(r"(private|secret|psk|password|token|credential)", body, re.I)
        assert not re.search(r"[A-Za-z0-9+/]{40,}={0,2}", body)   # 长 base64 串
```

### 7.6 布局持久化与并发

| 项 | 决定 |
|---|---|
| 存储 | SQLite `layout(node_id PK, x, y, label, group_id, rev)` |
| 写回 | `PATCH /admin/api/layout`，body 带 `rev` |
| 冲突 | `rev` 不匹配 → `409`，返回服务端最新布局，前端提示「另一台设备已修改，已为你刷新」 |
| 默认布局 | 力导向预计算一次后落库；「自动排列」按钮可重算 |
| 上界 | 200 节点 / 500 边；超出切列表视图（不渲染画布） |

不用 OT/CRDT：布局是低冲突场景，乐观锁足够，引入 CRDT 是过度设计。

### 7.7 画布的视觉编码

| 维度 | 编码 |
|---|---|
| 状态 | 颜色 + **线型**双通道（绿实线 / 黄流动虚线 / 灰虚线 / 红点线 / 蓝点线） |
| 节点类型 | 图标 + 边框形状（工具=方形、模型=六边形、记忆=圆柱、桥=双框、设备=圆角） |
| 加密强度 | 连线上一个锁标：实心锁 = 端到端（Noise），空心锁 = 仅 TLS（浏览器） |
| 地址 | 节点副标题，等宽字体，超长省略中间段（`wss://10.0.1.7:91…01`） |
| 分组 | 同一 `module_id` 的多实例用虚线外框归组（手机 + 电脑同属 `ui`） |

颜色不是唯一编码，因为运维里色弱不罕见，且截图转灰度后仍需可读。

### 7.8 与「UI 是模块」的关系

配置页是 core 内置的**运维界面**，`ui` 模块是**用户界面**。两者技术栈不同、受众不同、边界不同：

| | 配置页 | `ui` 模块 |
|---|---|---|
| 谁用 | 管理员 | 终端用户 |
| 技术 | React + React Flow（Web） | Compose MP / SwiftUI |
| 部署 | core 内静态资源 | 独立进程 / 独立安装包 |
| 内容 | 拓扑、治理、审计 | 对话、会话、审批 |
| 断线影响 | 不影响 Agent | 用户看不到回复 |

移动端不做拓扑**画布编辑**：小屏上拖拽连线是反人类设计，且运维场景基本在桌面。但**列表、技能、记忆、设置必须能在 375pt 下操作**——`ui` 就是用 WebView 打开同一份页面，权限上没有降级（FR-9.15，AMEND-17 修订）。只有拓扑画布降级为节点列表。

### 7.9 v1 可配置项清单（能力边界）

配置页不是「core 的设置程序」。它管三件事：**连上来的东西（拓扑/治理）**、**模型能用什么（技能）**、**它记住了什么（记忆）**。这三件之外一律不进页面。

**原则**：能在不重启、不破坏在途状态的前提下改的，进页面；需要重启才生效的（监听地址、TLS 证书、存储路径），一律改文件 + 重启，页面**只读展示，不给编辑框**。

#### 可以改（模块治理，写入即生效，必审计）

| # | 项 | 粒度 | 约束 |
|---|---|---|---|
| 1 | 启用 / 停用模块实例 | 实例 | 停用即从工具表移除；在途 turn 不受影响 |
| 2 | 并发模型覆盖 | 实例 | `exclusive` / `pooled` / `mirrored`（与 manifest 声明同一枚举，见 §2.3），覆盖实例的声明值 |
| 3 | risk 覆盖 | 实例 | **只能上调**；下调被 `PERMISSION_DENIED` 拒（D47） |
| 4 | 吊销模块 | 实例 | 需重新配对；踢掉现有连接 |
| 5 | 断开重连 | 实例 | 不断信任，只断连接 |
| 6 | 创建 / 撤销邀请位 | 邀请 | 码一次性、180s |
| 7 | 改管理面密码 | 账号 | 需旧密码；成功后吊销其它会话 |
| 8 | 登记 / 吊销登录设备 | 账号 | 已登录 + **step-up 重输密码**；上限 5 台；软件密钥黄色标注；见 §7.1.3 |

#### 可以改（技能管理）

| # | 项 | 说明 |
|---|---|---|
| 1 | **安装** | 三种来源：上传 `SKILL.md` 单文件 / 上传 `.zip` / 从 https URL 拉取。落盘到 `skills/pending/<name>/`，`status=pending`，**不进工具表、不进 prompt**；人工批准后才 `active`（v1.15 改，见下） |
| 2 | 卸载 | 移入 `skills/.trash/<name>-<ts>/`，**可恢复**，不物理删 |
| 3 | 启用 / 禁用 | 只改 `meta.json` 的 `status`，**不改 frontmatter 原文** |
| 4 | 审核提案 | `pending/` → `active` / `.rejected/`；支持 rollback（D45） |
| 5 | 查看 | 当前 body、`revision`、历史摘要、`pending` 提案的 diff |
| 6 | 手动重扫 | 等价 `SIGHUP`，用于手工丢文件后刷新 |

**安装为什么必须先进 `pending/`（v1.15 改，覆盖 v1.14 的「装完即 active」）**

v1.14 的理由是「管理员主动装的，就是背书过的」。这个理由只在「管理员确实看过内容」时成立，而实际上：

- 上传 `.zip` 时，管理员看到的是**文件名**，不是里面那份 64KB 的 `SKILL.md`。skill 是**直接进 system prompt 的文本**，本质是「高权限提示注入载体」——一次「先装上试试」就等于把一段别人写的指令塞进了模型上下文。
- 从 URL 安装更危险：内容在下载那一刻才能确定，`preview()` 看到的内容和 `install()` 落盘的内容之间，若没有 hash 绑定就可以被换掉。
- 「装完即生效」让**批准**这个动作消失了。而批准是唯一能让人真正读一遍的机会。

所以安装流程改成三步，**中间那步不能省**：

```
preview()  → 返回 preview_token（绑 name + sha256(内容)），不落盘
install()  → 必须带对的 preview_token，落盘到 skills/pending/<name>/，status=pending
approve()  → 人工在 /skills 页点「启用」，status=active，写 .history/，热加载
```

- **`pending/` 里的 skill 绝对不参与注入**：`SkillLoader` 扫描时显式排除 `pending/`（与 C3d 的提案区同一个目录，靠 `meta.json` 的 `origin` 区分 `install` / `proposal`）。
- 两个来源共用 `pending/`，但 UI 分两栏：**「待安装确认」**（`origin=install`，展示完整正文 + 来源 + sha256 + 文件清单）与**「待审核修订」**（`origin=proposal`，展示 diff）。
- **`pending/` 有 7 天 TTL**，超时移到 `.trash/` 并写审计。理由是磁盘上不该无限堆积没人批准的内容。
- **安装期不执行任何脚本**：skill 只有文本与资源文件，没有 `post_install`、没有 `package.json`、没有 `setup.sh`。zip 里出现可执行文件直接拒绝（不是忽略，是拒绝——忽略会让人以为装上了）。
- **版本更新同样回 `pending/`**：任何 `sha256` 变化都重新走三步。唯一例外是管理员在 `/skills` 对该技能勾选「信任此来源的更新」**且**该技能带签名（`SKILL.md.sig` 用发布者密钥签），此时自动通过但仍写审计。
- `requires_tools` 缺失时允许装（标 `unavailable`），但**批准按钮旁必须显示缺哪个工具**——不然装完不生效，人会以为是 bug。

**安装的安全约束**（skill 是直接进 system prompt 的文本，本质是「高权限提示注入载体」）：

- 只接受 `.md` 或 `.zip`。zip 内必须**恰好一个** `SKILL.md`，目录深度 ≤ 2。
- **防 zip-slip**：解压前逐个 entry 校验，拒绝 `..`、绝对路径、符号链接。用 `zipfile` 时**不要**直接 `extractall`。
- 上限：单文件 ≤ 64KB，zip ≤ 1MB，解压后总和 ≤ 5MB，条目数 ≤ 32。
- 从 URL 安装只允许 `https`；**禁止**回环 / 私网 / 链路本地 / 云元数据地址（`169.254.169.254`），重定向 ≤ 3 跳且每跳重新校验——否则这是一个现成的 SSRF。
- 名称冲突 → 拒绝，不静默覆盖。
- 安装前必须**预览 frontmatter 与正文**再确认，不能「传完就生效」。
- `requires_tools` 不存在时**允许安装**，标 `unavailable` 并在列表里显示缺哪个工具；不允许因此拒绝——工具可能稍后才接上。

#### 可以改（记忆管理）

| # | 项 | 说明 |
|---|---|---|
| 1 | 浏览 | 按 `layer`（session/global）/ `namespace` / `status`（staging/active/archived/superseded）/ 时间 / 关键词分页 |
| 2 | 单条详情 | 内容、来源指针（`session_id` / `turn_id`）、`created_at`、`expires_at`、`hit_count`、`supersedes` |
| 3 | 晋升 staging → active | **这就是 D43 的「显式确认」通道**，没有它，staging 只能靠「≥2 个会话」自动晋升 |
| 4 | 归档 / 恢复 | `archive(reason)`，可恢复；不物理删 |
| 5 | 配额用量 | global 当前条数 / 上限（5000）、staging 待处理数 |

**边界（防止配置页退化成聊天记录浏览器）**：

- 只显示**记忆条目**，不显示原始对话；来源指针只到 `turn_id`，**不提供「跳到完整对话」**——那是 `ui` 模块的事。
- **不允许编辑记忆原文**。记忆是观察记录，改了就没有溯源价值；要改就归档旧的、记新的，`supersedes` 链会保留。
- **读不审计**（会爆炸），**写审计**（晋升 / 归档 / 删除）。
- 记忆内容**不进 SSE 广播**，只在分页响应里返回。否则所有 attached UI 都能收到全量记忆。

#### 可以改（记忆与进化策略，AMEND-19）

这一组是 v1.17 新增的**策略面**：不碰内容、不碰存储路径，只调“花多少、多久一次、留多少”。全部**热生效**（下一轮或下一次触发器起用新值），因为改的都是读时参数或预算，不破坏在途 turn。

| # | 项 | 键 | 范围 | 生效语义 |
|---|---|---|---|---|
| 1 | 会话注入预算 | `memory.session_inject_char_limit` | 600–20000 字符 | 下轮起按新预算裁剪；**已存记忆不删**，只是不注入 |
| 2 | 召回上限 | `memory.recall_k_max` / `memory.recall_token_limit` | 1–20 / 200–2000 | 下次 `recall` 生效 |
| 3 | 自进化总开关 | `evolve.enabled` | bool | 关闭即退订、不再产生写入；**二次确认**（文案：已有记忆与 skill 保留） |
| 4 | 复盘间隔 | `evolve.nudge_interval` | 5–50 轮 | 下个计数周期生效；页面必须显示「按当前值 ≈ N 次/天、≈ M token/天」 |
| 5 | 短会话门槛 | `evolve.flush_min_turns` | 2–20 | 低于此轮数不调 LLM |
| 6 | 会话写入上限 | `evolve.session_char_limit` | 600–20000 字符 | 超顶返回 `MEMORY_FULL`，evolver 当轮整理 |
| 7 | 日 token 预算 | `evolve.daily_token_budget` | 0–1000000 | `0` = 不限，需**二次确认**并标红；页面显示今日已用/总量 |
| 8 | 提案预算 | `evolve.daily_budget_skills` / `evolve.session_budget_skills` | 0–20 / 0–3 | 超了记 `evolve_budget_exceeded`，不排队 |
| 9 | 压缩参数 | `compaction.threshold` / `target_ratio` / `protect_last_n` | 0.3–0.9 / 0.1–0.4 / 5–50 | 下次压缩生效；改 `threshold` 前提示「会改变费用曲线」 |
| 10 | 目录预算 | `skills.l0_token_budget` | 1000–8000 | 下调立即裁剪目录（按激活优先、hit_count 降序） |

**边界**：

- 只改**策略**，不改**内容**。页面不提供「清空记忆」「删除全部 skill」——那走归档/卸载，留可恢复路径。
- **每次写入都审计**：`config.changed {key, old, new, source, actor, at}`。这一组虽然热生效，但会影响成本与行为，必须可追溯。
- 三项必须**二次确认**并标红：`evolve.enabled=false`、`evolve.daily_token_budget=0`、`evolve.nudge_interval=5`（区间下界，会把复盘频率拉到 5 轮一次，费用线性上升）。
- 页面显示三元组：`当前值 / 来源 / 默认值`。来源是 `default` / `core.toml` / `env` / `page`——页面写入的值落到 `data/config.overrides.json`，**不回写 `core.toml`**，这样升级时不会被覆盖，也能一键「恢复默认」。
- 只读区（见下）与这一组用**视觉分隔**：能改的给滑杆/输入框，不能改的只显示 `value + source` 和一个「需重启」标签。

#### 可以改（布局，不审计）

拖动位置、别名 `label`、分组 `group_id`、自动排列、折叠。这些是「怎么看」，不是「怎么跑」，写审计会把审计表拖垮（§7.4）。

#### 只读

节点全部白名单字段（§7.5）、审计查询与详情、拓扑与实时状态、技能正文、记忆条目、当前生效配置（带 `value + source`）、`/admin/api/health`。

**需重启才能生效的配置项**（改文件 + 重启，页面只读，标「需重启」）：

| 键 | 为什么不能热改 |
|---|---|
| `memory.global_capacity` | 调小要先跑淘汰到水位并迁移，中途改会让配额判断与磁盘状态不一致 |
| `memory.embedding_model` / `memory.embedding_dim` | 换模型必须重建全部向量；维度不一致直接拒绝启动（`43-M4` §五） |
| `memory.ttl_days` | 清理任务按天调度，热改会让「已按旧 TTL 标记」的行状态漂移 |
| `compaction.tail_mode` | 改的是 prompt 布局，会立刻打断 prefix cache，必须在启动时固定 |
| `skills.body_max_chars` / `skills.active_limit` | 影响加载期校验与已有 skill 的可用性，热改会出现「刚才还能加载，现在不行」 |

#### v1 明确不做

| 不做 | 理由 |
|---|---|
| **直接编辑 `core.toml`**（监听、TLS、存储路径、embedding、容量配额） | 页面**不写 toml**：热生效项写 `data/config.overrides.json`（§7.9.1），重启项只读展示 `value + source`。这样升级不会覆盖用户改动，也不会出现「页面显示已改、进程还在用旧值」的假象 |
| 在页面拉起模块进程（L3） | core 不知道模块在哪台机器、怎么启动；承担拉起就等于要 SSH/K8s 凭据（§7.4） |
| 编辑记忆原文 / 批量导入记忆 | 记忆是观察记录，不可改写；批量导入没有可信来源标注，会污染检索 |
| 在页面里跑 skill（试运行） | skill 没有执行通道（D38），「试运行」只能靠发一轮真实对话，那属于 `ui` 模块 |
| 审计导出 | 直接读 SQLite（只读连接），页面不做文件下载——避免把「能看全部工具调用」的界面变成数据外带出口 |
| 配置上游 MCP（bridge 的 upstream） | 那是 bridge 模块自己的配置，core 不管（D39） |
| 多管理员 / 角色 / RBAC | v1 只有一个管理员账户；加角色要先有审计归属模型，v2 |

> 「页面只读展示配置生效值 + 来源」这条很关键：用户经常遇到「我改了 toml 怎么没生效」，答案往往是 env 或 CLI 覆盖了它。把 `value / source` 一起显示，比给个编辑框有用得多。

### 7.9.1 `core.toml` 完整 schema（C-18，v1.17 新增）

v1.16 及更早只有零散提及（“监听、TLS、policy 超时、audit 保留、memory 配额、skill 上限”），没有键名表、没有默认值、没有生效方式。10 个模块并行开发时，`5000`、`8KB`、`64` 这些数字会在各自的 `config.py` 里出现不同版本，合并门禁查不出来。所以本节把 schema 定死，并作为契约 **C-18** 由 C2 交付 `schema/config.schema.json`。

**四条规则**

1. 键名 `snake_case`；段名小写；未知键 → **拒绝启动**（不是 warning，否则拼写错误会静默用默认值）。
2. 每个键必须有三个属性：**默认值 / 生效方式 / 所有者**。生效方式只有两种：`hot`（下一轮或下一次触发起用新值）与 `restart`（改文件 + 重启）。
3. 优先级：`CLI > env > data/config.overrides.json > core.toml > 默认值`。页面写入只落 `overrides.json`，**永不回写 `core.toml`**——否则升级覆盖、diff 冲突、审计都说不清。
4. 密钥类只接受**环境变量名**，`core.toml` 出现字面量 `api_key = "sk-…"` 直接拒绝启动（`21-C2:89`）。

**`[core]` / `[tls]` / `[policy]` / `[audit]`**

| 键 | 默认 | 生效 | 所有者 |
|---|---|---|---|
| `core.listen` | `127.0.0.1:8443` | restart | C2 |
| `core.admin_host` | `127.0.0.1:8444` | restart | C5a |
| `core.data_dir` | `./data` | restart | C2 |
| `core.log_level` | `info` | hot | C2 |
| `tls.cert` / `tls.key` | `null`（回环可空） | restart | C2 |
| `tls.spki_pin` | `null` | restart | C2 |
| `policy.approval_timeout_s` | `60` | hot | C5a |
| `policy.max_steps` | `40` | hot | C3 |
| `policy.max_tool_result_tokens` | `4000` | hot | C3 |
| `audit.retention_days` | `30` | restart | C5a |

**`[policy.tool_risk]`（C5a 读；FR-7.12 的 risk 裁决表）**

> 这是「未登记即按 `high`」的反例清单：**登记在此表的 `(module_id, tool)` 按表值裁决，未登记的按 `high`**。表值只能上调（相对 manifest 申报值），不能下调。高危工具（`shell` / `fs.write` / `fs.delete` / 任何 `forget` 类）硬编码下限 `high`，本表也改不了。

| 键（示例） | 默认 | 生效 | 所有者 |
|---|---|---|---|
| `policy.tool_risk` | `{}`（空表 = 全部按 `high`） | hot | C5a |

> 语义：`{"com.example.fs": {"read_file": "low", "list_dir": "low", "search_files": "low"}, "com.example.web": {"fetch": "low", "search": "low"}}`。未出现的工具一律 `high`。

**`[server]`（C2 读；数据面 CORS 与 host 校验）**

| 键 | 默认 | 生效 | 说明 |
|---|---|---|---|
| `server.ui_origins` | `[]` | restart | 数据面 `/ui/session` 的 CORS 白名单，精确匹配，禁 `*`；为空则不回 CORS |
| `server.listen_hosts` | `["127.0.0.1", "localhost"]` | restart | 数据面接受的 Host 名；启动时校验 `ui_origins` 的 host ∈ 本表，否则拒绝启动（§7.1.2 ③） |

**`[dev]`（仅开发；生产禁开）**

| 键 | 默认 | 生效 | 说明 |
|---|---|---|---|
| `dev.allow_all` | `false` | restart | 允许 `AllowAll` 策略（跳过审批）。开启即打红字告警，`/healthz` 暴露 `policy=allow_all`；生产环境缺失 policy/audit 组件时**拒绝启动**（§6 fail-closed） |

**`[memory]`（M4a 读）**

| 键 | 默认 | 生效 | 说明 |
|---|---|---|---|
| `memory.global_capacity` | `5000` | restart | 调小要先淘汰迁移 |
| `memory.session_capacity` | `200` | hot | 结构上限，与字符闸先到先限 |
| `memory.session_inject_char_limit` | `4200` | hot | **注入侧**预算 |
| `memory.recall_k_max` | `20` | hot | 单次 recall 条数上限 |
| `memory.recall_token_limit` | `800` | hot | 单次 recall 返回预算 |
| `memory.ttl_days` | `7` | restart | session 层 TTL |
| `memory.embedding_model` | `""`（空 = 内置） | restart | 换模型必须重建索引 |
| `memory.embedding_dim` | `1536` | restart | 与库不一致拒绝启动 |

**`[evolve]`（M4b 读）**

| 键 | 默认 | 生效 | 说明 |
|---|---|---|---|
| `evolve.enabled` | `false` | hot | 关闭即退订 |
| `evolve.allow_transcript` | `false` | hot | 与 manifest 双许可 |
| `evolve.allowlist` | `[]` | restart | `module_id + 公钥指纹` 白名单 |
| `evolve.nudge_interval` | `10` | hot | 每 N 轮一次 `turn_review` |
| `evolve.flush_min_turns` | `6` | hot | 低于此轮数不跑 `session_close` 的 LLM |
| `evolve.session_char_limit` | `4200` | hot | **写入侧**预算，超顶 `MEMORY_FULL` |
| `evolve.summary_token_ratio` | `0.05` | hot | 与 `ctx` 相乘，取摘要预算 |
| `evolve.lesson_confidence_floor` | `0.6` | hot | lesson 初始置信上限 |
| `evolve.failure_learning` | `true` | hot | 失败轨迹是否产 lesson |
| `evolve.daily_token_budget` | `200000` | hot | 全局令牌桶；`0` = 不限（不推荐） |
| `evolve.daily_budget_skills` | `5` | hot | 提案/天 |
| `evolve.session_budget_skills` | `1` | hot | 提案/会话 |
| `evolve.redact` | `["tool_args","file_content"]` | restart | 改它等于改事件形状 |
| `evolve.model` | `"cheap"` | hot | 走 M4a 的 LLM 配置 |

**`[compaction]`（C3 读，v1.17 新增段）**

| 键 | 默认 | 生效 | 说明 |
|---|---|---|---|
| `compaction.enabled` | `true` | hot | 关掉会无限增长，仅调试用 |
| `compaction.threshold` | `0.50` | hot | 占上下文窗口比例 |
| `compaction.target_ratio` | `0.20` | hot | 压缩后保留尾部占比 |
| `compaction.protect_last_n` | `20` | hot | 最近 N 条不压 |
| `compaction.protect_first_n` | `3` | restart | 头部固定条数，影响缓存 |
| `compaction.tail_mode` | `"lean"` | restart | 改它打断 prefix cache |
| `compaction.proactive_prune_tokens` | `0`（关） | hot | 无 LLM 的工具结果剪枝阈值 |
| `compaction.model` | `"cheap"` | hot | 摘要模型 |

**`[skills]`（C3b 读）**

| 键 | 默认 | 生效 | 说明 |
|---|---|---|---|
| `skills.active_limit` | `64` | restart | 激活上限 |
| `skills.body_max_chars` | `8192` | restart | 超了截断标 `truncated` |
| `skills.l0_token_budget` | `3000` | hot | 目录常驻预算 |
| `skills.pending_ttl_days` | `7` | restart | 待批准 TTL |
| `skills.auto_approve` | `false` | hot | 仅 core 可设，模块无权 |

**两个键为什么分成读写两侧**：`memory.session_inject_char_limit` 管“注入多少”，`evolve.session_char_limit` 管“存多少”。默认相同（4200），但场景不同——排查成本问题时可能只调注入，不改写入。

**没进这个文件的项**：`[fs]` / `[bridge]` / `[llm]` 留在各模块自己的配置里，core 不代管（D39 的模块自治）。core 只管**路由、治理、预算**这三类。

### 7.10 在客户端模块里打开配置页（v1.15 反转）

**结论：配置页的主宿主是我们自己的 App，不是浏览器。**

| 姿势 | 做法 | 权限 | 结论 |
|---|---|---|---|
| **① App 内 WebView（主路径）** | `ui` 模块内嵌 WebView，加载**随 App 包分发的**管理页静态资源；签名与密钥由原生层通过白名单桥提供 | **全功能** | **采用** |
| ② 系统浏览器 | `Intent` / `NSWorkspace.open` / `xdg-open` 打开 `https://<core>:8444/admin` | **只读** | 保留为降级 |
| ③ 模块反向代理 `/admin/*` | `ui` 把请求转发给 core | —— | **仍然禁止** |

**为什么禁止 ③（这条比 v1.14 更重要了）**

v1.14 的理由是「管理面明文经过模块进程，被入侵的 `ui` 能读走全部拓扑」。加上原生桥之后，还多一条更硬的：**模块进程能拿到管理员会话**。代理意味着 access token 与设备密钥的签名请求都会流经模块代码，那 WebView 的隔离就白做了。所以：

- 禁止的是**任何形式**的转发：HTTP 代理、本地端口映射、把 `fetch` 改写到模块自己的 server、用 WebView 的 `shouldInterceptRequest` 中转。审查清单里按「搜不到 `/admin/api` 字符串」检查。
- 模块的 Noise 身份是**模块**，不是**人**。让模块代证等于把「某台设备被入侵」升级成「管理面被接管」。
- WebView 直连 core，TLS 由 WebView 自己的网络栈终止，**不经过模块的任何代码**。

**登录来源与权限矩阵**

| `ui` 与 core 的关系 | core 看到的来源 | 因子 | 权限 |
|---|---|---|---|
| App 内 WebView，跨机 | 非回环 | 密码 + 硬件设备密钥签名 | 全功能 |
| App 内 WebView，同机 | 回环 | 密码 + 设备签名（绑定后） | 全功能 |
| 系统浏览器，任意 | 非回环 / 回环 | 仅密码 | **只读** |

**首次绑定的鸡生蛋问题，v1.15 彻底解决**

v1.14 给的三条出路（同机先配 / setup 阶段登记 / SSH 隧道）里有两条要求「有一台电脑」。现在只剩一条，且手机就能完成：

> **扫 setup 页的二维码 → 核对指纹 → 输入密码 → 完成。**

细节在 §7.12。核心是：App 在扫码时**已经持有**设备密钥对（首次启动时生成，或用户显式「生成一台新设备」），绑定请求由这把私钥签名，所以绑定完成的那一刻，设备身份就已经建立，不需要再单独「登记公钥」。

**`ui` 模块不得替用户保管可导出的私钥**：私钥只能在 `Secure Enclave` / `StrongBox` / `TEE` 里，标记为不可导出。桌面端没有安全区时降级为软件密钥（`hardware_backed=false`），并在 `/settings` 里用黄色标签标出来——不能让它悄悄混在硬件密钥里。

### 7.11 WebView 的安全约束（新增，A55 配套硬约束）

「自研 App 所以可信」这个说法只在**加载的是我们自己的代码**时成立。一旦 WebView 加载 core 下发的 HTML，同时又有 JS 桥，那么 core 上任何一个 XSS（哪怕是被装进去的 skill 诱发的、或被社工的管理员粘贴的）都会变成「在设备上调用签名接口」。这是 Cordova / Capacitor 一类框架被反复打穿的经典路径，必须在设计阶段封死，不能靠代码评审自觉。

**八条硬约束，逐条可测**

1. **只加载 App 内置资源。** URL scheme 固定 `app://admin/`（iOS 自定义 scheme）与 `file:///android_asset/admin/`（Android），**禁止**用 WebView 顶层加载 `https://<core>:8444/admin`。管理页的 HTML/JS/CSS 随 App 包一起发布，版本随 App 走。
2. **core 只提供 JSON。** `/admin/api/*` 一律 `Content-Type: application/json` + `X-Content-Type-Options: nosniff`；**任何情况下不返回 `text/html`**。core 的 `/admin` 路由只服务浏览器只读模式，App 不访问它。
3. **桥接是白名单，不是反射。** Android 不用 `addJavascriptInterface(anyObject, "Native")` 暴露任意对象——只暴露一个 `@JavascriptInterface` 标注的 final 类，方法固定为 `getDeviceInfo()` / `sign(payload, origin)` / `secureStore(op, key, value)`。iOS 用 `WKScriptMessageHandler` + `message.name` 白名单，**禁止** `allowJavaScriptAccessFromFileURLs`。
   `secureStore` 的 `key` **只接受固定枚举**（`admin.host` / `admin.fp8` / `admin.bind_id`），**不落文件系统**、不接受路径字符、值 ≤ 1KB。**禁止**把 refresh token 放进 `secureStore`——它由原生自己持有，JS 连读都不该读到。
4. **签名必须原生确认。** `sign()` 不接受 JS 传入的任意文本直接签——原生层先校验 `payload` 是本 App 生成的 challenge 响应模板（前缀匹配 `vade-admin-v2\n`、长度固定、origin 与当前 core 一致），不匹配直接拒绝。会话建立后的 refresh 签名可免弹窗，**首次登录必须弹**，弹窗里显示「向 `<host>` 登录，密码已通过」。
5. **禁用一切「方便」开关**：`setAllowFileAccessFromFileURLs(false)`、`setAllowUniversalAccessFromFileURLs(false)`、`setAllowFileAccess(false)`（除 asset 目录）、`setJavaScriptCanOpenWindowsAutomatically(false)`、`setDomStorageEnabled(true)` 但**不持久化**（`clearOnExit`）、禁止 `WebSettings.setMixedContentMode(ALWAYS_ALLOW)`。
6. **CSP 双保险**：内置页面 `<meta http-equiv="Content-Security-Policy">` 写 `default-src 'self'; connect-src https://*; script-src 'self'; object-src 'none'; base-uri 'none'`。允许 `connect-src https://*` 是因为要连用户自己的 core（地址运行时才知道），代价是**页面里绝不能有 `innerHTML = 用户数据`**——这条进审查清单。
7. **证书处理**：默认**不放行任何证书错误**，不实现 `onReceivedSslError { handler.proceed() }`。唯一例外是**用户导入过的 SPKI pin**：`tls` 字段与 `SHA-256(SPKI)[:16]` 逐字相等时，把这一张证书注册为本次连接的信任锚（`X509TrustManager` 只接受它），**不是**「在告警框上点继续」。不符一律断开并提示「可能被中间人」。
   两个指纹不要混：`fp8` 是 **core 身份**（Ed25519 公钥哈希，防「连错服务器」）；`tls` 是 **证书 SPKI**（防「传输被替换」）。自签部署两个都要核；可信证书部署只核 `fp8`。
8. **模板消息不走 WebView**：审批弹窗（HITL）、密码输入、指纹核对全部用**原生 UI**，不在 WebView 里做。WebView 只承载「配置表单 + 列表 + 画布」这类可被替换的界面。

**验收方式**（进 R3 安全审查清单）

| # | 检查 | 期望 |
|---|---|---|
| 1 | 全局搜索 `loadUrl("http` / `loadUrl("https` | 0 处（除开发者调试开关且 release 构建剔除） |
| 2 | 桥接类是否只含白名单方法 | 方法数 = 3，无 `getClass()` / 反射入口 |
| 3 | `/admin/api/*` 响应头 | 全部 `application/json` + `nosniff` |
| 4 | 页面内 `innerHTML` / `dangerouslySetInnerHTML` | 只允许常量，不允许变量插值（ESLint 规则强制） |
| 5 | `proceed()` 在 `onReceivedSslError` 里 | 0 处；pin 匹配的证书由 TrustManager 接受，不靠 `proceed()` 放行 |
| 6 | App 内 WebView 地址栏 | 不显示（内置页面无需地址栏），且无法导航到外部（`shouldOverrideUrlLoading` 全拦，白名单外 `return true`） |
| 7 | 卸载 App 后钥匙串残留 | refresh token 随 App 卸载清除（`kSecAttrAccessibleWhenUnlockedThisDeviceOnly` + 首次启动检测 `bind_id` 不存在则 `deleteAll`） |

### 7.12 设备绑定协议（新增，A56）

这一节把「App 怎么安全地成为一台被信任的管理设备」写死。设计目标是三条：**用户只用手机**、**core 公钥不经云端**、**双方都明确同意**。

#### 7.12.1 双向同意到底指什么

「双向同意」容易被说成口号，这里给出可验证的定义：

| 方向 | 表达方式 | 失败会怎样 |
|---|---|---|
| **客户端 → core 同意** | 用户**手工**把 core 身份导入 App（扫码或粘贴 `host` + `fp8`），App 把它 pin 住，之后所有请求只发往这个身份 | 用户不导入，App 里根本不存在这个 core 的条目，无法发起任何请求 |
| **core → 客户端同意** | core 验证**绑定票**（setup token / 一次性 bind ticket）+ 设备对 challenge 的签名 | 票无效或签名不对，`401 BIND_UNAUTHORIZED`，不登记任何设备 |

注意「同意」的主体不同：前者是**人**对**服务器身份**的确认（防钓鱼），后者是**服务器**对**设备身份**的接纳（防陌生人）。两个都要有，缺一个都能被利用：只有前者，任何人拿到地址就能绑；只有后者，用户可能绑到一台假冒的 core。

#### 7.12.2 core 身份包（用户导入的东西）

用户在已解锁的 setup 页看到二维码，或在 core 主机执行 `core admin issue-bind-ticket` 得到同样一段文本：

```
vade://bind?h=core.example.com&p=8444&fp=3f9a1c7e5b2d4a86&tls=8c1d0e5a7b3f2469&t=A7K2M9QX4B3F8N5R2T6V9Y0CDE
```

| 字段 | 含义 | 校验 |
|---|---|---|
| `h` / `p` | 管理面地址 | 必须 `https`；`p` 默认 8444 |
| `fp` | `SHA-256(core_identity_pub)[:16]` 小写 hex（§7.1.3 的身份密钥，**不是** TLS 证书指纹） | 与握手时下发的 `core_pub` 重新计算比对，不一致即中止 |
| `tls` | `SHA-256(SPKI)[:16]`，TLS 证书公钥指纹 | App 把它作为 SPKI pin；只在 pin 匹配时接受自签证书（§7.11 第 7 条）。走可信证书的部署可为空 |
| `t` | **一次性 `bind_ticket`**（16B，26 字符 base32，10 分钟），**不是 setup token** | 一次性；后端 `DELETE ... WHERE consumed_at IS NULL` 消费 |

二维码密度：约 140 字符（含 `tls` 与 26 字符票），QR version 7 纠错 M，手机屏 3 米内可扫。**手输兜底**三字段分开填，`fp` 支持分段粘贴。

#### 7.12.3 握手序列

```
① App → core   GET /admin/bind/info
               请求头 X-Bind-Ticket: <t>
   core → App   {ok: true, core_pub: base64(32B), fp8: "3f9a1c7e5b2d4a86",
                 kex_pub: base64(32B X25519), challenge: base64(32B),
                 setup_required: <bool>, expires_at,
                 hello_sig: base64(Ed25519(core_identity_priv,
                   "vade-bind-hello-v1\n" || kex_pub || challenge
                   || SHA256(t) || admin_host))}

   App 侧依次：
     a. SHA-256(core_pub)[:16] == fp ? 继续 : 中止（显示「指纹不一致」）
     b. 用 core_pub 验 hello_sig。它覆盖 kex_pub / challenge / ticket_hash / host，
        失败即中止（「服务器身份签名无效」）
     c. 只信 b 验过的 kex_pub —— 否则替换 kex_pub 就能把密码解走

   ★ 为什么必须签这一下（v1.16 补）：fp 只绑定 core_pub，管不到 kex_pub 与 challenge。
     不签的话，「能终止 TLS 的攻击者」（恶意反代、LB、被替换的自签证书）可以保留真的
     core_pub 通过 fp 校验，同时把 kex_pub 换成自己的，于是 App 把密码与票加密给攻击者。
     签名把「问候三元组」整体绑到身份密钥上，这条攻击才不成立。

   challenge 60s 一次性，绑定 (来源 IP /24, ticket_hash)

② App 本地生成（或复用）设备密钥对
   ECDSA P-256，私钥写入 Secure Enclave / StrongBox，hardware_backed=true
   device_pub = 65 字节未压缩点（0x04 || X || Y）

③ App → core   POST /admin/bind
   {
     ticket: <t>,               ← 明文。它已经在二维码里，再加一层没有收益，
                                  却会让 AAD 算不出来（见下）
     eph_pub: base64(32B X25519 临时公钥),
     nonce: base64(12B),
     ct: base64(AES-256-GCM(明文, AAD, k))
   }

   ct 内明文 = {
     password: "<明文>",        ← 未初始化：新密码（两次一致，前端校验）
                                  已初始化：当前密码（step-up）
     device_pub: base64(65B),
     alg: "p256",
     label: "iPhone 15",
     hardware_backed: <bool>,   ← 设备自述，见下方说明
     ts: <unix>,
     sig: base64(ECDSA(challenge_from_①))
   }

   ★ 这里就是「用公钥加密再发」的落点：
     k = HKDF-SHA256(X25519(eph_priv, kex_pub), salt="vade-bind-v1", info="aes-256-gcm")
     AAD = "vade-bind-v1" || fp8 || SHA256(ticket) || challenge || kex_pub

   ★ AAD 必须在**解密前**就能算出来。v1.15 把 ticket 放进密文、又要求 AAD 含
     ticket_hash，是循环依赖（不解密拿不到票，没票算不出 AAD）。v1.16 把 ticket 移到
     明文字段，AAD 用 `SHA256(ticket)`，其余两项都是问候里收到并已被 hello_sig 覆盖的值。
     密码、设备公钥、签名仍在密文里，不进日志。

   ★ `hardware_backed` 是设备自述，core 无法验证。存 `{hardware_backed, attested:false}`，
     UI 标「设备自述」。**不要拿它做授权判断**——它只影响列表里的黄色标签。

   ★ 密码在加密信封里传，core 收到后立刻 Argon2id 哈希，明文不落盘、不进内存快照。
     未初始化时这一步**同时完成 setup 与绑定**（先写 admin.json，再删 token，见 §7.1.4）。

   ★ 两种状态的语义（同一个端点，别写成分支两套）：
     · setup_required=true  → password 是**新密码**（前端两次校验一致性），成功后写 admin.json + 删 token
     · setup_required=false → password 是**当前密码**（step-up），票必须是已登录会话签发的 bind_ticket
     如果用户先在浏览器里把 setup 走完了，再扫码绑定，走的就是第二种；不会重复初始化。

④ core 侧依次校验（任一失败即 401，不区分原因）：
   a. ticket 存在、未过期、未消费（**只 SELECT，不删**）
   b. 解密成功，AAD 校验通过，ts 与服务器时间差 ≤ 120s
   c. sig 对 challenge 有效（P-256，拒 high-S）
   d. device_pub 不在吊销列表
   e. 当前设备数 < 5
   f. 以上全过，才在**同一事务**里
      DELETE FROM bind_tickets WHERE ticket_hash=? AND consumed_at IS NULL
      → rowcount=1 才登记设备；rowcount=0 即并发已消费，401
      任一步失败 ROLLBACK，**票不消费**

   ★ 为什么不能「先删再验」（v1.16 修）：先删的话，拿到票的人发一个坏密文或错签名
     就能把票烧掉——重复几次，合法用户在 10 分钟内再也绑不上。这是一个零成本的
     onboarding DoS。同理，`with conn:` 块里如果 `return` 而没有 raise，事务会
     COMMIT，所以失败分支必须显式 `ROLLBACK`，不能靠异常传播。

⑤ core → App   {device_id, label, access_token(15min), refresh_token(30d)}
   App 侧（**原生层，不经过 WebView**）：refresh_token 进钥匙串（ThisDeviceOnly），
   access_token 只留内存，页面通过桥只拿到 access。
```

**为什么用 X25519 临时密钥做信封，而不是直接用设备私钥加密**：设备密钥是**签名**密钥（P-256 ECDSA），它不能做加密。硬拿它做 ECIES 是误用（ECDSA 密钥对可以做 ECDH，但硬件密钥库通常不允许对同一把密钥同时开签名与协商用途）。所以走标准的「临时 X25519 → core 静态 X25519」ECDH，一次一密。

**关于前向保密，说准确**：这是 ephemeral-static（不是 ephemeral-ephemeral）。它给的是**对被动抓包保密**；**不给**「core 长期密钥泄露后仍保护历史绑定」——攻击者拿到 `data/core.key` 后，可以用它 + 录到的 `eph_pub` 重算共享密钥，解开所有历史信封（含管理员密码）。要真正的前向保密，core 侧必须每次绑定新生成 ephemeral、私钥用后即毁（v2 可做）。文档里不要写「前向保密」四个字糊过去。

**为什么绑定也要签名，明明有 TLS**：TLS 保护的是这一跳，签名绑定的是**这把设备密钥确实持有在发起方手里**。没有签名，任何人截获 ticket 就能登记自己的公钥。签名把「有票」升级成「有票且有这把私钥」。

#### 7.12.4 绑定票的三种来源

| 场景 | 票 | 时效 | 谁能取 |
|---|---|---|---|
| 首次配置（还没有任何设备） | `bind_ticket`（**setup 页解锁后现场签发**） | **10 分钟**、一次性 | 已用 setup token 解锁 setup 页的人；回环访问者免解锁 |
| 加一台新设备（已有设备） | `bind_ticket`（**16B base32，26 字符**） | **10 分钟**、一次性 | **已登录设备**在 `/settings` 点「添加设备」 |
| 救援（设备全丢） | `core admin issue-bind-ticket` | 10 分钟、一次性 | 能在 core 主机执行命令的人 |

- **`setup_token` 不直接当票用**（v1.16 改）。它是「解锁 setup 页」的凭据，只走 `POST /admin/setup/unlock` 的 body；二维码里的 `t` 是解锁后另签的一张 10 分钟票。这样二维码被拍照、被截图、进聊天记录，泄露的也只是一张一次性票，不是那个还能重放去解锁表单的凭据。
- 加新设备的票**必须由已登录会话签发**，且签发动作要 **step-up 重输密码**。理由：会话被借用时，攻击者不能给自己发一张票。（这条限制**只管加设备**；首次配置没有「已登录会话」可用，票由 setup 解锁态签发。）
- 票在服务端存 `{ticket_hash, issued_at, expires_at, issued_by, consumed_at}`；**只存 hash**，日志里不出现原文。
- 票的消费与设备登记在**同一个 SQLite 事务**里：`DELETE FROM bind_tickets WHERE ticket_hash=? AND consumed_at IS NULL RETURNING ...`，`rowcount=0` 即拒绝。防并发重放。

#### 7.12.5 错误码

| 码 | HTTP | 出现条件 | 前端文案 |
|---|---|---|---|
| `BIND_UNAUTHORIZED` | 401 | 票不存在 / 过期 / 已消费 / 解密失败 / 签名错 | 「绑定码无效或已使用，请重新获取」（**不区分**，避免探测） |
| `BIND_FINGERPRINT_MISMATCH` | 409 | ①a 步 `SHA-256(core_pub)` 与 `fp` 不符 | 「服务器身份与二维码不一致，可能被中间人拦截」+ 中止 |
| `BIND_HELLO_SIG_INVALID` | 403 | ①b 步 `hello_sig` 验签失败（`kex_pub`/`challenge`/`ticket_hash`/host 被改） | 「服务器身份签名无效，可能被中间人拦截」+ 中止 |
| `BIND_DEVICE_LIMIT` | 409 | 已达 5 台 | 「设备数已满，请先在设置里移除不用的设备」 |
| `BIND_CLOCK_SKEW` | 400 | `ts` 偏差 > 120s | 「手机时间不准，请开启自动校时」 |
| `BIND_KEY_UNSUPPORTED` | 400 | `alg` 不在 `{p256, ed25519}` 或公钥格式非法 | 「此设备不支持安全密钥」 |

#### 7.12.6 会话模型（WebView 不用 cookie）

| 项 | 决定 | 理由 |
|---|---|---|
| access token | 15 分钟，`Authorization: Bearer`，**只存内存** | 不落盘就不怕被读；WebView 刷新页面会重新换，代价可接受 |
| refresh token | 30 天，钥匙串 `ThisDeviceOnly`，**每次刷新必须设备签名** | 单靠 refresh token 被偷就能续期，加签名后偷到也没用 |
| refresh 的持有点 | **不出原生层**：登录与刷新都由原生代发（密码框也是原生的，§7.11 第 8 条），WebView 只拿到 `access_token` | 否则 WebView 内存里的 refresh 被 XSS 读走，再调 `sign()` 就能无限续期——「刷新要签名」对 XSS 完全无效 |
| 会话绑定 | `(device_id, 来源 IP /24 段, TLS session)` | IP 变化不直接踢（手机切网很常见），但记录并在 `/settings` 显示「最近登录地点」 |
| 并发会话 | 每设备 1 个活跃 refresh，新登录踢旧 | 防止一台设备无限分裂 |
| 登出 | 删 refresh + 加入 `jti` 黑名单（保留至自然过期） | access token 无状态，只能靠短时效 + 黑名单 |
| cookie | **WebView 模式完全不用**；只有浏览器只读模式用 `__Host-ac_admin` | 见 §7.1.2 前置说明 |

**为什么不用 cookie 而用 Bearer**：cookie 在 WebView 里要处理 `SameSite`、第三方 cookie 策略（iOS 17 后默认拦跨站）、自签证书下 `Secure` 不生效、`__Host-` 前缀限制这一堆；而 Bearer 只要求一个请求头。用 `Authorization` 之后 CSRF 也自动消失（浏览器不会自动附带）。代价是 XSS 能读到 token——所以 §7.11 的「页面内不允许 `innerHTML` 插值」是硬约束，不是风格建议。

## 8. 协议 SDK：Rust 单实现

```
              ┌──────────────────────────┐
              │  protocol-core (Rust)    │
              │  noise · aead · frame    │
              │  rpc · seq · reconnect   │
              └────────────┬─────────────┘
                           │ UniFFI
        ┌──────────┬───────┴────────┬──────────┐
        ▼          ▼                ▼          ▼
    Kotlin      Swift           Python      JS/Wasm
  (JVM/Native) (iOS app)      (模块 SDK)   (Web, 见 §9.3)
        │          │
        ▼          ▼
  Compose MP   SwiftUI
```

**为什么**：三端各自实现握手，最难查的 bug 是「Python 模块连得上、iPhone 连不上」，而且要查两遍密码学。

**UniFFI 的异步支持**：`async fn` 通过 tokio runtime 导出，Kotlin 侧是 `suspend`，Swift 侧是 `async`。事件用 callback trait 回推。

**KAT 仍然保留**：固定密钥 + 固定 nonce + 固定明文 → 固定密文，锁死协议行为，防止某次重构悄悄改了参数。

## 9. UI 架构（T1）

### 9.1 分层

```
shared (KMP commonMain)
├── SessionStore       # 本地镜像，按 seq 排序去重
├── TurnQueueView      # 队列视图模型
├── ReconnectPolicy    # 指数退避 + last_seq 续传
└── ProtocolClient     # ← UniFFI 生成的绑定

ui-compose (Android / Desktop / Web)
├── core/screens       # 聊天页、会话列表、设置页 —— 只调 Skin 接口
├── skin/Skin.kt       # interface（见 9.2）
├── skin/material/
├── skin/miuix/
├── skin/glass/
└── shell/BottomBar.kt # 按 style 分发（照 KernelSU 的做法）

ui-ios (SwiftUI)
├── Core/              # 复用 KMP framework
├── Skin/Skin.swift    # 同构接口
├── SkinSystem/        # 系统 TabView（iOS 26 玻璃）
├── SkinMaterial/
└── SkinGlass/         # iOS 17-25 用 UnionTabView
```

### 9.2 Skin 接口（关键：不只是 token）

只有颜色和圆角不够——Miuix 的按钮和 Material 的按钮形状逻辑完全不同。所以 Skin 是**组件工厂**：

```kotlin
interface Skin {
    val colors: ColorScheme
    val shapes: ShapeScheme
    val typography: TypeScale
    val motion: MotionSpec          // 时长与缓动曲线

    @Composable fun Button(text: String, onClick: () -> Unit, style: BtnStyle)
    @Composable fun TextField(value: String, onValueChange: (String) -> Unit, ...)
    @Composable fun Card(modifier: Modifier, content: @Composable () -> Unit)
    @Composable fun ListItem(leading: @Composable () -> Unit, title: String, ...)
    @Composable fun Switch(checked: Boolean, onCheckedChange: (Boolean) -> Unit)
    @Composable fun BottomBar(items: List<NavItem>, selected: Int, onSelect: (Int) -> Unit,
                              style: BottomBarStyle)
    @Composable fun ApprovalDialog(request: ApprovalRequest, onDecide: (Decision) -> Unit)
}
```

**业务页面只准出现 `Skin.Button(...)`，不准出现 `androidx.compose.material3.Button`。** 这条会写成 lint 规则 + 一个会失败的单元测试（扫描 import）。

### 9.3 三皮肤 × 三底栏

| 皮肤 | Android | Desktop | Web | iOS | 交付 |
|---|---|---|---|---|---|
| `material` | M3 原生 | M3 原生 | M3 原生 | 自绘近似 | v1 必做 |
| `miuix` | Miuix + haze | Miuix（密度补偿） | Miuix（Wasm +约 900KB） | 自绘近似 | v1 必做 |
| `glass` | AGSL 折射 / 低版本降级 | Skia 模糊（**不折射**） | CSS `backdrop-filter`（**不折射**） | **系统 TabView** | v1 必做 |

> 三个皮肤全量交付，不再区分必做/可选。跨端强度按 D22 执行：折射只在 Android AGSL 与 iOS 系统玻璃上出现。

| 底栏态 | 行为 |
|---|---|
| `fixed` | 贴底，内容区让出 inset |
| `floating` | 悬浮，四周留 12dp，圆角胶囊 |
| `floating+blur` | 悬浮 + 背景采样模糊 |

**9 种组合全部要有验收截图**，这是 AC-13。

### 9.4 Web 端的协议绑定（唯一技术风险）

UniFFI **不支持 Wasm**。所以 Web 有两条路：

| 路径 | 做法 | 结果 |
|---|---|---|
| **W-A（主）** | Rust → `wasm32-unknown-unknown` + `wasm-bindgen` → JS 包；Compose/Wasm 通过 `@JsExport`/external 调用 | 协议仍 1 份 |
| **W-B（兜底）** | Web 用 TypeScript 重写协议（只这一处） | 协议 2 份，需额外 KAT 交叉验证 |

**C0 批次专门验证 W-A**。若 3 天内跑不通，降级 W-B，并在文档里明确记录「Web 端协议实现为第二份」。

## 10. 部署拓扑

| 形态 | 说明 |
|---|---|
| 裸进程 | `python -m vade --config core.toml`，systemd / launchd 托管 |
| Docker | 单镜像，`/data` 卷挂载密钥、SQLite、审计 |
| 组合 | core 容器 + 模块容器，同 docker network；跨机模块走公网 + WSS |

配置优先级：命令行 > 环境变量 > `data/config.overrides.json` > `core.toml` > 默认值。

### 10.1 端口与监听矩阵（部署检查清单）

| 端口 | 谁在用 | 默认监听 | 对外 | 谁可以访问 |
|---|---|---|---|---|
| `8443` | 数据面：模块 WSS、`POST /ui/session`、`GET /healthz` | `127.0.0.1` | `--bind 0.0.0.0` | 模块、已配对客户端 |
| `8444` | 管理面：`/admin`、`/admin/api/*`、SSE、`/admin/bind` | `127.0.0.1` | **`--admin-bind 0.0.0.0` 允许** | 已绑定设备：全功能；浏览器：只读 |
| `8080` | 模块 app（webui 的 `ui` 模块） | 由模块自定 | 由模块自定 | 浏览器用户 |

五条硬约束：

1. **`admin` 不得与 `data` 共用 listener。** 端口不同不够，还要 host 不同（见 §7.1.2 ①）——cookie 不区分端口。
2. **管理面对外后，写操作只有已绑定设备能做。** `--admin-bind 0.0.0.0` 允许，但此时：已绑定设备走「密码 + 设备密钥签名」→ 全功能；浏览器走「仅密码」→ **只读**；设备表（`data/admin/devices.json`）为空时，启动即打红字告警并**拒绝启动**——不允许「开了公网端口却一台设备都没绑」的半开状态。
3. **Docker 部署**：`-p 8443:8443` 数据面；管理面按需 `-p 8444:8444`（对外）或 `-p 127.0.0.1:8444:8444`（仅宿主）。对外时必须已绑好至少一台设备。
4. **反向代理**：若用 nginx/caddy 终止 TLS，只允许代理 `:8443` 数据面。**不要代理管理面**——一旦代理，`Origin` 与 `Sec-Fetch-Site` 校验面对的是代理重写后的头，等于失效；管理面要暴露就直连 `8444`。
5. **`__Host-` cookie 只在 HTTPS 下可设**（仅影响浏览器只读模式）。自签证书必须先导入系统/浏览器信任库（core 首次启动会打印 CA 路径与导入命令），否则浏览器会静默拒收 cookie，表现为「登录成功但立刻 401」。App 内 WebView **不涉及**这条——它用 Bearer，见 §7.12.6。

### 10.1.1 首次 setup 的部署姿势（v1.15：手机优先）

setup 期是整套系统唯一「还没有管理员」的窗口。v1.14 之前的排序把 SSH 隧道放在很前面，隐含前提是「配置的人面前有一台装了 SSH 的电脑」。**这个前提不成立**——用户在通勤路上、只有手机、容器已经在云上跑着，这是常态。所以 v1.15 把所有姿势重新按「需不需要电脑」排序：

| 姿势 | 做法 | 需要什么 | 剩余风险 |
|---|---|---|---|
| **⓪ 编排注入（推荐）** | 起容器前 `core admin init --password-stdin --pubkey-file`，开机即已初始化 | 能 `exec` 进容器 | 密码进编排 Secret |
| **① IP 白名单 + 扫码绑定（推荐）** | 安全组只放行自己出口 IP 访问 `8444`；白名单来源视为已解锁，`GET /admin/setup` 直接给二维码，手机 App 扫 | **只要手机** | 出口 IP 变动；容易忘记撤 |
| **② setup token + 扫码绑定** | 公网开放 `8444`；手机先从云控制台/日志取 token，粘贴解锁 setup 页，再扫码 | **只要手机** | token 落数据卷；需 TLS 可信 |
| ③ 只回环 + SSH 隧道（开发者） | 管理面绑 `127.0.0.1`，`ssh -L 9444:127.0.0.1:8444` | 电脑 + SSH 可达 | 依赖 SSH |

**优先级 ⓪ > ① ≈ ② > ③。** ①② 覆盖「只有手机」；③ 保留给开发者本机调试，**不再写进面向用户的 runbook**。

**手机上的完整流程（这就是用户路径，四步）**

```
1. 云主机起容器，管理面暂时对外（或先绑回环再看下面「没有公网 IP 怎么办」）
      docker run -d -p 8443:8443 -p 8444:8444 -v /srv/ac:/data vade:latest

2. 取 setup token（三选一，都不需要电脑）
   a. 云控制台看容器日志：setup token written to data/setup.token (acs_4f2a…)
   b. 云厂商 Web 终端 / docker exec：cat /data/setup.token
   c. 临时 IP 白名单：把手机出口 IP 加白，该来源免 token（姿势①）

3. 手机浏览器打开 https://<host>:8444/admin/setup
   → 只看到「请输入访问令牌」；**没有二维码、没有指纹**
   → 粘贴 token → POST /admin/setup/unlock → 解锁（10 分钟）
   → 页面这时才出现：二维码 + 大字指纹 3f9a 1c7e 5b2d 4a86

4. 打开 Vade App →「添加服务器」→ 扫码
   → App 校验 fp8 与 hello_sig，显示「即将连接 core.example.com，指纹 3f9a1c7e5b2d4a86」
   → 与页面上的大字指纹逐字核对，一致才点「确认」

5. App 内设置密码（≥12 位，两次）→ 完成
   → 同时完成：写 admin.json、登记这台设备、消费票
   → 页面自动跳到 /login，二维码消失
```

**第 5 步之所以能一步完成两件事**：未初始化时 `POST /admin/bind` 允许携带**新密码**，core 在同一个事务里「验票并消费 → 登记设备 → 写密码」。顺序是：先 `os.replace(admin.json)`、再 `unlink(token)`（§7.1.4），设备登记在同一事务内。任一步失败整体回滚，票不消费，可以重来。

> **v1.16 修的就是第 3 步**：v1.15 把二维码画在「还没验 token」的页面上，等于把入口凭据印在门上。现在门先锁，钥匙在用户手里（云控制台/日志/白名单），开门后才给二维码。

**没有公网 IP / 不想开放 8444 怎么办**

| 情况 | 做法 |
|---|---|
| 云安全组可改 | 临时把自己的出口 IP 加白（①）。用完撤掉，这是攻击面最小的一条 |
| 完全不能开放端口 | 只能在 core 主机上跑一次 `core admin init`（⓪），或走 SSH 隧道（③）。**明说：这条需要电脑** |
| 有内网（同一 Wi-Fi） | 手机与 core 同网时直连内网 IP，不需要公网 |

**关键：先绑回环起、再决定是否放开。** 反过来（先 `0.0.0.0` 起容器再想办法收）会有一段「端口开着、token 还没取出来」的裸奔窗口。

**token 每次启动都换（D59）。** 所以 `docker restart` / OOM / 主机重启之后，之前 `cat` 出来的值和二维码都会作废，必须刷新页面重新扫：

```bash
docker exec vade cat /data/setup.token   # 每次重启后都要重取
```

setup 完成前**不要**做「重启一下试试」——那一试就要重新扫。真要在 setup 期间重启，先把值钉进 `VADE_SETUP_TOKEN`（此时不轮换）。

## 11. 被否方案

| 方案 | 否决理由 |
|---|---|
| 自研 4 步握手（v1.0） | 密码学状态机自研出错概率高；`snow` 已实现，无理由重写 |
| 纯 RSA-OAEP 包 AES key | 无前向保密、无双向认证、无重放防护 |
| mTLS | 用户明确要求应用层加密且私钥在 core；证书运维成本过高 |
| 完全兼容 MCP | 原生 stdio/SSE，跨机加密要外挂；但语义对齐保留，将来可薄适配 |
| gRPC + protobuf | 模块开发门槛高，schema 变更需重新生成，热插拔体验差 |
| Flutter 一套三端（A9） | 拿不到 Miuix（Kotlin/Compose），拿不到 iOS 系统玻璃 |
| iOS 用 Compose 渲染 + 仿玻璃 | 质感差距明显，iOS 用户对「不像原生」敏感 |
| 皮肤做成跨进程模块 | 渲染是每帧决策，跨进程无意义且引入延迟 |
| 会话状态放 UI 端 | 多端必然打架，无解 |
| **SSH 隧道作为首次配置主路径**（v1.14 采用，v1.15 撤销） | 它把「配置」与「面前有一台装了 SSH 客户端的电脑」绑定。目标场景是用户在手机上、容器在云上，这个前提不成立。且隧道把公钥因子外包给 SSH 的 `authorized_keys`，意味着「谁能登机器」和「谁能管 Vade」是两套互不知情的权限，吊销时容易漏一边。改为 App 内设备密钥后，权限收回到一张表里 |
| **浏览器直连管理面做全功能**（v1.15 降为只读） | 浏览器没有裸签名 API，拿不到第二个因子。要么强行用 WebAuthn（另一套凭据模型，且不能复用设备密钥），要么放弃双因素。降为只读比「假装双因素」诚实 |
| **WebView 加载 core 下发的 HTML + JS 桥**（明确不做） | XSS 直接升级成本地代码执行，是 Cordova/Capacitor 类框架被反复打穿的原因。改为只加载 App 内置资源、core 只给 JSON（§7.11） |
| **setup 页无条件渲染含 token 的二维码**（v1.15 采用，v1.16 撤销） | 8444 对外时，任何打开 setup 页的人都能拿到票、绑自己的设备、设初始密码。它不是「二维码被拍照」的弱风险，是「打开网页就拿到」。改为先 `unlock` 再下发，且二维码只放 10 分钟一次性票（§7.1.4） |
| **只校验 `core_pub` 的 ECDH 信封**（v1.15 采用，v1.16 撤销） | `kex_pub` 不受签名覆盖，能终止 TLS 的攻击者可以保留真 `core_pub` 通过指纹校验、替换 `kex_pub` 解出密码。改为 core 身份密钥对问候整体签名（§7.12.3） |
| **先删票再验签**（v1.15 采用，v1.16 撤销） | 拿到票的人发一个坏密文就能烧票，构成零成本的 onboarding DoS。改为先验后删、失败 ROLLBACK（§7.12.3） |

## 12. 风险

| 风险 | 严重度 | 缓解 |
|---|---|---|
| Web 端 Wasm↔Wasm 互操作（§9.4） | **高** | C0 先验，3 天不通即降级 W-B |
| 三个皮肤全量交付 = UI 工作量大 | **高** | 每皮肤只做 §9.2 的 7 个组件；`miuix` 直接用上游库不重写；U2/U3 并行且独立验收 |
| Miuix 未到 1.0，API 会变 | 中 | 锁定版本；皮肤层隔离，升级只改一层 |
| AGSL 在 Android 12- 不可用 | 中 | 运行时探测 + 降级为模糊+描边（FR-8.23） |
| 队列 + 审批 + 多端同时出问题难复现 | 中 | 全部事件带 seq 与 turn_id，审计可完整回放 |
| 模块拿到工具执行权后被滥用 | 高 | 白名单 + 逐模块吊销 + risk 分级 + 全量审计 |
| 模块 app 的 XSS 打到管理面（v1.10 前是同源，直接可打） | **高** | 管理面独立 origin + 不回 CORS 头 + `__Host-` cookie + Origin/`Sec-Fetch-Site` 双校验；推荐 admin 用 `127.0.0.1`、webui 用 `localhost` 做 cookie 硬隔离 |
| 中文配对码在非 UTF-8 终端/输入法下变形 | 中 | `NFKC` 归一化 + 二维码主路径 + `LANG=C` 时打印 ASCII 二维码；C-01 加归一化测试向量 |
| cookie 不区分端口导致跨端口串用 | 中 | 不依赖「不同端口」做隔离，改用不同 host + `__Host-` 前缀 + Origin 校验 |
| 管理面暴露公网后被密码爆破 | 高 | 非回环的**写操作**强制双因素（密码 + 设备密钥签名）；IP/全局双计数锁定；Argon2id；设备表为空则拒绝启动 |
| 浏览器无法持有裸私钥，远程直连不能做写操作 | 中 | 主路径改**App 内 WebView + 硬件设备密钥**；浏览器降为只读；`core admin proxy` 给脚本；不给「看起来能用但点了 403」的表单 |
| WebView 加载远程 HTML + JS 桥 → XSS 变本地代码执行 | **高** | 只加载 App 内置资源；core 只返回 `application/json`；桥接白名单三方法；签名前原生确认；`onReceivedSslError` 只 `cancel()`（§7.11） |
| 设备密钥在软件里（桌面无安全区）被提取 | 中 | 标 `hardware_backed=false` 并黄色告警；每账户 ≤5 台；吊销立即踢会话；`/settings` 摊开全部设备与最后使用时间 |
| 绑定票被截获后被人抢先绑定 | 中 | 票一次性（**先验后删**，失败 ROLLBACK 不消费）、10 分钟时效（首次也用独立 `bind_ticket`，不拿 setup token 当票）；绑定必须设备签名 + 指纹核对 |
| **setup 页在验 token 前就渲染二维码 → 打开网页即可接管** | **高** | 非回环 GET 只回 `requires_token`；`POST /admin/setup/unlock` 验过才下发 `bind_url`/`fp8`；二维码里是 10 分钟一次性票（§7.1.4） |
| **绑定向导的 `kex_pub` 被替换 → 密码与票被解走** | **高** | `hello_sig` 用 core 身份密钥签 `kex_pub ‖ challenge ‖ ticket_hash ‖ host`，App 用已 pin 的 `core_pub` 验签；AAD 覆盖 `challenge` 与 `kex_pub`（§7.12.3） |
| 自签证书 + WebView 禁证书错误 → 合法 core 也连不上 | 中 | `vade://bind` 带 `tls=SHA-256(SPKI)[:16]`；pin 逐字匹配时把该证书注册为信任锚，**不是** `proceed()`；可信证书部署不需要这一步 |
| 移动端用户没有电脑、SSH 不可用 | **高** | 首次绑定改为扫码 + 设备密钥，全程手机可完成（§7.12）；SSH 只留给开发者 |
| rekey 后 `counter` 归零被误判为重放 | 中 | 帧头加 `generation`，按 `(gen, counter)` 字典序判定；KAT 覆盖 rekey 前后各 1000 帧 |
| setup 期公网裸奔（端口先开、token 后取） | 高 | runbook 强制「先绑回环起、需要时再放开」；对外 + 未初始化 + 无可用 token → 拒绝启动 |
| setup token 落在数据卷/日志里 | 中 | 文件 `0600`；stdout 默认打码；审计只记 `source`/`ok`；消费即 `unlink`；首选临时 IP 白名单 |
| 静态密钥与会话库明文落盘 | 中 | 文档不掩盖；部署建议全盘加密；`0600` + 目录 `0700` |
| **每 10 轮复盘把 token 成本放大，用户不自知** | 中 | `evolve.daily_token_budget` 全局令牌桶（耗尽即降级为纯规则）；`flush_min_turns` 挡掉短会话；配置页显示「≈N 次/天、≈M token/天」 |
| **模型自写的 `lesson` 过度泛化，反而误导后续任务** | 中 | 初始 `confidence ≤ 0.6` + 召回标 `unverified`；必须有 `evidence`；二次验证才升 `0.8/verified`；同分排序中 lesson 低于 fact |
| **配置页把 `nudge_interval` 调到 5，费用翻倍而无人察觉** | 中 | 该项二次确认并显示预估日耗；`daily_token_budget` 兜底；审计记 `config.changed` 的 `old/new` |
| Python core 调用 Rust 扩展的 GIL 开销 | 低 | 加密在 Rust 侧批量处理，不逐字节跨界 |

## 13. 验证

| 层 | 怎么测 | 判定 |
|---|---|---|
| 协议单元 | KAT、篡改密文、重放（重发 counter）、方向反射、错误认证标签 | 全部拒绝且错误可解释 |
| 注册 | 非法 manifest 六类 | 全部拒注册，错误信息指向具体字段 |
| 会话 | attach(last_seq) 覆盖/不覆盖两种情况 | 补发正确；不覆盖时快照完整 |
| 队列 | 三端交替提交 10 条 + 中途 interrupt | 顺序正确，中断后队列继续 |
| 审批 | 两端同时收到、一端批准 | 另一端弹窗撤销；审计记录批准者 |
| 安全 | `tcpdump` 抓包 grep 工具参数关键字 | 0 命中 |
| 管理面隔离 | 在 webui 页面（`:8080`）的控制台里 `fetch('https://127.0.0.1:8444/admin/api/nodes',{credentials:'include'})` | 被 CORS 拦；伪造 `Origin` 的 curl 请求返回 `403 ORIGIN_MISMATCH` |
| 管理面鉴权 | 远程四种组合（只密码 / 只签名 / 未登记设备 / 双因素齐）；**回环浏览器写请求** | 只密码 → 200 但 `mode=browser` 只读；只签名与未登记设备 → `401 AUTH_FAILED`；双因素齐 → 200 且可写；回环浏览器 `POST` → `403 READ_ONLY_SESSION`（cookie 一律只读） |
| 设备绑定 | 扫码 → 核对指纹 → 绑定；重复用票；改 `fp` 一位；改 `kex_pub` 一位；错签名后重试 | 首次成功且 `devices.json` 有 `alg=p256`；第二次 `401`；改 fp 本地中止；改 `kex_pub` → `BIND_HELLO_SIG_INVALID`；**错签名后票仍可用**（不被烧掉） |
| WebView 隔离 | release 包里搜 `loadUrl("http`；JS 调 `sign("hello")` | 0 处；被原生层拒绝（模板校验） |
| 技能安装 | install → 查 list_all → approve → 再查 | 第一次不含、第二次含；`pending/` 里有目录；`rev=1` |
| 无电脑路径 | 只给一台手机 + 一个对外暴露的 core | 从零到「看到拓扑」全程不碰命令行（AC-56） |
| setup 准入 | 回环 GET；非回环 GET（未解锁）；非回环 unlock 后 GET；非回环 POST 错 token；已初始化后访问 | 依次：200 且含 `bind_url` / **200 但只有 `requires_token`，无 `bind_url`/`fp8`** / 200 且含 `bind_url`（票为 26 字符 `bind_ticket`，不是 `acs_`）/ `401 SETUP_UNAUTHORIZED` / 302→`/login` |
| setup token 生命周期 | `print` → `rotate` → `reset` | 值依次一致 / 旧值 401 新值 200 / token 变化且回到 setup |
| rekey | 单侧触发；**双方同时触发**；rekey 前后各 1000 帧；重复收到同一 `0x06` | 无丢失无重复；旧 gen 帧被拒；gen 回退被拒；同时触发**不断连**（按方向独立）；重复的 `0x06` 被忽略但不断连 |
| UI | 3 皮肤 × 3 底栏截图比对 | 9 组合无塌陷（AC-13） |
| 跨机 | docker-compose 两容器 + 真机 | 上述全通过 |

## 14. 批次

### 14.1 分组

| 组 | 含义 | 批次 |
|---|---|---|
| **C** | core | C0a/C0b/C0c（验证 spike）– C5b（含 C3b / C3d） |
| **M** | 业务模块（并行） | M1–M6（M4 拆 a/b） |
| **U** | UI | U1–U4 |
| **F** | 交付 | F1 |

> 全项目共 **23 个批次**（与 `status.md` 台账、`00-orchestration.md` 波次表一致）：C0a/C0b/C0c、C1、C2、C3、C3b、C3d、C4、C5a、C5b、M1–M6（M4 拆 M4a/M4b）、U1–U4、F1。

### 14.2 批次表

| 批次 | 内容 | 出口判据 |
|---|---|---|
| **C0a** | 验证 spike：UniFFI 绑定（Rust → Kotlin/Swift/Python） | 跑通一次真实握手，或定降级路径 |
| **C0b** | 验证 spike：Rust → Wasm 互操作（决定 W-A / W-B） | 能被 Compose/Wasm 调用，或定 W-B + ADR-001 |
| **C0c** | 验证 spike：Miuix on CMP | 能编译并渲染，或定降级 |
| **C1** | 协议核心（Rust）：Noise、帧、AEAD、KAT | 单测全绿含负向用例 |
| **C2** | transport + registry + manifest 校验 | 假模块可注册，非法被拒 |
| **C3** | session-store + queue + router + agent-loop | turn 全生命周期正确 |
| **C3b** | skill-loader：扫描、激活、渐进注入、审计 | 三个 skill 的注入/截断/缺工具降级可验；prompt 稳定可复现 |
| **C3d** | 事件推送 + skill 提案受理 | 订阅白名单生效；`pending/` 不被注入；断点补推正确 |
| **C4** | Python SDK v0.1 + echo 模块 | **G1** 端到端跑通，SDK 接口冻结 |
| **C5a** | policy/approval + audit + **admin API 契约 + 列表/审计页** | **G2** 高危需确认、审计可查、API 白名单通过 |
| **C5b** | **拓扑画布**（React Flow）：状态推送、拖拽布局、L2 治理、邀请位 | AC-17～AC-20 全过；200 节点不卡 |
| **M1** | `fs` 文件模块 | 五道门 + 路径逃逸被拒 |
| **M2** | `shell` 命令模块 | 五道门 + 高危审批联动 |
| **M3** | `web` 网络模块 | 五道门 + SSRF 被拒 |
| **M4a** | `memory` 存储与召回（两层） | 五道门 + 两层隔离 + 冲突/淘汰生效 |
| **M4b** | `evolve` 自动演化（memory 的后台角色） | 五触发器可验；关闭时零动作；提案不直接生效 |
| **M5** | `mcp-bridge` | 动态能力更新可用 |
| **M6** | `model` 模块 | core 切到模块后行为不变 |
| **U1** | KMP + Compose 骨架 + material | **G3** Android/Desktop/Web 聊天可用 |
| **U2** | `miuix` 皮肤 | 独立验收，9 组合中对应项通过 |
| **U3** | `glass` 皮肤 | 独立验收，含低版本降级 |
| **U4** | iOS（SwiftUI + 系统 TabView） | **G4** 真机跑通 |
| **F1** | 联调 + 安全验证 + 打包 | **G5** 抓包 0 泄漏，可交付 |

### 14.3 依赖与并行

```
C0 → C1 → C2 → C3 ─┬─ C3b ── C3d
                    │          │
                    ├─ C4 ──┬─ M1 ─┐
                    │       ├─ M2 ─┤
                    │       ├─ M3 ─┤
                    │       ├─ M4a ┼─┐   ← 6 个模块同时开发
                    │       │      └─ M4b
                    │       ├─ M5 ─┤
                    │       └─ M6 ─┘
                    ├─ C5a ── C5b
                    └─ U1 ─┬─ U2 ─┐
                            └─ U3 ─┘
                                   └──→ U4 → F1
```

| 并行组 | 成员 | 前提 |
|---|---|---|
| 第一批 | C4 / C5a / U1 / C3b / C3d | C3 完成，`schema/` 冻结 |
| 配置页组 | C5b | C5a 的 admin API 契约冻结（前端可先用 mock 数据并行开发） |
| **模块组** | M1 / M2 / M3 / M4a / M5 / M6 | C4 的 SDK v0.1 冻结 |
| 演化组 | M4b | M4a + C3b + C3d 全部完成（不与其他模块并行，避免和 M4a 抢同一张表） |
| 皮肤组 | U2 / U3 | U1 的 `Skin` 接口冻结 |

**模块组是全项目最干净的并行**：零共享代码、零目录冲突、零互相等待。

## 15. 模块规格

### 15.1 统一目录结构

```
vade-module-fs/   # 模块独立仓（不在主仓）
├── manifest.json          # 由 tools.py 的装饰器生成，不手写
├── src/fs_module/
│   ├── tools.py           # 纯函数实现，不依赖 SDK
│   └── server.py          # 协议接入层，只做转发
├── tests/
│   ├── test_tools.py      # 不启动 core 也能跑
│   └── test_server.py
├── Dockerfile
└── README.md              # 工具清单 + 安全边界 + 独立运行方式
```

`tools.py` 里的函数**不 import SDK**，这样才能在 SDK 就绪前先写、先测。

### 15.2 各模块规格

| 模块 | 工具签名 | 安全要点 | 输出上限 |
|---|---|---|---|
| **fs** | `read_file(path, max_bytes?)` / `write_file(path, content)` / `delete_file(path)` / `list_dir(path, depth=1)` / `search_files(pattern, root?, limit=50)` | 配置 `roots[]` 白名单；`realpath` 解析后必须落在根内（拦 `../` 与软链逃逸）；可配 `deny[]`（如 `.ssh`、`.git`） | 单文件 256KB，截断标 `truncated` |
| **shell** | `exec(cmd, cwd?, timeout_ms=30000, env?)` | `risk=high`，默认每次审批；超时杀进程组；环境变量白名单（不继承全量 env，防密钥泄漏）；禁 `sudo` | stdout/stderr 各 64KB |
| **web** | `fetch(url, method="GET", headers?, body?, max_bytes?)` / `search(query, count=10)` | **SSRF**：DNS 解析后校验 IP，拒私网/回环/链路本地/云元数据（`169.254.169.254`）；重定向逐跳校验，最多 5 跳；不自动带 cookie | 2MB |
| **memory** | `remember(text, tags?, scope="global")` / `recall(query, k=5, scope?)` / `forget(id)` | 自带 embedding 调用（配置 API key），**不依赖 model 模块**；写入去重（内容哈希） | 单条 8KB |
| **mcp-bridge** | 动态代理上游 MCP 工具的 `tools/list` / `tools/call` | 上游工具名加前缀避免冲突；上游 `risk` 未知时一律按 `high` 处理 | 继承上游，另限 2MB |
| **model** | `chat(messages, tools?, temperature?)` | 不记录 prompt 全文到日志（只记 digest）；超时与重试有上限 | — |

### 15.3 模块的五道门（交付前逐条验）

| 门 | 怎么验 |
|---|---|
| 独立运行 | `python -m fs_module --standalone read_file --args '{"path":"/tmp/a.txt"}'` 能出正确结果 |
| 独立测试 | 不启动 core，`pytest` 全绿；覆盖正常 + 非法参数 + 越权 |
| 独立部署 | `docker build` 成功，镜像可单跑 |
| 独立失败 | `kill -9` 该模块，其他模块与会话不受影响 |
| 独立观测 | 日志每行带 `module_id` / `instance_id` / `tool` / `duration_ms` |

### 15.4 模块的验收脚本（每个模块同一套）

```bash
# 1. 单测
pytest -q
# 2. 独立运行（参数走 --args，JSON；不要用位置参数——SDK 只实现了这一种）
python -m <mod> --standalone <tool> --args '{"path":"/tmp/x"}'
# 3. 接入 core
python -m <mod> --core wss://localhost:8443 --pair-code <code>
# 4. 越权用例（必须失败）
python -m <mod> --standalone <tool> --args '{"path":"../../etc/passwd"}'
# 期望：非零退出 + {"code":"PERMISSION_DENIED",...}，不是 traceback
```

**`--standalone` 的配置来源**：和连 core 时读同一份 env（`FS_ROOTS` 等），不额外造一套。CI 里用 `.env.test` 注入，不要把这些值写死在测试里。

## 16. 记忆分层与自进化

> 参考了 NousResearch 的 Hermes Agent（分层记忆 + 技能自演进 + 每 N 轮反思）的**问题划分**，但没有沿用它的实现：它是一体式单体、靠 system prompt 提醒模型自觉写入；本系统是模块化 + 可审计的，写入必须走提案与审核，触发必须是确定性的。差异见 §16.6。

### 16.1 两层记忆

| | `session` 层 | `global` 层 |
|---|---|---|
| 归属 | 一个 `session_id`，由 core 注入，模型不能伪造 | `namespace`（`user:default` / `agent:reviewer` / `project:orion`） |
| 类型 | `kind ∈ {fact, preference, lesson}`；`lesson` 另带 `evidence` / `verified_at` | 同左 |
| 写 | evolver 自动（每 10 轮复盘 + 会话结束总结）；也接受显式 `remember(scope="session")` | 同一事实在 ≥2 个会话出现、`lesson` 单次带证据、或用户显式 `remember` |
| 读 | **自动注入**，不调 recall——它就是当前会话的工作台；**按字符预算注入，不是全量** | 按需 `recall` 检索；单次返回 ≤ `recall_token_limit`（默认 800 token） |
| 生命周期 | 会话关闭后 7 天 TTL。**consolidate 不会提前删它**——物理删除只有两个来源：`forget` 与 TTL 到期 | 永久，受全局配额 |
| 上限 | **双闸**：≤200 条 **且** ≤`session_inject_char_limit`（默认 4200 字符），先到先限 | 5000 条，按 score 淘汰 |
| 淘汰 | TTL | 分数淘汰（§16.3） |

**为什么上限从“条数”改成“字符 + 条数双闸**（AMEND-19/D79）：条数不是成本。200 条短句可能只有 3k token，200 条长结论能到 20k——而注入是按 token 付钱的，每轮都付。改成字符硬顶后，成本可预测，且“满了”变成**写入时**的显式错误（`MEMORY_FULL`），evolver 当轮就得合并或淘汰；不必等到每日 consolidate 才发现膨胀。条数上限保留作结构约束（防止 1 个字一条刷爆行数）。

**为什么注入不全量**：session 层“自动注入”原本等于“每轮把 200 条全塞进 prompt”。100 轮会话就是 100 次重复付费，而且越到后面越贵。现在按 `importance × recency` 排序取到预算为止，超出部分靠 `recall(include_session=true)` 按需捞。

为什么必须分两层：一层会让「刚才说过什么」和「用户三年前说过什么」抢同一张召回表。session 层解决前者（注入即用，零检索成本），global 层解决后者（检索 + 排序）。

### 16.2 自进化流水线

五个触发器，全部**确定性**（不靠 prompt 提醒模型自觉）：

| 触发 | 时机 | 动作 | LLM 调用 | 预算 |
|---|---|---|---|---|
| `turn_end` | 每轮结束 | 规则抽取（**仅 `user_text`** + 工具结果里的环境事实），写 session | 否（v1 用规则） | ≤ 2 条/轮 |
| `turn_review` | 每 `nudge_interval` 轮（默认 10） | 旁路复盘最近 N 轮 → ≤3 条候选 + ≤1 条 lesson | 1 次（便宜模型，输入 ≤1.5k token） | 共享 `daily_token_budget` |
| `session_close` | 会话结束（**总轮数 ≥ `flush_min_turns`**，默认 6） | 读 rolling summary + 最后 N 轮 → summary + 候选 | 1 次（便宜模型） | ≤ 5 条 |
| `consolidate` | 每日 03:00、启动补跑、或手动 | 去重、合并、冲突、晋升、衰减、淘汰 | 批量 1 次 | — |
| `skill_evolve` | 轨迹满足条件时 | 提炼新 skill 或 patch 现有 | 1 次 | ≤ 1 提案/会话 |

**`turn_review` 是本版新增的第五个触发器**（AMEND-19/D80）。它解决两个问题：

1. **只靠会话末总结，等于“等人死了才写遗书”**——长会话（几十上百轮）里最该记的决策发生在中途，等关闭时再让便宜模型读全文，既贵又丢细节。
2. **纯自主运行没有用户信号**：`turn_end` 的五类规则信号大多由用户侧触发，agent 自己跑 200 轮工具调用时几乎抽不到东西。`turn_review` 看的是“状态变化”（出错、重试、换了方案、得出中间结论），不依赖用户开口。

**它不是“prompt 提醒模型自觉”**：按 FR-13.7，触发是 harness 确定性的（每 N 轮一次），执行在**旁路**（独立的 evolver 进程/线程，不占主对话上下文，不往主 prompt 里塞提醒）。这是与 Hermes 的 `nudge_interval` 对齐的做法，但落点不同——它提醒主 agent，我们让 evolver 自己看结构化骨架。

**输入是骨架不是原文**：每轮 `{turn_id, state, tool_names[], first_sentence, last_sentence, error_flag}`，约 120 token/轮。10 轮 ≈ 1.2k token，比 `session_close` 吞全文低一个数量级。

`skill_evolve` 的条件（**四条同时满足**，缺一不触发）：

1. 该 turn `state=completed`；
2. **有效**工具调用 ≥ 5，且至少涉及 2 个不同工具（读 5 个文件不算“复杂流程”）；
3. 无用户负反馈（用户没有说“不对/重来/不是这样”）；
4. **轨迹不含 `self_correction` 标记**（AMEND-19/D81）——模型自己推翻过前序结论的轨迹，是“纠错案例”不是“最佳实践”。这类轨迹只能进 `lesson`，不能当 `create` 的正面样本；否则会把“先轻信、后补救”的整条路径学成流程。

**失败轨迹的去处**（AMEND-19/D81）：`state=failed` 或 `negative=true` 的轨迹不产 skill，但**可以产 lesson**——条件是有可定位的证据（哪个工具、返回什么、断言被什么推翻）。这样“失败学不到东西”的洞被补上，同时不违反“不能把失败流程写成 skill”的安全约束。

```
session 会话
  ├─turn_end（每轮，规则，0 token）────> 候选事实 ──> session 层（立即可用）
  ├─turn_review（每 10 轮，旁路）──────> 候选 + lesson ──> staging
  └─session_close（总轮数 ≥ 6）────────> summary + 候选 ──> staging
                                                    │
                       consolidate（每日 03:00 / 启动补跑 / 手动）
                         ├ 语义去重/合并
                         ├ 冲突检测 → supersedes
                         ├ 晋升：fact ≥2 会话；lesson 1 次 + 证据
                         ├ 衰减 + 淘汰
                         └ skill_evolve ──> 提案 ──> core skills/pending/
```

**staging 是必须的**：一次会话里的推断不能直接进长期记忆，否则模型的自言自语会变成"事实"。staging 要求同一事实在不同会话出现 ≥2 次，或用户显式确认。

**`lesson` 是唯一的例外**（AMEND-19/D81）：它允许“单次 + 证据”就进 `agent:lessons`，因为教训的价值随时间衰减——等第二次踩坑才记，等于没记。代价用三件事抵住：① `confidence ≤ 0.6` 且召回时标 `unverified`；② 必须带 `evidence`（哪个 turn、哪个工具结果、哪条断言被推翻）；③ 二次验证后升 `0.8 / verified`，长期不被验证则随 recency 衰减出队。

### 16.3 熵增的五种形态与对策

用户明确要求「避免熵增」。熵增不是一种问题，是五种，各有对策：

| 形态 | 表现 | 对策 | 实现位置 |
|---|---|---|---|
| **重复** | 同一事实存了 5 遍，措辞不同 | 内容哈希去重（精确）+ consolidate 语义聚类（近似，只合并不删除原文） | M4a / M4b |
| **矛盾** | 旧说 A、新说 B，两条都在 | **不覆盖**：新条写 `supersedes=旧id`，旧条写 `superseded_by` 并软失效 | M4a |
| **过时** | 项目改了，旧结论还在被召回 | 时间衰减进 score；`recall` 权重含 recency；支持显式 `forget` | M4a |
| **膨胀** | skill 越来越多，职责重叠 | active 上限 `skills.active_limit`（默认 64）；`create` 前必须先检索，命中相似则只能 `patch`；L0 目录另有 token 预算（§16.5） | M4b + C3b |
| **腐化** | skill 指向已不存在的工具/路径 | 加载时校验 `requires_tools`；连续 3 次调用失败转 `stale` 并移出注入（内存计数，重启清零） | C3b |

评分与淘汰（M4a）：

```
score = 0.45 * importance        # LLM 打 1–5；显式 remember 固定 5
      + 0.30 * exp(-age / 30d)   # 30 天半衰
      + 0.20 * log1p(hit_count)  # 用得多的留下
      - 1.00 * (superseded_by IS NOT NULL)
```

超配额时按 score 升序淘汰，**淘汰进 `archive` 表而不是物理删除**（可恢复，且保留审计）。物理删除只发生在 `forget` 与 session TTL。

### 16.4 冲突与版本

```sql
-- 在 memories 表上追加
supersedes     TEXT,      -- 我替代了谁
superseded_by  TEXT,      -- 我被谁替代（软失效）
confidence     REAL,      -- 0–1，evolver 给；显式 remember 固定 1.0
source         TEXT,      -- 'user' | 'evolve' | 'import'
session_id     TEXT NOT NULL DEFAULT '',  -- session 层用实际 id；global 层存空串（**不能用 NULL**：SQLite 的 UNIQUE 里 NULL 互不相等，会让 global 去重失效，见 FR-6.10）
kind           TEXT NOT NULL DEFAULT 'fact',  -- fact | preference | lesson
evidence       TEXT,      -- lesson 必填：{turn_id, tool, result_digest, claim}
verified_at    INTEGER,   -- lesson 二次验证时间；NULL = unverified
hit_count      INTEGER DEFAULT 0,
last_hit_at    INTEGER
```

`kind` 不是标签，是**晋升规则的分支**：`fact` / `preference` 走 ≥2 会话（D43）；`lesson` 走“1 次 + 证据”（D81）。把它做成列而不是 tags 里的一个值，是为了让 consolidate 能按类型选规则、让 `recall` 能按类型加权（`lesson` 默认排在同分 fact 之后，避免一句教训压过十条事实）。

- `recall` 默认 `WHERE superseded_by IS NULL`；调试时 `include_superseded=true` 才返回历史链。
- 冲突判定由 consolidate 做：**同一 namespace 下语义相似度 > 0.9 且结论否定/相反**。判不准就**两条都留**（宁可冗余，不可误删）——误删一条真偏好，比多存一条废话代价大得多。

### 16.5 skill 演进与审核

skill 是 core 的本地文件（D38），模块碰不到。所以走提案：

```
M4b 产出提案 ──skill/proposal──> core 校验 ──> skills/pending/<name>/
                                                （meta.origin=proposal）│
                            人工审核（admin /skills）──> skills/<name>/
                                                │        （auto_approve=true 时跳过审核）
                                                └──> 拒绝则进 .rejected/，7 天后清

管理员安装 ──preview→install──> skills/pending/<name>/
                                （meta.origin=install）│
                            人工批准（admin /skills）──> skills/<name>/
```

**统一布局（v1.15 起 `pending/` 由扁平文件改为目录）**

```
skills/
  <name>/SKILL.md + meta.json          # 生效中
  pending/<name>/SKILL.md + meta.json  # 待批准，origin ∈ {install, proposal}
  .history/<name>/<rev>.md             # 历史版本
  .rejected/<name>-<ts>/               # 被拒，7 天后清
  .trash/<name>-<ts>/                  # 卸载，可恢复
```

改目录的理由：安装来的 skill 可能带资源文件（模板、示例），单文件 `.md` 装不下；且 `meta.json` 需要和正文同目录才能原子替换。**四条排除规则只写一次**：`SkillLoader` 扫描 `skills/*/`，显式跳过 `pending`、`.history`、`.trash`、`.rejected`——这四个名字**保留**，不允许作为 skill 名。

**`SKILL.md` 必须是四段式**（AMEND-19/D82，C-11 修订）：

```markdown
## When to Use   # 触发条件（可判断，不是“当你需要时”）
## Procedure    # 步骤（可复用，不含本次路径/ID）
## Pitfalls     # 已知失败模式与规避（必填，至少 1 条）
## Verification # 怎么确认做对了（可执行命令或可观察结果）
```

`Pitfalls` 与 `Verification` **必填**。理由很直接：`skill_evolve` 最该沉淀的不是“成功路径”（那东西模型本来就会），而是“哪里会错、怎么验”。没有这两段，生成的 skill 只是一篇更长的 prompt，不会让下一次少犯错。生成后自检缺段 → 重写一次，再缺就丢弃并记 `evolve_schema_invalid`。

- 提案必须是**完整**的 `SKILL.md`（符合 C-11），不是 diff；patch 也提交全文 + `base_revision`。
- `base_revision` 与当前不符 → 拒绝（防止基于旧版本改出新旧混合）。
- 每个 skill 带 `revision`（单调递增），历史存 `skills/.history/<name>/<rev>.md`。回滚 = 复制旧版 + `rev+1`，**不删历史**。
- `pending/` 里的内容**不参与注入、不进工具表**。这是硬约束：否则模块等于能间接写 skill，D38 就形同虚设；安装路径同理，否则「装完即生效」等于绕过了人工审阅（A54）。

**三层加载**（AMEND-19/D83）：

| 层 | 内容 | 何时进上下文 | 预算 |
|---|---|---|---|
| **L0 目录** | 每行 `name: description`（+ `unavailable` / `stale` 标记） | **常驻** | `skills.l0_token_budget`（默认 3000）；超了按 `(激活优先, hit_count 降序, name 字典序)` 截断 |
| **L1 正文** | `SKILL.md` 全文 | 仅被激活的（≤3 个） | 合计 ≤ 12KB |
| **L2 引用** | `references/*.md`、模板、示例 | **永不自动注入**，模型用 `fs` 按需读 | 无（读多少算多少） |

- 目录超预算时**不是“只留 name”**（v1.16 的做法），而是按使用频次裁剪——只留 name 的信息量等于没有，模型不知道要不要激活。
- `references/` 允许存在（安装的 `.zip` 可以带资源），但 `SkillLoader` 只扫 `SKILL.md`，不递归；正文里用相对路径引用，不拼接绝对路径。
- 这样 64 个 active skill 的常驻成本固定在 3k token 量级，与实际激活数无关。

### 16.6 与 Hermes Agent 的差异

| 维度 | Hermes Agent | 本方案 | 为什么不同 |
|---|---|---|---|
| 触发 | **每轮结束后跑一次后台复盘**（`nudge_interval` 默认 10，可配）；由 harness 强制，不是模型自觉 | 五个确定性触发器，其中 `turn_review` 每 10 轮一次 | 两者都是确定性触发。差别在**落点**：它复盘后直接改记忆/技能，我们让 evolver 读结构化骨架、只写 staging |
| 记忆预算 | 字符硬顶（`memory_char_limit` 2200 / `user_char_limit` 1375，可配），**写超即报错**，agent 当轮自己腾 | 字符硬顶（`session_inject_char_limit` 4200，可配）+ 条数双闸，**写超返回 `MEMORY_FULL`** | 同一个思路：把预算变成显式错误，逼模型做取舍。我们数值更大、按 namespace 分区，能承载多项目 |
| 写入路径 | agent 用 `memory` / `skill_manage` 直接改文件；`write_approval=true` 时才暂存 | 模块只提提案 → core 落地 → `pending/` → 人工批准 | 模块跨机运行，不能碰 core 文件系统；且我们的默认值更严（它默认自由写） |
| 记忆结构 | 两个 Markdown 文件 + 冻结快照 + FTS5 跨会话检索 | 两层（session / global）+ 向量召回 + TTL + `kind` 分类 | 单文件无并发、无版本、无审计；但**FTS5 跨会话检索我们该抄**（v1.18 补） |
| 加载 | L0 目录 ~3k token 常驻 / L1 全文按需 / L2 引用按需 | 同三层（D83） | 直接借鉴：64 个 skill 不可能全塞 |
| 熵增 | 靠 skill 自 patch 自愈 +  curator 后台清理 | 五类形态各有对策 + 硬配额 + 归档 | 自 patch 只处理"腐化"，治不了重复/矛盾/膨胀 |
| 审计 | 有 pending/审批开关，但默认自由写入 | 每次演化有 provenance、revision、可回滚；默认必须人工批准 | 企业向；学错了要能查、能退。**它的 `write_approval` 证明"可选审批"是可行的，只是默认值不同** |
| 关闭 | 三个键分散：`memory.memory_enabled` / `memory.write_approval` / `skills.write_approval` | 一个 `[evolve] enabled=false`，外加 `allow_transcript` / 指纹白名单 | 它按功能分开关，我们按模块分——模块能读全量转录，边界必须更粗更硬 |

**v1.16 曾把 Hermes 写成「靠 system prompt 提醒模型自觉、不可测不可关」，这是误读，v1.17 已改**（AMEND-19）。原判断导致我们绕开了它最有价值的部分：轮次级、低成本、字符硬顶、四段式 skill。绕开的代价就是 v1.16 那套“会话末吞全文 + 只学成功 + 200 条全量注入”。

明确不做的：用户画像（Honcho 那类跨会话人格建模）、prompt 自动变异（DSPy/GEPA 那类）、skill 自动上线。前两个是研究向，第三个是安全红线。

### 16.7 权限与开关

`M4b` 是**全系统权限最大的模块**：它能读到完整会话转录。边界必须写死：

- 默认 `[evolve] enabled = false`，显式开启才订阅事件。
- **有全局令牌桶**：`[evolve] daily_token_budget`（默认 200000）由 `turn_review` / `session_close` / `consolidate` / `skill_evolve` 共享；耗尽即降级为纯规则抽取，次日 00:00 重置，日志 `evolve_budget_exhausted`。分项预算（条数、提案个数）只管输出，管不住输入侧膨胀——必须有这一道总闸。
- **滚动摘要也计入总预算**：`session_close` 只读 rolling summary + 最后 N 轮，不重读全文；`summary_token_ratio`（默认 0.05）决定每次压缩的摘要预算。
- 订阅走**三重**白名单：① manifest 声明 `subscriptions`（`session.turn` / `session.closed`，两个独立主题，权限可分别授予）；② `[evolve] allow_transcript = true`；③ **`module_id + 公钥指纹` 在 `[evolve] allowlist` 里**。第三条是关键——前两条都写在不可信侧，任意模块都能声明 `role=evolve` 并订阅全量转录；只有 core 侧的指纹白名单能挡住。
- 推送内容**由 core 强制裁剪**，默认 `digest(tool_args)` + 丢弃 `file_content` + `digest(tool_results)`；模块只能在 `events/subscribe` 里**追加**要裁的字段，**不能减少**。模块传 `redact: []` 不生效——裁剪清单在 core 配置里，不在请求里。
- 审计：每次推送记 `{module_id, instance_id, session_id, turns, bytes}`，**不记内容**。
- 关闭后：已写入的记忆与 skill 保留，不再产生新提案。

## 已确认（2026-11-07）

| # | 议题 | 结论 |
|---|---|---|
| 1 | W-A 跑不通是否接受 W-B | 接受。C0 三天不通即降级，协议多一份实现，靠 KAT 交叉验证兜住 |
| 2 | glass 在 Desktop/Web 的强度 | 只做模糊，不做折射 |
| 3 | 三皮肤交付范围 | 三个全部进 v1 |
| 4 | 开发组织 | 并行子代理开发 + 独立子代理审查 + 原代理返工（resume） |

下一步：阶段 3 拆任务，从 **C0** 开始。
