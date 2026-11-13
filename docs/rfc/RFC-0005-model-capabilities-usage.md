---
rfc: RFC-0005
title: 模型能力查询与用量：`model/list`、会话级选择优先级与两级用量口径
status: proposed
date: 2026-11-14
proposer: 编排（依据 `research/ui-decisions.md` #15 / #16、`research/ui-02-messages-input-attachments.md` §3.9 C7；AMEND-22 §8.22.2 R5、D96 / D101 / D102；PROGRESS.md 技术债 1）
affects: C-05 `schema/rpc.json`（追加 `model/list`、`session/model.get|set`、`session/usage.get` 与 `ui/attach` snapshot 注记）、C-03 `schema/manifest.schema.json`（`capabilities.models[]`，追加）、C-06 `schema/events.json`（只追加）、C-18 `schema/config.schema.json`（`[llm]` 4 键 + `[usage]` 5 键）；内部 `core/vade/llm/*`、`core/vade/session/*`、`core/vade/admin/app.py`
related: RFC-0004（附件与多模态；§9 的四个能力字段是本提案的硬依赖）；AMEND-22 §8.22.2 R5；FR-6.3、FR-8.46～8.50、FR-10.3；D96（只存压缩图）、D101（会话级优先）、D102（用量两口径）
type: 契约追加（C-03 / C-05 / C-06 / C-18）；`LLMProvider.chat` 的 Python 协议变更属 core 内部破坏性
stage: 草稿，未实现；不与 RFC-0004 同时实现时，能力字段全 `null`，降级路径见 §9
---

# RFC-0005 · 模型能力查询与用量

## 1. 摘要

core 现在既没有 `model/*` RPC，也没有任何“这个模型能读图吗”的声明面：`schema/rpc.json` 的 `ui_to_core` 只有 6 个方法，`ModuleLLMProvider.select()` 只按 `manifest.priority` 选模块。本提案加一个只读的 `model/list`（内置 provider 读 `[llm.capabilities]`，模块在 manifest 的 `capabilities.models[]` 自报），加会话级选择 `session/model.get|set`（解析顺序 **会话显式 > 全局默认 > `manifest.priority`**，在 turn 开始时解析），并把用量拆成同一份数据的两个视图：`turn.completed.usage` 只追加字段给会话累计，审计 `llm.call` 行给模块归因。

| # | 决定 | 理由 |
|---|---|---|
| 1 | 能力由**提供服务的一方**声明，core 只聚合，绝不从模型名推断 | 猜错的代价是一次 5 MiB 白传（RFC-0004 §9），以及把 image block 塞给纯文本模型 |
| 2 | 未声明 `capabilities.models` 的 `chat` 模块仍出现在列表里，条目为 `<module_id>/*`、能力字段全 `null` | 否则现有 M6 模块在切换器里“隐身”，FR-8.46 的只读徽标会变成空列表 |
| 3 | 会话选择落在 `sessions.model_id`，在 turn **出队时**解析，运行中的 turn 不受影响 | “下一轮生效”是可验收的边界；运行中换模型会让同一 turn 的 usage 无法归因 |
| 4 | 显式选择**禁用** `fallback_to_builtin`；全局默认和 priority 路径保留回落 | 用户选了 B，core 静默用 A 回答，比报错更糟；运维默认则允许降级 |
| 5 | 预算**默认真阻断**（FR-8.49）；仅当 usage 不可信（`source="none"`）时降级为告警并标注 `degraded`；`turn.completed.usage` 与审计 `llm.call` 必须逐 turn 对账 | 需求原文要求超限拒绝；但用不可靠的账硬断会吞消息，所以只在“账可信”时硬断 |

## 2. 动机与证据

### 2.1 契约里没有 `model/*`，UI 只能猜

`schema/rpc.json:90-121` 的 `ui_to_core` 是封闭的 6 个方法：`ui/attach`、`ui/detach`、`ui/submit`、`ui/interrupt`、`ui/queue.cancel`、`approval/decide`。FR-8.46 要求“切换器显示当前模型名 + 会话级/全局切换”，FR-8.47 要求“能力徽标”，而 core 一个字段都不给。UI 若自行维护模型清单，就是第二份真相，且必然与 `[llm].model` / 模块在线状态漂移。

### 2.2 模块只能通过 `chat` 工具表达“我是模型”

`core/vade/llm/module.py:24` 把 `MODEL_TOOL = "chat"` 当作唯一判据；`select()`（46-66）按 `(-manifest.priority, -last_frame_at)` 排序。`schema/manifest.schema.json` 的 `capabilities` 只 `required: ["tools"]`，`additionalProperties: true`——**没有任何能力声明位**。于是“这个模块背后是哪个模型、能不能读图、上下文多大”只有模块自己知道，core 与 UI 都不知道。

### 2.3 能力未知 = 一次 5 MiB 白传

RFC-0004 §9 已把接口收敛为四个字段（`input_modalities`、`max_image_bytes`、`max_image_px`、`max_images_per_turn`），并规定“能力未知 → 允许上传、提交时 core 判”。这条降级是**为了不误伤**，代价是用户在纯文本模型上选一张 5 MiB 图会完整上传后才收到 `UNSUPPORTED`。FR-8.50 ① 要求“选图即提示、不白传”，其前提正是本提案的 `model/list`。

### 2.4 用量有基础，但缺三样东西

现状是完整的“累加—落库—发事件”链路：

- `core/vade/loop/engine.py:104` 起 `usage: dict[str, Any] = {}`，每个 delta 的 int 值累加（131-134）；
- `core/vade/session/queue.py:182-199` 把 `outcome.usage` 写进 `Turn.with_state(COMPLETED, usage=...)` 并原样放进 `turn.completed` payload；
- `core/vade/session/sqlite_store.py:47` 的 `turns.usage TEXT NOT NULL DEFAULT '{}'` 已持久化。

缺的三样：

1. **没有 `model_id` / `provider`**：同一个会话换过模型后，累计值无法按模型拆分，审计也无法归因。
2. **snapshot 不含 turns**：`core/vade/session/manager.py:281-293` 只返回 `session`、`messages`、`queue`、`pending_approvals`。换端 attach 后，客户端拿不到历史 turn 的 usage，只能从零开始累加事件流——FR-8.49 的“换端 attach 后累计一致”在今天的契约下**做不到**。
3. **失败/中断的 turn 丢账**：`queue.py` 的 `FAILED` / `INTERRUPTED` 分支不写 `usage`，`Turn.with_state` 也不带——已经烧掉的 token 既不进会话累计，也不进审计。

### 2.5 审计侧已经“读”了 `llm.call`，但没人“写”

`core/vade/admin/app.py:862-877` 的今日用量是这么算的：查 `kind="llm.call"`、`limit=500`，对当天行累加 `extra.tokens`。但全仓 `kind="llm.call"` 的**写入点是 0 个**（admin 只是 reader，测试里也没有写审计行）——今日用量恒为 0；即使写，500 行截断也会少算。FR-10.3 的“按模块归因”因此没有数据源，D102 的审计视图是空的。

### 2.6 三条结论

1. 能力必须由提供方声明，core 只做聚合与校验；未知就是 `null`，不是 `false`、不是 `[]`。
2. 选择优先级必须落在 turn 边界并暴露“生效来源”，否则 `manifest.priority` 会继续偷偷覆盖用户选择。
3. 用量必须一份数据两种视图，且**逐 turn 可对账**；否则“会话累计”和“审计归因”迟早给出两个数。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 当前模型不支持视觉时，选图阶段即提示，**上传端点调用次数为 0**（FR-8.50 ①） |
| G2 | 会话内 A→B 切换后，**下一条 turn** 的请求体 `model` 为 B，历史 turn 的 usage 不重算（FR-8.46） |
| G3 | 任一 turn：`turn.completed.usage` 与审计 `llm.call`（`purpose="chat"`）逐项相等；会话累计 = Σ 所有终止 turn 的 `usage`（含 failed/interrupted） |
| G4 | 换端 attach 后 `session/usage.get` 返回同一组数字与同一预算状态（FR-8.49） |
| G5 | 能力缺失全链路显示“未知”：不入选过滤器、不显示“视觉 ✓”、不预压到模型专属尺寸、不伪造条目 |
| G6 | 覆盖目标离线/被移除时 `resolved=false`，提交返回可读错误并给 [切换模型]，**不静默换模型** |
| G7 | 旧客户端、旧会话、旧模块零迁移可读；旧 core 缺方法时 UI 仍走 FR-8.46 的只读徽标 |
| N1 | 不重写路由策略：`Router`、`tools/call`、实例选择、熔断、超时不动；`manifest.priority` 只收敛为“默认” |
| N2 | 不做计费/价格表/成本估算（FR-10.3 的“成本”一半仍缺，见 Q3） |
| N3 | 不做会话级高级参数（FR-8.48）：`session/*` 名字空间预留，本提案只定义 `model.*` 与 `usage.get` |
| N4 | 不做主动能力探测：不发试探请求、不从 4xx 反推能力、不从模型名映射能力表 |
| N5 | 不改 C-09 组件接口（只消费 RFC-0004 的 `AttachmentChip`）；C-04 **仅追加**一个 `BUDGET_EXCEEDED`（见 §6.4；产品不接受则退化为 `PERMISSION_DENIED` + `data.reason`） |
| N6 | 不做模块内部 LLM 调用的归因：模块自己调上游模型的花费对 core 不可见，v1 只归因 core 发起的调用 |

## 4. `model/list`

### 4.1 方法形状

```json
{"jsonrpc":"2.0","id":7,"method":"model/list","params":{"if_revision":12}}
→ {"revision":13,"not_modified":false,
   "models":[{"id":"builtin/gpt-4o-mini","provider":"builtin","display_name":"GPT-4o mini",
              "input_modalities":["text","image"],"max_image_bytes":20971520,
              "max_image_px":7680,"max_images_per_turn":10,"context_window":128000,
              "supports_tools":true,"priority":0,"available":true}],
   "default":{"model_id":"builtin/gpt-4o-mini","source":"global","resolved":true}}
```

- `params.if_revision` 可选；与服务端 `revision` 相等时返回 `{"revision":N,"not_modified":true,"models":null}`，客户端复用缓存。
- `errors`: `["UNAVAILABLE"]`（core 尚未装配完成）。无 `model` 概念时返回空 `models`，**不**返回 404。
- 候选集由 `[llm].provider` 决定，`model/list` 只列**当前模式真的能路由到**的模型，不列“理论上存在”的：

| `[llm].provider` | 候选集 |
|---|---|
| `builtin`（默认） | `builtin/<llm.model>`（`base_url` 为空时为空列表） |
| `module` | 所有在线 `chat` 模块声明的模型 + `fallback_to_builtin=true` 且已配置时的 `builtin/<llm.model>` |
| `mock` | `builtin/mock`（能力全 `null`，仅供测试） |

这条过滤是刻意的：列出无法路由的模型，等于让用户选一个必然失败的 id（N1 不改路由）。

### 4.2 条目字段

| 字段 | 类型 | 可空 | 语义 |
|---|---|---|---|
| `id` | string | 否 | `<provider>/<model>`；在**第一个** `/` 处切分。`provider` 为 `"builtin"` 或 `module_id`（`module_id` 的 pattern 禁 `/`，见 `manifest.schema.json:36-42`），`model` 可含 `/`（如 `meta-llama/Llama-3`） |
| `provider` | string | 否 | `"builtin"` 或模块 `module_id`；与 `id` 前缀一致 |
| `display_name` | string | 否 | 展示名；缺省回落 `model`（`*` 时回落 `module_id`） |
| `input_modalities` | string[] | **是** | `text` / `image` / `audio` / `file` 的子集；`null` = 未声明 |
| `max_image_bytes` | int | **是** | 压缩后单图字节上限；与 `attach.image.max_bytes` 取 min（RFC-0004 §9） |
| `max_image_px` | int | **是** | **长边**像素上限；与 `attach.image.max_edge` 取 min（RFC-0004 §9 的比较口径） |
| `max_images_per_turn` | int | **是** | 单 turn 图片数；与 `attach.max_per_turn` 取 min |
| `context_window` | int | **是** | token 数；已知时 UI 显示“已用 x%”，未知显示“未知” |
| `supports_tools` | bool | **是** | `false` = core 不发 `tools` 数组；`null` = 未声明，**按现状继续发**（见 §4.5） |
| `priority` | int 0..100 | 否 | 模块取 manifest，内置固定 0；只用于默认选择，不参与会话覆盖 |
| `available` | bool | 否 | 该 provider 至少 1 个健康实例；离线条目保留但 `false` |

`null` 与 `false` / `[]` **不可互换**：`supports_tools:null` 不是“不支持工具”，`input_modalities:null` 不是“只支持文本”。FR-8.47 的“能力未知显示未知、不得乐观假设”在契约层就靠这条区分。

### 4.3 谁提供

| 来源 | 声明位置 | 说明 |
|---|---|---|
| 内置 provider | `[llm]` 的 `model` + `[llm.capabilities]`（C-18 新增） | 单模型；`capabilities` 整块可省，省了就是全 `null` |
| 模块（自报） | manifest `capabilities.models[]`（C-03 追加） | 每项一个模型；同一模块多实例声明不一致时取**保守交集**（`min` / 逻辑与 / `null` 传染） |
| 模块（未自报） | 无 | 合成一条 `<module_id>/*`，`display_name=module_id`，能力全 `null`；选中后请求体**不带** `model`，由模块用自己的默认 |
| mock | 内置 | `builtin/mock`，能力全 `null` |

**为什么不让 core 反向 RPC 问模块要列表**：`model/list` 是 UI 冷启动就会调的只读方法，反向 RPC 会让“模块离线”和“查询模型列表”互相拖死——而这正是最需要看到列表（含离线项）的时刻。manifest 已经在注册时校验（C-03），`module/capabilities.update` 提供原子刷新（`cap_epoch += 1`），比每次现问更省也更有据可查。

`capabilities.models[]` 的形状：

| 键 | 必填 | 校验 |
|---|---|---|
| `name` | 是 | 非空、≤128、不含 `/` 与空白；同模块内唯一 |
| `display_name` | 否 | ≤128，仅展示 |
| `input_modalities` | 否 | 未知枚举值丢弃并告警；最多 4 项 |
| `max_image_bytes` / `max_image_px` / `max_images_per_turn` / `context_window` | 否 | 正整数；越界或非整数 → 该字段置 `null` 并告警，**不拒绝整个 manifest** |
| `supports_tools` | 否 | bool |

`models` 最多 16 项；超过 → `MANIFEST_INVALID`（防 manifest 变成第二个模型目录）。单个坏条目只丢该条目，不拖垮模块注册——与 C-03 “未知能力类型忽略并告警”的既有容错一致。

### 4.4 缓存与刷新

- **服务端没有 TTL 缓存**：列表由 `Registry.snapshot()`（内存快照）+ 配置在每次调用时现算，成本是模块数级别的遍历。真正的缓存是 registry 本身。
- `revision` 是 core 进程内的单调计数器，在以下事件 +1：`module/register`、`module/capabilities.update`、`module/drain` 完成、连接断开、健康状态在 `offline`/`rejected` 与可用态之间翻转、`[llm]` 热改键生效。重启后从 1 重新开始；客户端按“不相等即全量”处理，**不假设跨进程单调**。
- 客户端缓存 `(revision, models, default)`；收到 `module.status` 事件、重连、或打开切换器时带 `if_revision` 复验。`not_modified=true` 时不得清空 UI 上的能力徽标。
- `available:false` 的条目在 registry 记录存在期间**保留**：用户需要看到“我的会话选的模型掉线了”，而不是它凭空消失。模块被 drain/移除后条目移除，会话覆盖变成悬空（§5.4）。

### 4.5 能力语义的三条硬规则

1. `input_modalities` 缺失 → UI 允许选图但标“未知”，提交时由 core 判（RFC-0004 §9 第 3 条）；**不得**在选图时拦截。
2. `supports_tools:null` → core 维持现状（发 `tools`）；`false` → 不发，且该模型不能调用工具。这条只影响“发不发”，不影响审批与 risk 判定（N1）。
3. `max_image_*` 缺失 → 只用 `attach.*` 的静态上限；`context_window` 缺失 → 不显示占用百分比，也不拿它做压缩阈值（`compaction` 仍按自己的配置）。

## 5. 会话级选择

### 5.1 方法

```json
{"method":"session/model.get","params":{"session_id":"s-1"}}
→ {"model_id":"com.example.model/qwen2.5-vl-72b","source":"session","resolved":true,
   "effective":{"id":"com.example.model/qwen2.5-vl-72b","provider":"com.example.model",
                "model":"qwen2.5-vl-72b","available":true},
   "global_default":"builtin/gpt-4o-mini","reason":null}

{"method":"session/model.set","params":{"session_id":"s-1","model_id":"builtin/gpt-4o-mini"}}
→ {"model_id":"builtin/gpt-4o-mini","source":"session","effective_at":"next_turn"}
```

- `session/model.set` 的 `model_id: null` 表示**清除**会话覆盖，回落到全局默认/priority；返回里 `source` 是清除后的来源。
- 校验：`model_id` 必须出现在当前 `model/list` 且 `available=true`；格式错 → `INVALID_ARGUMENT`，未知 id → `NOT_FOUND`（`data.reason="unknown_model"`），已知但无健康实例 → `UNAVAILABLE`（`data.reason="model_unavailable"`）。
- `session/model.set` 是写操作：显式只读会话返回 `READ_ONLY_SESSION`；`get` 与 `model/list` 只读放行。
- `ui/attach` 的 `snapshot.session` 追加 `model: {model_id, source}`，换端后无需再调 `get` 就能点亮徽标。

### 5.2 解析顺序（D101）

| 层 | 来源 | 持久化 | 目标不可用时的行为 |
|---|---|---|---|
| 1 | 会话显式 `sessions.model_id` | DB | **不回落**，`UNAVAILABLE` + `data.reason="model_unavailable"` |
| 2 | `[llm].default_model` | core.toml | 模块目标且 `fallback_to_builtin=true` → 回落内置；否则 `UNAVAILABLE` |
| 3 | 可用候选中 `priority` 最高；并列取 `last_frame_at` 最新，再并列取 `id` 字典序 | 无 | 无候选 → `UNAVAILABLE`（`no model is configured`） |
| — | 都没有 | — | `UNAVAILABLE` |

- `source` 的取值是 `session` / `global` / `priority` / `none`（`none` = 无任何可用候选），FR-8.46 的“切换器显示生效来源”直接用它，不需要客户端反推。
- **显式选择禁用回落**：用户选了模块 B，B 掉线就报错，不拿 A 顶替。全局默认是运维意图，允许 `fallback_to_builtin` 降级；priority 是兜底，本来就走现有回落链。
- 并列时的第三键 `id` 是为了让测试与两台 core 的默认选择确定，不依赖注册顺序。
- `default_model` 不在当前候选集（§4.1）内 → 不回落、按 `UNAVAILABLE` 处理，并写启动/解析日志；候选集会随模块上下线变化，所以这条校验发生在解析时，不在启动时。

### 5.3 持久化与生效边界

- `sessions` 增列 `model_id TEXT NULL`（`ALTER TABLE`，启动用 `PRAGMA table_info(sessions)` 判断，幂等；NULL = 无覆盖）。旧会话零迁移。
- `turns` 增列 `model_id TEXT NULL`：turn 开始时把解析结果钉在 turn 上，用于恢复、`turn.started` 与中断后的归属；这是内部列，不是契约。
- **解析发生在 turn 出队（`QUEUED → RUNNING`）时**，不是 `ui/submit` 时：排队期间切换模型，尚未开始的 turn 用新模型；正在跑的 turn 用旧模型跑完，其 `usage.model_id` 记录旧值。这满足“下一轮生效”，也避免同一 turn 中途换模型导致 usage 无法归因。
- 切换不重建会话、不清草稿（草稿是客户端状态），不等价于中断；`session/model.set` 不触碰 `queue`。

### 5.4 不一致时 UI 提示

| 状态 | 判据 | UI |
|---|---|---|
| 会话覆盖与全局默认不同 | `source="session"` 且 `model_id != global_default` | “会话级”徽标 + tooltip“仅本会话；全局默认为 X” |
| 全局默认 | `source="global"` | “全局”徽标 |
| 按 priority 兜底 | `source="priority"` | “默认（按 priority）”徽标 |
| 覆盖悬空 | `resolved=false`，`reason="model_unavailable"` | 会话顶部横幅“当前模型不可用” + [切换模型]；提交前就提示，不等 `turn.failed` |
| 能力未知 | 字段 `null` | “未知”徽标；**不入选** FR-8.47 的视觉/工具/长上下文过滤器 |
| 列表 revision 变化且当前选择消失 | `session/model.get.resolved=false` | 同“覆盖悬空”，并刷新切换器 |

“不入选过滤器”是 FR-8.47 修订后的原文要求：能力未知的模型只能在“全部”里看到，不能因为用户勾了“视觉”就被当成支持视觉。

## 6. 用量

### 6.1 `turn.completed.usage` 的兼容扩（只追加）

现有键 `prompt_tokens` / `completion_tokens` / `total_tokens` 的名字、类型、含义**一律不改**（`core/vade/loop/engine.py:131-134` 的累加逻辑也不改）。追加：

| 键 | 类型 | 语义 |
|---|---|---|
| `model_id` | string \| null | 本 turn 实际使用的模型 id；历史 turn 为 `null` |
| `provider` | string \| null | `builtin` 或 `module_id` |
| `source` | `"provider"` \| `"none"` | usage 是否由 provider 上报；`none` 时三个 token 数均为 0，**core 不估算** |
| `steps` | int | 本 turn 的 LLM 调用次数（多步工具循环 >1），用于解释累加值 |
| `cached_tokens` | int | 命中前缀缓存的输入 token，未上报为 0 |
| `reasoning_tokens` | int | 推理 token，未上报为 0 |

同一套键也用于 `turn.failed` / `turn.interrupted` 的可选 `usage`；`turn.started` 只追加 `model_id` / `model_source`。中断的 turn 已经烧掉的 token 不该凭空消失；中断时 provider 往往还没发最终 usage，此时 `source="none"`、计数为 0——**承认不知道，不编数**。`Turn.usage` 在四个终止态都写库（修掉 §2.4 第 3 条）。

### 6.2 审计侧：`llm.call` 是归因的唯一数据源

每次 core 发起 LLM 调用（含多步循环里的每一步、压缩、记忆、evolve）写一行审计：

| 列 / `extra` | 值 |
|---|---|
| `kind` | `llm.call` |
| `session_id` / `turn_id` | 所属会话与 turn；非 turn 触发的内部调用 `turn_id=null` |
| `module_id` / `instance_id` | 模块 provider 取 `Router` 的目标（`dispatch.py` 的 `Target`）；内置为 `null` / `null` |
| `method` | `chat` |
| `status` / `code` / `duration_ms` | 沿用现有审计列 |
| `extra.model` / `extra.provider` | 模型名与 `builtin` / `module_id` |
| `extra.purpose` | `chat` \| `compaction` \| `memory` \| `evolve` |
| `extra.prompt_tokens` / `completion_tokens` / `total_tokens` | provider 上报值；未上报为 0 且 `extra.source="none"` |
| `extra.source` | `"provider"` \| `"none"`，与 `turn.usage.source` 同口径 |
| `extra.tokens` | `= total_tokens`，**保留**是因为 `admin/app.py:869` 已经在读它 |

三条纪律：

1. **不重复计数**：`Router` 对 `chat` 工具也会写 `tools/call` 行，那一行没有 token，归因只读 `kind="llm.call"`，两条链路互不叠加。
2. **模块归因只有一个答案**：一次 LLM 调用只属于一个 provider 模块（`module_id`）；工具模块不因被调用而分摊 token——它们没消耗模型的 token（N6）。内置调用归到 `builtin`。
3. **不落内容**：`extra` 只放计数与模型名，不放 prompt/completion 文本、不放 `base_url`、不放密钥；`args_digest` 由审计层统一脱敏。

### 6.3 `session/usage.get`

```json
{"method":"session/usage.get","params":{"session_id":"s-1"}}
→ {"session_id":"s-1",
   "chat":{"prompt_tokens":1200,"completion_tokens":800,"total_tokens":2000,"turns":5,"partial_turns":0},
   "aux":{"total_tokens":300,"calls":2},
   "total_tokens":2300,
   "by_model":[{"model_id":"builtin/gpt-4o-mini","total_tokens":1200,"turns":3},
               {"model_id":"com.example.model/qwen2.5-vl-72b","total_tokens":800,"turns":2}],
   "day":{"day":"2026-11-14","core_total_tokens":9100,"core_limit":200000,
          "session_tokens":2300,"state":"ok"},
   "budget":{"scope":"session","limit":50000,"used":2300,"ratio":0.046,
             "state":"ok","enforced":false}}
```

| 字段 | 口径 |
|---|---|
| `chat` | Σ 本会话所有终止 turn 的 `usage`（completed + failed + interrupted）；`turns` 是终止 turn 数，`partial_turns` 是 `source="none"` 的 turn 数 |
| `aux` | 本会话的 `purpose != "chat"` 调用（压缩/记忆/evolve），来自审计 `llm.call`；`calls` 是行数 |
| `total_tokens` | `chat.total_tokens + aux.total_tokens`；预算判它，不判 `chat` |
| `by_model` | 按 `usage.model_id` 分组；`null` 的历史 turn 归入 `model_id:null` 一行 |
| `day` | 本会话当天（UTC）的 token 与 core 当天总量；`core_limit` 来自 `[usage].daily_tokens`，`state` 按 §6.4 状态机（`off`/`ok`/`warn`/`exceeded`） |
| `budget` | 会话预算状态；`limit=0` 时 `state="off"`、`ratio=null` |

`day` 的“当天”用 **UTC**，与 `audit/writer.py:day_key()` 和 `admin/app.py:864` 一致；跨时区显示由 UI 转换，core 不猜用户时区（Q3）。`day.core_total_tokens` 是聚合值，**不给**其他会话的明细，避免 `session/usage.get` 变成跨会话窥探口。

### 6.4 预算与超限：默认真阻断，用量不可信时降级

| 键（`[usage]`） | 默认 | 生效 | 语义 |
|---|---|---|---|
| `enabled` | `true` | hot | 预算评估开关；`false` 仍记账，只是不发事件 |
| `session_tokens` | `0` | hot | 单会话总 token 预算；0 = 关 |
| `daily_tokens` | `0` | hot | core 当天总 token 预算；0 = 关 |
| `warn_ratio` | `0.8` | hot | 达到该比例进入 `warn` |
| `include_aux` | `true` | hot | 预算是否把 `aux` 计入；关掉只算 `chat` |
| `enforce` | `block` | hot | **`block`** = 超限拒新 turn（FR-8.49 默认）；`warn` = 只提示 |

- 状态机：`ok` → `warn`（`used >= limit * warn_ratio`）→ `exceeded`（`used >= limit`）；`limit=0` 恒为 `off`。
- **`exceeded` + `enforce="block"` + usage 可信 → 拒绝新 turn**：`ui/submit` 返回 **`BUDGET_EXCEEDED`**（C-04 追加一个码）并带 `data.state="exceeded"`、`data.scope`、`data.used`、`data.limit`；已在队列中的 turn 不回溯取消，只拒新提交。
- **降级**：若本轮 usage 的 `source="none"`（provider 不报账），视为“账不可信”，自动降级为告警（`enforce` 不改），发一次 `usage.budget` 且 `degraded=true`，`session/usage.get` 的 `state` 仍为 `exceeded`。降级只影响“拒 turn”这一个动作，不影响记账与提示。
- UI 负责提示与解释（D102：“预算判定归 core，UI 只负责提示”）。
- 事件去重：每个 `(scope, state, period)` 只发一次（session scope 的 period 是 `session_id`，day scope 是 UTC 日期）；重启后最多重发一次，客户端按三元组去重。事件的“真相”仍是 `session/usage.get` 的 `state`，事件只是通知。
- **与 FR-8.49 对齐**：默认 `enforce="block"`，满足验收句“超限后新 turn 被拒”；唯一例外是 usage 不可信时的降级（见上），并在 `usage.budget` 里显式标 `degraded=true`，不静默放过。

### 6.5 对账规则（D102 的可测定义）

1. 单 turn：Σ `llm.call` where `turn_id=T and extra.purpose="chat"` 的 `prompt_tokens` / `completion_tokens` / `total_tokens`，分别等于 `turn.completed.usage` 的同名键。
2. 单会话：`session/usage.get.chat.total_tokens` = Σ 该会话所有终止 turn 的 `usage.total_tokens` = Σ 审计 `purpose="chat"` 行。
3. 当天：`session/usage.get.day.core_total_tokens` = 审计当天全部 `llm.call` 行的 `extra.tokens` 之和（所有 purpose）。
4. **回归护栏**：`admin/app.py` 的 evolve 预算必须改成只累加 `purpose="evolve"` 的行。现在它累加全部 `llm.call`；一旦 chat 行开始写，evolve 的 `daily_token_budget` 会把聊天 token 算进去，直接误伤自进化触发。

## 7. 契约变更

### 7.1 汇总（9 行：契约级 8 + 内部 2 合并在第 9 行）

| # | 契约 | 改动 | 判定 |
|---|---|---|---|
| 1 | C-05 | 新方法 `model/list` | **追加**（v1.0.0 → 1.1.0） |
| 2 | C-05 | 新方法 `session/model.get` / `session/model.set` | **追加**（新 `session/*` 名字空间） |
| 3 | C-05 | 新方法 `session/usage.get` | **追加** |
| 4 | C-05 | `ui/attach` 的 snapshot 注记追加 `session.model` 形状（`snapshot` 仍是 `object`） | **追加** |
| 5 | C-03 | `capabilities.models[]`（≤16 项，字段见 §4.3） | **追加**（`additionalProperties: true`，旧 core 忽略并告警） |
| 6 | C-06 | `turn.completed.usage` 追加键；`turn.started` 追加 `model_id`/`model_source`；`turn.failed`/`turn.interrupted` 追加可选 `usage` | **追加**（`required` 不动） |
| 7 | C-06 | 新事件 `usage.budget` | **追加**（v1.0.0 → 1.1.0） |
| 8 | C-18 | `[llm]` 追加 4 键、新增 `[usage]` 5 键 | **追加**，但 root `additionalProperties:false`：必须随 schema 同批发版；旧 core 读新 `core.toml` 会 `UNKNOWN_CONFIG_KEY` |
| 9 | 内部（非契约） | `LLMProvider.chat(..., model=...)`；`sessions.model_id` / `turns.model_id` 追加列；`turns.usage` 覆盖四个终止态；审计 `llm.call` 写入 | **Python 协议破坏性**（3 个实现全在 core）；DB 追加列 |

### 7.2 C-05：四个新方法

| 方法 | params | result | errors |
|---|---|---|---|
| `model/list` | `{if_revision?: int\|null}` | `{revision, not_modified, models[]\|null, default{model_id, source, resolved}}` | `UNAVAILABLE` |
| `session/model.get` | `{session_id}` | `{model_id\|null, source, resolved, effective\|null, global_default\|null, reason\|null}` | `NOT_FOUND`, `PERMISSION_DENIED` |
| `session/model.set` | `{session_id, model_id: string\|null}` | `{model_id\|null, source, effective_at:"next_turn"}` | `INVALID_ARGUMENT`, `NOT_FOUND`, `UNAVAILABLE`, `READ_ONLY_SESSION` |
| `session/usage.get` | `{session_id}` | 见 §6.3 | `NOT_FOUND`, `PERMISSION_DENIED` |

`model/list` 只读、无副作用；旧 core 没有该方法时，客户端收到 JSON-RPC 的“方法不存在”（-32601），按 FR-8.46 降级为只读徽标 + “由管理员在 core 配置”，**不得**用空列表冒充“没有模型”。

### 7.3 C-03：`capabilities.models[]`

只在 `capabilities` 下追加一个数组，`required` 与既有 `tools`/`prompts`/`events` 均不动。旧 core 的 registry 对未知能力类型“忽略并告警”（`manifest.schema.json:5` 已写明），因此新模块可以在旧 core 上注册，只是模型不出现在列表里。新 core 对坏条目逐项丢弃（§4.3），不让一个笔误拖垮整个模块。

### 7.4 C-06：只追加

- `turn.completed.payload` 顶层**不新增**键；新字段全部在 `usage` 对象内部（events.json 里 `usage` 本来就是 `"object"`），`required` 保持 `[turn_id, state, usage]`。
- `turn.started.payload` 追加 `model_id`、`model_source`（optional）。
- `turn.failed.payload` / `turn.interrupted.payload` 追加 `usage`（optional；中断时可能 `source="none"`）。
- 新事件 `usage.budget`：`payload {scope:"session"|"day", state:"warn"|"exceeded", used:int, limit:int, ratio:number, session_id:string|null}`，`required [scope, state, used, limit, ratio]`。`turn_states` 不改。
- C-06 的 `version` 由 1.0.0 → 1.1.0；已冻结事件一个字段都不改。

### 7.5 C-18：`[llm]` 与 `[usage]`

| 键 | 类型 | 默认 | 生效 | 说明 |
|---|---|---|---|---|
| `llm.model_module` | string | `""` | restart | 空 = 任意 `chat` 模块；非空 = 只允许该 `module_id`（补 PROGRESS 技术债 1） |
| `llm.fallback_to_builtin` | bool | `true` | restart | 模块离线时是否回落内置；显式会话选择不受它影响 |
| `llm.default_model` | string | `""` | hot | 全局默认的完整 model id；空 = 走 priority。指向尚未注册的模块模型时启动不报错，首次解析返回 `UNAVAILABLE` |
| `llm.capabilities` | object | `{}` | restart | 内置模型的能力块：`input_modalities` / `max_image_bytes` / `max_image_px` / `max_images_per_turn` / `context_window` / `supports_tools` / `display_name`，全部可省 |
| `usage.enabled` | bool | `true` | hot | 预算评估 |
| `usage.session_tokens` | int ≥0 | `0` | hot | 单会话预算，0=关 |
| `usage.daily_tokens` | int ≥0 | `0` | hot | core 日预算，0=关 |
| `usage.warn_ratio` | number 0.1..1 | `0.8` | hot | 告警阈值 |
| `usage.include_aux` | bool | `true` | hot | 预算是否计 `aux` |

`llm.model_module` / `fallback_to_builtin` 今天被 `core/vade/session/__init__.py:183-184` 用默认值读、却被 schema 拒绝（`additionalProperties:false`），这次一并补齐。`[usage]` 是全新节，不覆盖 `[evolve].daily_token_budget`（后者只限自进化触发器，两者共用审计数据但过滤器不同，见 §6.5 第 4 条）。

### 7.6 不改

C-04 的既有码（`INVALID_ARGUMENT` / `NOT_FOUND` / `UNAVAILABLE` / `READ_ONLY_SESSION` / `PERMISSION_DENIED`，**只追加** `BUDGET_EXCEEDED` 一个）、C-09（不新增组件）、`turn_states`、`Router` / 审批 / 风险判定、`[evolve]` 与 `[compaction]` 的既有键。

## 8. 验收

A1–A12 全部要求真实执行；截图与手写记录不算证据。

| # | 断言 | 承载 |
|---|---|---|
| A1 | 只有 `chat`、无 `capabilities.models` 的模块 → `model/list` 出现 `<module_id>/*`，六个能力字段全 `null`；内置条目来自 `[llm]`；无 `base_url` 时内置条目不出现 | `core/tests/model/test_list.py` |
| A2 | 同 `revision` + `if_revision` → `not_modified=true`、`models=null`；`module/capabilities.update` 后 `revision` +1 且全量返回 | `core/tests/model/test_list_cache.py` |
| A3 | 会话内 A→B：B 轮请求体 `model=="B"`、`turn.usage.model_id=="B"`；A 轮的历史 usage 不变；新建会话用全局默认 | `core/tests/model/test_session_switch.py` |
| A4 | 会话 B（priority 10）> 全局 A（priority 90）：本会话用 B；`set null` 后回 A；`default_model=""` 后回 priority 最高者；并列时按 `id` 字典序确定 | `core/tests/model/test_precedence.py` |
| A5 | 纯文本模型 + 1 图：UI 选图阶段 `POST /ui/blob` 调用次数 **0**；点“仍然发送”后 1 次上传且 part `type=="file"`；core 收到 image part → 501 `data.capability="vision"`，provider 请求体无 image block | `ui/shared/src/commonTest/.../ModelGateTest.kt` + `core/tests/model/test_vision_gate.py` |
| A6 | `input_modalities:null` → 不进“视觉”过滤结果、不显示“视觉 ✓”；提交不预拦；`supports_tools:null` 仍发 `tools`，`false` 不发 | `core/tests/model/test_unknown.py` |
| A7 | `turn.completed.usage` 保留三个旧键并含 `model_id`/`provider`/`source`/`steps`/`cached_tokens`/`reasoning_tokens`；旧客户端 golden 只读旧键不崩 | `core/tests/session/test_usage_shape.py` |
| A8 | 连跑 5 轮 → `session/usage.get.chat.total_tokens == Σ turn.usage.total_tokens`；第二台设备 attach 后调同一方法得到完全相同的数字与 `budget.state` | `core/tests/session/test_usage_cumulative.py` |
| A9 | 每个 turn：Σ 审计 `llm.call`（`purpose="chat"`）的三项 token == `turn.usage` 同名项；模块调用的 `module_id` 等于目标模块；内置调用 `module_id=null` | `core/tests/model/test_audit_attribution.py` |
| A10 | `session_tokens` 设为实际用量的 80% → 恰好 1 条 `usage.budget(state="warn")`，且**没有** turn 被拒；超过 100% → 1 条 `exceeded`，下一条 turn 仍正常执行 | `core/tests/session/test_budget.py` |
| A11 | 模块被 drain 后：`session/model.get.resolved=false`、`reason="model_unavailable"`；`ui/submit` → `UNAVAILABLE` 且**未**调用内置回退；UI 出现横幅 | `core/tests/model/test_dangling.py` |
| A12 | 同时写 `purpose="chat"` 与 `purpose="evolve"` 的 `llm.call` → `admin` 的 `tokens_used` 只等于 evolve 部分；当天 chat 行超过 500 条也不截断 | `core/tests/admin/test_config_usage.py` |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿（写本文件时已实测；docs/rfc 不参与 C1–C10 计数）。

## 9. 兼容与迁移

| 场景 | 行为 |
|---|---|
| 旧会话（`sessions.model_id` NULL） | 解析从全局默认/priority 开始，不写库、不改写历史 |
| 旧客户端 | 不调 `model/*`，忽略新键；`turn.completed.usage` 多出的键不进它的反序列化 |
| 旧 core + 新客户端 | `model/list` 方法不存在（-32601）→ 只读徽标 + “由管理员在 core 配置”，**不出现空列表** |
| 旧 core + 新模块 | `capabilities.models` 被忽略并告警，模块照常注册，模型不出现在列表 |
| 新 core + 旧模块 | 合成 `<module_id>/*` 条目，能力全 `null`；选中后不带 `model`，模块用自己的默认 |
| 配置降级 | 新 `core.toml` 被旧 core 读到 → `UNKNOWN_CONFIG_KEY` 拒绝启动。发布说明必须写明“回滚要同时回滚配置”，这是 root `additionalProperties:false` 的固有代价 |
| DB 回滚 | 两个新列（`sessions.model_id`、`turns.model_id`）均可空；旧版本忽略它们，读写不受影响 |
| 历史用量 | **不回填**：旧 turn 的 `usage` 是 `{}`，审计里也没有对应 `llm.call` 行（写入点本来就不存在）。UI 显示“历史轮次未计量”，`partial_turns` 如实计数（Q4） |

## 10. 安全与隐私

- `model/list` 暴露的是模型名与能力，**不暴露** `base_url`、`api_key_env`、密钥、模块连接信息；`base_url` 可能内嵌凭据，绝不进 payload。
- `session/model.set` 是写操作，显式只读会话拒绝；`session/usage.get` 要求会话可见性（`PERMISSION_DENIED`），日总量只给聚合值，不给其他会话明细。
- 审计 `llm.call` 只存计数与模型名；不存 prompt/completion、不存原始 args、不存上游响应；`args_digest` 沿用审计层脱敏，`extra` 由 `_normalise` 的未知键收纳，需在写入侧显式过滤敏感键。
- `model/list` 无模块反向 RPC，不存在“一个 UI 请求放大成 N 个模块请求”的路径；`revision` 只增不减，不构成枚举面。
- 预算状态是用户可见信息，不含费用、不含单价；本提案没有价格表（N2），因此不产生“按金额推断用量”的旁路。

## 11. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| A. core 反向 RPC `models/list` 问模块 | 列表在模块离线时最需要，反向 RPC 恰好在这时失败；每次刷新多一跳，还要自造超时与缓存。manifest 声明 + `capabilities.update` 已有原子刷新 |
| B. 会话覆盖写进 `data/config.overrides.json` | 那是管理面单例配置，写的是全局值；把会话状态塞进去会让“会话级”变成“全局”，且与 C-18 的 override 语义冲突 |
| C. 客户端自己累加事件、自己存累计 | snapshot 不含 turns（§2.4），换端必丢；且 D102 明确“core 给口径”，客户端累加等于第二份真相 |
| D. 预算只告警 | 与 FR-8.49 验收句直接冲突，且会让“预算”形同虚设；改为默认 `enforce="block"`，只在 usage 不可信时降级 |
| E. 价格表 / 成本估算 | 非目标 N2：需要币种、单价、缓存折扣与供应商差异，产出的是不可执行的信息，不是可执行决策 |
| F. 发一张 1×1 图探测视觉能力 | 有成本、有延迟、有副作用（部分 provider 会计费），且探测失败无法区分“不支持”与“暂时故障” |

## 12. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **`capabilities.models[]` 的能力字段是否必填？** | 倾向 v1 全部可选（`null` = 未知），第一方模块（M6）在文档里要求填全；等 provider 矩阵稳定后再收紧为必填。若现在就必填，旧模块全部 `MANIFEST_INVALID`，与“旧模块零迁移”冲突 |
| Q2 | **多 provider 同名模型怎么展示与持久化？** | `id` 已用 `<provider>/<model>` 消歧；`display_name` 冲突时切换器按 provider 分组，不合并。若模块 `module_id` 改名，旧 `sessions.model_id` 变成悬空且**没有别名表**——v1 接受悬空 + 提示，v2 再考虑 alias |
| Q3 | **预算单位与日界？** | v1 用 token + UTC 日（与审计 `day_key` 对齐）。成本口径缺价格表（N2）；本地时区日界会让“今天”在同一 core 的审计与会话视图里不一致，暂不做。缓存命中是否按折扣权重计 token，也留给 v2 |
| Q4 | **历史用量是否回填？** | 倾向不回填：审计里没有历史 `llm.call` 行，任何回填都是编数。UI 显示“历史轮次未计量”，`partial_turns` 计数；若产品要求“累计看起来完整”，只能从 `turns.usage` 反推 chat 部分，aux 永远缺 |
| Q5 | **新增 `BUDGET_EXCEEDED` 错误码是否接受** | 本提案默认 `enforce="block"`（§6.4），用新码表达“预算超限拒 turn”；若不允许新增 C-04 码，则退化为 `PERMISSION_DENIED` + `data.reason="budget_exceeded"` |
| Q6 | **`max_image_px` 是长边还是总像素？** | 按 RFC-0004 §9 的“与 `max_edge` 取 min”解释为**长边**。字段名容易被读成“总像素”，建议 v2 改名 `max_image_edge`，但改名要同步 RFC-0004，v1 先按长边写死并在注记里说明 |
| Q7 | **provider 每 chunk 都报 usage 时怎么算？** | 现有 `engine.py:131-134` 是逐 delta 累加，重复上报会翻倍。本提案不改累加语义（N1），要求 provider 适配层每步只 emit 一次；若实测有 provider 逐 chunk 上报，需在 llm 层去重（记为本提案落地后的跟进项，不是契约变更） |

## 13. 影响面与后续

实现阶段要动的文件：`schema/rpc.json`（4 个方法 + snapshot 注记）、`schema/manifest.schema.json`（`capabilities.models`）、`schema/events.json`（3 处追加 + 1 个事件）、`schema/config.schema.json`（§7.5 九键）、`core/vade/llm/base.py` 与 `openai_compat.py` / `module.py`（`chat(model=...)`、按 id 钉住 provider）、`core/vade/session/models.py` + `sqlite_store.py`（两列 + 四终止态 usage）、`core/vade/session/manager.py`（snapshot 追加 `model`、turn 出队解析）、`core/vade/loop/engine.py`（`llm.call` 审计写入、usage 追加键）、`core/vade/admin/app.py`（今日用量改聚合查询 + `purpose` 过滤，§6.5 第 4 条）、`tools/check_contracts.py`（断言 4 个方法、`usage.budget` 已声明、`[usage]` 默认值、`capabilities.models` 形状）。

**不动**：`core/vade/router/*`、`core/vade/policy/*`、`core/vade/audit/{writer,reader}.py`（只加调用点，不改列）、`schema/errors.json`、C-09 组件、`[evolve]` / `[compaction]` 既有键。

**依赖顺序**：RFC-0004 先落地（`parts`、`attach.*`、blob 上传）→ 本提案的 `model/list` 才有“不白传”的落点；本提案先落地而 RFC-0004 未落地时，`model/list` 的能力字段仍可服务 FR-8.47 的徽标，但 §5 的图片门控不生效。U1 任务书必须写明这条降级，否则 UI 会等一个不存在的上传前判据。

**后续（不在本 RFC）**：FR-8.48 的会话级高级参数（复用 `session/*` 名字空间，`session/config.get|set`）；成本/价格表（FR-10.3 的另一半）；模型别名与迁移；`[usage].enforce` 的硬断模式；模块内部 LLM 花费的上报协议（需要模块侧主动报账，属 M6 契约）。

**参考**：`docs/requirements.md` §8.21（FR-8.46～8.50）、§8.22.2 R5、§10（D96 / D101 / D102）；`research/ui-decisions.md` #15 / #16；`research/ui-02-messages-input-attachments.md` §3.9 C7；`schema/rpc.json`、`schema/manifest.schema.json`、`schema/config.schema.json`、`schema/events.json`；`core/vade/llm/{base,module,openai_compat}.py`、`core/vade/loop/engine.py`、`core/vade/session/{models,queue,manager}.py`、`core/vade/admin/app.py`；RFC-0004（§9 四字段与降级路径）。
