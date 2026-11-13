# 契约台账（所有 agent 开工前必读）

> 并行开发的唯一前提是**接口先冻结**。本文件列出所有跨 agent 的契约：谁产出、什么时候冻结、谁消费。
> 冻结 = 文件已提交且带版本号；此后只能新增可选字段，不能改语义、不能改必填项。

## 一、契约清单

| ID | 文件 | 所有者 | 冻结时点 | 消费者 |
|---|---|---|---|---|
| **C-01** | `protocol/v1/frames.md`（帧格式、握手、错误） | C1 | W1 末 | C2 / C4 / U1 / F1 |
| **C-02** | `protocol/v1/kat.json`（已知答案向量） | C1 | W1 末 | C2 / C4 / U1 / W-B 兜底 |
| **C-03** | `schema/manifest.schema.json` | C2 | **W2 第 1 天** | C4 / M1–M6 / C5a |
| **C-04** | `schema/errors.json`（错误码枚举） | C2 | **W2 第 1 天** | 全部（C1 先用 Rust 枚举，W2 做机器比对） |
| **C-05** | `schema/rpc.json`（方法表，机器可读） | C2 | **W2 第 2 天** | C3 / C4 / U1 |
| **C-06** | `schema/events.json`（core→UI 事件表） | C3 | **W3 第 1 天**（含 `turn.approval_resolved`） | C4 / C5a / U1 |
| **C-07** | `sdk/PYTHON_SDK.md` + `vade_sdk` 公开面 | C4 | **W4 末（G1）** | M1–M6 / F1 |
| **C-08** | `schema/admin-api.yaml`（OpenAPI 3.1，含 skills 审核/**安装**、memory 管理、devices 端点、`/admin/setup/unlock`、`/admin/bind*`） | C5a | **W4 第 1 天**（当天第一件事） | C5b / F1 |
| **C-09** | `ui/.../skin/Skin.kt`（组件工厂接口 + `SkinRegistry`） | U1 | **W4 第 3 天** | U2 / U3 / U4 |
| **C-10** | `ui/shared/api/ProtocolClient.kt`（Kotlin 绑定面 + 语言中立方法表） | U1 | **W4 末** | U2 / U3 / U4 |
| **C-11** | `schema/skill.schema.json`（`SKILL.md` frontmatter + **四节必填** `When to Use`/`Procedure`/`Pitfalls`/`Verification` + 注入顺序 + `{name,revision,status}` 目录快照） | C3b | **W4 中** | C3b（skill-loader）/ C3d / F1 |
| **C-12** | `schema/memory.schema.json`（记忆记录 + `staging` + `archive` 字段与枚举） | M4a | W5 中 | M4b / C3d / C5a（admin 查看） |
| **C-13** | `schema/skill-proposal.schema.json`（提案载荷 + `base_revision` 语义） | C3d | W4 中 | M4b / C5a（审核页） |
| **C-14** | `schema/events.json` **追加**两个事件（不改已有定义，向后兼容）：`session.turn` / `session.closed` | C3d（在 C-06 上追加，**C3d 有追加权，无改写权**） | W4 中 | M4b |
| **C-15** | `schema/pair-code.json`（字符集、256 字汉字表、`normalize()` 规则、PSK 派生测试向量） | **C1**（PSK 派生在 C1，归一化向量也由它出；C2 的 `pairing.py` 只调用） | **W1 第 2 天** | C2 / C4 / C5a / C5b / U1 |
| **C-16** | `schema/memory-admin.json`（`memory/admin_*` 六个私有 RPC 的入参出参与 `MemoryQuery` 定义；**不进工具表**） | M4a | **W5 中**（与 C-12 同批） | C3 / C5a |
| **C-17** | `schema/device-bind.json`（绑定协议：`vade://bind` URI 字段与校验（含 `tls`）、`/admin/bind/info` 的 `hello_sig` 与信封结构、`BIND_*` 错误码、设备公钥编码 `p256/ed25519`） | **C5a**（获准写 `schema/`） | **W4 第 2 天**（T1 出骨架，T4 补签名与错误码） | U1 / U4 / F1 / R3 |
| **C-18** | `schema/config.schema.json`（`core.toml` 全量键、默认值、`hot\|restart` 生效方式、所有者；`config.changed` 审计载荷；`data/config.overrides.json` 的合并规则） | **C2** | **W2 中**（与 C-02 同批） | C3 / C3b / C5a / M4a / M4b / F1 |

> C-08 与 C-09 必须在**实现之前**提交。C5b 和 U2/U3 拿不到冻结文件就不得开工——mock 也要对着契约 mock。
>
> **冻结时点一律是「生产者自己波次内的 D+n」，不是全局固定周次。** 早于生产者波次的契约不存在——C5b 等不到 C-08、U2 等不到 C-09，不是他们慢，是表写错了。历史版本把 C-03～C-11 全部标成 W1–W3 冻结，与 C2/C3/C4/C5a/C3b 分别在 W2/W3/W4/W4/W4 的事实矛盾，已按实际波次改正（AMEND-11）。
>
> **C-01/C-02（帧格式与握手）是唯一在 W1 就冻结的**，因为 C1 是实现方，且 C0a/C0b 要先验证绑定。

## 二、错误码（C-04 的唯一来源）

```json
{
  "INVALID_ARGUMENT":   {"retriable": false, "http": 400},
  "NOT_FOUND":          {"retriable": false, "http": 404},
  "PERMISSION_DENIED":  {"retriable": false, "http": 403},
  "TIMEOUT":            {"retriable": true,  "http": 504},
  "CANCELLED":          {"retriable": false, "http": 499},
  "UNAVAILABLE":        {"retriable": true,  "http": 503},
  "OVERLOADED":         {"retriable": true,  "http": 429},
  "MEMORY_FULL":        {"retriable": true,  "http": 409},
  "PAYLOAD_TOO_LARGE":  {"retriable": false, "http": 413},
  "UNSUPPORTED":        {"retriable": false, "http": 501},
  "PROTOCOL_MISMATCH":  {"retriable": false, "http": 400},
  "MANIFEST_INVALID":   {"retriable": false, "http": 400},
  "TOOL_CONFLICT":      {"retriable": false, "http": 409},
  "RISK_DOWNGRADE_FORBIDDEN": {"retriable": false, "http": 403},
  "APPROVER_INVALID":   {"retriable": false, "http": 403},
  "ORIGIN_MISMATCH":    {"retriable": false, "http": 403},
  "READ_ONLY_SESSION":  {"retriable": false, "http": 403},
  "BIND_UNAUTHORIZED":  {"retriable": false, "http": 401},
  "BIND_FINGERPRINT_MISMATCH": {"retriable": false, "http": 409},
  "BIND_HELLO_SIG_INVALID":    {"retriable": false, "http": 403},
  "BIND_DEVICE_LIMIT":  {"retriable": false, "http": 409},
  "BIND_CLOCK_SKEW":    {"retriable": false, "http": 400},
  "BIND_KEY_UNSUPPORTED": {"retriable": false, "http": 400},
  "BIND_SETUP_LOCKED":  {"retriable": true,  "http": 423},
  "AUTH_FAILED":        {"retriable": true,  "http": 401},
  "ADMIN_LOCKED":       {"retriable": true,  "http": 429},
  "SETUP_UNAUTHORIZED": {"retriable": true,  "http": 401},
  "MODULE_DRAINING":    {"retriable": true,  "http": 503},
  "INTERNAL":           {"retriable": false, "http": 500},
  "CONFIRM_REQUIRED":   {"retriable": false, "http": 409},
  "UNKNOWN_CONFIG_KEY": {"retriable": false, "http": 400},
  "CODE_INVALID":       {"retriable": false, "http": 400},
  "CODE_EXPIRED":       {"retriable": false, "http": 400}
}
```

规则：
- 模块**只准**返回上表中的 code，不许自定义字符串。新增 code 走 RFC。
- `CONFIRM_REQUIRED`：管理面写危险配置项（`evolve.enabled=false` / `daily_token_budget=0` / `nudge_interval=5`）缺 `confirm:true` 时返回（§7.9 三项二次确认）。
- `UNKNOWN_CONFIG_KEY`：`core.toml` / `overrides` 出现未知键 → 拒绝启动或 400（C-18 全量 schema，拼写错误不静默用默认值）。
- `CODE_INVALID` / `CODE_EXPIRED`：配对码校验失败（码错/已用）与超时（>180s）两种，供 UI 分提示，**均不回显码本身**。
- `RISK_DOWNGRADE_FORBIDDEN`：`capabilities.update` 试图下调 risk（A37）。
- `APPROVER_INVALID`：`approval/decide` 的调用方不具备批准权（浏览器 / 非批准设备，A38）。
- `ORIGIN_MISMATCH`：管理面请求的 `Origin` 不在允许集合内，或 `Sec-Fetch-Site` 不是 `same-origin`（A42）。**只在管理面用**，不发给模块。
- `READ_ONLY_SESSION`：已认证的 **cookie** 会话发起写操作。**Bearer 永远不会拿到这个码**；`login` / `login/challenge` / `logout` / `token/refresh` / `setup*` / `health` 豁免。
- `BIND_*`：设备绑定专用，语义见方案 §7.12.5。`BIND_SETUP_LOCKED`（423）表示 setup 页面处于未解锁态，需要先 `POST /admin/setup/unlock`。
- `AUTH_FAILED`：管理面登录失败。**密码错、key 未登记、签名错、未初始化，全部用这一个码**，不允许细分（细分等于告诉攻击者「密码对了，钥匙不对」）。
- `ADMIN_LOCKED`：管理面登录被限流锁定，带 `retry_after`。
- `SETUP_UNAUTHORIZED`：setup 阶段的准入失败——非回环来源未带 `setup_token`，或 token 不匹配（A47/A48）。**缺 token 与错 token 用同一个码**，不区分。已初始化后的 `/admin/setup` 返回 404，不用这个码。
- **不要用 `FORBIDDEN`**——v1.1 的任务书里出现过这个名字，它不在表里，模块会拒收。统一用 `PERMISSION_DENIED` 或上面两个专用码。
- 人读信息放 `message`（≤ 300 字，不泄漏路径以外的环境信息、不回显密钥）。
- 裸异常（traceback）绝不跨越进程边界，进程内日志可以打。

## 三、统一返回封套

**工具调用（模块 → core）**

```json
{"ok": true,  "result": {}, "truncated": false, "duration_ms": 12}
{"ok": false, "error": {"code": "PERMISSION_DENIED", "message": "...", "retriable": false}}
```

**RPC 层（JSON-RPC 2.0）**：成功走 `result`，失败走 `error{code, message, data}`；`data` 只放结构化字段，不放堆栈。

## 四、manifest 必填字段（C-03）

| 字段 | 约束 |
|---|---|
| `module_id` | 反向 DNS，小写，`^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$` |
| `instance_id` | `^[a-z0-9][a-z0-9-]{0,31}$`，同 module 内唯一 |
| `version` | 语义化版本 |
| `protocol` | `vade/v1`，主版本不匹配即拒 |
| `concurrency` | `exclusive` / `pooled` / `mirrored`，缺省 `exclusive` |
| `capabilities.tools[]` | 每项含 `name` / `description` / `input_schema` / `risk` / `timeout_ms` / `idempotent` |
| `limits` | `max_concurrency` / `max_payload` |

可选：`display_name`、`priority`（model 模块选择用）、`extensions`（core 不解析、不透传）。

**工具名**：`^[a-z][a-z0-9_]{0,63}$`；单模块 ≤ 64，全局 ≤ 128；`module_id/tool` 全局唯一，冲突拒绝后注册者。

## 五、目录所有权

| 目录 | 所有者 | 其他人 |
|---|---|---|
| `protocol/core/`、`protocol/v1/` | C1 | 只读 |
| `schema/` | C2（管理**公共** schema：`manifest`/`errors`/`rpc`/`events`/`config`；追加 `admin-api.yaml`） | 只读 |
| `core/vade/transport/`、`crypto_gateway/`、`registry/`、`identity/` | C2 | 只读 |
| `core/vade/{__main__,app,config,logging}.py` | C2 | 只读（其他批次通过 `install(app)` 接入） |
| `core/vade/slots.py` | C3 | 只读（C2 的 `app.py` 从这里 import 并调用 `install_slots(app)`） |
| `core/vade/session/`、`router/`、`loop/`、`llm/` | C3 | 只读 |
| `core/vade/skill/`（不含 `proposal.py`） | C3b | 只读 |
| `core/vade/events/`、`skill/proposal.py` | C3d | 只读 |
| `core/vade/policy/`、`audit/`、`admin/` | C5a | 只读 |
| `sdk/python/` | C4 | 只读 |
| `vade-module-<name>`（独立仓） | 对应 M 代理 | 主仓只读（主仓不含模块源码） |
| `vade-module-memory`（独立仓，含 `manifest.json`、建表 SQL、`evolve/` 空包预置） | **M4a** | M4b 只写 `evolve/` 内已存在的文件，不改 manifest 与 store |
| `vade-module-memory/src/memory_module/evolve/` | M4b（内容） | 目录骨架由 M4a 在 W5 预置 |
| `ui/shared/`、`ui/composeApp/src/commonMain/kotlin/core/`、`skin/material/`、`skin/Skin.kt`、`skin/SkinRegistry.kt` | U1 | 只读 |
| `ui/composeApp/src/*Main/{android,desktop,wasmJs}/`（仅 `skin/glass/` 子树） | U3 | 树其余部分归 U1 |
| `ui/shots/`（`material/` / `miuix/` / `glass/` 三个子目录各自归属） | U1 / U2 / U3 | F1 只读汇总 |
| `ui/composeApp/src/commonMain/kotlin/skin/miuix/` | U2 | 只读 |
| `ui/composeApp/src/commonMain/kotlin/skin/glass/` | U3 | 只读 |
| `ui/ios/` | U4 | 只读 |
| `admin-ui/src/pages/`、`src/app/`、`package.json`、`vite.config.ts`、`tsconfig.json`、`dist/` | C5a | 只读（C5a 负责在 W4 预置 reactflow 依赖与 `/topology` 常驻路由） |
| `admin-ui/src/canvas/` | C5b | 只读 |
| `deploy/`、`tests/e2e/`、`release/` | F1 | 只读 |
| `spike/<name>/` | 对应 C0x | 只读，验收后可删 |
| `schema/skill.schema.json` | C3b | 只读 |
| `schema/skill-proposal.schema.json` | C3d | 只读 |
| `schema/memory.schema.json` | M4a | 只读 |
| `schema/pair-code.json` | **C1** | 只读 |
| `schema/memory-admin.json` | **M4a** | 只读 |
| `schema/device-bind.json` | **C5a**（获准写 `schema/`） | 只读 |
| `tests/fixtures/`、`core/tests/{transport,registry}` | C2 | 只读 |
| `core/tests/{session,router,loop,e2e}`、`core/tests/test_skill` | C3 / C3b | 各自只读 |
| `core/tests/{policy,audit,admin}` | C5a | 只读 |
| `docs/adr/` | 提案批次（C0b 首发） | 追加式，不改他人 ADR |
| `data/`、`core.toml` | 运行时产物，**不入库** | 加入 `.gitignore`，合并门禁排除 |

跨目录改动的唯一合法路径：**RFC**（见第七节）。发现别人目录里有 bug，提 issue 描述，不改代码。

## 六、命名与版本

| 项 | 规则 |
|---|---|
| 协议版本 | `vade/v1`；主版本不同拒连，次版本告警放行，补丁忽略 |
| SDK 版本 | SemVer；`0.x` 期间次版本可破坏，模块必须锁 `==0.1.*` |
| 工具 ID | 协议内 `module_id/tool`，LLM 侧 `{最后一段}__{tool}`，冲突加序号 `fs2__` |
| 事件名 | `域.动作`，过去式：`turn.completed`、`module.status` |
| 日志字段 | 每行 JSON：`ts / level / module_id / instance_id / tool / duration_ms / code` |

## 七、契约变更 RFC（唯一合法改法）

1. 提 RFC：在 `docs/rfc/RFC-<序号>.md` 写「改什么、为什么、谁受影响、怎么兼容」。
2. 编排者裁决：影响已冻结契约的，必须先改契约文件 + 升版本，再改实现。
3. 通知消费方：在台账表登记，列出需要适配的批次。
4. **禁止**边做边改：实现里先加字段、回头补契约，一律打回。

## 八、变更记录

| 日期 | 契约 | 变更 | 影响 |
|---|---|---|---|
| 2026-11-07 | 全部 | 初始冻结计划 | — |
