---
rfc: RFC-0012
title: 子代理事件族 R3：`subagent.spawned` / `subagent.status` / `subagent.result` 三事件与 `subagent/list` / `subagent/get` 两方法
status: proposed
date: 2026-11-14
proposer: 编排（依据 `docs/requirements.md` §8.23.2 R3 子代理族、§8.21.2 AM21-E FR-8.56～8.58）
affects: C-06 `schema/events.json`（追加 3 个 `subagent.*` 事件与顶层 `subagent_states`，`x-vade.appends`；顶层 `version` 仍 2.0.0）、C-05 `schema/rpc.json`（`ui_to_core` 追加 2 方法，`version` 1.3.0 → **1.4.0**）；core 新增 `core/vade/subagents/` 包、`subagent_runs` 表、`SubagentRun` 记录
related: RFC-0007（`task.*` 四事件与 `subagent/message`；本提案撤销其 §4.2 对子代理那一半的「统一命名」裁定，`subagent/message` 原样保留）、RFC-0008（`turn_states` 所有权，本提案不改）、AMEND-22 §8.22.2（R3 布局决策）
type: 契约追加（C-06 / C-05）+ 新增子代理记录与运行器；无破坏性改名、无枚举改写、无错误码新增
stage: 已实现（core 侧）；`turn_states`（7 值）/ `task_states` / C-04 错误码（34 个）均不动
---

# RFC-0012 · 子代理事件族（R3）

## 1. 摘要

本提案给**子代理**（有自己的 turn 与 LLM 循环的派生代理）一套自己的事件族与查询方法：三个 `subagent.*` 事件、一张 `subagent_runs` 表、一个 `core/vade/subagents/` 运行器包。RFC-0007 曾把子代理统一进 `task.*`（`kind:"subagent"`），当时的判据是「两族的生命周期逐项相同」；R3 的落地证明这个判据对子代理那一半不成立——**子代理有 `child_session_id` 指向一个真的在跑 LLM 循环的会话，而 `kind:"job"` 只是外部进程**。生命周期的相似（spawn → 运行 → 终态）掩盖了本质的不同（谁在执行）。本提案按「执行体」重新划界：LLM 循环归 `subagent.*`，外部进程归 `task.*`（§8）。

| # | 决定 | 理由 |
|---|---|---|
| 1 | **拆族**：子代理发 `subagent.*`，不再借道 `task.*`；撤销 RFC-0007 §4.2「不发送 `subagent.*`」对子代理的裁定（作业的 `task.*` 不变） | R3 事实表明写 `subagent.spawned / subagent.status / subagent.result`（§2.1）；子代理与作业的唯一共同点是「都有状态机」，而执行体不同（LLM 循环 vs 外部进程），一个事件族的 payload 无法同时自述两者（§8） |
| 2 | 事件只有三个：`subagent.spawned`（创建）、`subagent.status`（状态/步骤变更）、`subagent.result`（终态摘要指纹） | 与 C-06 既有事件同粒度；把「状态变更」与「终态结果」分开，是因为终态要多带 `result_digest` / `result_chars` / `finished_at`，且每任务恰好一次（§5） |
| 3 | `subagent.result` 只带 **`result_digest`（sha-256 指纹）+ `result_chars`，不带结果全文**；全文只存在于子会话的消息流里，`subagent/get` 返回 ≤500 字的 `result_preview` 投影 | `subagent.*` 走父会话 seq 并进 replay/events 表——结果全文进事件等于把子代理的全部输出复制进父会话审计（§5.3）；FR-8.56 的「≤500 字摘要」由 `subagent/get` 的只读投影满足，不靠事件 |
| 4 | 事件发在**父会话**的 seq 流上（`session_id == parent_session_id`） | FR-8.58 的跳转目标是子会话，但**订阅方 attach 的是父会话**；子会话不必被 attach 才能看见进度（§5.1） |
| 5 | 状态机五值：`spawned → running → {completed|failed|cancelled}`，非法转移拒绝且不发事件 | 五值里 `spawned`（已登记、循环未开）与 `running`（循环在跑）必须分开——父 turn 结束时两者都可被取消，但「从未开跑」与「跑了一半」的可观测语义不同（§7.2） |
| 6 | 父 turn 到终态时（`_finalize` 的 pre-terminal 钩子），该 turn 名下未完成的子代理 run 一律 `cancelled` | 子代理的循环寄生在父 turn 的生命周期上：父 turn 已经完了，子代理继续跑只会产生无处归属的事件（§7.3） |
| 7 | RPC 只加**观测面**两个：`subagent/list`、`subagent/get`（均只读）；`subagent/message` 原样保留 | 创建/审批/取消不在本提案（入口属 loop/模块契约，§3 N3）；RFC-0007 §6.4 的消息通道是既有事实，不动 |

## 2. 动机与证据

### 2.1 需求原文（R3 族）

`docs/requirements.md` §8.23.2 的 R3 事实行（行 1186）：

> | R3 子代理 | `subagent.spawned` / `subagent.status` / `subagent.result`、`parent_id`、`session_id`/`turn_id` 映射 | FR-8.56、8.57、8.58 | C-06 无 `subagent.*` |

R3 的缺口栏明写「C-06 无 `subagent.*`」——即需求方把三个事件名当作**待补的缺口**，而不是已被 `task.*` 满足的等价物。FR-8.56 的正文（行 1126）确实给了「或统一 `task.*` 命名」的选项，RFC-0007 采用了它；但 FR-8.57 的 `parent_id` 层级树与 FR-8.58 的 `session_id`/`turn_id` 映射是**子代理独有**的语义，`kind:"job"` 永远为 null 的字段撑不起这两个验收。

### 2.2 C-06 里一个 `subagent.*` 都没有

`schema/events.json`（HEAD `e200772`）现有 **29 个 events 条目**（28 个真事件 + `turn.state` 枚举条目），grep 确认 **没有任何 `subagent.*`**：

```
session.attached, turn.queued, turn.started, turn.delta, turn.tool_call,
turn.tool_result, turn.approval_required, turn.approval_resolved,
turn.input_required, turn.input_resolved, turn.completed, turn.failed,
turn.interrupted, turn.cancelled, queue.updated, module.status,
task.started, task.status, task.progress, task.finished, turn.state,
session.turn, session.closed, skill.proposed, usage.budget, turn.superseded,
message.edited, session.updated, session.list_changed
```

28 个事件没有一个描述「一个有自己的 turn 与 LLM 循环的派生代理」。今天一条「派生 3 个子代理」的调用在事件面上只有两种残缺形态：要么父 turn 阻塞到全部结束（断连后无法判断子代理是死是活），要么走 RFC-0007 的 `task.started(kind="subagent")`——但那套事件的 `summary` 是写死的 500 字截断文本，`task.progress` 的 `pct` 对 LLM 循环无意义，`waiting_approval` 与审批的归属（§7 of RFC-0007）也没有回答「子代理自己的 turn 在哪个会话」。

### 2.3 三条验收句（原文引用）

- **FR-8.56 [M]**（行 1126）：「**子代理列表与状态**：core 广播子代理生命周期（`subagent.spawned` / `subagent.status` / `subagent.result`，或统一 `task.*` 命名）；UI 在会话内提供"子代理"入口，每项显示名称、状态（运行中/完成/失败/取消）、当前动作、耗时、可展开的输出摘要」；验收：「派生 3 个子代理 → 3 项出现且状态逐个从"运行中"转"完成"；展开任一项见 ≤500 字摘要与耗时；一个失败 → 该项标红并给错误码，其余项不受影响」。
- **FR-8.57 [S]**（行 1127）：「**子代理层级与并发**：事件带 `parent_id`；UI 以缩进树呈现父子关系并显示并发数（运行中 N / 上限 M）」；验收：「A 派 B、B 派 C → 3 层缩进正确；并发计数随派生/结束实时变化」。
- **FR-8.58 [S]**（行 1128）：「**子代理跳转**：子代理有独立 turn/日志时，点击进入其详情，返回后父会话位置不丢」；验收：「点击有独立 turn 的子代理 → 进入详情可见其消息与工具调用，返回后父会话滚动位置不变」。

注意 FR-8.57/8.58 标 **[S]**（应达），FR-8.56 标 **[M]**（必须）。三者的承载字段本提案一次给齐：`parent_id` 语义由 `parent_session_id` + `parent_turn_id` 承担（层级树按 run 的派生链缩进）；跳转映射由 `child_session_id` 承担。

### 2.4 为什么 R3 的两条事实推翻 RFC-0007 的统一裁定

RFC-0007 §4.2 说「不发送 `subagent.*`」，其判据表里 FR-8.58 一行写「同一字段，`kind=job` 时为 `null`」。这个判据在实现层站不住：`task.started` 的 `child_session_id` 只是一个跳转指针，**那个指针指向的会话由谁执行、执行到哪一步、产出什么，`task.*` 一个字段都没有**——因为作业没有这些东西。子代理有：它有自己的 turn（在 `child_session_id` 的会话里，走完整的 LLM 循环与工具审批），它的状态推进单位是「一步推理/一次工具调用」而不是「进程存活」。硬把两种执行体塞进一个事件族，结局是 payload 里一半字段对一半受众永远是死键。**按执行体划族**（LLM 循环 = `subagent.*`，外部进程 = `task.*`）之后，两族各自的自述字段都是活的（§8）。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 子代理从 spawn 到终态的全过程 ≤1s 在父会话事件流可见：`subagent.spawned`（登记）、`subagent.status`（状态/步骤）、`subagent.result`（终态指纹）（FR-8.56） |
| G2 | `parent_id` 语义齐备：每条事件带 `parent_session_id` / `parent_turn_id` / `child_session_id`，客户端可建层级树与跳转映射（FR-8.57 / FR-8.58） |
| G3 | 结果内容不进事件流：`subagent.result` 只带 sha-256 指纹与长度；≤500 字摘要是 `subagent/get` 的**只读投影**，不是持久化文本（§5.3） |
| G4 | 状态机非法转移被拒：终态不可离开、`spawned` 不能直接跳终态（除取消）、拒绝时**既不改记录也不发事件**（§7.2） |
| G5 | 父 turn 终态收口：`turn.completed` / `turn.failed` / `turn.interrupted` / `turn.cancelled` 写盘**之前**，该 turn 名下未完成的 run 一律 `cancelled`（§7.3） |
| G6 | 观测面方法齐备：`subagent/list`（分页/过滤）、`subagent/get`（单条详情 + preview）；两个方法在显式只读会话上可用 |
| N1 | **不做子代理的独立 UI 页**：入口沿用活动页（FR-8.56 的「会话内入口」）；UI 侧属 U1 批次，本提案只供事件与方法 |
| N2 | **不做子代理嵌套**：`max_spawn_depth = 1`——子代理不能再派生子代理；`parent_session_id` 只可能是主会话。层级树的深度固定为 1 层，FR-8.57 的「深度 4 层折叠」按 N2 降级为「1 层树」 |
| N3 | **不做子代理跨会话**：run 归属唯一父会话；`subagent/list` 不带 `session_id` 是「跨会话查询」，不是共享执行。子会话（`child_session_id`）是独立 seq 空间，但 run 的记录与事件都在父会话 |
| N4 | **不改创建/审批/取消入口**：谁 spawn 子代理（loop 侧内置工具签名）属 loop 契约；审批复用 `turn.approval_required`（RFC-0007 §7 的单通道裁定不变）；`task/cancel` 不取消 `subagent_runs`（两族不混用，§8） |
| N5 | **不新增 C-04 错误码**：复用 `INVALID_ARGUMENT` / `NOT_FOUND`（仍 34 个） |
| N6 | **不改** `turn_states`（7 值）/ `task_states` / `schema/config.schema.json`（无 `[subagents]` 节） |

## 4. 数据模型

### 4.1 `subagent_runs` 表（`core/vade/session/sqlite_store.py`）

```sql
CREATE TABLE IF NOT EXISTS subagent_runs(
    run_id             TEXT PRIMARY KEY,          -- run_<32hex>
    parent_session_id  TEXT NOT NULL,
    parent_turn_id     TEXT,                       -- 创建它的父 turn；可空
    child_session_id   TEXT,                       -- 子代理自己的 LLM 循环所在会话
    label              TEXT NOT NULL,              -- 1..64 字符
    state              TEXT NOT NULL,              -- subagent.state
    created_at         INTEGER NOT NULL,           -- unix ms
    finished_at        INTEGER,                    -- 仅终态
    result_digest      TEXT,                       -- sha-256 hex，仅终态
    result_chars       INTEGER,                    -- 结果全文长度，仅终态
    error_code         TEXT                        -- 仅 failed
);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_parent
    ON subagent_runs(parent_session_id, created_at);
```

表走既有 `_SCHEMA` 的 `CREATE TABLE IF NOT EXISTS` 路径，`__init__` 里与其他表同一次 `executescript` 执行——对已存在的库（无此表）与新库（首次建）都幂等，重开第二次连接是 no-op（PRAGMA 可重入，§11 A8）。

### 4.2 `SubagentRun` 记录（`core/vade/session/models.py`）

冻结 dataclass，与 `Task`/`Turn` 同风格：运行器 swap 记录、不原地改。

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | `string` | `run_` + 32 位小写 hex（§7.1），core 分配，全局唯一 |
| `parent_session_id` | `string` | 父会话 = 事件所在的 seq 空间 |
| `parent_turn_id` | `string \| null` | 创建它的父 turn；模块侧 spawn 可为 null |
| `child_session_id` | `string \| null` | 子代理自己的 turn 与 LLM 循环所在会话（FR-8.58 跳转目标）；循环尚未建立时为 null |
| `label` | `string 1..64` | 人类可读名（§7.1） |
| `state` | `subagent.state` | §7.2 |
| `created_at` / `finished_at` | `int (unix ms)` | `finished_at` 仅终态 |
| `result_digest` | `string \| null` | sha-256 hex（结果全文的指纹）；仅终态 |
| `result_chars` | `int \| null` | 结果全文的字符数；仅终态 |
| `error_code` | `string \| null` | 仅 `failed`；C-04 名 |

### 4.3 `subagent_states` 枚举（C-06 顶层键）

```
subagent_states = ["spawned", "running", "completed", "failed", "cancelled"]
```

照 `task_states` 的先例放 C-06 **顶层**，不做成 `events` 里的 `subagent.state` 条目（会被 `check_contracts.py` 判为「declared events nobody emits」，RFC-0007 §9.2 的同一个坑）。代码侧 `SubagentState` 枚举与 `SUBAGENT_TERMINAL_STATES = {completed, failed, cancelled}` 在 `core/vade/session/models.py`，两边由 `check_contracts.py` 断言零漂移。

## 5. 事件族（三个）

### 5.1 流归属：发在父会话的 seq 上

三条事件的 `session_id` **都等于 `parent_session_id`**：`subagent.*` 走父会话的 seq 空间（与 `task.*` 的 `session_id` 同一语义——C-06 的流归属键）。子会话不必被 attach：订阅父会话的 UI 天然收到全部子代理事件。`subagent.spawned` 的 payload 同时声明 `session_id` 与 `parent_session_id`，两者恒等——前者是 C-06 的流键，后者是 FR-8.57/8.58 的层级语义字段；冗余是刻意的（每事件自描述，消费者不用回溯 `subagent.spawned` 才知道流归属）。

### 5.2 payload 与 required

| 事件 | `required`（必带键） | 可选键 | 注记 |
|---|---|---|---|
| `subagent.spawned` | `run_id, session_id, parent_session_id, parent_turn_id, child_session_id, label, at` | — | 每 run 恰好一次；**不带 `state`**（创建即 `spawned`，事件名即状态，C-06 rule 4 的 payload-only 类）；在父 turn 的任何后续事件之前发出 |
| `subagent.status` | `run_id, session_id, state, at` | `step, tool_name` | 每次状态迁移必发（`spawned→running`）；`running` 期间的步骤/工具更新同事件携带（`state` 仍为 `running`）；`step` ≤120 字符；终态后不得再发 |
| `subagent.result` | `run_id, session_id, state, result_digest, result_chars, finished_at` | `error_code` | 每 run 恰好一次；`state` 只取三终态；`failed` 时 core 必须给 `error_code`；**不带结果全文**（§5.3） |

可空键（`parent_turn_id` / `child_session_id`）写进 `required`——键存在、值可空，与 C-14 把 `user_text`/`assistant_text` 分开、RFC-0007 §5.3 把 `turn_id` 必带同源。

### 5.3 `result_digest` 是指纹，不是摘要文本

`result_digest = sha256(结果全文)` 的 hex，`result_chars = len(结果全文)`。为什么事件里不放全文或 ≤500 字摘要：

1. **落点错**：`subagent.*` 进父会话的 seq 空间 → 进 `events` 表 → 进 replay 窗口 → 进诊断导出面。子代理的结果全文已在**子会话的消息流**里持久化了一次；再往父会话的审计里抄一份，是两份真相与双倍脱敏面。
2. **指纹的用途恰好不需要全文**：客户端用 digest 判断「重连后 snapshot/list 拉到的 run 与事件流里的 result 是不是同一次结果」、审计用 digest 对账子会话的最终消息——身份验证与对账是指纹的本职。
3. **FR-8.56 的「≤500 字摘要」由 `subagent/get` 满足**：`result_preview` 是读时投影（子会话最后一条助手消息截 500 字），不是持久化字段；子会话不可读时为 null，UI 回退为「跳转查看」（FR-8.58）。

**脱敏红线**：任何 `subagent.*` 事件 payload 都不得出现结果全文键（`result_text` / `summary` / `output`）；`check_contracts.py` 与测试双向锁死（§11 A4）。

### 5.4 与 replay / snapshot 的关系

- 三条事件都是**普通 seq 事件**（无 `x-vade.coalesce`、无 `x-vade.push`）：replay 覆盖率、persist-before-broadcast、seq 连续性全部沿用 C-06 既有规则。
- 重连真相：`ui/attach` 的 snapshot **不加 `subagents` 键**（C-05 的 snapshot 是 `object|null`，但不加键是本提案的裁定——run 列表用 `subagent/list` 拉，一次 RPC 拿全量状态；`task.*` 进 snapshot 是因为进度高频需要播种，`subagent.*` 的状态迁移低频，replay 补得到）。
- 事件是**增量**，`subagent/list` 是**真相**：客户端 reducer 以 `run_id` 为键，终态优先；replay 里迟到的 `subagent.status`（终态后）不得复活 run。

## 6. RPC

### 6.1 `subagent/list`（新）

```json
{"method": "subagent/list",
 "params": {"session_id": "string|null (缺省=跨会话)",
            "states": ["subagent.state"],
            "limit": "int 1..200 = 50",
            "cursor": "string|null (不透明)"},
 "result": {"runs": ["SubagentRunView"],
            "next_cursor": "string|null"},
 "errors": ["INVALID_ARGUMENT"]}
```

- 排序固定：`created_at` 升序、`run_id` 决胜；keyset 游标（`base64("s1|created_at|run_id")`），客户端不得解析，越界/非法 → `INVALID_ARGUMENT`。
- `states` 过滤在排序**之后**的子集内分页；`session_id` 缺省为跨会话查询（N3：不是共享执行）。
- `SubagentRunView` = §4.2 全字段 + `duration_ms`（运行中报已耗时，终态报总耗时）+ `result_preview`（≤500 字|null，见 §6.3）。
- 显式只读会话**可调用**（只读）。

### 6.2 `subagent/get`（新）

```json
{"method": "subagent/get",
 "params": {"run_id": "string (run_<32hex>)"},
 "result": {"run_id": "string", "parent_session_id": "string",
            "parent_turn_id": "string|null", "child_session_id": "string|null",
            "label": "string", "state": "subagent.state",
            "created_at": "int", "finished_at": "int|null",
            "result_digest": "string|null", "result_chars": "int|null",
            "error_code": "string|null", "result_preview": "string|null (<=500)",
            "duration_ms": "int"},
 "errors": ["NOT_FOUND", "INVALID_ARGUMENT"]}
```

- `run_id` 格式不对（非 `run_<32hex>`）→ `INVALID_ARGUMENT`（`data.field="run_id"`）；格式对但不存在 → `NOT_FOUND`。两种失败分开，客户端才能区分「手滑」与「真没有」。
- `result_preview`：`completed` 且子会话可读时 = 子会话最后一条助手消息前 500 字；否则 null。**读时投影，不落库**。
- 显式只读会话可调用。

### 6.3 `subagent/message`（已有，不动）

RFC-0007 §6.4 的 `subagent/message` 原样保留（`tasks.subagent_message=false` 默认关闭）。它是子代理的**写通道**，本提案的两个方法是**读通道**；三个方法同一个 `subagent/` 前缀族但分属两个 RFC——`subagent/message` 的 `task_id` 仍是 RFC-0007 的任务 id，不改参数、不改错误集。

### 6.4 只读与入口边界

| 方法 | 显式只读会话 | 说明 |
|---|---|---|
| `subagent/list` | 允许 | 只读 |
| `subagent/get` | 允许 | 只读；`result_preview` 是子会话消息的投影，不进诊断导出包 |

**创建入口不在本 RFC**（N4）：spawn 的工具签名、审批、独立取消路径属 loop/模块契约。core 内的派生方（未来的 `_spawn` 内置工具）通过 `SubagentManager.spawn()` 登记——本提案只保证登记之后的一切（事件、状态机、查询、父 turn 收口）就位。

## 7. 校验

### 7.1 字段校验

| 字段 | 规则 | 违反 |
|---|---|---|
| `label` | 非空字符串，≤64 字符（截断不行——超长即拒，label 是用户起的名） | `INVALID_ARGUMENT`，`data.field="label"` |
| `run_id` | `^run_[0-9a-f]{32}$` | `subagent/get` 与 `require()` 处：`INVALID_ARGUMENT`（格式）→ `NOT_FOUND`（格式对但没有） |
| `parent_session_id` | 非空字符串 | `INVALID_ARGUMENT`，`data.field="parent_session_id"` |
| `error_code` | `state=failed` 时必带，且必须是 C-04 34 码之一 | `INVALID_ARGUMENT`，`data.field="error_code"` |

### 7.2 状态机与合法转移

```
spawned ──▶ running ──▶ completed
   │            │
   │            ├──▶ failed
   └────────────┴──▶ cancelled        （终态不可离开；spawned 不能直接 completed/failed）
```

| 当前 \ 目标 | `running` | `completed` | `failed` | `cancelled` |
|---|---|---|---|---|
| `spawned` | ✓ | ✗ | ✗ | ✓ |
| `running` | ✓（步骤刷新，非迁移） | ✓ | ✓ | ✓ |
| 终态 | ✗ | ✗ | ✗ | ✗ |

- `spawned → completed/failed` 被拒：一个从未开跑的循环没有「结果」，直接报终态只可能是实现 bug——宁可让派生方显式走 `running`。
- `running → running` 允许：`subagent.status` 在运行期携带 `step`/`tool_name` 更新，`state` 不变；这不是迁移，但**是**一次合法的状态事件（不更新 `created_at`）。
- 非法转移：`INVALID_ARGUMENT`，**记录不动、事件不发**（§11 A5）。终态幂等：对已终态 run 再 `finish` 返回现记录、不产生第二个 `subagent.result`。

### 7.3 父 turn 收口

`TurnQueue._finalize` 的 pre-terminal 钩子（RFC-0008 S5 的同一个挂点）扩展为链：先关 RFC-0008 的待答输入，再调 `SubagentManager.cancel_for_turn(parent_session_id, parent_turn_id)`——把该 turn 名下所有非终态 run 置 `cancelled` 并各发一条 `subagent.result{state:"cancelled", result_digest: sha256(""), result_chars: 0, finished_at}`。两条硬规则：

1. **顺序**：`cancel_for_turn` 在 turn 终态事件写盘**之前**执行（挂点即为此设）——客户端先看见子代理收口、再看见父 turn 收口，不会出现「父 turn 已完、子代理还在跑」的窗口（§11 A7）。
2. **覆盖四个终态**：`completed` / `failed` / `interrupted` / `cancelled` 都触发。父 turn 被用户打断（`ui/interrupt`）同样收口——RFC-0007 对 `kind=subagent` 的任务也是这个语义（`registry.cancel_turn`），两条路互不替代、互不重复（任务归任务，run 归 run，§8）。

被收口的 run 不区分 `spawned` 与 `running`：两者都可 `cancelled`（§7.2 表）。

## 8. 与 `task.*` 的边界

| 维度 | `subagent.*`（本提案） | `task.*`（RFC-0007） |
|---|---|---|
| 执行体 | **LLM 循环**：有自己的 turn、消息流、工具审批，跑在 `child_session_id` 的会话里 | **外部进程**：模块/作业进程，无自己的 turn |
| 状态推进单位 | 一步推理 / 一次工具调用（`step`、`tool_name`） | 进程存活与进度百分比（`pct`） |
| 结果 | `result_digest`（指纹）+ 子会话消息（真相源） | `summary ≤500`（截断文本，随事件走） |
| 真相源 | `subagent_runs` 表 + `subagent/list|get` | `snapshot.tasks` + `task/list` |
| 取消 | 父 turn 终态收口（§7.3）；无独立 `subagent/cancel`（N4） | `task/cancel`（幂等 + 宽限期） |
| 审批 | 复用 `turn.approval_required`，归属子会话的 turn | `waiting_approval` 任务状态（RFC-0007 §5.2） |
| 消息通道 | `subagent/message`（RFC-0007 §6.4，不动） | — |

三条硬边界（都是「不混用」的具体含义）：

1. **一次子代理生命周期只发 `subagent.*`**。RFC-0007 路径上 `kind:"subagent"` 的任务行（`task.started` 等）仍可存在——它是任务表对「派生动作」的观测镜像；但 `subagent_runs` 是子代理**生命周期**的唯一权威，两族的 `task_id` 与 `run_id` 互不相认，客户端不得用一方的事件更新另一方的状态。
2. **`task/cancel` 不取消 run，`subagent.*` 无 cancel 方法**。取消一个子代理 = 取消它的父 turn（或它自己跑完失败）——v1 没有独立的子代理取消语义（N4）。
3. **事件不跨族引用**：`subagent.*` payload 无 `task_id`，`task.*` payload 无 `run_id`。将来若要关联，加可选键并同步改两个契约，不做隐式约定。

RFC-0007 §4.2 的裁定**部分撤销**：该节说「线上只有右列四个名字（task.*）」——对本提案后，这句话只对 `kind:"job"` 成立；`kind:"subagent"` 的生命周期事件改走 `subagent.*`（本提案）。`subagent/message` 的名字、参数、错误集一字不改。

## 9. 契约变更

### 9.1 汇总

| # | 契约 | 改动 | 判定 | 兼容策略 |
|---|---|---|---|---|
| 1 | C-06 `schema/events.json` | 追加 3 个 `subagent.*` 事件 + 顶层 `subagent_states` + `x-vade.appends` 块；**顶层 `version` 仍 2.0.0** | **追加**（C-06 只可追加） | 旧客户端忽略未知事件；`subagent_states` 是新顶层键，不 widen 任何既有枚举 |
| 2 | C-05 `schema/rpc.json` | `ui_to_core` 追加 `subagent/list`、`subagent/get`（21 → 23 个方法族……实际：`ui_to_core` 31 → 33）；`version` 1.3.0 → **1.4.0**；`x-vade.appends` 追加块 | **追加** | 未实现 core 返回 `NOT_FOUND`（`make_rpc_handler` 的既有兜底） |
| 3 | C-04 `schema/errors.json` | **不改**（34 码不变） | 无变更 | 复用 `INVALID_ARGUMENT` / `NOT_FOUND` |
| 4 | C-18 `schema/config.schema.json` | **不改**（无 `[subagents]` 节；`max_spawn_depth=1` 是代码常量，不是配置） | 无变更 | — |
| 5 | core 内部 | `subagent_runs` 表（PRAGMA 可重入）、`SubagentRun` + `SubagentState`（`session/models.py`）、`core/vade/subagents/` 包（manager + rpc）、`session/manager.py` 的 pre-terminal 链 | **core 内部**，无契约面 | — |

### 9.2 C-06：追加块

```json
"x-vade.appends": [{ "contract": "C-06", "version": "2.0.0", "owner": "C3",
  "doc": "docs/rfc/RFC-0012-subagent-events.md",
  "adds": ["subagent.spawned", "subagent.status", "subagent.result"],
  "overrides": ["top-level `subagent_states` key appended (task_states precedent); NOT an events entry",
                 "subagent.spawned is payload-only (no `state`); subagent.status / subagent.result are state-change events"],
  "rewrites": "none. Top-level `version` stays 2.0.0: no enum widening — subagent_states is a NEW top-level key and every pre-existing entry, rule and enum is unchanged. No x-vade.push on any of the three." }]
```

`check_contracts.py` 的新增断言（§12）：三事件存在且 `required` 冻结、`subagent.spawned` 不带 `state`、`subagent.status|result` 必带 `state`、顶层 `subagent_states` 与 `SubagentState` 零漂移、终态集 = `{completed, failed, cancelled}`、三个事件有 `emit()` 点。

### 9.3 C-05：方法与版本

```json
"x-vade.appends": [{ "contract": "C-05", "version": "1.4.0", "owner": "C3",
  "doc": "docs/rfc/RFC-0012-subagent-events.md",
  "adds": ["subagent/list", "subagent/get"],
  "rewrites": "none. Two read-only ui_to_core methods appended; no existing method changed. subagent/message (RFC-0007 §6.4) is untouched." }]
```

`version` 1.3.0 → 1.4.0 的依据：C-05 每次追加方法族都升 minor（1.1.0 四方法、1.2.0 三方法、1.3.0 十方法、1.4.0 两方法）。`events_core_to_ui.names` 不动（RFC-0010 先例：镜像表由 C-06 拥有，事件名以 C-06 为准）。

### 9.4 兼容判定与回滚

- **事件追加**：旧客户端按「不知道就不处理」忽略三个新事件（C-06 rule 1 的既有语义）；`subagent_states` 是新顶层键，无人读取即无害。
- **方法追加**：旧 core 对两个新方法天然 `NOT_FOUND`；UI 的降级是「子代理入口只显示事件流里的 run」，不伪造列表。
- **回滚**：不注册 `subagents.install()`、删 `subagents/` 包即回到现状；`subagent_runs` 表留在库里不被读取，事件留在 `events` 表被旧客户端忽略。无降级脚本。

## 10. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **`result_preview` 的来源边界** | 目前取「子会话最后一条助手消息前 500 字」。若子代理最后一步是工具调用（助手文本很短），preview 信息量不足——要不要改取「最后一条助手消息，或最长一条助手消息」？倾向 v1 维持「最后一条」，语义最简单、可预测；待 UI 实测再调 |
| Q2 | **`subagent.result` 是否也要 `step`/`tool_name`**（终态时最后在做什么） | 诊断价值高（失败时知道死在哪一步），但终态 payload 每多一个键就多一个脱敏面。倾向不加，失败现场去子会话看（FR-8.58） |
| Q3 | **`spawned → cancelled` 的 `result_digest` 语义** | 目前 = `sha256("")`（与 `result_chars=0` 自洽）。另一种是 null（「无结果」与「空结果」分开）。倾向保持 sha256("")——终态 digest 恒非空，客户端校验逻辑只有一条 |
| Q4 | **`max_spawn_depth=1` 是否足够** | R3 的层级树验收（FR-8.57）按 N2 降级为一层。若将来放开嵌套，`parent_session_id` 不再恒等于主会话、`subagent/list` 的跨会话过滤要带深度，`subagent_states` 不变——契约面无破坏，但 N2 是本提案对「一层」的承诺，放开需新 RFC |
| Q5 | **`subagent/list` 是否加 `parent_turn_id` 过滤** | 「这个 turn 派了哪些子代理」是 UI 高频查询，靠遍历 `subagent/list{session_id}` 客户端过滤也能做。倾向 v1 不加（参数面最小），等真实 UI 需求再追加 |

## 11. 验收（A1–A10）

全部要求真实执行（pytest），截图与手写记录不算证据。测试落点 `core/tests/subagent/`。

| # | 断言 | 承载 |
|---|---|---|
| A1 | spawn 3 个 run → 恰好 3 条 `subagent.spawned`，payload 含全部 required 键，`session_id == parent_session_id`，`run_id` 命中 `^run_[0-9a-f]{32}$`，行已落 `subagent_runs` | `test_spawned.py::test_spawned_is_self_describing` |
| A2 | `label` 空串 / 64+1 字符 / 非字符串 → `INVALID_ARGUMENT(data.field="label")`；恰好 64 字符通过；64 字符以上截断不可发生 | `test_spawned.py::test_label_validation` |
| A3 | `spawned → running` 发 `subagent.status`（`state="running"`）；`running` 期 `step`/`tool_name` 更新发同事件且 `state` 不变 | `test_status.py::test_spawned_to_running_emits_status` / `test_status_updates_while_running` |
| A4 | `subagent.result(completed)` 的 `result_digest == sha256(全文)`、`result_chars == len(全文)`，且**全文不出现在事件 payload**（5000 字文本，digest 64 hex）；`subagent/get` 的 `result_preview` ≤500 字且是子会话最后一条助手消息的前缀 | `test_result.py::test_result_digest_is_digest_not_fulltext` |
| A5 | 状态机：`running → spawned`、终态 → 任何状态、`spawned → completed/failed` 全部 `INVALID_ARGUMENT`，记录与事件流**零变化**；终态幂等（二次 finish 无第二条 result） | `test_status.py::test_illegal_transition_rejected` / `test_terminal_cannot_resume` / `test_result.py::test_result_is_terminal_idempotent` / `test_result.py::test_illegal_result_transition` |
| A6 | `failed` 必带 `error_code`（缺失 → `INVALID_ARGUMENT`）；`error_code` 必须是 C-04 码；失败时 `result_digest == sha256("")`、`result_chars == 0` | `test_result.py::test_failed_result_requires_error_code` / `test_failed_result_digest_of_empty` |
| A7 | 父 turn 终态收口：turn 完成后其名下未完成 run 变 `cancelled`，`subagent.result(cancelled)` 的 seq **小于** `turn.completed` 的 seq | `test_parent_turn_cancel.py::test_parent_turn_end_cancels_unfinished_runs` / `test_result_precedes_turn_completed` |
| A8 | `subagent/list`：返回视图全字段、`states` 过滤生效、非法 `states`/`limit` → `INVALID_ARGUMENT`；`subagent/get`：`NOT_FOUND` 与格式 `INVALID_ARGUMENT` 分开 | `test_rpc.py` |
| A9 | `sqlite_store` 对已存在库重开第二次连接：`subagent_runs` 幂等建表不报错，另一连接写入本连接可读 | `test_spawned.py::test_store_table_is_reentrant` |
| A10 | `subagent_states`（schema）== `SubagentState`（code），终态集一致；三事件 `required` 与本 RFC §5.2 逐键一致（契约闸） | `tools/check_contracts.py` 的 RFC-0012 块 |

文档级：`python docs/check_docs.py` 仍 12/12（新 RFC 文件与索引行不参与 C1–C10 计数）；`python tools/check_contracts.py` OK。

## 12. 影响面

| 文件 | 改动 |
|---|---|
| `core/vade/session/models.py` | `SubagentState` 枚举 + `SUBAGENT_TERMINAL_STATES` + `SubagentRun` 冻结 dataclass + `new_run_id()` |
| `core/vade/session/sqlite_store.py` | `_SCHEMA` 追加 `subagent_runs` 表与索引；`upsert_subagent_run` / `get_subagent_run` / `list_subagent_runs` |
| `core/vade/subagents/`（新增） | `manager.py`（状态机闸、事件发射、父 turn 收口、digest 计算）、`rpc.py`（两方法）、`__init__.py`（`install(app)`，照 `vade.tasks` 的接线） |
| `core/vade/session/manager.py` | `_queue()` 的 pre-terminal 钩子改为链（输入收口 + `cancel_for_turn`），duck-typed `log.subagents`，session 组件不 import 子代理包 |
| `schema/events.json` | 3 事件 + 顶层 `subagent_states` + `x-vade.appends` 块（version 仍 2.0.0） |
| `schema/rpc.json` | 2 方法 + `x-vade.appends` 块（version 1.4.0） |
| `tools/check_contracts.py` | RFC-0012 检查块；`STATE_CHANGE_EVENTS`/`PAYLOAD_ONLY_EVENTS` 追加；C-05 版本钉改 1.4.0 |
| `core/tests/subagent/`（新增） | ≥15 条测试（§11） |
| `docs/rfc/README.md` | 索引追加 RFC-0012 行 |

**不动**：`core/vade/blob/`、`core/vade/input/`、`core/vade/modulepage/`、`core/vade/prompt/`、`core/vade/loop/`（子代理的 spawn 工具签名与结果回灌父模型属 loop 另案）、`core/vade/tasks/`（`task.*` 族与 `subagent/message` 原样）、`turn_states` / `task_states` / C-04 错误码 / `schema/config.schema.json`。

**参考**：`docs/requirements.md` §8.23.2 R3 事实行（行 1186）与 §8.21.2 FR-8.56～8.58（行 1126–1128）；RFC-0007（`task.*` 四事件、`subagent/message`、§4.2 被部分撤销的裁定、§9.2 的 `task_states` 顶层键先例）；RFC-0008（pre-terminal 钩子所有权）；RFC-0010（C-05 追加与版本纪律先例）。
