# C3d · 事件推送与 skill 提案受理（core 内）

| 项 | 值 |
|---|---|
| Agent 代号 | `c3d-events` |
| 波次 | W4（与 C3b 同批，契约先定） |
| 依赖 | C3 的 session-store（提供转录）、C3b 的 skill schema |
| 目录所有权 | `core/vade/events/`、`core/vade/skill/proposal.py`、`schema/skill-proposal.schema.json`（C-13）、`schema/events.json`（C-14，**追加权、无改写权**）；`schema/manifest.schema.json` 的 `subscriptions` 字段由 **C2 在 W2 预置**（见下） |
| 出口 | **C-13 / C-14 两份契约在 W4 中冻结**；订阅白名单生效 + 推送可断点 + 提案落 pending 且不被注入 |

## 一、一句话任务

给模块开一条**只能收、不能要**的通道：模块声明想听什么，core 决定推不推、推多少；模块提交的 skill 只能落在 `pending/`，永远不直接生效。

## 二、必读

- `docs/design.md` §2.5（RPC 方法表）、§16.5（提案与审核）、§16.7（权限与开关）
- `docs/requirements.md` A30、A31、AC-27
- `docs/agents/01-contracts.md` C-03、C-11
- `docs/agents/23-C3b-skill-loader.md`（pending / revision / history）

## 三、目录

```
core/vade/events/
├── subscribe.py     # 订阅表、白名单校验
├── deliver.py       # 推送器：拉转录、裁剪、背压
└── transcript.py    # 从 session-store 组装某段 seq 的转录

core/vade/skill/proposal.py   # skill/proposal 受理
schema/skill-proposal.schema.json   # 【T0 产出】C-13：提案载荷 + base_revision 语义
schema/events.json                  # 【T0 产出】C-14：在 C-06 上追加 session.turn / session.closed（及 skill.proposed，见 §五）
```

> **关于 `manifest.schema.json` 的 `subscriptions` 字段（v1.18 修正）**：该文件是 **C-03，C2 所有，W2 第 1 天已冻结**，C3d 无权直接改。`subscriptions` 这个可选字段已由 **C2 在 W2 的 T6 预置**（`additionalProperties: true` 本就前向兼容，C2 只需在 examples 里带一个示例）。C3d 只做**主题名合法性**校验（注册期 C2 只校验「字符串数组」结构，主题名在 `events/subscribe` 时由 C3d 校验）。若 C2 漏预置，C3d 提 RFC 补，**不得直接改已冻结文件**。

## 三·五、接入方式（与 C3b 同一手法）

```python
def install(app) -> None:
    bus = EventBus(
        store=app.session_store,
        allow_transcript=app.config.evolve.allow_transcript,
        allowlist=app.config.evolve.allowlist,       # [(module_id, fp), ...] core 侧白名单
        redact=app.config.evolve.redact,             # core 侧裁剪清单，模块不能改
    )
    app.rpc.register("events/subscribe", bus.on_subscribe)
    # events/deliver 是 core→模块 的推送，你只负责发，不需要注册 sink
    app.rpc.register("skill/proposal", proposal_handler(app))
    app.on_session_end.append(bus.emit_closed)       # C3 的 T6 遍历调用；你不改 C3 的代码
```

- **只 append 回调，不改 `session/` 与 `loop/`**。接入契约是**包根 `__init__.py` 暴露 `install(app)`**（C2 的 `install_components` 只找这个属性）；**不要在包里放 `install.py`**，C2 不会加载它，等于整个 events 组件静默不注册。
- `install()` 必须幂等。
- `allowlist` / `redact` **只从 core 配置读**，不从请求读（§16.7）。模块传 `redact: []` 要能识别为"试图关裁剪"并记审计。

## 四、订阅主题与白名单

| 主题 | 载荷 | 允许的模块类型 |
|---|---|---|
| `session.turn` | `{session_id, turn_id, state, user_text, assistant_text, tool_calls:[{tool,args_digest,ok}]}` | 仅 `evolve` 类 |
| `session.closed` | `{session_id, turns_total, turns:[{turn_id,state,tool_calls,negative,self_corrected}], tail:[{turn_id,user_text,assistant_text}], closed_at}` | 仅 `evolve` 类 |
| `skill.proposed` | `{name, origin, revision, base_revision, status:"pending"}`（**只含元数据，不含正文**） | 推给已订阅的 `ui` / admin 前端（C5a `/skills` 页可用它刷新「待审核」列表） |

> **`skill.proposed` 归 C-14**（v1.18 修正）：本节原在「提案受理」里引用「C-14 的 `skill.proposed` 事件」，但 C-14 初始只定义了 `session.turn` / `session.closed`。现明确 **C-14 追加三个主题**，`skill.proposed` 是其中之一，随 C-14 在 W4 中一起冻结。

**字段级要求**（AMEND-19，C-14 追加时就按这个来）：

- `user_text` 与 `assistant_text` **分开给**，不要合成 `messages[]`。理由：M4b 的 `turn_end` 只允许从 `user_text` 抽候选（`46:§五`），合并后它就得靠猜角色——猜错一次，模型自己的“结论是…”就进了记忆。
- `session.closed` **不含整段转录**，只含 `tail`（最近 10 轮原文，**core 强制截断**），也**不含 rolling summary**。摘要由 M4b 自己在 `turn_review` 里累计（同一次调用顺带产出，边际成本为零）；core 再算一遍等于重复付费。
- `self_corrected` 由 C3 提供：同一会话内后轮推翻了前轮结论时为 `true`（判据：assistant 文本命中自纠模式 **且** 后续工具调用与结论一致）。M4b 用它挡住“把纠错轨迹学成最佳实践”。

**双许可**，缺一不可：

1. **调用方在 core 侧白名单里**：`(module_id, 公钥指纹)` 必须出现在 `[evolve] allowlist`。这一条在 `on_subscribe` 里做，`EventBus` 持有 allowlist 引用；不在白名单 → `rejected`，理由 `not allowlisted`，记审计。**这是三重许可里唯一不可被模块伪造的一条**，不能省略。
2. manifest 的 `subscriptions` 数组里声明了该主题。**注意分工**：C2 在注册期只校验它是「字符串数组」（结构），**主题名合法性由你在 `events/subscribe` 时校验**——C2 在 W2 已合并，不该为了两个主题名返工；
3. core 配置 `[evolve] allow_transcript = true`。

任一不满足 → `events/subscribe` 返回 `rejected: [{topic, reason}]`，**不断连接**（模块可以只用工具能力）。

## 五、任务分解

### T1 · manifest 扩展（0.5 天）

```json
{
  "subscriptions": ["session.turn", "session.closed"]
}
```

- 可选字段，默认 `[]`。
- 枚举校验：只允许上表两个主题；拼错 → 返回 `{accepted:[], rejected:[{topic, reason:"unknown topic"}]}`，**不是拒注册**。
- **只有声明了 `subscriptions` 的模块才允许接收 `events/deliver`**，即使它没调用过 subscribe。

### T2 · `events/subscribe`（0.5 天）

```json
// 请求
{"topics": ["session.turn"], "since_seq": 128, "redact": ["tool_args"]}
// 响应
{"accepted": ["session.turn"], "rejected": [{"topic": "session.closed", "reason": "not in manifest"}]}
```

- `since_seq` 可选：重连时从该 seq 之后补推（只对 `session.turn` 有效，`session.closed` 不补）。
- 订阅表存内存：`{instance_id → {topics, since_seq, redact}}`，进程重启即失效（模块重连会重新订阅）。
- 同一实例重复订阅 → 覆盖，不报错。

### T3 · `events/deliver` 推送（1.5 天）

```
turn 完成 → 组装 {topic, session_id, seq_from, seq_to, payload}
          → 找订阅者 → 按 redact 裁剪 → 异步推送（每实例一个有界队列）
```

- 队列长度 32，满了**丢最旧的**并计 `delivery_dropped`（进化是 best-effort，不能因为模块慢拖垮会话）。
- 推送超时 5s，失败记 `delivery_failed`，**不重试**——模块重连后用 `since_seq` 自己补。
- 会话转录从 session-store 按 seq 范围读，不额外存一份（避免双写不一致）。
- `redact` 命中字段替换为 `sha256(...)[:16]`，并在 payload 里加 `redacted: true`。
- **审计只记 `{module_id, instance_id, session_id, turns, bytes, ok}`，不记 payload**。

> 明确接受的不完美：`session.closed` 若推送时模块正好断线，这次演化就丢了。不做持久化重投——为一条 best-effort 通道引入投递语义（至少一次 / 恰好一次）的复杂度不值。这条写进 README。

### T4 · `skill/proposal` 受理（1 天）

```json
{"name": "code-review", "action": "create" | "patch",
 "content": "<完整 SKILL.md>", "base_revision": 3,
 "derived_from": {"session_id": "...", "turn_ids": ["..."], "model": "..."}}
```

受理顺序（**任一失败即整体拒绝，不落盘**）：

| 校验 | 规则 | 错误码 |
|---|---|---|
| schema | frontmatter 符合 C-11；正文 ≤ 8KB | `MANIFEST_INVALID` |
| name | `^[a-z][a-z0-9-]{1,31}$`；`create` 时 active 不得已存在 | `TOOL_CONFLICT` |
| action=patch | 必须带 `base_revision`，且等于当前 revision | `TOOL_CONFLICT`（理由写实际 revision） |
| 配额 | active + pending 合计 ≤ 64 | `OVERLOADED` |
| 内容扫描 | 无 `sk-` 密钥、无 `BEGIN PRIVATE KEY` | `INVALID_ARGUMENT` |
| 路径 | 落盘路径 `realpath` 后仍在 `skills/pending/` 内 | `PERMISSION_DENIED` |

落盘：`skills/pending/<name>/SKILL.md` + 同目录 `meta.json`（含 `derived_from`、`received_at`、`received_from`、`origin="proposal"`）。**必须是目录**，与安装路径共用同一布局（方案 §16.5）；单文件布局存不下资源，也无法整目录 `os.replace`。

- 同名提案已存在 → 覆盖旧的 pending 文件（同一个 name 只留最新一个），旧的重命名为 `.superseded`。
- `pending/` **不参与注入**，也不出现在 `skill_catalog`（C3b 保证）。

### T5 · 审核落地的入口（0.5 天）

只做**核心函数**，UI 由 C5a 的 admin 提供：

**不在这里实现 `approve` / `reject` / `rollback`。** 这三个动作的唯一实现在 C3b 的 `SkillLoader`（T4.8 已声明），你只负责：

- 提案落盘（`pending/<name>/SKILL.md + meta.json`，`origin=proposal`）；
- 提供 `SkillProposal` 的校验与 `base_revision` 冲突判定；
- 把「该 skill 有新的 pending 提案」通过 C-14 的 `skill.proposed` 事件推给 UI。

调用方（C5a 的 `/admin/api/skills/{name}/approve|reject|rollback`）直接调 `SkillLoader` 的同名方法。**两套 approve 会让「提案」和「安装」两条路径各自只认自己那一套布局**，其中一条永远批不了。

- 三个动作的审计在 `SkillLoader` 里写（`actor` 从鉴权上下文取，不能由调用方传）。
- `approve` 时若文件已被改动（hash 不符）→ 拒绝，要求重新提交。

## 六、验收

| # | 场景 | 期望 |
|---|---|---|
| 1 | manifest 未声明 `subscriptions` 的模块调 `events/subscribe` | `rejected` 含 topic 与 `not in manifest`；连接不断 |
| 2 | 声明了但 `allow_transcript=false` | `rejected` 含 `disabled by config` |
| 3 | 正常订阅后跑一轮 | 收到 `events/deliver`，payload 含 seq 范围，审计有记录且无内容 |
| 4 | 模块慢消费（每次 sleep 2s） | 队列满后丢最旧，`delivery_dropped` 递增，会话不受影响 |
| 5 | 断线 10s 后重连，带 `since_seq` | 补推缺口，无重复无遗漏（按 seq 校验） |
| 6 | `create` 一个已存在的 skill 名 | `TOOL_CONFLICT`，pending 无新文件 |
| 7 | `patch` 带错 `base_revision` | `TOOL_CONFLICT`，理由里有实际 revision |
| 8 | 提案正文含 `sk-xxxx` | `INVALID_ARGUMENT`，不落盘 |
| 9 | 提案落 `pending/` 后重启 core | prompt 里**没有**该 skill；`skill_catalog` 里也没有 |
| 10 | `approve` 后 | 出现在 `skill_catalog`；`revision` +1；`.history/` 有旧版 |
| 11 | `rollback` 到 rev1 | 内容等于 rev1，`revision` 变成当前+1（不删历史） |
| 12 | 提案路径构造 `../../core.toml` | `PERMISSION_DENIED` |

## 七、禁止

- 禁止向未声明 `subscriptions` 的模块推送（即使它 subscribe 成功）。
- 禁止推送 `session.closed` 之外的任何会话内容给非 evolve 类模块。
- 禁止在审计里记录转录内容。
- 禁止让 `pending/` 参与注入或出现在 `skill_catalog`。
- 禁止 `approve` 时跳过 hash 校验。
- 禁止 `actor` 由调用方传入（必须取鉴权上下文）。
- 禁止为 `events/deliver` 引入重投/确认机制（v1 明确 best-effort）。

## 八、交付格式

按 §三，额外附：一次订阅被拒的请求/响应原文、一次推送的 payload（脱敏）、水位与丢包计数日志、一个 create 与一个 patch 提案的落盘路径与 meta、approve 前后的 `skill_catalog` 对比。
