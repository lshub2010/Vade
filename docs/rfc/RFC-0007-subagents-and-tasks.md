---
rfc: RFC-0007
title: 子代理与后台任务：统一 `task.*` 事件族、查询/取消/日志尾部与审批归属
status: proposed
date: 2026-11-14
proposer: 编排（依据 `docs/requirements.md` §8.21 FR-8.56～8.62、`research/ui-decisions.md` #1、`research/agent-01-injection-comparison.md` R4；`docs/agents/50-U1-ui-skeleton.md` §四把 U1-d 挂在 RFC-0007 上）
affects: C-06 `schema/events.json`（追加 `task.*` 事件族与顶层 `task_states`，`turn.approval_required` / `turn.approval_resolved` 追加可选字段）、C-05 `schema/rpc.json`（`ui_to_core` 追加 4 方法、`events_core_to_ui.names` 追加）、C-18 `schema/config.schema.json`（新增 `[tasks]`）；`core/vade/session/replay.py` 的覆盖率判定（core 内部）
related: RFC-0003（格式与章节风格来源；其 N3 明确「不做子代理（R4 另立提案）」）；RFC-0004（同批 UI 提案）；RFC-0008（ask-user；`task_states` 与其 `waiting_input` 互不依赖）
type: 契约追加（C-06 / C-05 / C-18）+ 审批事件 payload 的可选字段追加；无破坏性改名、无枚举改写
stage: 草稿，未实现；不新增 C-04 错误码，不改 `turn_states`，不碰 `schema/`
---

# RFC-0007 · 子代理与后台任务

## 1. 摘要

子代理与后台作业是**同一条状态机**：一个带 `parent_id` 的任务。本提案只新增一族 `task.*` 事件（四个）、一个顶层 `task_states` 枚举、三个查询/控制方法（外加一个默认关闭的 `subagent/message`），并把任务快照塞进 `ui/attach` 的 snapshot。子代理不再单独拥有一套事件名。

| # | 决定 | 理由 |
|---|---|---|
| 1 | **统一 `task.*`**，子代理是 `kind:"subagent"` 的任务 | FR-8.56 原文允许「或统一 `task.*` 命名」；两族的生命周期、取消、日志、历史、通知去重逐项相同（§4） |
| 2 | 事件只有四个：`task.started` / `task.status` / `task.progress` / `task.finished` | 与 C-06 现有 14 个事件同一粒度；状态变化与高频进度必须分开（§5.3） |
| 3 | 进度**只在有 UI attach 时发**，且 ≤2 次/秒合并，`x-vade.coalesce` | 关闭 UI 后任务继续跑但没人看进度；否则 10 分钟任务把 1000 条 replay 窗口冲掉（§5.4） |
| 4 | **snapshot 是重连真相**，事件是增量 | `ui/attach` 走 snapshot 时不会补发历史事件；`snapshot.tasks` 缺失就等于「重连后任务消失」（§5.6） |
| 5 | 审批**复用** `turn.approval_required`，只加归属字段 | 两条审批通道会破坏 FR-8.53 的「弹层关闭只认 `approval_resolved`」（§7） |
| 6 | 通知按 `task_id` 去重，只对「曾见过运行中」的任务发一次 | 「重连不重复通知」与「冷启动不要补发历史」是同一个判据的两面（§8） |

## 2. 动机与证据

### 2.1 客户端无法知道子代理在跑什么

`schema/events.json` 现有 18 个事件名（C-06 15 个 + C-14 追加 3 个），**没有一个是 `subagent.*` 或 `task.*`**。子代理今天只能以两种残缺形态出现：`turn.tool_call` / `turn.tool_result` 只知道 `tool`、`args_digest` 与 `summary<=300`，一次「派生 3 个子代理」的调用要么阻塞到全部结束（移动端断连就无法判断「还在跑还是已死」），要么立即返回一个 id——而后一种情况下**没有任何后续事件**告诉 UI 进展；`turn.completed` 只描述父 turn，子代理自己的工具调用、失败码、耗时不在任何事件里，UI 只能画一个「正在调用工具」的转圈。R4 的结论是「立即返回 id + 完成通知 + 可继续会话」：id 与通知都是**事件面**的事，不补事件族，子代理在 UI 上永远是黑盒。

### 2.2 关闭 UI 就失联

`core/vade/session/manager.py:272-291` 的 `snapshot()` 只返回 `from_seq / session / messages / queue / pending_approvals`，没有任务；而 `attach()`（同文件 198-241）在 replay 不覆盖时**只给 snapshot**、不补事件。于是用户启动 60s 任务、退出客户端、任务继续跑（FR-8.59 的硬要求），重开客户端时 snapshot 里没有任务，活动页空白；若任务已结束，则**永远不出现**。这不是 UI 没做，是**真相源里没有这个字段**——任务状态必须进 snapshot，与 `queue`、`pending_approvals` 同级。

### 2.3 多子代理下审批归属不清

`core/vade/policy/approval.py:153-159` 实际发出的 payload 是 `{turn_id, request_id, tool, args, risk, expires_at, session_id}`，而 `schema/events.json` 的 `turn.approval_required` 只登记了前 6 个键——**`session_id` 已经在线上发了却没登记**，`tools/check_contracts.py` 只查 `required` 字段是否在 payload 里声明，抓不到多余字段。本提案顺手登记它（§7），并补 `task_id` / `task_name` / `depth`：三个子代理同时请求高风险工具时，用户看到三张一模一样的卡片，只能靠 `turn_id` 猜是谁。

### 2.4 两族分裂的代价

把 `subagent.*` 与 `task.*` 并列，要复制六套东西：状态机、reducer、活动页列表、badge 计数、取消路径、历史查询。而两族的**真实差异只有一个字段**：子代理的 `child_session_id` 非空（可以点进去看它自己的消息），作业为空。把「有没有子会话」升格成两个事件族，等于让每个未来能力（进度、日志、重试、审批）都选边站一次。

反方论据成立：子代理要审批、可对话，作业不要。但这两件事在统一模型里分别是 `state:"waiting_approval"` 与一个可选的 `subagent/message`，不需要第二个族。三条结论收束本节：任务必须是**一等记录**（进 snapshot、进查询、进审计）；状态与进度必须**分级**（状态不可丢，进度可丢）；事件族的边界按「生命周期是否相同」划，不按「谁创建的」划。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 子代理/作业从创建到终态的全过程 ≤1s 可见，含名称、状态、当前动作、耗时、≤500 字摘要（FR-8.56） |
| G2 | 关闭客户端不中断任务；重连后（无论 replay 覆盖与否）任务列表与进度恢复（FR-8.59） |
| G3 | 终态每个任务只通知一次，重连、replay、冷启动都不重复（FR-8.61） |
| G4 | `task/cancel` 幂等，非终态任务 3s 内变 `cancelled`，且能区分「协作退出」与「超时强制」（FR-8.59） |
| G5 | 日志尾部单次 ≤200 行 / ≤64 KiB，UI 内存不随日志行数线性增长（FR-8.60） |
| G6 | 审批弹层能显示来源（子代理名与层级），且「本会话同类自动放行」不向子代理继承（§7） |
| N1 | **不做跨会话共享任务**：任务归属唯一会话；`task/list` 不带 `session_id` 只是「跨会话查询」，不是共享执行 |
| N2 | **不做任务编排 DSL**：无依赖图、无 cron、无声明式重试策略；`retryable` 只是给 UI 的提示位 |
| N3 | 不做子代理前缀复用，也不定义「结果如何回灌父模型上下文」（loop 侧另案，见 §13 后续） |
| N4 | 不改 `turn_states`，不引入 `waiting_input`（RFC-0008 的事）；`task_states` 独立枚举 |
| N5 | 不做跨端「只提醒一次」的 ack 协议（需要设备 presence，见 Q5） |
| N6 | 不新增 C-04 错误码：复用 `NOT_FOUND` / `INVALID_ARGUMENT` / `OVERLOADED` / `READ_ONLY_SESSION` / `CANCELLED` / `PERMISSION_DENIED` / `TIMEOUT` |

## 4. 命名裁定：统一 `task.*`

### 4.1 对比

| 维度 | 两族 `subagent.*` + `task.*` | 统一 `task.*`（**采纳**） |
|---|---|---|
| UI 列表 | 活动页两套 reducer、两套 badge、两个空态 | 一套 reducer，`kind` 分组 |
| 状态机 | 两份几乎相同（queued/running/terminal） | 一份 |
| 取消 / 日志 / 历史 / 通知去重 | 要么复制，要么只给 `task` 做，子代理永远没有日志与历史 | 天然共享 |
| FR-8.58 跳转 | `subagent` 独有 `session_id`/`turn_id` 映射 | 同一字段，`kind=job` 时为 `null` |
| 订阅粒度 | 可按族订阅 | 按 `kind` 过滤（客户端一行代码） |
| 需求措辞 | FR-8.56 明写「或统一 `task.*` 命名」 | 允许 |
| 演进 | 子代理要进度就得加 `subagent.progress`，两族再次合并 | 加字段即可 |

### 4.2 需求名 → 线上名的映射（唯一权威）

评审若按 FR-8.56 / FR-8.59 的字面点名，对照此表即可；**线上只有右列四个名字**。

| 需求里的名字 | 线上事件 | 判别 |
|---|---|---|
| `subagent.spawned` / `subagent.status` / `subagent.result` | `task.started` / `task.status` / `task.finished` | `kind:"subagent"`，`finished.summary` 即结果摘要 |
| `task.started` / `task.progress` / `task.finished` | 同名 | `kind:"job"`（进度对两种 `kind` 都发） |

**不发送 `subagent.*`**。若评审坚持两族并列，唯一可接受的退路是「别名事件」：同一状态变化同时发 `task.*` 与 `subagent.*`，客户端按 `task_id` 去重——代价是每次转换双倍事件、双倍 seq 消耗，以及「两个族的状态不一致时信谁」的新问题。本提案不采纳（§11 A）。

### 4.3 `kind` 的两个值

- `subagent`：由 agent 循环派生，通常有 `child_session_id` + `child_turn_id`，可审批、可跳转（FR-8.58）。
- `job`：由模块或 core 发起的长任务（索引、下载、批处理），无子会话，有进度与日志。

`kind` 在任务生命周期内**不可变**；它不是权限位，不参与鉴权。

## 5. 模型与事件族

### 5.1 Task 记录

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | core 分配，与 `turn_id` 同构（uuid hex），全局唯一 |
| `kind` | `subagent\|job` | §4.3 |
| `parent_id` | string \| null | 父**任务** id；根任务为 null（不是父 turn id） |
| `depth` | int ≥0 | 根为 0；`depth = parent.depth + 1`，由 core 算，客户端不自己推 |
| `session_id` | string | 事件所在会话（即 seq 空间） |
| `turn_id` | string \| null | 创建它的 turn；`job` 可为 null |
| `child_session_id` / `child_turn_id` | string \| null | FR-8.58 的跳转映射；`job` 为 null |
| `name` | string ≤80 | 人类可读名（子代理名 / 作业名） |
| `state` | `task.state` | §5.2 |
| `action` | string ≤120 | 当前动作，如「读取 docs/design.md」 |
| `pct` | int 0..100 \| null | null = 不确定进度 |
| `started_at` / `ended_at` | int (unix ms) | `ended_at` 仅终态 |
| `elapsed_ms` / `duration_ms` | int | 运行中报 `elapsed_ms`，终态报 `duration_ms` |
| `summary` | string ≤500 | 仅终态；子代理取最终助手消息截断，作业取结果摘要 |
| `code` / `message` | string \| null | 仅 `failed`；C-04 名 + ≤300 人话 |
| `retryable` | bool | 仅作 UI 提示（FR-8.62 的重试入口），core 不据此自动重试 |
| `forced` | bool | 仅 `cancelled`：宽限期到点被强制置终态 |

`elapsed_ms` 由 core 给，客户端**不得**只用本地时钟减 `started_at` 显示（设备时钟漂移会让「已用时」跳变）；本地只能做两次事件之间的平滑递增，且下一个事件到达时必须对齐 core 值。

### 5.2 状态机

```
queued ──▶ running ──▶ completed
  │           │  ▲
  │           │  └──▶ waiting_approval ──(resolved)──▶ running
  │           ├──▶ failed
  │           └──▶ cancelling ──▶ cancelled
  └──────────▶ cancelling（尚未启动就被取消）
```

`task_states = [queued, running, waiting_approval, cancelling, completed, failed, cancelled]`，终态 = `completed|failed|cancelled`。

三条硬规则：**终态不可离开**；`cancelling` 必须在 `tasks.cancel_grace_ms`（默认 2000，硬上限 3000）内落到 `cancelled`，否则这个状态就是 UI 上永远的「正在取消…」；`waiting_approval` 只阻塞该任务，不阻塞父 turn 的其它工具调用。

### 5.3 四个事件

| 事件 | `required`（必带键） | 可选键 | 注记 |
|---|---|---|---|
| `task.started` | `task_id, kind, state, session_id, turn_id, parent_id, depth, name, started_at, retryable` | `child_session_id, child_turn_id` | 每任务恰好一次，`state ∈ {queued, running}`；在 spawn 工具的 `turn.tool_result` **之前**发出，UI 才能先出一行再让工具卡收口 |
| `task.status` | `task_id, kind, parent_id, depth, state, elapsed_ms` | `action, pct` | 每次状态迁移必发、不节流；仅 `action` 文案变化按 ≤1 次/秒合并（保留最新）；终态后不得再发 |
| `task.progress` | `task_id, pct, elapsed_ms` | `detail` | 只带绝对值不带增量；**不得携带 `state`**（C-06 rule 4 的 payload-only 类）；带 `x-vade.coalesce:true` |
| `task.finished` | `task_id, kind, parent_id, depth, state, duration_ms, summary` | `code, message, forced, by, usage` | 每任务恰好一次；`state` 只取三终态；`failed` 必须带 `code`；宽限期到点的取消必须 `forced:true`；`by` 取 `user\|parent\|system` |

可空键仍写进 `required`（键存在、值可为 null），这样每个事件都自描述，消费者不必回溯找 `task.started` 才能建树——与 C-14 把 `user_text` / `assistant_text` 分开同源。`task.progress` 例外：它只更新已知记录的 `pct`，不承担建树职责，因此不要求 `kind`/`parent_id`。代价是每事件多 ~40 字节。

### 5.4 限流、合并与背压

| 约束 | 值 | 依据 |
|---|---|---|
| 每任务进度 | ≤2 次/秒 | FR-8.59「≤2 次/秒合并」 |
| 每会话任务事件 | ≤20 次/秒（`tasks.event_burst`），超出**只丢 `task.progress`** | 见下 |
| 状态事件 | 不节流、不合并、不丢 | FR-8.56「≤1s 内可见」 |
| 无 UI attach 时 | **不发 `task.progress`**（记录里的 `pct` 照常更新） | 任务继续跑，只是没人看 |

背压不是理论问题：`SinkWorker` 的队列是 256（`core/vade/session/manager.py:107`），满了直接 `drop()` 并把这个 sink 摘掉（同文件 48-58）——**丢一个进度事件不会掉线，掉线的是整个连接**。8 个并发子代理 × 2 次/秒 = 16 事件/秒，再叠加 `turn.delta`，几分钟就能打满；所以必须有会话级总闸，且只允许丢可丢的那一类。

「无 attach 不发进度」同时解决了另一件事：`ReplayBuffer` 只保留最近 **1000** 条事件或 24h（`core/vade/session/replay.py:19-20`）。一个 10 分钟任务按 2 次/秒就是 1200 条进度事件，足以把整个会话的 replay 窗口冲掉，让每个重连都退化成 snapshot。§9.4 给了一个不改契约的修法。

### 5.5 终态幂等与重放

| # | 规则 |
|---|---|
| R1 | core 先写终态记录再发 `task.finished`（C-06 rule 2 的「persist before broadcast」）；崩溃恢复后以记录为准，不补发 |
| R2 | 任务已终态后，worker 迟到的 `task.status` / `task.progress` 由 core **丢弃**，不入库不发事件 |
| R3 | 客户端 reducer 以 `task_id` 为键，**终态优先**：已知终态的任务再收到 `started`/`status` 一律忽略（不复活） |
| R4 | 去重两层：先按 `seq`（C-06 既有规则，覆盖 replay 重叠），再按 `task_id` 的终态幂等（覆盖「同一事件从 snapshot 与 replay 两条路到达」） |
| R5 | `task.progress` 允许在 replay 里缺号：它是绝对值不是增量，缺一条只影响一次渲染。客户端**不得**把「只缺 `task.progress` 的 seq 区间」当成漏洞去要 snapshot |
| R6 | `snapshot.tasks` 先播种，replay 事件叠加；播种为终态的任务收到同 id 的 `task.started` 时只更新非状态字段 |

R5 需要 C-06 里有一条明文规则（「`x-vade.coalesce` 的事件可以被 replay 丢弃」），否则下一批实现者会把它当 bug 修回去。§9.2 给了追加文本。

### 5.6 snapshot 与降级

`ui/attach` 的 `snapshot` 增加两个键（`snapshot` 在 C-05 里是 `object|null`，不改结构）：`tasks` = 全部非终态 + 最近 `tasks.snapshot_finished`（默认 20）条终态，运行中按 `started_at` 升序在前、已结束按 `ended_at` 降序在后；`tasks_truncated=true` 表示还有更早历史，走 `task/list` 分页。`pending_approvals[]` 同步增加 `task_id`（可为 null）。

**降级规则**：UI 在 attach 后调一次 `task/list`；返回 `NOT_FOUND` 说明 core 未实现本 RFC，活动页只显示静态空态，**不得**伪造进度（与 `docs/agents/50-U1-ui-skeleton.md:457` 的既定降级一致）。反方向（新 core + 旧 UI）无需处理：旧客户端忽略未知事件与 snapshot 新键。

### 5.7 级联规则

| 触发 | `kind=subagent` | `kind=job` |
|---|---|---|
| UI detach / 客户端进程退出 | 继续 | 继续（FR-8.59） |
| 父 turn `ui/interrupt` | 递归取消（子先、父后） | **不取消**（作业按定义脱离 turn） |
| 父任务进入终态 | 递归取消，`task.finished{state:"cancelled", by:"parent"}` | 不适用 |
| 会话关闭 / 删除 | 取消 | 取消（seq 空间没了，事件无处可发） |
| 超过 `max_depth` | spawn 拒绝 `INVALID_ARGUMENT`，`data.field="depth"` | — |
| 超过 `max_concurrent` | 拒绝 `OVERLOADED` + `data.retry_after` | 同 |

「中断父 turn 取消子代理、但不取消作业」是刻意的：用户按停止键的意思是「别再替我思考了」，不是「取消我十分钟前让你下载的那个模型」。

## 6. RPC

### 6.1 `task/list`

```json
{"method":"task/list",
 "params":{"session_id":"string|null (缺省=跨会话)","kinds":["subagent|job"],
           "states":["task.state"],"limit":"int 1..200 = 50","cursor":"string|null"},
 "result":{"tasks":[TaskView],"next_cursor":"string|null",
           "running_count":"int","limits":{"max_concurrent":"int","max_depth":"int"}},
 "errors":["INVALID_ARGUMENT"]}
```

- 排序固定：非终态按 `started_at` 升序在前，终态按 `ended_at` 降序在后；带 `states` 过滤时只在该子集内排序。FR-8.62 的「最近 50 条」= `states:[completed,failed,cancelled], limit:50`，不再需要 `order` 参数。
- `cursor` 不透明（`base64(ended_at|task_id)`），客户端不得解析；越界或非法游标 → `INVALID_ARGUMENT`。
- `running_count` 与 `limits` 供 FR-8.57 直接渲染「运行中 N / 上限 M」，不让客户端自己数（它会漏掉本端未 attach 的会话）。
- `TaskView` = §5.1 的字段 + `log_available:bool`（作业有环形日志、纯子代理可能没有）。

### 6.2 `task/log_tail`

```json
{"method":"task/log_tail",
 "params":{"task_id":"string","lines":"int 1..200 = 200","max_bytes":"int 1..65536 = 65536",
           "cursor":"string|null","follow":"bool = false","wait_ms":"int 0..30000 = 10000"},
 "result":{"lines":[{"ts":"int","stream":"stdout|stderr|core","text":"string"}],
           "cursor":"string|null","eof":"bool","truncated":"head|line|null","lost":"bool"},
 "errors":["NOT_FOUND","INVALID_ARGUMENT"]}
```

- `follow=false`：立即返回尾部快照。`follow=true`：core 可挂起至多 `wait_ms`，直到有新行 / 任务终态 / 超时，然后返回；客户端拿到响应立刻再发一次。**不是**服务端推送，也不是 SSE。
- `eof=true` 表示任务已终态且日志已取尽；此后不必再轮询。
- 双重上限：`lines ≤ 200` **且** 响应 payload `≤ 64 KiB`，先到先截；`truncated:"head"` 表示更早的行被丢弃，`truncated:"line"` 表示单行超 `tasks.log_line_chars`（默认 4096）被截断。
- `lost=true` 表示请求的 cursor 早于环形缓冲起点（core 重启或日志被挤出），返回最旧可用行并置位；UI 显示「早期日志已丢失」，**不得**假装完整。
- 日志只存在内存环形缓冲（默认 2000 行）；`log_available=false` 的任务不提供该方法（返回 `NOT_FOUND` 而不是空列表）。

### 6.3 `task/cancel`

```json
{"method":"task/cancel",
 "params":{"task_id":"string","reason":"string<=200"},
 "result":{"ok":true,"state":"task.state","forced":"bool"},
 "errors":["NOT_FOUND","READ_ONLY_SESSION","INVALID_ARGUMENT"]}
```

- **幂等**：任务已是终态 → `ok:true` + 当前终态，**不**产生第二个 `task.finished`，也不改 `state`。
- 非终态 → 立即转 `cancelling` 并返回；core 先协作取消（子会话 `ui/interrupt`、模块 `tools/cancel`），`tasks.cancel_grace_ms` 到点仍未退出则强制置 `cancelled`、`forced:true`。**从 RPC 返回到 `task.finished` 的实测上限是 3s**（默认 2s 宽限 + 调度余量）。
- 与 `turn.interrupted` 的既定口径一致：core 不等待模块确认（`core/vade/session/queue.py:7-10`），但多给一个 `forced` 位，让「真停了」与「我们不再等它」在 UI 上可区分。

### 6.4 `subagent/message`（可选，v1 默认关闭）

```json
{"method":"subagent/message",
 "params":{"task_id":"string","text":"string 1..2000"},
 "result":{"ok":true,"turn_id":"string|null","delivery":"queued|steering"},
 "errors":["NOT_FOUND","INVALID_ARGUMENT","UNSUPPORTED","READ_ONLY_SESSION","OVERLOADED"]}
```

仅 `kind=subagent` 且 `child_session_id` 非空时可用；`tasks.subagent_message=false`（默认）时一律 `UNSUPPORTED`。`delivery:"queued"` = 子代理当前 turn 结束后作为新消息处理；`"steering"` = 注入正在运行的 turn。v1 只要求实现 `queued`。

### 6.5 只读与入口边界

| 方法 | 显式只读会话 | 说明 |
|---|---|---|
| `task/list` | 允许 | 只读 |
| `task/log_tail` | 允许 | 只读；日志可能含路径与参数，**不得**进诊断导出包（FR-8.71 的脱敏面） |
| `task/cancel` | `READ_ONLY_SESSION` | 写 |
| `subagent/message` | `READ_ONLY_SESSION` | 写 |

`READ_ONLY_SESSION` 按 AMEND-22 #14 只用于**显式**只读会话，不再等同 cookie 会话（D76 已废止）。只读判定在 core，前端隐藏按钮只是配套。

**创建入口不在本 RFC**：`task/list` 等四个方法都是观测/控制面。谁派生任务（模型侧的 `task_spawn` 内置工具、模块的长任务 API）属 loop / 模块契约，签名另案。唯一约定是工具名沿用 RFC-0003 §4.2.4 的命名族（core 内置 = 单下划线 `task_spawn`，模块 = 双下划线），避免与模块工具撞名。

## 7. 审批归属

### 7.1 字段

`turn.approval_required` 与 `turn.approval_resolved` 各追加四个可选字段（`required` 不变，旧客户端忽略）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `task_id` | string \| null | 发起审批的任务；主 turn 直接发起的为 null |
| `task_name` | string ≤80 \| null | 冗余的人类可读名，避免 UI 在任务索引缺失时显示裸 id |
| `depth` | int | 0 = 主 turn；>0 = 子代理层级 |
| `session_id` | string | **补登记**：`approval.py:153-159` 早已发送，`events.json` 未声明 |

### 7.2 四条规则

1. **单通道不变**：不为子代理新开 `task.approval_required`。审批的广播、60s 倒计时、`approval/decide` 的 `request_id` 路由、`turn.approval_resolved` 才是唯一关闭信号——这些 FR-8.53 已定，多一条通道就多一个「两个弹层互相覆盖」的 bug。
2. **发在拥有者的会话流里**：子代理的审批以**父会话**的 seq 广播（`session_id` 是父会话），`turn_id` 是**子 turn** 的 id，`task_id` 指向子代理。这样活动页与全局浮层都能收到，而子会话不必被 attach。注意 `turn.approval_resolved.state` 是**子 turn 的状态**：父会话不认识这个 turn，必须按「未知 turn 忽略状态、只按 `request_id` 关卡片」处理，不得据此改父 turn 的状态机。
3. **弹层必须显示来源**：标题格式 `子代理 · {task_name}`，副标题给路径 `会话 › A › B`（由 `parent_id` 链在客户端拼，缺链时退化为 `task_name`）。同一 `request_id` 的多端弹层内容一致。
4. **自动放行不向下继承**：FR-7.5 的 `scope:"session"` 只覆盖**本会话主 turn** 的同工具同参数调用；子代理（`depth>0`）即使命中父会话的 grant 也必须重新询问。否则「允许一次文件写入」会被一个派生子代理放大成任意写入——这是本提案唯一的权限边界主张。

### 7.3 与 FR-8.53 的关系

FR-8.53 的验收项一字不改：跨 tab 3s 内出浮层、60s 超时灰化、浏览器会话 `APPROVER_INVALID`、另一端批准后 1 个 RTT 内消失。本提案只增加「这张卡是谁的」这一维。超时仍是 fail-closed：子代理拿 `PERMISSION_DENIED` 并 `task.finished{state:"failed", code:"PERMISSION_DENIED"}`，父 turn 不受影响（§5.7）。

## 8. 通知去重

### 8.1 客户端本地状态

| 键 | 内容 | 持久化 |
|---|---|---|
| `notified` | 已发过系统通知的 `task_id` 集合（环形，保留最近 500） | 是（500ms 防抖，与草稿同机制） |
| `known_running` | 上次会话结束时仍非终态的 `task_id` 集合 | 是 |
| `unseen` | 已终态但用户未在活动页看过 → badge 计数 | 是 |
| `perm_asked` | 是否已请求过系统通知权限 | 是 |

### 8.2 判定（唯一入口是 `task.finished`）

```
on task.finished(t):
  if t.task_id in notified:         return             # 重连/replay 不重发
  if t.task_id in known_running:    notify(t); unseen.add(t.task_id)
  elif t.task_id in known_terminal: return             # 已知终态，什么都不做
  else:                             seed_seen(t)       # 冷启动/首次见到 → 不发不计数
  notified.add(t.task_id); known_running.discard(t.task_id)
```

| 场景 | 系统通知 | badge | 依据 |
|---|---|---|---|
| 前台运行中完成 | 1 条 | +1 | FR-8.61 |
| 断线 10s 后重连，replay 补到 `task.finished` | 1 条 | +1 | 客户端上次记录为运行中 |
| 冷启动 snapshot 里已有 5 条终态 | 0 | 0 | 用户没在等这 5 条，补发是噪声 |
| 系统通知权限被拒 | 0 | +1 | 静默回落应用内；`perm_asked` 置位，**不再索权**（FR-8.61 / FR-8.70） |
| 同一任务两个设备在线 | 2 条（各端 1 条） | 各 +1 | 跨端只提醒一次需 ack 协议，v1 不做（Q5） |

### 8.3 点击与清空

通知携带深链 `vade://task/{task_id}`，落地到活动页详情；打开详情即把该 id 从 `unseen` 移除。活动页「清空」只清 `unseen` 与本地列表视图，**不删 core 审计、不发 RPC**（FR-8.62）。

## 9. 契约变更

### 9.1 汇总

| # | 契约 | 改动 | 判定 | 兼容策略 |
|---|---|---|---|---|
| 1 | C-06 `schema/events.json` | 追加 4 个事件 + 顶层 `task_states` + `x-vade.coalesce` 标记 + `x-vade.append` 块 | **追加**（C-06 只可追加） | 旧客户端忽略未知事件；须同 PR 有 `emit()` 点，否则 CI 红（`check_contracts.py` 的 `missing_handlers`） |
| 2 | C-06 既有两个审批事件 | `turn.approval_required` / `turn.approval_resolved` 追加 `task_id` / `task_name` / `depth` / `session_id` | **可选字段追加**（`required` 不变） | 旧客户端忽略；`session_id` 是补登记既有漂移 |
| 3 | C-05 `schema/rpc.json` | `ui_to_core` 追加 `task/list`、`task/log_tail`、`task/cancel`、`subagent/message`；`events_core_to_ui.names` 追加 4 个；`ui/attach` 注记 snapshot.tasks | **追加** | 未实现 core 返回 `NOT_FOUND`，UI 按 §5.6 降级 |
| 4 | C-18 `schema/config.schema.json` | 新增 `[tasks]`（13 键） | **追加**，root `additionalProperties:false` 需随批发版 | 默认值即 FR 默认值；`max_concurrent` 保守取 8 |
| 5 | `core/vade/session/replay.py` | 覆盖率判定跳过 `x-vade.coalesce` 事件（仍入 sqlite） | **core 内部**，无契约面 | 见 §9.4 |
| 6 | C-04 `schema/errors.json` | **不改** | 无变更 | 复用 7 个既有码（§3 N6） |

### 9.2 C-06：追加文本

```json
"x-vade": {"append": {"contract":"RFC-0007","owner":"C3","adds":
  ["task.started","task.status","task.progress","task.finished"]}}
```

三条实现约束，写进事件注记而不是让下一批人猜：

- **`task_states` 是顶层键，不是 `events` 里的条目**。若照 `turn.state` 的样子写一个 `task.state` 条目，`tools/check_contracts.py:224` 只豁免 `turn.state`，新条目会被判为「declared events nobody emits」直接红。同时在 `check_contracts.py` 里加 `STATE_CHANGE_EVENTS |= {task.started, task.status, task.finished}`、`PAYLOAD_ONLY_EVENTS |= {task.progress}`，把「状态事件必带 `state`、进度事件必不带」变成机器断言。
- **追加一条规则**：`events carrying x-vade.coalesce=true may be dropped from replay coverage; a client MUST NOT treat a sequence gap consisting only of such events as a hole.` 这需要 C3 点头（C-06 的 `rules[]` 是冻结文本，追加规则属修订而非追加）。若不点头，R5 退化为「必须由 snapshot 兜底」，代价见 §9.4。
- **`task.progress` 不得携带 `state`**，否则与 rule 4 的 payload-only 分类冲突。

### 9.3 C-05：方法与注记

四个方法按 §6 的签名登记；`events_core_to_ui.names`（`rpc.json:123-141`）追加四个名字——该表由 C-06 拥有、在 C-05 里只是镜像，但不同步就会被下一个读 C-05 的人当成「没有这些事件」。`ui/attach` 的 `result.snapshot` 注记加一句：`snapshot.tasks` 是任务列表的权威起点，缺失 `tasks` 键表示 core 未实现 RFC-0007。

### 9.4 C-18：新增 `[tasks]`

| 键 | 默认 | 生效 | 说明 |
|---|---|---|---|
| `max_concurrent` | 8 | hot | 每会话并发任务上限（子代理 + 作业合计） |
| `max_depth` | 4 | restart | 子代理嵌套上限；**必须 ≥ `ui_fold_depth + 1`**，否则 FR-8.57 的「4 层折叠」验收无法构造 |
| `ui_fold_depth` | 3 | hot | 客户端折叠阈值（服务端下发，保证三端一致） |
| `history_limit` | 50 | hot | FR-8.62 的「最近 N 条」 |
| `snapshot_finished` | 20 | hot | snapshot 里带多少条终态 |
| `log_lines` | 200 | hot | 单次拉取行数上限 |
| `log_bytes` | 65536 | hot | 单次拉取字节上限 |
| `log_ring_lines` | 2000 | restart | 内存环形缓冲；超出丢最旧并 `lost=true` |
| `log_line_chars` | 4096 | hot | 单行截断阈值 |
| `progress_hz` | 2 | hot | 每任务进度上限（FR-8.59 的 2 次/秒） |
| `event_burst` | 20 | hot | 每会话任务事件总闸，超出只丢 progress |
| `cancel_grace_ms` | 2000 | hot | 强制终态期限；超过 3000 的值启动即拒 |
| `subagent_message` | false | restart | v1 关闭 §6.4 |

**replay 覆盖率的修法**：`ReplayBuffer.covers()` 只是「能否证明连续」的判据，真正的重放读 sqlite（`attach()` 的 `read_since`），因此让 `add()` 跳过 coalesce 事件即可——进度行仍能 `read_since` 出来，覆盖证明只由结构事件承担，1000 条窗口不会被冲掉。这不是丢弃事件，只是不再让它们充当连续性证人。

### 9.5 兼容判定

- **事件追加**：C-06 没有「客户端必须忽略未知事件」的明文规则（RFC-0003 §5.5 已指出同一缺口）。本提案按「不知道就不处理」实现，不依赖该规则；建议 C3 在本次追加里一并补上。
- **snapshot 追加**：`snapshot` 在 C-05 是 `object|null`，加键不改结构；旧客户端读不到就忽略。
- **`check_contracts.py` 的红线**：只改 schema 不改代码会以「declared events nobody emits」失败，因此实现 PR 必须同时给出四个 `emit()` 点（与 RFC-0003 §5.5 同一约束）。
- **回滚**：`[tasks]` 整段删除 + 不注册四个 `emit()` 即可回到现状；已产生的任务记录留在库里不被读取，事件留在 `events` 表里被旧客户端忽略。不需要降级脚本。

## 10. 验收

A1–A16 全部要求真实执行；截图与手写记录不算证据。

| # | 断言 | 承载 |
|---|---|---|
| A1 | 派生 3 个子代理 → 3 条 `task.started`（`kind=subagent`、`parent_id=null`、`depth=0`），状态逐个转 `completed`，每项 `summary≤500` 且 `duration_ms>0` | `core/tests/task/test_lifecycle.py` |
| A2 | **关闭 UI 后任务不断**：detach 全部 sink → 任务继续推进 → 期间 `events` 表无 `task.finished`；重 attach 后 `snapshot.tasks` 含该任务且 `elapsed_ms` 大于 detach 时刻 | 同上 + `test_snapshot_tasks.py` |
| A3 | **取消 3s 内生效**：对一个忽略取消的任务调 `task/cancel` → 从 RPC 返回到 `task.finished` 的单调时钟差 ≤3000ms，`forced=true`，之后 5s 内无该 `task_id` 的任何事件 | `core/tests/task/test_cancel.py` |
| A4 | `task/cancel` 幂等：连调两次 + worker 迟到一条 `task.status` → 恰好一条 `task.finished`，`state` 仍 `cancelled` | `core/tests/task/test_terminal_idempotent.py` |
| A5 | 进度限流：任务内部每秒报 100 次 → 观察者按秒分桶计数每组 ≤2，整分钟 ≤120；`pct` 单调不减 | `core/tests/task/test_progress_rate.py` |
| A6 | 无 attach 时不发 `task.progress`（计数为 0），但任务记录里的 `pct` 仍在推进；重新 attach 后 `snapshot.tasks[].pct` 等于最新值 | `core/tests/task/test_progress_gate.py` |
| A7 | 1000 条进度事件后 `ui/attach(last_seq=0)` 仍走 replay（`snapshot=false`）而不是 snapshot：`ReplayBuffer.covers` 跳过 coalesce 事件但 `read_since` 仍返回全部行 | `core/tests/session/test_replay_coalesce.py` |
| A8 | **子代理失败不影响父**：3 个子代理其中 1 个抛错 → 父 turn `completed`；只有该任务 `failed` 且带 `code`，另两个 `completed`；父 turn 的工具结果数 = 3 | `core/tests/task/test_failure_isolated.py` |
| A9 | 审批归属：`depth=2` 的子代理请求 high-risk → 父会话收到 `turn.approval_required`，含 `task_id`/`task_name`/`depth=2`/`session_id`；`turn.approval_resolved` 带同一 `task_id`；主 turn 的直接审批 `task_id=null` | `core/tests/task/test_approval_attribution.py` |
| A10 | 自动放行不继承：父会话已有同工具同参数的 `scope:"session"` grant → 子代理仍产生新的 `turn.approval_required`（`request_id` 不同） | `core/tests/task/test_approval_scope.py` |
| A11 | 级联：`ui/interrupt` 父 turn → 所有 `kind=subagent` 后代在 3s 内 `cancelled`、`kind=job` 仍在运行；父任务终态时子代理递归取消；会话关闭时两者都取消 | `core/tests/task/test_cascade.py` |
| A12 | 层级与上限：A→B→C→D→E 五层，`depth` 依次 0..4；第 5 层 spawn 返回 `INVALID_ARGUMENT` 且 `data.field="depth"`；第 9 个并发任务返回 `OVERLOADED` 且带 `data.retry_after` | `core/tests/task/test_depth_limit.py` |
| A13 | **重连不重复通知**（客户端）：完成 → 通知 1 次 → detach/attach 且 replay 重放 `task.finished` → 通知计数仍为 1；冷启动 snapshot 含 5 条终态 → 通知 0、badge 0；权限被拒 → badge +1 且 `perm_asked=true` | `ui/tests/notifications/test_task_dedupe.*`（三端同 fixture） |
| A14 | 日志上限：5000 行日志 → 首屏 `lines≤200` 且响应 `≤65536` 字节、`truncated="head"`；`follow=true` 只返回新行并推进 `cursor`；`lost=true` 在 cursor 早于环形起点时置位 | `core/tests/task/test_log_tail.py` |
| A15 | 只读：显式只读会话 `task/cancel` / `subagent/message` → `READ_ONLY_SESSION`；`task/list` / `task/log_tail` 正常返回 | `core/tests/task/test_read_only.py` |
| A16 | 历史：60 条终态 → `task/list{states:[completed,failed,cancelled],limit:50}` 返回 50 条、按 `ended_at` 降序、`next_cursor` 非空；审计行数在「清空本地视图」前后不变 | `core/tests/task/test_history.py` |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿（写本文件时已实测；新文件不参与 C1–C10 计数）。

## 11. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| A. 两族并列 `subagent.*` + `task.*` | 六套机制复制；未来每个能力都要选边（§4.1）。别名双发方案还会让 seq 消耗翻倍、状态不一致时无仲裁 |
| B. `task/status` 轮询代替事件 | 移动端断连后无法判断「还在跑还是已死」正是 R4 要解决的问题；轮询在后台被系统杀掉时必然漏采，且做不到 ≤1s |
| C. 进度不进事件，只靠 `task/list` 拉 | FR-8.59 明写「广播 + 限流合并」；而且活动页要实时进度条，轮询会把 RPC 变成心跳 |
| D. 日志走事件推送（每条一行一个事件） | 无界日志进 seq 会重演 §5.4 的窗口冲毁，且 FR-8.60 明确「不得把全量日志灌进 UI」 |
| E. 子代理单开 `task.approval_required` | 两条审批通道 = 两个弹层、两套关闭信号，直接违反 FR-8.53「只认 `approval_resolved`」 |
| F. 把 `task.progress` 也写进 replay 覆盖率 | 10 分钟任务 1200 条进度会冲掉 1000 条窗口，每个重连退化成 snapshot——省一条规则，换来重连成本翻倍（§5.4） |

## 12. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **子代理是否独立 session？** | (a) 独立 child session（可跳转、可多轮、snapshot 独立）；(b) 父会话里的隐藏 turn（省存储，但 FR-8.58 的「进入详情」无处可去）。本提案字段两种都支持（`child_session_id` 可空），**倾向 (a)**，因为它同时解决「返回后父会话位置不丢」——详情是另一个 session，父会话的滚动位置天然不变 |
| Q2 | **进度合并窗口** | 服务端 2 次/秒是需求上限；客户端是否再合并到 1 次/秒或只在 `pct` 变化 ≥1 时渲染？倾向客户端按「值变或 ≥500ms」渲染，服务端不替它决定；`event_burst=20` 的默认值需压测确认 |
| Q3 | **日志是否落盘？** | 倾向 v1 只放内存环形（2000 行），重启即丢、用 `lost` 明示。落盘要定保留期、清理与审计三件事，且 FR-8.60 没要求跨重启。**代价**：崩溃后无法复盘作业输出 |
| Q4 | **并发上限默认值** | 8 是保守猜测（移动端设备 + 单 core）。若子代理主要吃 LLM 配额，真正的闸在 provider 速率而不在 core；需要一次压测后定，且要允许 `OVERLOADED` 的 `retry_after` 有意义 |
| Q5 | **跨端只提醒一次** | 需要 `task/ack{task_id, device_id}` + presence，或由 core 指定「主设备」。v1 不做（N5），但两设备同时在线会双通知，发布说明要写清 |
| Q6 | **`subagent/message` 的 v1 语义** | `queued` 够用还是必须 `steering`？倾向只做 `queued` 并把 `steering` 留为 `UNSUPPORTED`——注入运行中的 turn 需要 loop 侧的中断点设计，与 RFC-0003 的 step 边界同源 |
| Q7 | **任务是否要进审计** | 目前只说「清空视图不删审计」。是否每次 `task.finished` 写一条 `audit` 行（含 `task_id`、`kind`、`code`、`duration_ms`）？倾向写，否则「重试入口」与「失败原因」在事后无从对账 |
| Q8 | **`max_depth` 与 `ui_fold_depth` 的默认值** | 4 / 3 是为了让 FR-8.57 的 4 层折叠可构造。若产品要求更深（子代理再派生是常见模式），两个值要一起调，不能只调 `max_depth`（否则折叠永远是死代码） |

## 13. 影响面与后续

| 文件（实现阶段，非本 RFC） | 改动 |
|---|---|
| `core/vade/task/*`（新增） | 任务注册表、状态机、runner、环形日志、并发/深度闸 |
| `core/vade/session/models.py` | `Task` 冻结 dataclass + `TaskState` 枚举 + `TERMINAL_TASK_STATES` |
| `core/vade/session/manager.py` / `rpc.py` | `snapshot()` 加 `tasks` / `tasks_truncated`、任务事件入口、级联钩子；四个 RPC 方法与只读判定 |
| `core/vade/session/sqlite_store.py` | `tasks` 表（`session_id, task_id, parent_id, state, started_at, ended_at, code`）+ 索引 |
| `core/vade/session/replay.py` | `add()` 跳过 `x-vade.coalesce` 事件（sqlite 仍全量） |
| `core/vade/policy/approval.py` | `ApprovalRequest` 增 `task_id`/`task_name`/`depth`；payload 补四个字段 |
| `schema/events.json` / `rpc.json` / `config.schema.json` | §9 的三处追加 |
| `tools/check_contracts.py` | 两组事件集合断言 + `task_states` 与 `TaskState` 不漂移 + 四个事件有 emit 点 |

**不动**：`core/vade/router/*`（路由语义）、`core/vade/loop/*`（prompt 装配；子代理结果回灌父模型不在本提案）、`core/vade/skill/*`、`docs/design.md` §9（Skin 接口；活动页只用 C-09 既有组件）、`turn_states`（RFC-0008 的所有权）。

**跨提案依赖**：`docs/agents/50-U1-ui-skeleton.md:106` 把 U1-d 挂在 RFC-0007 上，未批准时按同文件 457 行只做静态骨架与空态；本提案批准后 U1-d 可直接接 `task/list` + `snapshot.tasks`，不需要等 RFC-0008。RFC-0003 的 N3 把子代理明确留给本提案，两者无字段冲突（RFC-0003 不引入 `parent_id`）。

**后续（不在本 RFC）**：子代理前缀复用（R4 的「fork 保留继承前缀」需要 core 能克隆父请求前缀，而当前每轮重拼 prompt，短期做不到）；结果回灌父模型的机制（`<task-result>` 注入 vs 新 turn，属 loop 设计）；`task/retry`（需要定义「幂等任务」的判据，FR-8.62 只要求入口）；跨会话共享任务（N1 之外的另一件事，涉及权限模型）。

**参考**：`docs/requirements.md` §8.21 的 FR-8.56～8.62 与 §8.21.2 R3/R4、AMEND-22 §8.22.2 的契约缺口表；`research/ui-decisions.md` #1（活动为四个一级目的地之一，承载运行中 turn、待审批、最近失败）；`research/agent-01-injection-comparison.md` R4（立即返回 id、完成通知、审批浮层标源线程、前缀复用列为后续）；`docs/agents/50-U1-ui-skeleton.md` §四 / §九（U1-d 依赖与未批降级）；RFC-0003（章节与元信息风格、`check_contracts` 的 emitted-event 约束）；RFC-0004（同批提案的可选字段与兼容写法）。
