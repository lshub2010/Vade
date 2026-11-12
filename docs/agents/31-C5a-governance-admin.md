# C5a · 审批 + 审计 + Admin API + 治理页（G2）

| 项 | 值 |
|---|---|
| Agent 代号 | `c5a-admin` |
| 波次 | W4（与 C4 / U1 并行） |
| 依赖 | C3 的 `ApprovalPort` / `AuditPort` / 事件总线、C2 的 registry 查询面 |
| 目录所有权 | `core/vade/policy/`、`audit/`、`admin/`、`admin-ui/src/app/`、`admin-ui/src/pages/` |
| 产出 | 审批通道 + 审计库 + `/admin/api/*` + 列表/审计/设置/技能/记忆页 |
| 出口 | **G2**；**C-08 `schema/admin-api.yaml` 必须在第 1 天冻结**（C5b 等它）；**C-17 `schema/device-bind.json` 第 2 天冻结**（U1/U4 等它） |

## 一、一句话任务

让高危操作必须有人点头、每次点头都留痕、管理员能在一个页面上看到「谁连上来了、干了什么」，且这个页面**一个字的气密信息都不许漏**。

## 二、边界

| 做 | 不做 |
|---|---|
| risk 判定、审批挂起/超时/放行、会话级放行 | 不做拓扑画布（C5b） |
| 审计写入、查询、保留策略、不可改 | 不记录 LLM 全文 prompt（只记 digest） |
| Admin API + 鉴权 + 字段白名单 + SSE | 不做对话界面（U 组） |
| 列表页、审计页、登录页 | 不做 L3 部署编排（D32） |

**第 1 天必须先交 `schema/admin-api.yaml`**，哪怕实现是空的。C5b 与 F1 都按它写。

## 三、必读

- `docs/design.md` §6（审批）、§7 全节（配置页、字段白名单、布局、状态推送）
- `docs/requirements.md` FR-7 / FR-9 / FR-10、A2（token 与 cookie）、A19/A20
- `docs/agents/01-contracts.md` §二、§三
- `docs/agents/22-C3-session-loop.md` §四 T4（ports 定义）

## 四、已裁决的冲突

| 冲突 | 裁决 |
|---|---|
| ~~需求 60s vs 方案 120s~~ | **不成立**：方案 v1.9 §6 就是 60s，与需求 FR-7.3 一致。这条是早期版本遗留，已删除——别再照它写实现说明。 |
| ~~cookie 8h vs 30min~~ | **不成立，且已过时**：v1.15 起 App 用 Bearer（access 15min / refresh 30d），cookie 只在**浏览器只读**模式存在。这行是 AMEND-13 时期的裁决，不要再按它实现。 |

## 五、任务分解

### T1 · C-08 契约（第 1 天上午，先做这个）+ C-17 骨架（第 2 天）

本批交付**两个** schema 文件：
- `schema/admin-api.yaml`（**C-08**，第 1 天上午冻结，C5b / F1 等它）
- `schema/device-bind.json`（**C-17**，T1 出骨架、T4 补 `hello_sig` 与 `BIND_*` 错误码，**第 2 天冻结**，U1 / U4 / F1 / R3 等它）

`schema/admin-api.yaml`（OpenAPI 3.1）至少包含：

```yaml
servers:
  - url: https://127.0.0.1:8444     # 管理面独立 origin；不要写 :8443
```


| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/setup` | 未初始化：**回环** → 200 `{setup_required:true, requires_token:false, bind_url, fp8}`；**非回环且未解锁** → 200 **只有** `{setup_required:true, requires_token:true}`，**不得含 `bind_url` / `fp8` / 任何 `acs_` 串**；**已解锁** → 200 含 `bind_url` 与 `fp8`。已初始化 → **302 `/login`**（AMEND-18/A58） |
| POST | `/admin/setup/unlock` | `{setup_token}` → 验过则设 `__Host-ac_setup`（10 分钟、HttpOnly、SameSite=Strict、仅 setup 路由）并返回 `{bind_url, fp8, expires_at}`；缺/错 → `401 SETUP_UNAUTHORIZED`。**这是二维码的唯一闸门** |
| POST | `/admin/setup` | `{setup_token?, password, pubkey?}` → 初始化。回环免 token；非回环缺/错 token → `401 SETUP_UNAUTHORIZED`。**这是电脑路径**；手机路径走 `/admin/bind` |
| GET | `/admin/bind/info` | 请求头 `X-Bind-Ticket` → `{core_pub, fp8, kex_pub, challenge, setup_required, expires_at, hello_sig}`。`hello_sig = Ed25519(core_identity, "vade-bind-hello-v1\n" ‖ kex_pub ‖ challenge ‖ SHA256(ticket) ‖ admin_host)`；App 用已 pin 的 `core_pub` 验签，失败 `BIND_HELLO_SIG_INVALID`。票无效一律 `401 BIND_UNAUTHORIZED` |
| POST | `/admin/bind` | body `{ticket, eph_pub, nonce, ct}`（**ticket 明文**，其余在密文里；`ct` 内含 password/device_pub/alg/label/hardware_backed/ts/sig）。AAD = `"vade-bind-v1" ‖ fp8 ‖ SHA256(ticket) ‖ challenge ‖ kex_pub`。**先验后删**：解密→验签→限额全过才 `DELETE ... WHERE consumed_at IS NULL`，失败 ROLLBACK 不消费票。未初始化时**同时完成 setup**；成功 → `{device_id, label, access_token, refresh_token}` |
| POST | `/admin/api/login/challenge` | 无鉴权 → `{challenge_id, challenge, expires_at, devices:[{device_id,label,fp8,alg}]}`；60s 一次性，绑 `challenge_id` + 来源 IP `/24` + 设备表版本。**不绑 TLS session id**（HTTP/2 连接池下它不唯一） |
| POST | `/admin/api/login` | `{challenge_id, password, device_id?, signature?}` → `{access_token, refresh_token}`（**Bearer，不设 cookie**）；非回环必须带签名。**`mode=webview` 时由原生发起**：`refresh_token` 进钥匙串、不回给页面，页面只拿 `access_token`。**回环且无设备签名、无 `X-AC-Client: cli` 时**，只 `Set-Cookie __Host-ac_admin`（只读），响应体不含任何 token |
| POST | `/admin/api/token/refresh` | `{refresh_token, signature}` → 新 access；**每次刷新都要设备签名**，签名内容含 `refresh_jti` |
| POST | `/admin/api/logout` | 删 refresh + `jti` 进黑名单 |
| POST | `/admin/api/password` | `{old_password, new_password}` → 改密；成功后**吊销除当前外的所有会话**；`role` 不支持，v1 只有一个管理员 |
| POST | `/admin/api/logout-all` | 吊销当前账户的**全部**会话（含自己），用于「设备丢了」 |
| GET | `/admin/api/devices` | 设备列表：`{device_id, label, alg, fp8, hardware_backed, added_at, added_from_ip, last_used_at}`。**不返回公钥原文** |
| POST | `/admin/api/devices` | `{password, pubkey, alg, label}` → 登记新设备。**step-up**：body 里的密码必须是当前密码；上限 5 台 |
| POST | `/admin/api/devices/{id}/revoke` | 吊销该设备并踢掉它换发的所有会话 |
| POST | `/admin/api/devices/ticket` | `{password}` → `{bind_ticket, bind_url, expires_at}`（10 分钟、一次性）。**step-up**；用于「加一台新设备」 |
| GET | `/admin/api/nodes` | 节点列表（白名单视图） |
| GET | `/admin/api/nodes/{id}` | 单节点详情 |
| PATCH | `/admin/api/nodes/{id}` | 启用/停用、并发模型覆盖、risk 覆盖。响应体必须含 `{impact:{tool_count, tool_names[], sessions_affected}, audit_id}`——C5b 的确认弹窗要显示影响面，**不能前端自己数** |
| POST | `/admin/api/nodes/{id}/revoke` | 吊销（破坏性） |
| POST | `/admin/api/nodes/{id}/disconnect` | 断开重连 |
| POST | `/admin/api/invites` | 创建邀请位（返回 pair_code + QR payload，**仅此一次明文**） |
| DELETE | `/admin/api/invites/{id}` | 撤销邀请 |
| GET | `/admin/api/audit` | `from/to/module_id/limit/cursor` |
| GET | `/admin/api/config` | `{items:[{key, value, default, source, mode:"hot"|"restart", min?, max?}]}`；**只返回 §7.9 白名单键**，密钥类一律不返回 |
| PUT | `/admin/api/config` | `{key, value, confirm?}` → `{applied, needs_restart?, audit_id}`。未知键/越界 → 400；三项危险项缺 `confirm:true` → 409 `CONFIRM_REQUIRED` |
| GET | `/admin/api/config/usage` | `{day, tokens_used, tokens_budget, reviews_today, proposals_today}`；页面进度条用，**不含任何记忆内容** |
| GET | `/admin/api/layout` | 全量布局 |
| PATCH | `/admin/api/layout` | `{rev, nodes:[{id,x,y,label,group_id}]}`，冲突 409。**L1 布局（含 `label`）不记审计**——改了别名就写审计，审计表会被拖垮 |
| POST | `/admin/api/layout/auto` | 力导向重算并落库，返回新布局。不做的话 C5b 只能前端 `d3-force` 兜底，每次刷新布局都会变 |
| GET | `/admin/api/skills` | skill 列表：`{name, revision, status, origin, created_by, derived_from}`；`status ∈ {active, pending, disabled, stale, unavailable}`（`rejected` 是**目录**不是状态，不出现） |
| GET | `/admin/api/skills/{name}` | 单个 skill 详情：当前 body、`revision`、`pending` 提案的 **diff**、历史摘要。**diff 由后端算，前端不解析 YAML** |
| POST | `/admin/api/skills/{name}/approve` | `{revision}` → 从 `pending/` 转正（**install 与 proposal 两条路径共用**，按 `meta.origin` 决定是否需要 `revision` 匹配），`rev+1`，写 `.history/`，热加载 |
| POST | `/admin/api/skills/{name}/reject` | `{revision, reason}` → 移到 `.rejected/`，7 天后清理 |
| POST | `/admin/api/skills/{name}/rollback` | `{to_revision}` → 复制旧版 + `rev+1`，**不删历史** |
| GET | `/admin/api/memory` | 分页：`layer=session\|global`、`namespace`、`status=staging\|active\|archived\|superseded`、`from/to`、`q`、`limit`、`cursor`。**只返回记忆条目，不含原始对话** |
| GET | `/admin/api/memory/{id}` | 详情：内容、`layer/namespace/status`、`session_id`/`turn_id` 指针、`created_at/expires_at`、`hit_count`、`supersedes` 链（前向 + 后向） |
| POST | `/admin/api/memory/{id}/promote` | `staging` → `active`（D43 的「显式确认」通道），写审计 |
| POST | `/admin/api/memory/{id}/archive` | `{reason}` → 归档，可恢复，写审计 |
| POST | `/admin/api/memory/{id}/restore` | 从 `archived` 恢复为 `active`，写审计 |
| GET | `/admin/api/memory/stats` | `{global_count, global_limit, staging_count, archived_count, oldest_at}` |
| POST | `/admin/api/skills/install/preview` | `{source: "upload"\|"url", ...}` → **预检**：解析后的 frontmatter、正文摘要、`requires_tools` 可用性、重名冲突。**不落盘** |
| POST | `/admin/api/skills/install` | 携带 `preview_token`（预检返回，60s、一次性）→ 落盘到 **`skills/pending/<name>/`**，`status=pending`。**没有 preview_token 一律拒绝**；**装完不生效**，必须再调 `approve`（AMEND-17/A54） |
| DELETE | `/admin/api/skills/{name}` | 卸载：移到 `skills/.trash/<name>-<ts>/`，可恢复 |
| PATCH | `/admin/api/skills/{name}` | `{status: "active"\|"disabled"}` —— **启停**，只改 `meta.json`，不改 frontmatter。与 `approve`（提案转正）是两件事，不要合并 |
| GET | `/admin/api/stream` | SSE，`Last-Event-ID` 续传 |
| GET | `/admin/api/health` | 无鉴权。回环绑定时返回 `{ok, version, e2e_enabled}`；**对外绑定时只返回 `{ok:true}`**（不泄露版本，供 F1 探活） |

统一错误体：`{code, message}`，`code` 取自 `schema/errors.json`。

**数据来源约束（别踩这条线）**：

| 数据 | 从哪来 | 禁止 |
|---|---|---|
| 拓扑 / 节点 | C2 的 `registry` 查询面 | 不自己扫端口、不缓存到本地表再对外 |
| 审计 | 自己的 `audit/` 库 | — |
| **记忆** | **C3 的 `MemoryPort`**（背后是 M4 私有 RPC） | **禁止 `import memory_module`、禁止直连 `memory.db`**——那是 M4 的目录，且 SQLite 直连会绕过它的向量索引一致性 |
| **技能** | **C3b 的 `SkillLoader`**（`list_all / preview / install / approve / reject / uninstall / set_status / reload`） | **禁止自己 `open(SKILL.md)` 解析 frontmatter**——校验规则有两份就会分叉 |

C3 的 `MemoryPort` 需要为本批扩四个方法（`admin_list / admin_promote / admin_archive / admin_stats`），**由 C3 在 ports 里加，C5a 只调用**。C3 没加之前用 mock 顶着，不要自己写 SQL。

> 配对码的生成/消费调用 C2 的 `identity/pairing.py`（`create(purpose, ...)/consume(code, purpose)/revoke`），**不要在 admin 里再写一套**。
>
> **一个码只服务一个用途**：`purpose=module` 给模块走 XX 配对，`purpose=browser` 换浏览器 cookie。C2 的 `consume` 会校验用途，传错直接失败。不要设计成"一个码两用"——泄露一次等于同时丢掉模块身份和浏览器会话。
>
> 邀请位（`POST /invites`）除了码，还要返回 `node_id`，并把 `pending` 记录写进 registry；模块接入时按 `pair_code` 绑定同一个 `node_id`，否则画布上会多出一个节点（AC-20 用例 6 直接失败）。

### T2 · policy（1.5 天）

**包入口**：`core/vade/policy/__init__.py` 暴露 `install(app)`，向 C3 的 ports 注册 `PolicyAdapter`；`vade/audit/__init__.py` 同样暴露 `install(app)`。C2 的 `app.py` 负责调用，不要改 `__main__.py`。

```
core/vade/policy/
├── engine.py      # 判定
├── approval.py    # 挂起、超时、多端竞争
└── adapter.py     # 实现 C3 的 ApprovalPort
```

判定表：

| risk | 默认行为 |
|---|---|
| `low` | 直接放行 |
| `medium` | 直接放行（配置可改为需审批） |
| `high` | **必须审批** |

审批流程：
- 生成 `request_id`（ULID），广播 `turn.approval_required{turn_id, request_id, tool, args(完整), risk, expires_at}` 给所有 attached UI；
- **RPC 分发不在你这里**：`approval/decide` 由 C3 的 T6 注册，它做完身份校验后调你的 `ApprovalPort.decide(request_id, decision, scope, by)`。你返回 `(ok, resolved)`，C3 负责广播 `turn.approval_resolved`。**两边都不写就没人接**——这是审查抓到的第一个接线缺口，别假设对方会做。
- **只接受 `device_key` 与 loopback 管理员**。`browser_session` 与 `ui` 模块**不能批 high**——浏览器 XSS 可控，让它批准等于把 HITL 交给攻击者。
- 超时（默认 60s）→ **拒绝**，`code=PERMISSION_DENIED`，`message` 说明「审批超时」；
- 无 UI attached → 直接拒绝（`no_approver`），**不挂起等设备上线**。**不做 TTY 交互**：systemd 下 stdin 可能是 TTY，弹一个 y/n 会把队列挂死，而没人看得见。
- `scope=session`：缓存 key 是 **`(session_id, tool_id, args_digest)`**，不是 `(session_id, tool_id)`。只按 tool 缓存的话，批一次 `ls` 就把 `rm -rf /` 也放行了。`shell` 这类通用执行器**只允许 `once`**。会话结束清除；写审计 `approval_scope_granted`。实现 `on_session_end(session_id)`，由 C3 在会话结束时调用；放行表只存在 policy 侧，C3 不持有。
- **risk 由你定，不由模块定**：模块 manifest 里的 `risk` 只是申报值。你维护 `[policy.tool_risk]`，规则是「只能上调、未登记按 high、高危工具硬编码下限 high」。`module/capabilities.update` 想把 risk 改低 → 拒绝。
- 参数展示与审计：UI 显示**完整参数**；审计存 `args_digest` + 脱敏后的参数（命中 `password|token|secret|key|credential` 的字段值替换为 `[redacted]`）。

并发与竞态：`request_id` 的状态迁移用单锁保护；重复 decision 返回 409，不重复执行。

### T3 · audit（1.5 天）

```
core/vade/audit/
├── writer.py      # 批量异步写，队列满时降级为同步
├── reader.py      # 查询 + 回放
├── retention.py   # 按天分表、30 天清理
└── adapter.py     # 实现 C3 的 AuditPort
```

表：`audit_YYYYMMDD(id INTEGER PK, ts, kind, session_id, turn_id, module_id, instance_id, method, tool, duration_ms, status, code, request_id, actor, args_digest, args_redacted, extra)`

- 只追加：建 `BEFORE UPDATE` / `BEFORE DELETE` 触发器 `RAISE(ABORT)`；代码里不提供改/删接口。
- 回放：`GET /admin/api/audit?turn_id=...` 按 ts 排序返回完整链（LLM 调用 → 工具 → 结果 → 下一轮）。
- 保留：每天 03:00 删 30 天前的表；删除动作本身写一条审计（写进当天表）。
- 性能：写入走队列 + 每 200ms 批量 commit；队列 > 10000 时同步写并告警。
- **配置变更审计**（AMEND-19）：`kind="config.changed"`，`extra={key, old, new, mode}`，`actor` 记管理员 `device_id`（不是 IP）。只记被改的键与前后值，**不记整份配置**——否则审计表会被每次保存刷爆。

### T4 · Admin API 与鉴权（5.5 天，v1.12 加 setup token 通道，v1.13 加改密/吊销会话，v1.15 加设备绑定与 Bearer 会话，v1.16 加身份密钥与 setup 解锁）

```
core/vade/admin/
├── server.py      # 自有 listener：127.0.0.1:8444，独立 ASGI app
├── app.py         # 路由（/admin、/admin/setup、/admin/api/*）
├── setup.py       # 首次配置：回环免 token / 非回环验 token；写 admin.json 后 unlink token
├── setup_token.py # token 生成、读取优先级（file > env > 生成）、比对、轮换、删除
├── identity.py    # core 身份密钥（Ed25519）生成/加载、fp8、hello_sig；0600，永不轮换
├── auth.py        # 密码校验、challenge、P-256/Ed25519 验签（按 alg 分派）、cookie、Origin/Host/CSRF
├── devices.py     # devices.json 读写、device_id 计算、吊销、Low-S 校验
├── views.py       # to_public_view 白名单
├── stream.py      # SSE
└── static.py      # 托管 admin-ui/dist
```

**必须自己起 listener，不许挂到数据面上（v1.10 反转原结论）**：原任务书写「不要另起端口，由 C2 在同一 8443 上 mount」。这条是错的，已由 AMEND-12/A42 推翻。

理由：`/app` 里跑的是 `ui` **模块**的代码，不是 core 的代码。同源意味着模块的一个 XSS 就能读 `/admin/api/nodes`、读审计、发吊销请求——`HttpOnly` 只防读 cookie，不防借用 cookie 发请求，CSRF token 同源也可读。**同源 + 第三方代码 = 没有任何前端隔离手段可用。**

落地要求：

- `admin/server.py` 自己 `uvicorn.run`（或用 C2 暴露的 `make_server(cert, key, app)` 工厂，但**独立实例、独立端口**），默认 `127.0.0.1:8444`，`--admin-bind` 才改。
- 证书可复用 C2 的 CA 与证书文件，但**必须显式传参绑定**，不能隐式共用 listener 对象。
- C2 的 `transport/server.py` 里**不出现** `/admin` 路由；`GET :8443/admin` 必须是 404。
- 启动日志打印两行，明确区分：
  ```
  data plane  : wss://127.0.0.1:8443   (modules, ui/session)
  admin plane : https://127.0.0.1:8444 (auth: password [+ ed25519 key if remote])
  ```

鉴权（v1.11 全量替换，**没有 token 了**）：

| 来源 | 要求 |
|---|---|
| 回环 | 密码 |
| 非回环 | 密码 **+** 已登记设备的挑战签名（**P-256 主路径**，Ed25519 仅软件密钥；按 `alg` 分派） |
| 首次配置 | 回环免 token；非回环需 `setup_token`；**无硬超时** |

**① setup（首次）**

判据只有一个：`data/admin/admin.json` 是否存在。**不要用「有没有 initialized 标志位」二次判断**，两处状态会不一致。

```
未初始化：
  GET  /admin/setup                    → 200（回环：含 bind_url+fp8；非回环未解锁：只含 requires_token）
  POST /admin/setup/unlock {token}      → 设 __Host-ac_setup(10min) → 返回 bind_url+fp8+expires_at
  POST /admin/setup {token?, pw, key?}  → 校验来源 + token → 写 admin.json → unlink token
  POST /admin/bind {ticket,eph,nonce,ct}→ 验票 → 登记设备 → 写 admin.json → unlink token
  其它 /admin/api/*                      → 404（/admin/api/health 除外，只回 {ok:true}）
已初始化：
  GET  /admin/setup                    → 302 /login
  POST /admin/setup                    → 404（不读 token 文件，残留也不认）
```

**准入分流**（A47）：

| 来源 | 要求 | 判定方式 |
|---|---|---|
| 回环 | **不需要 token** | `request.client.host` ∈ `{127.0.0.1, ::1}`，且 `Host` 的 host 部分也是回环字面量 |
| 非回环 | **必须带正确 token** | 以上不满足时，读 `setup_token` 比对 |

- 只有 `GET/POST /admin/setup`、`POST /admin/setup/unlock`、`GET /admin/bind/info`、`POST /admin/bind` 可用，其余 `/admin/api/*` 全部 404（`health` 例外）。
- **二维码的闸在 `unlock`**：非回环未解锁时，`GET /admin/setup` 的 JSON 里不能出现 `bind_url`、`fp8`、`acs_` 任何一项。这条要写成会失败的测试，不是靠前端不渲染。
- 必须设置密码（两次输入，**不提供跳过**）+ 可选粘贴首把公钥。
- **写入顺序必须是「先写 `admin.json`（tmp + `os.replace` 原子 rename）→ 再 `unlink` token 文件」**。反序会出现「token 删了、密码没写成」→ 只能 `core admin reset` 的死局；正序即使崩在中途，残留 token 也因已初始化而不被接受。
- **取消 30 分钟硬超时**（v1.12）。替代做法：未初始化时每 24h 打一次 `admin plane still uninitialized (uptime=<h>)`；对外绑定时若拿不到任何可用 token → **拒绝启动**。**不要**再实现「超时自杀」，它会让「先起容器过两天再配」的部署流程不可用。

**setup token 的实现细节**（A48 + A49）：

| 项 | 要求 |
|---|---|
| 格式 | `acs_` + `base64url(os.urandom(32))` → 47 字符；**不要用 uuid4、不要用 base32 长串** |
| 存储 | `data/setup.token`，`0600`，父目录 `0700`；**只有这一个位置**，不要同时写 `admin/` 下 |
| 生成时机 | **每次进程启动都跑一遍 `ensure_setup_token()`**，不是「只有首次启动」：<br>① 已初始化 → 有残留就 `unlink`，返回 `None`<br>② `VADE_SETUP_TOKEN` 非空 → 用它并写回文件，**不轮换**（用户钉住了值）<br>③ 否则 → **生成新 token 覆盖**文件（D59：重启即换代） |
| 为什么轮换 | 「文件在就沿用」会让 `docker cp` / 备份 / 误挂载复制出去的 token 永久有效，而它是「谁能当管理员」的钥匙 |
| 单实例 | 启动时 `flock data/core.lock`（非阻塞），拿不到就退出并提示「另一个 core 正在使用该 data 目录」。**不加锁的话两个进程会互相覆盖 token** |
| 启动输出 | 默认只打 `setup token: acs_4f2a…（本次启动新生成，已写入 data/setup.token；用 core admin print-setup-token 查看完整值）`；`--print-setup-token` 才打全量。**每次启动都会变**，日志里不要写「见上次输出」 |
| 传输 | **只从 POST body 的 `setup_token` 字段读**。禁止 `?token=`、禁止 `X-Setup-Token`（前者进 log，后者容易被中间件透传） |
| 比较 | `hmac.compare_digest(expected, provided)`；先判长度再比，长度不等也走一次假比较 |
| 归一化 | **不做任何归一化**。不去空格、不改大小写——token 是机器复制的，不是手输的 |
| 消费 | 成功路径 `os.unlink`；失败路径不动文件（用户可能只是打错一次） |
| 审计 | 记 `setup.attempt{ok, source:"loopback"\|"token", ip}`，**绝不记 token 值，也不记前后缀** |

- 失败限流：非回环 5 次/5 分钟/IP；全局 20 次/5 分钟锁 15 分钟。**这层不是安全边界**（256 bit 爆不了），只是为了压日志噪音和挡无脑扫描器。
- `VADE_SETUP_TOKEN` 的风险要写进代码注释：`/proc/<pid>/environ`、`docker inspect`、部分编排系统的事件日志都会带出来。所以文件是推荐路径，env 是「我知道我在做什么」的显式覆盖。

**② 密码**

- `Argon2id(m=64MiB, t=3, p=1)`，16B 随机 salt，PHC 字符串存 `data/admin/admin.json`（0600）；参数随哈希一起存，将来调参不影响老密码。
- 最短 12 字符；**不强制大小写/符号**（强制组合只会逼出 `Passw0rd!`），改为拒绝常见弱密码表前 10k 条。
- `hmac.compare_digest` 比较；失败响应统一 `AUTH_FAILED`，不区分「密码错」「未初始化」「无此 key」。
- 日志/审计/响应里不出现密码，**连长度都不记**。
- 改密码需旧密码；忘记走本机 `core admin reset-password`（要对 `data/` 有写权限，远程用不了）。

**③ 设备密钥（core 不签发任何东西）**

**算法：主路径 P-256（ECDSA + SHA-256），不是 Ed25519。** 这是硬件决定的：iOS Secure Enclave **只支持 P-256**，Android StrongBox 也是 EC 普遍可用、Ed25519 要看厂商。既然要求「私钥进安全区、不可导出」，就只能选 P-256。Ed25519 保留给**软件密钥**（桌面 CLI、救援、CI）。

- 设备密钥对由 **App 在设备上生成**，私钥进 `Secure Enclave` / `StrongBox` / `TEE` 且不可导出；core 只收公钥，**永远不生成设备密钥**。
- `devices.json` 条目带 `alg ∈ {p256, ed25519}`，验签按 `alg` 分派，**不靠试**。
- **P-256 验签必须拒 high-S**：`cryptography` 解出的 `(r, s)` 里，`s > n // 2` 直接判失败。否则同一条 challenge 能改出一个「不同但有效」的签名，一次性语义就破了。
- 公钥编码：P-256 用 65 字节未压缩点（`0x04 || X || Y`），base64 传输；Ed25519 用 32 字节原始公钥或 OpenSSH 行（导入时转换）。

- **三条登记路径**：

  | 路径 | 前提 | 用在哪 |
  |---|---|---|
  | **① `POST /admin/bind`**（主路径） | 绑定票 + 用户已导入 core 公钥 + 设备签名 | 首次配置、加新手机 |
  | ② `POST /admin/api/devices {password, pubkey, alg, label}` | **已登录** + **step-up 重输当前密码** | 换机、加平板 |
  | ③ `core admin trust-key --pubkey-file ./id_ed25519.pub --label rescue` | 能在 core 主机执行 | 救援、设备全丢 |

  **② 不是匿名入口**：上传前必须先登录，再重输一次密码。只有公钥没密码进不来；只有密码没已登记设备，非回环的**写操作**也进不来。**step-up 是这条路径唯一的安全承重点**，不许省。
- 上限 **5 台/账户**，超出拒绝（不自动淘汰）。淘汰哪台由人决定。
- 存储 `data/admin/devices.json`（0600，同目录 0700）：`{device_id, label, alg, pubkey, fp8, hardware_backed, added_at, added_from_ip, last_used_at}`，`device_id = SHA-256(pubkey)[:16]`。**列表接口不返回公钥原文**。
- 吊销立即生效，并关闭该设备换发的所有会话（不是等 token 过期）。
- **`cryptography` 只用于验签与 ECDH**；禁止在这里生成设备密钥对、禁止用它实现协议加解密（那是 C1 的事）。验签不是协议加解密，这是明确豁免。

**④ 登录时序**

```
POST /admin/api/login/challenge
  → {challenge(32B), expires_at, devices:[{device_id,label,fp8,alg}]}
  challenge 60s 一次性，绑 (challenge_id, 来源 IP /24, 设备表版本)

签名对象 = "vade-admin-v2\n" || challenge || "\n" || admin_origin
  （v1→v2 因为算法从 Ed25519 换成 P-256，前缀换代可杜绝旧签名重放）

POST /admin/api/login {challenge_id, password, device_id, signature}
  → 先验密码（常数时间），再验签名（按 alg 分派，P-256 查 low-S）
  → 任一失败 401 AUTH_FAILED 且 challenge 立即作废
  → 都过：{access_token(15min), refresh_token(30d)}，**不设 cookie**
```

- 回环来源可省略 `device_id` / `signature`，但**不能省密码**；省略时**只发 cookie（只读）**。要拿 Bearer 必须额外带 `X-AC-Client: cli`（浏览器不会发这个头）或提供设备签名。
- 签名前缀 `vade-admin-v2` 是域分离；带 `admin_origin` 是防「A 站点的 challenge 拿到 B 站点用」。
- **refresh**：`POST /admin/api/token/refresh {refresh_token, signature}`，签名内容含 `refresh_jti`，**每次刷新都验签**。只偷到 refresh token 续不了期。

**⑤ 会话与请求校验**

**两套会话，不要混**：

| 模式 | 凭据 | 判定 | 权限 |
|---|---|---|---|
| `device` | `Authorization: Bearer <access>` | 由 `/login` 或 `/bind` 换发 | 全功能 |
| `browser` | `__Host-ac_admin` cookie | 浏览器登录（只输密码） | **只读**：**已认证**的非 GET 业务请求 → `403 READ_ONLY_SESSION`；`login`/`login/challenge`/`logout`/`token/refresh`/`setup*`/`health` 豁免 |

- access token **15 分钟**内不查服务端 session 表（短时效 + 签名刷新已足够）；但**每次请求仍要查 `device_id` 是否已吊销**（内存索引，O(1)），否则「吊销立即生效」做不到。refresh 走服务端表，刷新时额外校验 `jti` 不在黑名单。
- **Origin 校验（必须，不是可选）**：`/admin/api/*` 每请求检查 `Origin` 是否等于 `https://<admin_host>`，不等 → `403 ORIGIN_MISMATCH`；再查 `Sec-Fetch-Site` 必须是 `same-origin`（缺失则要求 `Authorization`，兼容 curl）。
- **`Host` 白名单**：必须 ∈ `admin_host`（默认 `127.0.0.1:8444`），防 DNS rebinding。
- **不返回任何 CORS 头**，OPTIONS 预检也不回。
- **CSP**：`default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'`。
- **HSTS**：`max-age=31536000`（不带 `includeSubDomains` / `preload`）。
- **`/admin/api/*` 只返回 `application/json` + `X-Content-Type-Options: nosniff`**，永不返回 `text/html`。这条是给 App 内 WebView 用的（§7.11 第 2 条）：core 这边一旦能吐 HTML，WebView 那边就多一个注入面。
- CSRF 双提交**只对 `browser` 模式**生效（cookie 才会被自动附带）；`device` 模式用 Bearer，天然免疫 CSRF。

**⑥ 限流与对外**

| 维度 | 阈值 | 动作 |
|---|---|---|
| 同 IP 密码失败 | 5 次 / 5 分钟 | 锁 5 分钟（429） |
| 全局密码失败 | 20 次 / 5 分钟 | 关闭登录 15 分钟 + `admin_lockout` 审计 |
| challenge | 30 次 / 分钟 / IP | 只挡刷 nonce |
| 签名失败 | 计入密码失败同一计数 | 防探测「密码对了但设备不对」 |
| `/admin/bind` | 10 次 / 分钟 / IP | 只挡刷票，票本身就一次性 |

- `--admin-bind 0.0.0.0` **允许对外**，但非回环的**写操作**强制双因素（浏览器只能读）。
- **设备表为空 + `admin_bind` 非回环 → 拒绝启动**并打红字，不允许半开状态。
- 必须走 TLS（复用 C2 的证书）；远程首次访问必须在登录页显示 **core 身份指纹 `fp8`**（§7.1.3 的 Ed25519 身份密钥，**不是** TLS 证书指纹——TLS 会轮换，pin 会断）。TLS 仍要校验（SPKI pin 或可信证书），但它不是身份锚点。`core --print-fingerprint` 只打 `fp8`；TLS 指纹是另一个命令 `--print-tls-fingerprint`，两者不要混。


字段白名单（`to_public_view`，唯一出口）：

```
node: id, kind, module_id, instance_id, label, capabilities, state,
      endpoint, protocol, version, auth_mode, rtt_ms, last_seen,
      x, y, revoked, tool_count, tool_names[]
```

- `endpoint` 只保留 `scheme://host:port`：解析 URL，丢掉 userinfo / query / fragment；解析失败 → 置空并记 warning。
- 禁止透传 `manifest` 原文、公钥原文、PSK、pair_code 明文（生成时一次性返回）、任何 header。
- 测试必须扫描**所有** `/admin/api/*` 响应体（含 SSE 首帧）：`private|secret|psk|password|token|credential` 命中 0；长度 ≥ 40 的 base64 串命中 0。

SSE：
- 事件格式 `id: <seq>\ndata: <json>\n\n`；
- 增量事件三类：`node.upsert` / `edge.state` / `node.remove`；
- `Last-Event-ID` 补齐，与 C3 的 replay 复用同一 seq 逻辑（**不要另造一套序号**）；
- 15s 发一次注释行 `: ping` 防代理断流。

### T5 · 前端：登录 + 列表 + 审计 + 设置 + skill 审核（3 天，v1.17 加策略组）

> v1.17 增量（约 +0.5 天）：`/settings` 增加「记忆与进化策略」10 项表单（数字/开关/二次确认/成本预估/用量条）。这一块**必须真的能改并落到 `overrides.json`**，不能只画控件——画了不能改比没有更糟。

```
admin-ui/
├── package.json / vite.config.ts / tsconfig.json
├── src/app/           # 路由、API client、鉴权守卫、布局壳（C5a 所有）
├── src/pages/         # 列表页、审计页、登录页（C5a 所有）
└── dist/              # 构建产物，提交进仓库
```

- React + TypeScript + Vite；**不引 UI 框架**（避免与 C5b 的 React Flow 版本打架），用原生 CSS 变量。
- 路由：`/setup`、`/login`、`/nodes`（列表）、`/audit`、`/settings`、`/skills`、`/memory`、`/topology`（`lazy(() => import('../canvas'))`，模块由 C5b 第一天提供占位；若尚未合并，先不注册该路由，构建不能因此失败）。
- **`/setup`**：仅在 core 返回 `setup_required=true` 时可达（否则重定向 `/login`）。**先过闸，再给二维码 —— 顺序不能反**：
  - **0. 解锁（非回环必过）**：`requires_token=true` 且没有 `__Host-ac_setup` 时，页面**只渲染**一个 token 输入框 + 取 token 的三条路径说明。**不渲染二维码、不渲染指纹、不渲染密码表单**。提交 → `POST /admin/setup/unlock`，成功后原地展开下面两块。回环直接跳过这一步。
  - **A. 手机扫码（主路径）**：解锁后才显示 —— 二维码（`bind_url`，内含 26 字符 `bind_ticket`）+ 大字 `fp8`（分四组八位，字距拉开）+ 10 分钟倒计时 + 一句「用 Vade App 扫码，核对指纹一致后继续」。这一块在窄屏上占满宽度。
  - **B. 在浏览器里直接配（备用）**：折叠面板，展开后三段表单 —— ① **访问令牌**（`setup_token`，**只在未解锁时**渲染；`acs_` 开头 47 字符，旁边一行小字给 `docker exec <c> cat /data/setup.token`）；② **密码 + 确认**（不提供跳过）；③ **可选设备公钥**（P-256 65B base64，或 `ssh-ed25519 AAAA... label` 给开发者用）。
  - **倒计时到 0 时**：二维码区域替换成「已过期，请重新解锁」，**不要留一张扫了必然 401 的图**。
  - 提交后提示「已初始化，请用刚才的密码登录」。失败时**不要**把已填的 token 回填到 DOM，清空重填。
  - 二维码**只在未初始化或仍有有效绑定票时渲染**；已初始化后这个页面本来就不存在了。
  - **不要在页面上写「推荐用 SSH 隧道」**——SSH 已从用户路径移除（AMEND-17/A57）。
- **`/login`**：
  - **两种运行形态，UI 必须自己判断并如实呈现**（用 `window.__AC_HOST__` 注入的 `mode ∈ {webview, browser}`，由 App 宿主或 core 注入）：
    - `webview`：App 已注入设备上下文。**密码框由原生弹窗渲染，页面不画密码框**（§7.11 第 8 条）；登录的 challenge 获取、签名、`POST /admin/api/login` 全部由原生代发。页面只通过桥拿到 `{access_token}`，**只存内存**（不要写 `localStorage`——§7.11 第 5 条要求 DOM storage 不持久化）；`refresh_token` 留在原生钥匙串。
    - `browser`：**只渲染密码框 + 一条常驻只读提示**「当前为只读模式；要修改配置请用 Vade App 打开」。**不要渲染设备选择、不要渲染签名失败的报错**——浏览器根本走不到那条路。
  - **登录页固定显示 core 身份指纹 `fp8`**，旁边一行小字：「与二维码 / `core --print-fingerprint` 一致才输入密码。管理面没有端到端加密，密码只靠 TLS 保护。」
  - 错误提示统一为「密码或设备不正确」，**不区分**是哪个错。
- **浏览器不能做写操作 —— 这是设计决定，不是能力缺陷。**

  浏览器没有裸签名 API，拿不到第二个因子。v1.14 曾试图用 SSH 隧道绕过，结果是「必须有电脑才能配」——这个前提在目标场景不成立（AMEND-17/A57）。现在的处理是**诚实降级**：

  | 形态 | 能做什么 | 怎么做 |
  |---|---|---|
  | App 内 WebView（主） | 全部 | 密码 + 硬件设备密钥签名 |
  | 系统浏览器（降级） | **只读** | 仅密码；已认证的非 GET 业务请求返回 `403 READ_ONLY_SESSION`（登录/刷新/登出/setup 不拦） |
  | 脚本 / CI | 全部 | `core admin proxy` 在本机代签（开发者工具，不进用户 runbook） |

  **前端不要渲染点不动的按钮**：`mode=browser` 时按只读模式渲染，编辑类控件直接不出现。给一个「看起来能点、点了 403」的界面比不给更糟。
- **WebAuthn 不在 v1**：它绑定平台凭据而非设备密钥，两套信任模型混用会让人分不清哪把钥匙在生效，v2 单独立项。
- 列表页：模块、实例、状态、地址、版本、RTT、最后心跳、工具数；操作：停用/启用、吊销、断开。
- **`/settings`**：五块，都很小但必须有。
  1. **改密码**：旧密码 + 新密码 + 确认。成功后提示「其它设备的登录已失效」（当前 token 保留，不把自己踢出去）。
  2. **已绑定设备**：列表（`label / fp8 / alg / hardware_backed / added_at / added_from_ip / last_used_at`）+ 「添加设备」。
     - 「添加设备」按钮 → **step-up 弹窗**（再输一次当前密码）→ 调 `POST /admin/api/devices/ticket` → 显示二维码 + `bind_url` 文本 + 倒计时（10 分钟）。
     - **扫码后是否自动刷新列表**：轮询 `GET /admin/api/devices` 每 3 秒，直到出现新条目，最多 2 分钟。别让用户自己按 F5。
     - `hardware_backed=false` 的条目显示**黄色标签「软件密钥，可被提取」**，`alg=ed25519` 时同样标注。
     - 到 5 台上限时「添加设备」置灰，文案「先移除一台不用的设备」。
     - **不显示公钥原文**，只显示 `fp8`。
  3. **加一台设备的备用方式**（折叠，给开发者）：文本框接受 `ssh-ed25519 AAAA...`，同样要 step-up。
  4. **登出全部**：二次确认（破坏性）；文案写清「包括当前这台设备」。
  5. **记忆与进化策略（可改，热生效）**：10 项，字段与范围见 `design.md §7.9`。控件：数字 `input[type=number]` + 前后端同范围校验，布尔用 switch；每项右侧常显 `当前值 / 默认值 / 来源`。
     - 保存：`PUT /admin/api/config`，body `{key, value}`；返回 `{applied:true}` 或 `{applied:false, needs_restart:true}` 或 `{error:"..."}`。
     - **三项必须二次确认**：关 `evolve.enabled`、`evolve.daily_token_budget=0`、`evolve.nudge_interval<5`。确认弹窗要写清后果（如「关闭后不再产生新记忆与提案，已有内容保留」）。
     - `nudge_interval` 下方实时显示「按最近 7 天均值 ≈ N 次/天、≈ M token/天」——不显示成本的调速旋钮等于让人蒙眼花钱。
     - `daily_token_budget` 显示今日已用进度条（`GET /admin/api/config/usage`）。
     - 校验失败就地红字，**不 reload 页面**（reload 会把用户其它未保存的改动冲掉）。
  6. **其余生效配置（只读）**：监听地址、管理面 host、**core 身份指纹 `fp8`**、`policy.approval_timeout_s`、审计保留天数、`e2e` 开关，以及 §7.9 的「需重启」组。每项显示 `value` 与 `source`（`default` / `core.toml` / `overrides` / `env` / `cli`）——这是为了回答「我改了 toml 怎么没生效」。**不给编辑框**。
- **`/skills`**：列表（名 / 当前 rev / 状态 / 来源）+ 详情抽屉。
  - 详情里显示 `pending` 提案的 **diff**（后端算好，`GET /admin/api/skills/{name}` 返回），左旧右新，按行高亮。
  - 三个操作：通过（`approve`）、驳回（`reject`，必填理由）、回滚（`rollback`，选目标 rev）。
  - **通过/回滚都要二次确认**，文案说明「下一轮对话起生效，当前会话不受影响」。
  - `stale` 状态要显眼标注「依赖的工具已不存在」，并只允许驳回或回滚，不允许通过。
  - 这一页不实现的话，D45 的「提案 → 审核 → 生效」链路就断了：`pending/` 里的文件只能靠手工 `mv`。
  - **安装区**（与审核区分开，两个 tab）：
    - 三个来源：粘贴/上传 `.md`、上传 `.zip`、填 https URL。
    - **三步，不是两步**：预检 → 安装 → 批准。
      1. 「预检」→ 显示解析后的 frontmatter、正文前 30 行、`requires_tools` 哪些缺、是否重名 → 勾选「我已查看内容」→ 才允许「安装」。预检返回的 `preview_token` 60s 一次性，**没有它后端直接拒**（前端禁用按钮不算防线）。
      2. 「安装」**只是落到 `pending/`**，列表显示「待安装确认」，`status=pending`。**此时该 skill 不生效**——不在工具表、不进 prompt。UI 上必须写明这一点，否则用户会以为装坏了。
      3. 详情抽屉里点「启用」→ `approve` → `status=active`，下一轮对话生效。
    - 上传区要显示限制：单文件 64KB / zip 1MB / 解压后 5MB / 条目 32；**「zip 内不得含可执行文件」也要写出来**，否则用户不知道为什么被拒。
    - `pending` 有 7 天倒计时，列表里显示「剩 6 天」，过期自动进回收站。给一个「全部启用 / 全部丢弃」的批量操作，否则一次装十个要点十次。
    - 列表里标明「来源：手动安装」与「来源：自动提案」的差别（`meta.origin`）。
- **`/memory`**：三块。
  - 顶部筛选：`layer`（session / global）、`namespace`、`status`、时间范围、关键词。默认 `global + active`，**不要一进来就全量拉**。
  - 列表：内容截断 2 行、`namespace`、`status` 徽标、`created_at`、`hit_count`。分页游标，不做「加载全部」。
  - 详情抽屉：完整内容（可选「显示」按钮，默认遮住前 80 字符——**记忆里可能有用户隐私**，管理员不该一眼看全）；来源指针 `session_id / turn_id`；`supersedes` 链（能看出被谁取代）；操作：`staging → 晋升`、`归档`（必填理由）、`恢复`。
  - **没有「编辑」按钮**。不要为了「顺手改个错别字」就开这个口子——记忆可改就失去溯源价值（§7.9）。
  - 右上角显示配额：`global 1234 / 5000`、`staging 17`。
- 破坏性操作确认弹窗必须写**影响面**：
  > 停用 `com.example.fs` 将移除 4 个工具（read_file / write_file / list_dir / search_files）；当前会话不受影响，下一轮对话起模型将看不到它们。
- 审计页：时间、模块、方法、耗时、状态、code；支持按 turn_id 过滤；不显示参数原文（只有 digest 与脱敏值）。
- `npm run build` 产物提交；CI 校验 `git diff --exit-code admin-ui/dist`。
- **窄屏（<768px）是主场景，不是降级**（v1.15 改，覆盖 FR-9.15 的旧口径）：App 内 WebView 大多就是手机宽度。所以：
  - 列表页、`/skills`、`/memory`、`/settings` 全部要能在 375pt 宽度下操作，操作按钮放在卡片底部而不是 hover 出来的图标。
  - **只有拓扑画布**在窄屏降级为「节点列表 + 点击进详情」，因为力导向图在手机上没法拖。降级的是可视化，不是权限。
  - 不要用 `hidden md:block` 把功能藏起来——藏起来等于手机上做不到。

### T6 · 接线与测试（1 天）

- 启动时把 `PolicyAdapter` / `AuditAdapter` 注入 C3 的 ports；缺失时用 `AllowAll` / `NullAudit`（**仅测试环境**，生产必须显式配置）。
- 审批与审计都要覆盖「core 重启后」：未决审批作废（发 `turn.failed`），审计仍在。

## 六、验收（G2）

```bash
pytest core/tests/policy core/tests/audit core/tests/admin -q

# 审批超时（用可注入时钟，不真等 60s）
pytest core/tests/policy/test_timeout.py -q

# 密钥扫描（AC-19）
pytest core/tests/admin/test_no_secrets.py -q

# 审计不可改
pytest core/tests/audit/test_immutable.py -q

# 前端构建一致性
cd admin-ui && npm ci && npm run build && git diff --exit-code dist
```

必须有：

| 用例 | 判据 |
|---|---|
| high risk 无决策 | 60s 后拒绝，`turn.tool_result.ok=false`，审计有 `approval_timeout` |
| 两端同时收到 | 一端批准，另一端收到 `turn.approval_resolved` 并关弹窗 |
| 重复 decision | 第二次 409，不重复执行 |
| session scope | 同会话第二次同类工具不弹窗；新会话重新弹 |
| 无 UI 无 TTY | 立即拒绝，reason=`no_approver` |
| 审计 UPDATE | SQLite 报错，数据不变 |
| 白名单 | 响应里无 `manifest`、无私钥、`endpoint` 无 query/userinfo |
| 登录爆破 | 5 次失败锁 5 分钟，第 6 次仍 401/423 |
| **跨源读取** | webui origin（`:8080`）`fetch` 管理面 → 被 CORS 阻断，响应无 `Access-Control-Allow-Origin` |
| **伪造 Origin** | `curl -H 'Origin: http://127.0.0.1:8080' -b '__Host-ac_admin=...' https://127.0.0.1:8444/admin/api/nodes` 与带 Bearer 的同请求 → 均 `403 ORIGIN_MISMATCH`，且审计记一条 |
| **cookie 属性**（浏览器只读模式） | `Set-Cookie` 含 `__Host-` 前缀 + `Secure` + `HttpOnly` + `SameSite=Strict` + `Path=/`，**不含 `Domain`**；`/admin/api/login` 在 `mode=device` 时**不发** cookie |
| **数据面无 admin 路由** | `--bind 0.0.0.0` 时 `GET :8443/admin` → 404 |
| **CSP** | 响应头含 `frame-ancestors 'none'`，`<iframe src=...>` 被浏览器拒绝 |
| **归一化配对码** | `青松-A7K2M9` / `青松a7k2m9` / 全角 / 带空格 四写法首次均成功；第二次均失败 |
| **远程只密码** | 非回环 + 正确密码 + 无签名 → 200，但会话为 `mode=browser`，**只读**；`POST` 一律 403 |
| **远程只签名** | 非回环 + 无密码 + 正确签名 → `401 AUTH_FAILED` |
| **远程双因素** | 非回环 + 正确密码 + 已登记设备的签名 → 200，`mode=device`，发 Bearer |
| **未登记设备** | 用未进 `devices.json` 的私钥签名 → `401 AUTH_FAILED`（错误码与其他失败一致） |
| **challenge 重放** | 同一 challenge 用两次 → 第二次失败；过 60s 后失败；换 /24 段后失败；**同段内换 IP 仍可用**（手机切网不该踢人） |
| **setup 回环免 token** | 回环 `POST /admin/setup`（无 `setup_token`）→ 200；`data/setup.token` 已被 unlink |
| **setup 二维码门禁** | 非回环 `GET /admin/setup`（无 cookie）→ 响应体**不含** `bind_url`/`fp8`/`acs_`；`POST /admin/setup/unlock` 错 token → 401；对 token → 200 且 `bind_url` 的 `t` 是 26 字符 base32 |
| **setup 解锁过期** | unlock 后 10 分钟再 `GET /admin/setup` → 重新只回 `requires_token`，二维码消失 |
| **绑定问候签名** | 篡改 `kex_pub` 或 `challenge` 一位 → App 在 `POST /admin/bind` 前中止；core 侧该票**未被消费** |
| **setup 非回环无 token** | 非回环 `POST /admin/setup` → `401 SETUP_UNAUTHORIZED`，且 `admin.json` 不存在 |
| **setup 非回环带 token** | 非回环 + 文件里的正确 token → 200；响应体不含 token 任何片段 |
| **setup token 错误** | 非回环 + 错 1 字符 → `401 SETUP_UNAUTHORIZED`（与缺 token 同码） |
| **setup 完成后** | `GET /admin/setup` → 302 `/login`；`POST /admin/setup` → 404；**残留 token 文件也不认** |
| **写入顺序** | 注入「写 `admin.json` 失败」的故障点 → 进程退出，但 `data/setup.token` **仍然存在**（可重试，不必 reset） |
| **token 优先级** | 同时存在文件与 `VADE_SETUP_TOKEN` 且值不同 → **文件生效**；只有 env 时 → env 生效并写回文件 |
| **重启即轮换** | 未初始化 + 无 env，连续启动两次 → token 不同；第一次的值在第二次启动后 `401` |
| **env 钉住不轮换** | 设 `VADE_SETUP_TOKEN=T`，连续启动两次 → 文件内容恒为 `T`，两次都能用 |
| **已初始化清残留** | 初始化后手工放一个 `data/setup.token` → 重启后文件消失，`/admin/setup` 仍 302 `/login` |
| **单实例锁** | 同一 `data/` 起第二个 core → 非 0 退出，日志含 `core.lock` 被占用 |
| **token 轮换** | `rotate-setup-token` 后旧值 `401`、新值 200；`core admin reset` 后 token 变化且进 setup 模式 |
| **无凭据对外** | `--admin-bind 0.0.0.0` + 未初始化 + token 文件不可写 + 无 env → **拒绝启动**，退出码非 0 |
| **空设备表对外** | `--admin-bind 0.0.0.0` + `devices.json` 为空 → 拒绝启动，日志含明确原因 |
| **吊销设备** | `revoke-device` 后该设备的 Bearer 与 refresh 立即 401，不等过期 |
| **Host 校验** | `Host: evil.example` 打管理面 → 421/403（防 DNS rebinding） |
| **改密码** | 旧密码错 → `401 AUTH_FAILED`，密码不变；旧密码对 → 200，另一台设备的 cookie 立刻 401，当前设备仍可用 |
| **登出全部** | 调用后当前 cookie 也失效，`/admin/api/nodes` 返回 401 |
| **改密后旧会话** | 改密码前发的 cookie，改密后一律 401（服务端版本号失效，不靠过期时间） |
| **skill 审核** | `pending` 提案 approve 后 `revision+1` 且出现在 `GET /skills` 的 `active`；reject 后进 `.rejected/`；`stale` 的提案 approve 被拒 |
| **设备 step-up** | 已登录但 body 里密码错 → `401 AUTH_FAILED`，设备未写入；密码对 → 200，列表里多一条 |
| **加设备上限** | 已登录加第 6 台 → `PERMISSION_DENIED`，不淘汰旧设备 |
| **设备吊销（登录路径）** | 吊销后该设备的 token 立即 401；用该私钥签 challenge 也失败 |
| **记忆分页** | `GET /admin/api/memory` 响应里**不含** `user_text` / `assistant_text` 字段（只有条目正文与指针） |
| **记忆写操作审计** | promote / archive / restore 三条各产生一条审计；`GET` 不产生审计 |
| **记忆编辑不存在** | `PATCH /admin/api/memory/{id}` 与 `PUT` 均 405/404（没有这个端点） |
| **skill 安装两步** | 不带 `preview_token` 直接 POST install → `INVALID_ARGUMENT`；预检后 61s 再装 → `INVALID_ARGUMENT`（token 过期） |
| **zip-slip** | 构造含 `../../evil.md` 的 zip → `INVALID_ARGUMENT`，且 `data/` 外无新文件 |
| **安装不生效** | `POST /skills/install` 成功后 `GET /skills` 该条 `status=pending`；此时 `SkillLoader.list_all()` **不含**它；`approve` 后才含 |
| **zip 含脚本被拒** | zip 里有 `setup.sh` → `INVALID_ARGUMENT`，`skills/` 下没有该文件 |
| **设备绑定成功** | 扫码 → `POST /admin/bind` → 200，`devices.json` 多一条 `alg=p256`；`access_token` 能访问 `/admin/api/nodes` |
| **绑定票一次性** | 同一 `ticket` 第二次 POST → `401 BIND_UNAUTHORIZED`；并发两个请求只有一个成功（事务里 `DELETE ... WHERE consumed_at IS NULL`） |
| **错签名不烧票** | 同一票先发错签名 → 401；再用正确签名 → **200**（证明是「先验后删」而不是「先删后验」） |
| **指纹不符** | 二维码里 `fp` 改一位 → App 端中止，不发 `POST /admin/bind`（core 侧无日志记录该票被消费） |
| **P-256 low-S** | 构造 `s > n/2` 的签名 → `401 AUTH_FAILED`（即使数学上有效） |
| **浏览器只读** | browser 模式登录后 `GET /admin/api/nodes` 200；`POST /admin/api/skills/install` → `403 READ_ONLY_SESSION`；**回环浏览器同样 403**；`POST /admin/api/login` 不被拦 |
| **Bearer 不落 cookie** | `POST /admin/api/login` 响应**不含** `Set-Cookie`；响应体里有 `access_token` |
| **refresh 需签名** | 带正确 `refresh_token` 但无 `signature` → 401；签名内容换成别的 `jti` → 401 |
| **API 不吐 HTML** | `GET /admin/api/*` 全部 `Content-Type: application/json` + `X-Content-Type-Options: nosniff` |
| **设备上限** | 已有 5 台再绑定 → `BIND_DEVICE_LIMIT`，`devices.json` 不变 |
| **吊销设备** | `revoke` 后该设备的 access/refresh 立刻 401；重启 core 后仍 401（落盘，不是内存黑名单） |
| **core 身份指纹稳定** | 换 TLS 证书后 `admin print-fingerprint` 不变；重启不变 |
| **策略项热生效** | `PUT /admin/api/config {key:"evolve.nudge_interval", value:5}` → 200；`GET` 返回 5、`source=overrides`；`core.toml` **未变**；重启后仍为 5 |
| **未知键被拒** | `PUT` 一个 `evolve.foo` → 400 `UNKNOWN_CONFIG_KEY`；进程未重启也未被污染 |
| **危险项要确认** | 不带 `confirm` 关 `evolve.enabled` → 409 `CONFIRM_REQUIRED`；带 `confirm:true` → 200，审计有 `config.changed{key,old,new}` |
| **用量可见且不泄漏** | 跑 12 轮后 `GET /admin/api/config/usage` 返回 `reviews_today=1`、`tokens_used>0`；响应体里 grep 不到任何记忆文本 |

## 七、禁止

- 禁止 `pair_code` 明文存库（只存 `argon2id(normalize(code))`）；返回明文只允许一次。
- 禁止在审计里存完整环境变量或完整工具参数（除脱敏后）。
- 禁止用 `json.dumps(registry_node)` 直接返给前端。
- 禁止在前端 `localStorage` 存 token（XSS 直接拿走）。
- 禁止把 `/admin` 默认监听 `0.0.0.0`。
- **禁止把 admin app mount 到数据面 listener（`:8443`）上**——这正是 v1.10 修掉的错误。
- **禁止给管理面加任何 `Access-Control-Allow-*` 头**，包括 `OPTIONS` 预检也不回。
- **禁止用非 `__Host-` 前缀的 cookie 名**（`ac_admin`、`admin_token` 一律不认）。
- **禁止提供匿名的设备登记入口**——登记必须已登录且 step-up 重输密码，或走带绑定票的 `/admin/bind`；不允许「填个公钥就提交」的表单。
- **禁止 core 生成设备（管理员）密钥对**，也禁止 core 充当 CA 签发任何证书；`cryptography` 只允许用于**验签与 ECDH**。core 自己的**身份密钥**（`data/admin/core_identity.key`）是另一回事，由 core 生成并自持，不发给任何人（§7.1.3）。
- 禁止把设备私钥或 `core_identity.key` 写进日志、审计、响应体、错误信息。
- 禁止在管理面响应里返回 `text/html`（`/admin/api/*` 一律 JSON + `nosniff`）——App 内 WebView 不允许拿到可执行的 HTML（§7.11 第 2 条）。
- 禁止前端用 `innerHTML` / `dangerouslySetInnerHTML` 插值任何来自 API 的字符串（ESLint 规则 `react/no-danger` + 自定义规则强制），这是 Bearer 模式的代价（token 在内存里，XSS 能读）。
- 禁止在 `admin.json` 之外留任何**登录用** token 文件（`data/admin.token.hash` 已废弃，发现即删）。唯一允许的 token 文件是 `data/setup.token`，且它只用于 setup 阶段。
- 禁止把 `AUTH_FAILED` 细分成 `BAD_PASSWORD` / `KEY_NOT_FOUND` / `BAD_SIGNATURE`——审计里可以细分，响应体不行。
- **禁止在 URL、日志、审计里出现 `setup_token` 的值**，包括前缀与后缀。审计只记 `source` 与 `ok`。
- 禁止实现「setup 超时自杀」——会让云端「先起容器后配置」不可用。
- 禁止把 token 生成写成「只有首次启动才生成」——那样重启后沿用旧值，轮换形同虚设。
- 禁止在没有 `flock data/core.lock` 的情况下启动（两个进程会互相覆盖 token）。
- 禁止用「`initialized` 标志位」代替「`admin.json` 是否存在」做 setup 判据。
- **禁止 `import memory_module` 或直连 `memory.db`**——记忆只能经 `MemoryPort` 访问。
- **禁止自己解析 `SKILL.md` frontmatter**——一律调 `SkillLoader`，校验规则只能有一份。
- 禁止提供「编辑记忆原文」的任何端点（包括隐藏的 `PATCH`）。
- 禁止把记忆内容放进 SSE 广播——那等于把全量记忆推给所有 attached UI。
- 禁止在公钥上传时省掉 step-up 密码校验（只查 CSRF 不够）。

## 七之二、CLI 子命令（本批必须交付）

管理面的恢复与信任配置只能从本机做，因此 C5a 要提供**十一个**子命令，挂在 `python -m vade admin ...` 下：

> **接线责任（v1.18 明确）**：`core/vade/__main__.py` 归 C2（见 `01-contracts` 目录所有权）。C2 的 argparse **必须**预置一个 `admin` subparser，`set_defaults(func=vade.admin.cli:main)` 转发；C5a 只写 `core/vade/admin/cli.py`（含 `main()` 与各子命令实现），**不改 `__main__.py`**。双方在 W4 第 1 天对齐这个入口签名——C2 若未预置，C5a 全部子命令无法被调用。

| 命令 | 作用 | 约束 |
|---|---|---|
| `admin trust-key --pubkey-file F --label L` | 追加**设备**（`alg` 由文件格式推断，P-256 可用 `--alg p256` 指定） | 只能本机执行；**救援用**，正常路径是 App 扫码；重复公钥报错不静默覆盖 |
| `admin revoke-device --id K` | 删除设备并踢掉其会话 | 立即生效；写审计。**命令名统一为 `revoke-device`**（与方案 §7.1.3 一致，不要另造 `revoke-key`） |
| `admin issue-bind-ticket [--ttl 10m]` | 生成一张绑定票并打印 `bind_url` + 二维码 | **救援用**（设备全丢）；一次性、默认 10 分钟；写审计（不含票明文） |
| `admin print-fingerprint` | 打印 core 身份指纹 `fp8`（`SHA-256(core_identity_pub)[:16]`） | 与 setup 页、二维码 `fp` **必须一致**；已初始化后仍可用 |
| `admin print-tls-fingerprint` | 打印 TLS 证书 SPKI 指纹 `SHA-256(SPKI)[:16]` | 与二维码 `tls` 字段一致。**与 `fp8` 是两个值**，不要混 |
| `admin init --password-stdin [--pubkey-file F]` | 停机写入 `admin.json`、删 `data/setup.token`、退出 setup | **运行中执行必须拒绝**；密码规则与页面一致（D63 / FR-9.49） |
| `admin proxy --listen 9444 --to <core>:8444` | 本地代签代理（开发者工具） | 不缓存 cookie、不落盘私钥；help 里标注「开发者工具，不是用户路径」 |
| `admin reset-password` | 交互式重设密码（不走 HTTP） | 需要 stdin 是 TTY；读两遍；写 `admin.json` 后让所有会话失效 |
| `admin print-setup-token` | 打印 `data/setup.token` 的完整值 | 未初始化才有意义，已初始化直接报错退出 |
| `admin rotate-setup-token` | 重新生成 token，作废旧值 | 未初始化时用；写审计（不含 token 值） |
| `admin reset` | 清空 `data/admin/` 重新进 setup 模式并生成新 token | **必须二次确认**（输入 `RESET`）；写审计 |

这十一个命令都**不做远程调用**，直接读写 `data/`（`print-setup-token` / `rotate-setup-token` 动 `data/setup.token`，其余动 `data/admin/`）。C2 的 `--print-fingerprint` 与它们是并列的运维入口。

> **`--print-setup-token` 只有一个所有者 = C5a（v1.18 明确）**：`data/setup.token` 的整个生命周期只归 C5a 的 `setup_token.py`（生成/轮换/消费/打印）。C2 的 `__main__.py` **不再实现** `--print-setup-token`——两者读同一个文件必然打架。C2 只保留 `--print-fingerprint` / `--print-tls-fingerprint` / `--print-san` 这些无状态的诊断入口。

> **`admin trust-key` 与 `admin issue-bind-ticket` 都是救援工具，不是用户路径。** help 文本里要写「正常配置请在 App 里扫码」——否则运维会把它当成主流程，然后用户又被拖回「必须有电脑」的老路（AMEND-17/A57）。`--print-fingerprint` 例外，它是常规诊断命令。

- 禁止为了"实时"让前端轮询（用 SSE）。

## 八、交付格式

按 `00-orchestration.md` §三，额外附：
- `schema/admin-api.yaml`（C-08）与 `schema/device-bind.json`（C-17）的 sha256 与首版提交时间（证明第 1 天 / 第 2 天冻结）
- 密钥扫描的原始输出（0 命中）
- 一次完整审批的事件流 + 审计行
- 列表页/审计页截图（桌面 + 窄屏各一）
