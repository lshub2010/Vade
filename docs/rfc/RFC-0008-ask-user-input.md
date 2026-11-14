---
rfc: RFC-0008
title: agent 要求输入：`waiting_input` 状态、`turn.input_*` 事件对与 `turn/input` 取值通道
status: approved
approved: 2026-11-14
date: 2026-11-14
proposer: 编排（依据 `docs/requirements.md` §8.21 FR-8.54/8.55、§8.22.2 R6 与 D103；`docs/agents/50-U1-ui-skeleton.md` §九把 U1-d 的输入对话框挂在本 RFC 上）
affects: C-06 `schema/events.json`（新增 `turn.input_required` / `turn.input_resolved`；`turn_states` 与 `turn.state.enum` **追加 `waiting_input`**）、C-05 `schema/rpc.json`（`ui_to_core` 追加 `turn/input`、`turn/input.cancel`；`ui/attach` snapshot 注记 `pending_inputs[]`）、C-18 `schema/config.schema.json`（新增 `[ask_user]`）；`core/vade/session/models.py` 的 `TurnState`、`core/vade/input/`（新增模块）、`core/vade/policy/approval.py`（复用 id 生成与阻塞模式）
related: RFC-0006（"未知事件类型必须忽略"由它提出；"不改 `turn_states`"的口径由本 RFC **首次打破**）；RFC-0007（子代理审批在父会话 seq 广播的先例，本 RFC §5.4 沿用）；RFC-0004（同批 UI 提案；对话框容器复用 C-09 的 `InputBar`）；RFC-0003（`ask_user` 的工具命名族与 L0 目录）
type: 契约追加（C-05 / C-18）+ **一处枚举拓宽（C-06 `turn_states`，对穷举校验方属破坏性）**；无改名、无字段删除、无 C-04 新码
stage: 草稿，未实现；不改 `schema/`，不动 `waiting_approval` 的既有语义，不做表单 DSL
---

# RFC-0008 · agent 要求输入

## 1. 摘要

审批回答"做不做"，输入回答"是什么"。两者都是**阻塞型交互**，但审计口径、关闭信号与值的类型完全不同：把 `free_text` 塞进 `approval/decide` 只能让 `decision:"allow"` 兼职表示"用户填了一段话"，而审批的 `scope:"session"` 缓存会**替用户自动回答**下一次同类问题（§2.1）。因此本提案新开一条取值通道，并给 turn 一个新的阻塞状态。

| # | 决定 | 一句话理由 |
|---|---|---|
| 1 | 新增 turn 状态 **`waiting_input`**，不复用 `waiting_approval` | 需求 D103 已拍板；复用会让审计的 `kind` 失真、超时收场无状态可写（§2.1） |
| 2 | 事件只有两个：`turn.input_required` / `turn.input_resolved` | 与 C-06 现有粒度一致；每对请求-应答恰好一个 `input_id`（§6） |
| 3 | `turn/input` 以 **`input_id` 为幂等键**：同值重提返回首次结果，异值拒绝 | 多端双击与重连重发都会打同一次提交（§7.3） |
| 4 | 四类控件单值单控件：`free_text` / `single_choice` / `multi_choice` / `confirm` | 值编码只有 string / string[] / bool 三种，core 侧可穷举校验（§4） |
| 5 | 超时与无 UI 都**不收默认值**：模型一律收到 `CANCELLED` | FR-8.54 明文；填默认值等于替用户签字；无 UI 时排队只会白等 120s（§8.1、§8.4） |
| 6 | `turn_states` 拓宽记 C-06 **2.0.0**，老端按 `running` 渲染 | 枚举拓宽对穷举校验方是破坏性的，藏在小版本里会让客户端静默丢事件（§6.4） |

## 2. 动机与证据

### 2.1 审批通道装不下"取值"，还会替用户回答

`turn.approval_required` 的 payload 只有 `{turn_id, request_id, tool, args, risk, expires_at}`（`schema/events.json:111-128`），`approval/decide` 只接受 `decision: allow|deny`。把问题塞进 `tool`/`args`、把用户填的文本当成一次 `allow`，会同时坏掉三件事：审计行写的是 `kind:"approval"`、`status:"allowed"`（`approval.py:278-285`），事后无法区分"批准了一次工具"与"回答了一个问题"；`turn.approval_resolved` 的 `state` 恒为 `running`（`approval.py:296`），而输入可能以超时收场；最要命的是 `scope:"session"` 的放行缓存按 `(session_id, tool_id, args_digest)` 记录（`approval.py:123-128`、`:271-275`），一旦 `tool` 固定为 `ask_user`，**第二次同参数的提问会命中缓存直接返回 allow，用户根本不会看到对话框**。这不是实现瑕疵，是通道语义不匹配：审批的"记住这个决定"对输入是错的，同一个问题问两遍必须弹两次。

### 2.2 `waiting_approval` 在现网里没有生产者

`TurnState` 有 `WAITING_APPROVAL`（`core/vade/session/models.py:26-35`），C-06 的 `turn_states` 也有这个值（`schema/events.json:22-30`），但全仓库能写这个状态的只有 `queue.py:325-332` 的 `mark_state()`，而它的调用者只有测试（`core/tests/session/test_queue.py:188`）：`approval.py` 从不调用它，`mark_state` 也**不发任何事件**（只改内存并 `save_turn`）。也就是说"turn 正在等审批"这件事，客户端是从 `turn.approval_required` 的到达推断出来的，而该事件按 C-06 rule 4 属于 payload-only、不带 `state`。这解释了为什么 `waiting_approval` 只出现在枚举和测试里——它是一张没人兑现的支票。

本提案不重复这个错误：`turn.input_required` **带 `state:"waiting_input"`**，进入阻塞状态本身就是一次状态变更，必须能被 state-change 事件看见。代价是 `tools/check_contracts.py:52-58` 的 `STATE_CHANGE_EVENTS` 闭集要加两个名字（§10.1 第 4 行）。

### 2.3 真相源里没有"待答的问题"，重连就丢弹窗

`SessionManager.snapshot()` 返回 `from_seq / session / messages / queue / pending_approvals`（`core/vade/session/manager.py:272-293`），没有"待答输入"。而 `ui/attach` 在 replay 不覆盖时**只给 snapshot**。于是用户手机弹着问题、切到桌面客户端 attach——对话框不存在；如果 replay 恰好覆盖，`turn.input_required` 会重放出来，两条路径给出两个不同结果。这和 RFC-0007 §2.2 的"关闭 UI 就失联"是同一类缺陷：**pending 记录必须进 snapshot，与 `pending_approvals` 同级**，不能只靠事件重放。

### 2.4 模型中途问路，今天只能靠猜或终止

`ask_user` 要解决的是"计划里有一个只有用户知道的取值"：目标文件名、要改哪个环境、三个方案选一个。今天模型只有两条路——猜一个（猜错浪费一整轮工具调用），或把问题写进正文结束 turn；后者在协议上是一次**新的 `ui.submit`**，审计上成了"用户下了一条新指令"，模型侧还要重拼上下文。FR-8.54 要的是**同一个 turn 内挂起再继续**，这需要 core 能发起阻塞请求、能超时收场、能让模型拿到明确的"用户没答"信号——三者都在协议面，UI 自造只能做出假对话框。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 四类控件都能提交；值类型、长度、选项范围在 **core 侧**闭合校验，不依赖客户端自觉（FR-8.54） |
| G2 | 等待期间 turn 处于 `waiting_input`，输入区禁用并显示原因；离开只有三条路：提交 / 取消 / 超时（FR-8.54） |
| G3 | 同一 `input_id` 在所有 attach 的端同时弹；任一端提交广播 `turn.input_resolved`，其余端 1 个 RTT 内关闭；重复提交幂等（FR-8.55） |
| G4 | 超时或取消后模型收到 `CANCELLED`，**不收到**空值、`null` 或 `default` |
| G5 | 重连（replay 与 snapshot 两条路）后未答的对话框仍在，已答的**不复活** |
| G6 | 每次请求/应答各写一条审计：请求方、`input_id`、耗时、是否超时、值的摘要与长度 |
| N1 | **不做表单 DSL**：无对象值、无嵌套字段、无多问题合批；一次请求只问一个值 |
| N2 | **不做嵌套输入**：一个 turn 同一时刻只能有一个阻塞等待（输入或审批），见 §5.2 |
| N3 | 不做"输入即授权"：回答永不能满足审批；后续工具调用照常走 `engine._check_approval`（`engine.py:191`） |
| N4 | 不新增 C-04 错误码：复用 `INVALID_ARGUMENT` / `NOT_FOUND` / `READ_ONLY_SESSION` / `OVERLOADED` / `CANCELLED` / `PAYLOAD_TOO_LARGE` |
| N5 | 不做输入草稿的跨端实时同步（流式预览见 Q4） |
| N6 | 不做"稍后回答"（最小化/暂停）：对话框只有提交、取消、超时三个出口 |

## 4. 模型：请求记录与四类控件

### 4.1 记录字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `input_id` | string | core 分配，ULID 形态（复用 `approval.new_request_id()` 的字母表，`approval.py:46-60`），按创建时间可排序、全局唯一 |
| `turn_id` / `session_id` | string | 发起方；`session_id` 是**广播所在的 seq 空间**（子代理见 §5.4） |
| `kind` | `free_text\|single_choice\|multi_choice\|confirm` | 生命周期内不可变 |
| `prompt` | string 1..500 | 模型写的问题正文，纯文本不解析 markdown；`detail`（string ≤2000，可空）作为可选补充说明随事件同发 |
| `schema` | object | 取值描述符（§4.3），**不是**表单 DSL，也**不是**完整 JSON Schema |
| `options` | array | `{id, label≤120, description≤240\|null}`；choice 类 2..20 项，其余必须为空数组 |
| `timeout_s` | int 5..600 | 由发送方请求、core 夹紧到 `[ask_user]` 上下限（§8.1） |
| `expires_at` / `created_at` | int (unix ms) | 前者供客户端倒计时，后者供审计与多端排序；internal deadline 用单调时钟，不吃系统改时 |
| `default` | string\|string[]\|bool\|null | **仅供 UI 预填**，超时不采用（§8.1） |
| `source` | object | `{tool:"ask_user"\|"module", module_id\|null, depth:int}`；`depth>0` = 子代理（§9.5） |

### 4.2 四类控件

| `kind` | 控件 | `value` 编码 | 校验（core 侧，失败即拒绝） | 备注 |
|---|---|---|---|---|
| `free_text` | 多行文本框 | `string` | 1..`schema.max_length`（默认 4000，硬上限 8000）个 code point；首尾空白裁掉后不得为空 | 不自动发送、不当作新消息；换行保留 |
| `single_choice` | 单选列表/分段控件 | `string`（选项 `id`） | 必须命中 `options[].id`，区分大小写 | 选项 ≤5 且 label ≤20 字时客户端可用分段控件，这是**渲染**选择，不影响协议 |
| `multi_choice` | 多选列表 | `string[]`（选项 `id`，按提交顺序） | 长度 `min_select ≤ n ≤ max_select`；元素必须命中 `options[].id`；不得重复 | `max_select` 缺省 = 选项数；`min_select` 缺省 1 |
| `confirm` | 两按钮 | `bool` | 严格 JSON `true`/`false` | **不得**用于破坏性操作确认——那是审批通道（§9.3） |

`free_text` 与选项共存（"选一个或自己填"）在 v1 **明确不做**：它会让值编码变成 `{choice}` 与 `{text}` 的联合类型，校验、审计与三端控件都要多一维（Q1）。

### 4.3 `schema` 描述符

四个 `kind` 各对应一个固定形状，core 只认这四种，多余键忽略：

```json
{"type":"string","max_length":4000,"multiline":true}
{"type":"string","enum":["opt_a","opt_b"],"allow_other":false}
{"type":"array","items":{"type":"string","enum":["opt_a","opt_b"]},"min_items":1,"max_items":2}
{"type":"boolean"}
```

它不是渲染指令：客户端**可以**用它做长度计数与禁用态，但**不得**据此长出任意表单（没有 `properties`、没有嵌套、没有 `required` 数组）。`allow_other` 恒为 `false`，留键只为将来 Q1 演进时不改事件形状。

### 4.4 校验失败的语义

1. 校验在 `turn/input` 内部完成，失败返回 `INVALID_ARGUMENT` + `data.field="value"` + `data.expected`，**不改变 turn 状态、不发事件、不消耗 `input_id`**；客户端就地提示并保持对话框打开——把用户打错字当成"已取消"是最容易写出的 bug。
2. `free_text` 的空白串（`"   \n"`）按空处理；`multi_choice` 的重复元素按错误处理而不是去重（去重会掩盖客户端的选项渲染 bug）。
3. 超长值返回 `PAYLOAD_TOO_LARGE`（与 RFC-0006 的单条 32k 口径一致），`data.limit` 给具体上限。
4. 发起侧同样校验：`prompt` 超长、`options` 越界、`kind` 与 `options` 不匹配时，`ask_user` 工具调用得到 `INVALID_ARGUMENT` 的工具结果，turn 继续（不因一次坏提问而失败）。

## 5. 状态机

### 5.1 转移

```
            ┌────────────▶ waiting_approval ──(allow|deny)──┐
queued ─▶ running ◀──────────────────────────────────────────┘
             │
             ├──────────▶ waiting_input ──(submit|cancel|timeout)──▶ running
             ├──────────▶ completed
             └──────────▶ failed | interrupted
```

`waiting_input` 是**等待态**，不是终态；它只能从 `running` 进入，只能回到 `running` 或三个终态（`completed` / `failed` / `interrupted`）。`cancelled` 只属于从未启动的排队 turn（`queue.py:301-322`），`waiting_input` 的 turn 已经启动，不会走到 `cancelled`。

### 5.2 六条硬规则

| # | 规则 | 理由 |
|---|---|---|
| S1 | 一个 turn 同一时刻**至多一个**阻塞等待（审批或输入） | 两个等待 = 两个关闭信号，谁先到都可能让另一个永远悬着 |
| S2 | `waiting_input` 与 `waiting_approval` **互斥**；进入任一状态的前提是 `CURRENT == running` | 枚举上两者并列，语义上是同一个"单槽位"，不能同时为真 |
| S3 | **禁止嵌套**：`waiting_approval` 中的 turn 再发起输入 → 工具结果 `INVALID_ARGUMENT`，`data.reason="blocked_by_approval"`，turn 留在 `waiting_approval` | 悄悄排队会让弹层顺序与 seq 顺序脱钩 |
| S4 | 同一 turn 内第二个并发输入请求同样拒绝：`data.reason="input_pending"` | 与 S1 同源；并行派发是未来才有的能力 |
| S5 | `turn.input_resolved` 必须先于该 turn 的任何终态事件发出 | C-06 规定 `turn.interrupted` 之后不得再有该 turn 的 `turn.*`；顺序反了就是一个永不关闭的弹窗 |
| S6 | turn 已终态后到达的提交：`INVALID_ARGUMENT`，**不复活**状态 | 终态不可离开（C-06 既有承诺） |

> **S3/S4 今天几乎触发不到**：`engine.py:174-230` 是逐个执行工具的 `for` 循环，审批在 `ask_user` 之前就已 resolve。规则针对两类真实路径——子代理的 turn 在父会话 seq 里等审批而同会话主 turn 正在等输入（§5.4），以及未来的并行派发（RFC-0006 只加了 `batch_id` 标签，没有并行语义）。规则先立、按 §11 A9 验，但不要假装今天就能触发。

### 5.3 进入与离开的 seq 顺序

| 场景 | 事件序列（省略 `queue.updated`） |
|---|---|
| 正常提交 | `turn.tool_call(ask_user)` → `turn.input_required` → `turn.input_resolved{status:"submitted"}` → `turn.tool_result{ok:true, result.value}` |
| 用户取消 | 同上，`status:"cancelled"`，`turn.tool_result{ok:false, code:"CANCELLED"}` |
| 超时 | 同上，`status:"timeout"`，`turn.tool_result{ok:false, code:"CANCELLED"}` |
| 等待中 `ui/interrupt` | `turn.input_resolved{status:"cancelled", by:"system", reason:"turn_interrupted"}` → `turn.interrupted` |
| 等待中会话关闭 | `turn.input_resolved{status:"cancelled", reason:"session_closed"}` → `turn.interrupted`（随后 `session.closed`） |

`turn.input_required` 与 `turn.input_resolved` 都走 `SessionLog.emit()` 的 persist-before-broadcast（C-06 rule 2），因此 replay 能重放；`turn.input_resolved` 每 `input_id` **恰好一条**，这由 §7.3 的幂等矩阵保证，而不是靠客户端去重。

### 5.4 子代理与跨 turn 的弹层共存

沿用 RFC-0007 §7.2 的口径：子代理发起的输入以**父会话**的 seq 广播，`session_id` 是父会话、`turn_id` 是子 turn、`source.depth>0`。父会话不认识这个 `turn_id`，因此**不得**据此改父 turn 的状态机——弹层只按 `input_id` 开关，与审批卡只按 `request_id` 开关同源。

由此才可能出现 FR-8.54 验收里那句"审批与输入同时存在时两个弹层不互相覆盖且标题不同"：主 turn 等审批、子代理等输入，是两个 **turn** 各有一个等待，S1/S2 说的是同一个 turn，并不冲突。UI 必须按 `(turn_id, expires_at)` 排序堆叠，标题分别为"需要审批"与"需要你补充信息"，且**不得**把 ask-user 的对话框画成审批样式（§9.2）。

## 6. 事件

### 6.1 `turn.input_required`

| 项 | 内容 |
|---|---|
| `payload` | `input_id, turn_id, session_id, state:"waiting_input", kind, prompt, detail, schema, options[], timeout_s, expires_at, default, source{...}, created_at` |
| `required` | `input_id, turn_id, state, kind, prompt, schema, options, timeout_s, expires_at` |
| `notes` | 广播给每个 attach 的 UI，同一 seq 同一 payload。**超时 = 取消**（fail-closed），`default` 不参与超时。可空键写进 `required`、值为 `null`，让事件自描述（与 RFC-0007 §5.3 一致）。 |

`state` 进 `required` 是对 C-06 rule 4 的正面遵守，不是可选装饰：状态变更事件必须能被 reducer 直接消费。

### 6.2 `turn.input_resolved`

| 项 | 内容 |
|---|---|
| `payload` | `input_id, turn_id, state, status, value, by, reason, duration_ms` |
| `required` | `input_id, turn_id, state, status, value, by` |
| `notes` | **唯一**关闭信号，每 `input_id` 恰好一条。`status ∈ {submitted, cancelled, timeout}`；`submitted` 时 `value` 必非空且已通过 §4 校验，其余两种 `value` 恒为 `null`。`by` 取设备标识、`"user"` 或 `"system"`。`reason` 取 `user_cancel \| turn_interrupted \| session_closed \| no_ui \| superseded`。 |

`state` 是被恢复到的 turn 状态：提交/超时后是 `running`；turn 被中断时是 `interrupted`（此时它先于 `turn.interrupted` 发出，S5）。`by:"system"` 的 `status:"timeout"` 与 `by:"user"` 的 `status:"cancelled"` 必须可区分——FR-8.55 的审计要求"是否超时"，这个位就是它的线上来源。

### 6.3 `turn_states` 追加 `waiting_input`（C-06）

三处必须同时改，少一处 `tools/check_contracts.py` 就红：

| 位置 | 现状 | 变更 |
|---|---|---|
| `schema/events.json:22-30` `turn_states` | 7 值，`waiting_approval` 在 `running` 之前 | 追加 `waiting_input`，紧随 `waiting_approval` |
| `schema/events.json` `events["turn.state"]["enum"]` | 同一 7 值的副本 | 同步追加（`check_contracts.py:255-258` 断言两者一致） |
| `core/vade/session/models.py:26-35` `TurnState` | 7 个成员 | 加 `WAITING_INPUT = "waiting_input"`（`check_contracts.py:140-144` 解析该类并与 schema 比对） |

`TERMINAL_STATES`（`models.py:38-40`）**不动**：`waiting_input` 不是终态。另有一处测试会红是**预期内**的：`core/tests/events/test_schema_append.py:57` 用 `==` 钉死了 7 值列表，它存在的意义就是"证明 C-06 没被动过"，本 RFC 动了枚举，这条断言必须连同 C-06 版本号一起更新（§10）。

### 6.4 老客户端行为（前向兼容的唯一代价）

| 客户端 | 收到 `turn.input_required` | 收到 `turn.input_resolved` | 用户看到 |
|---|---|---|---|
| 旧版（只认 7 值枚举） | 未知事件类型 → 按 RFC-0006 §9.1 第 6 行忽略并推进 `last_seq`；若它硬校验 `state ∈ turn_states`，该事件被丢弃 | 同上，忽略 | turn 停在最后一个已知状态（`running`），输入区照常可用；直到超时后 turn 继续或失败。**表现为"卡住一段时间"，不是报错** |
| 旧版 + 硬校验且不忽略未知 | 可能拒绝该事件 | 同 | 最坏情况：整条连接按协议错误断开（AGENTS 里已把"未知事件必须忽略"列为升级检查项） |

两条约束把代价压到可接受：

1. **不按客户端能力降级**：C-05 没有能力协商字段，core 无法判断对面是不是旧端。要降级就只能加协商——那是另一个 RFC 的量级。
2. **超时上限兜底**：`timeout_s` 硬上限 600s、默认 120s（§8.1），旧端最坏等待时长有界，不会出现"永远转圈"。枚举拓宽记 C-06 `2.0.0` 并在 `x-vade.appends[]` 里带 `enum_widen`（§10.2）：伪装成 1.1.0，客户端会按"小版本只加字段"的假设升级，然后在校验层静默丢事件。

## 7. RPC

### 7.1 `turn/input`

```json
{"method":"turn/input",
 "params":{"session_id":"string","input_id":"string",
           "value":"string | string[] | bool"},
 "result":{"ok":true,"turn_id":"string","status":"submitted|timeout|cancelled",
           "value":"同 params.value | null","duplicate":"bool"},
 "errors":["INVALID_ARGUMENT","NOT_FOUND","READ_ONLY_SESSION","PAYLOAD_TOO_LARGE"]}
```

- `input_id` 是幂等键，**不接受**客户端自造；`session_id` 与记录不符时一律 `NOT_FOUND`（既不认自造 id，也不泄露别的会话有没有这个 id）。
- `duplicate:true` 只在"同 `input_id` + 同值"时出现：返回首次结果、不发第二个事件（§7.3）；`status:"timeout"` 的返回是给"迟到提交"用的——迟到者能知道发生了什么，但改变不了任何事。

### 7.2 `turn/input.cancel`

```json
{"method":"turn/input.cancel",
 "params":{"session_id":"string","input_id":"string","reason":"string<=200"},
 "result":{"ok":true,"turn_id":"string","status":"cancelled|submitted|timeout"},
 "errors":["NOT_FOUND","READ_ONLY_SESSION","INVALID_ARGUMENT"]}
```

- **幂等**：已 resolved 的 `input_id` → `ok:true` + 当前 `status`，**不**发第二个 `turn.input_resolved`。与 `ui/interrupt` 的区别：cancel 只结束这一个问题，turn 继续跑（模型收到 `CANCELLED` 后自行决定继续或收尾）；interrupt 结束整个 turn。这正是"用户不想答但也不想杀掉任务"的出口。
- 取消后 `turn.input_resolved{status:"cancelled", by:<提交方>, reason:"user_cancel"}`。

### 7.3 幂等矩阵

| # | 现状 | 请求 | 返回 | 事件 |
|---|---|---|---|---|
| 1 | pending | 合法值 | `ok:true, status:"submitted", duplicate:false` | 一条 `resolved{submitted}` |
| 2 | pending | 非法值/类型错 | `INVALID_ARGUMENT` + `data.field` | 无（状态不变，§4.4） |
| 3 | 已 submitted，同值 | 重提 | `ok:true, status:"submitted", value=首次值, duplicate:true` | 无 |
| 4 | 已 submitted，异值 | 重提 | `INVALID_ARGUMENT` + `data.reason="already_resolved"` + `data.status:"submitted"` | 无 |
| 5 | 已 timeout / cancelled | 任意提交 | `ok:true, status:"timeout\|cancelled", value:null, duplicate:true` | 无 |
| 6 | 未知 / 超保留期 | 任意 | `NOT_FOUND` | 无 |

第 4 行选"拒绝"而不是"接受后者"：接受会让两个端看到不同答案，而 `turn.input_resolved` 只有一条，谁也无法事后仲裁。第 5 行选"成功返回"而不是报错：客户端在倒计时到点与网络抖动之间重发是常态，把它当错误只会制造假告警。

### 7.4 只读与鉴权

| 方法 | 显式只读会话 | 说明 |
|---|---|---|
| `turn/input` / `turn/input.cancel` | `READ_ONLY_SESSION` | 写 |

除此之外**没有额外鉴权**：能 `ui/submit` 的会话就能 `turn/input`。这是刻意的——回答一个问题是"和 agent 说话"，与发一条消息同一量级；审批才需要 `device_key` / `loopback_admin`（`approval.py:34`），因为它会放行一次副作用。AMEND-22 #14 之后 `READ_ONLY_SESSION` 只指**显式**只读会话，cookie 会话不再默认只读（D100 数据面可写），所以这条不会把浏览器用户挡在外面。

### 7.5 错误码（全部已存在）

| 码 | 何时 | `data` |
|---|---|---|
| `INVALID_ARGUMENT` | 值不合 §4、异值重提、已终态、嵌套/并发请求 | `field`、`reason`、`expected`、`status` |
| `NOT_FOUND` / `READ_ONLY_SESSION` | 未知或过期 `input_id`、会话不符 / 显式只读会话 | — |
| `PAYLOAD_TOO_LARGE` / `OVERLOADED` | 值超上限 / 超出 §9.5 的频次 | `limit`、`actual` / `retry_after` |

## 8. 超时、断开与多端一致

### 8.1 超时

| 项 | 值 | 依据 |
|---|---|---|
| 默认与上下限 | `default_timeout_s = 120`，夹紧到 5..600（越界不报错，按夹紧值广播） | 比审批的 60s 长：输入要读题、打字、想清楚；倒计时必须与 core 的 deadline 同源，否则两端显示不同的剩余时间 |
| 时钟 | deadline 用单调时钟，`expires_at` 用墙钟 | 系统改时不能延长或缩短等待（同 `approval.py:71-73`、`:146`） |
| 到点 | `status:"timeout"`，`by:"system"`，`value:null`；模型收到 `CANCELLED` | FR-8.54 明文"超时后模型收到 CANCELED 而不是空值" |
| 竞态 | 截止瞬间到达的提交算提交（"real decision beats timeout"） | 直接复用 `approval.py:161-198` 的 `shield` + 锁内认领写法 |

**`default` 永不自动应用**。它是 UI 预填，不是 fallback：一个"默认值在超时后生效"的设计等于让超时替用户签字，而 FR-8.54 的验收恰好是"不操作到超时 → 收到 `CANCELED` 并给出可读原因"。想让模型拿到默认值，正确做法是模型把默认值写进 `prompt`（"不回答我就按 `main.py` 继续"），然后自己决定超时后怎么走——决策权与责任都留在模型。模型侧拿到的工具结果：`{ok:false, error:{code:"CANCELLED", message:"input request timed out after 120s (input_id=…)"}}`。用 `CANCELLED` 而不是 `TIMEOUT`，是因为 C-04 里 `TIMEOUT` 的 `retriable:true`，会诱导实现层自动重问一遍——而"用户没答"这件事重问一百次也是没答。

### 8.2 断开与重连

| 路径 | 结果 |
|---|---|
| `last_seq` 在 replay 窗口内 | 重放 `turn.input_required`（persist-before-broadcast），对话框重建，`expires_at` 照旧倒计时 |
| replay 不覆盖 → snapshot | `snapshot.pending_inputs[]` 提供 `input_id / turn_id / kind / prompt / options / schema / timeout_s / expires_at / created_at`；客户端据此重建同一对话框 |
| 客户端在提交途中断线 | 提交可能已生效：重连后先看 `turn.input_resolved` 或 snapshot 里该 `input_id` 是否消失；已消失就不弹 |
| core 重启 | 待答记录只存内存（与 `pending_approvals` 同级），重启即丢；该 turn 已在重启时落为 `interrupted`（`queue.py` 终态落库），因此**不会**出现"弹窗永远无人认领" |
| replay 重叠 | reducer 按 `input_id` 终态优先：已 resolved 的 id 再收到 `required` 一律忽略（与 RFC-0007 §5.5 R3 同构），否则重放会弹出鬼影对话框 |

`pending_inputs` 是 snapshot 的追加键（`snapshot` 在 C-05 是 `object|null`，不改结构），旧端读不到就忽略——与 RFC-0007 给 `tasks` 的处理完全一致。

### 8.3 多端

| 场景 | 规则 |
|---|---|
| 同时弹 | 所有 attach 的端收到**同一条**事件（同 seq、同 payload），不允许每个 sink 生成不同的 `input_id` |
| 任一端提交 | core 立即广播 `turn.input_resolved`；提交端**不得**在收到该事件前关闭对话框（可先禁用按钮），否则另一端会看到"我这边还开着、那边已经没了"；其余端在 1 个 RTT 内关闭并提示"已在另一台设备回答"，不弹错误 |
| 两端同时提交不同值 | 先到 core 者胜（seq 定序），后者按 §7.3 第 4 行得 `INVALID_ARGUMENT`，前端**不**把这条错误当成用户错误，而是关闭对话框并提示"已在另一台设备回答" |
| 只读端 | 按钮禁用 + 后端 `READ_ONLY_SESSION`（FR-8.55） |

### 8.4 无 UI 时不排队

`ui_count == 0` 时（例如定时任务或后台 turn 发起提问），**立即**返回 `status:"cancelled", reason:"no_ui"`，模型收到 `CANCELLED`。这直接复用 `approval.py:130-139` 的判据：没有审批人时立刻 deny，而不是让 turn 白等到超时、在用户眼里表现为"卡住"。

与审批相反的一处：**等待中途 UI 全部断开时不取消**。审批可以 fail-closed 立即拒绝，因为拒绝是安全的默认；而"用户合上了笔记本"不等于"用户拒绝了这个问题"，把对话框判死会让用户回来时发现 turn 已经带着 `CANCELLED` 跑完了。让它等到超时。

## 9. 安全与审计

### 9.1 值是数据，不是代码

`value` 只作为 `ask_user` 工具结果回给模型，**不得**被 core 拼接进 shell、文件路径或任何工具参数模板。模型若据此再调工具，那次调用照常过 policy 与审批（`engine.py:191`）——输入不是"预授权"，链路上不存在"用户说了就跳过审批"的捷径。

### 9.2 `prompt` 是模型写的，按不可信内容渲染

问题与选项文案由模型生成，可能被工具输出污染（提示词注入的典型路径）。因此：

1. 纯文本渲染，不解析 markdown、不自动链接、不渲染 HTML；`prompt`/`label` 里的 URL 只显示不可点。
2. 对话框标题固定为"需要你补充信息"，**不得**出现"安全校验""系统确认""授权"等审批语汇，也不得复用审批卡的红/黄风险色。
3. 按钮文案固定（"提交/取消"、`confirm` 为"是/否"），不随模型改变；长度上限（§4.1）同时是防注入与防布局爆破的手段。

### 9.3 回答永不能满足审批

用户在 `free_text` 里写"好的，删掉吧"**不构成**一次批准。这是本提案唯一的授权边界主张：`ask_user` 的回答只影响模型上下文，`risk=high` 的工具调用仍然走 `approval/decide`。反过来也成立：审批的 `allow` 不会替用户填任何 `input_id`。两条通道在审计里可区分（§9.4），在 UI 上可区分（§9.2），在代码里是两条 RPC。

### 9.4 审计

每次请求与应答各一条，`kind` 分别为 `input_request` / `input_response`：

| 字段 | 请求 | 应答 |
|---|---|---|
| `input_id` / `turn_id` / `session_id` | ✓ | ✓ |
| `actor` | 发起方（`tool:ask_user` 或 `module:<id>`） | 提交方设备标识 / `"system"` |
| `kind` / `status` | `free_text\|…` / `pending` | — / `submitted\|cancelled\|timeout` |
| `duration_ms` / `timed_out` | — | 从请求到应答的单调耗时 / bool（显式布尔，不从 `status` 反推） |
| `value_digest` / `value_chars` | — | `sha256(value)` 与 code point 数；**不落原值** |
| `reason` | — | `user_cancel\|turn_interrupted\|session_closed\|no_ui\|superseded` |

不落原值的原因：`free_text` 里出现密钥、身份证号、内部地址是常态，而审计表是长期保留、会被导出的（FR-8.71 的诊断包）。`value_digest` 足以支持"同一次回答被重放两次"的对账。是否要为客服场景保留原值，见 Q5。

### 9.5 限流、防刷与子代理

| 约束 | 值 | 理由 |
|---|---|---|
| 单 turn 请求数 / 会话频次 | ≤10 / ≤3 次/分钟 | 模型陷入"再问一次"循环时的硬闸；超出 `OVERLOADED` + `retry_after` |
| 单请求选项数 | ≤20 | 超过 20 项的单选在移动端不可用，该换 `free_text` |
| 子代理发起 | `[ask_user] allow_subagents = false`（默认关） | 子代理不该单独审问用户；打开后弹层必须显示来源（`source.depth`）与会话路径 |

`allow_subagents=false` 时，子代理内的 `ask_user` 调用直接得到 `CANCELLED`（`reason:"no_ui"` 的等价路径），而不是弹到父会话——默认关闭比默认打开安全，因为"一个你自己派出去的后台代理来问你问题"本身就是可疑交互。

## 10. 契约变更

### 10.1 汇总（7 项：追加 3、枚举拓宽 1、注记 1、实现断言 1、无变更 1）

| # | 契约 | 改动 | 判定 | 兼容策略 |
|---|---|---|---|---|
| 1 | C-06 新事件 `turn.input_required` | 见 §6.1 | **追加** | 旧端按未知类型忽略并推进 `last_seq` |
| 2 | C-06 新事件 `turn.input_resolved` | 见 §6.2 | **追加** | 同上 |
| 3 | C-06 `turn_states` + `turn.state.enum` + `TurnState` | 追加 `waiting_input`（三处同步） | **破坏性（枚举拓宽）**，C-06 `1.0.0 → 2.0.0` | 旧端未知状态按 `running` 渲染；不得断开连接；`TERMINAL_STATES` 不变 |
| 4 | C-06 `STATE_CHANGE_EVENTS`（`tools/check_contracts.py:52-58`） | 加入两个新事件名 | **实现侧断言更新**，非 schema | 不加入会以"state 出现在非状态事件上"失败 |
| 5 | C-05 `turn/input` / `turn/input.cancel` | 见 §7.1、§7.2 | **追加** | 旧端不调用；未知方法不影响既有方法 |
| 6 | C-05 `ui/attach` snapshot | 注记 `pending_inputs[]` | **追加注记** | `snapshot` 仍是 `object`；旧端忽略新键 |
| 7 | C-04 `errors.json` | **不改**：复用 5 个既有码（§7.5） | **无变更** | 码表与 Rust 枚举零改动 |

C-18 `[ask_user]` 追加 6 键（`default_timeout_s` / `max_timeout_s` / `max_free_text_chars` / `max_multi_select` / `allow_subagents` / `audit_value`），全部 `x-vade.apply: hot`——它们只影响后续请求，不需要重启。

### 10.2 C-06 追加块的形状

`events.json` 根部已有 `x-vade.append`（**单数**，C-14 的对象，字节不能动）。按 RFC-0006 §9.2 的约定使用兄弟键 `x-vade.appends`（数组）；若 RFC-0006 先落地则并入同一数组，否则由本 RFC 创建：

```json
{"contract":"C-06","version":"2.0.0","owner":"C3",
 "doc":"docs/rfc/RFC-0008-ask-user-input.md",
 "adds":["turn.input_required","turn.input_resolved"],
 "enum_widen":{"turn_states":["waiting_input"],
               "synced":["events.turn.state.enum","core/vade/session/models.py:TurnState"],
               "breaking":"only for clients that validate every state against a frozen enum"},
 "overrides":["turn.input_resolved is the only signal that closes an ask-user dialog",
              "turn.input_required carries state:waiting_input and is therefore a state-change event"],
 "rewrites":"none"}
```

### 10.3 判定说明

- **第 3 行是唯一需要评审重点看的一项**。`version` 取 `2.0.0` 而不是 `1.1.0`：`rewrites:"none"` 只保证字节没动，而**语义上**枚举变宽了；把它伪装成小版本，客户端会按"小版本只加字段"的假设升级，然后在校验层静默丢事件。若评审认为"枚举只加值不算破坏性"可以降版，但必须同时要求所有客户端把"未知状态"实现为 `running`——本提案选择"先承认破坏性，再要求客户端容错"，因为后者无法在协议层强制。
- **第 4 行不是 schema 变更**：`check_contracts.py` 的 `STATE_CHANGE_EVENTS` 是"`state` 恰好出现在这些事件上"的白名单，新事件带 `state` 就必须登记，否则 CI 红。这类"实现侧断言"要写进 RFC，否则实现者会以为是 schema 写错了。
- **第 7 行**：不新增 `INPUT_REQUIRED` / `INPUT_EXPIRED` 之类的码。`INVALID_ARGUMENT` + `data.reason` 能区分 `already_resolved` / `blocked_by_approval` / `input_pending`，新增码要同步 Rust 枚举、HTTP 映射与 33 条码表，收益只是少一个 `if`。
- **不改 `waiting_approval`**：本 RFC 不修 §2.2 发现的"审批没有状态事件"缺陷（那需要动 `turn.approval_required` 的 `required`，属另一份提案）。这里只保证输入不再重犯。

## 11. 验收

A1–A14 全部要求真实执行；截图与手写记录不算证据。

| # | 断言 | 承载 |
|---|---|---|
| A1 | `free_text` 全链路：`turn.input_required.state=="waiting_input"`；提交后 `input_resolved{status:"submitted", value}` 且 `seq` 小于 `turn.tool_result`；工具结果 `ok:true` 且 `result.value` 逐字相同 | `core/tests/input/test_free_text.py` |
| A2 | `single_choice` 与 `multi_choice` 编码：选 2 项 → `value` 是长度 2 的数组且顺序等于提交顺序；`confirm` → `value` 是 JSON 布尔而非 `"true"` | `core/tests/input/test_value_encoding.py` |
| A3 | 校验闭合：错误选项 id、`multi_choice` 重复元素、超 `max_select`、空白 `free_text`、超 `max_length` 各返回 `INVALID_ARGUMENT`/`PAYLOAD_TOO_LARGE` 且 `data.field=="value"`；**状态仍为 `waiting_input`、`seq` 不变** | `core/tests/input/test_validation.py` |
| A4 | 超时：`timeout_s=1` 不操作 → `input_resolved{status:"timeout", by:"system", value:null}` + 工具结果 `code=="CANCELLED"`；模型上下文中**没有** `default` 的值（把 `default` 设成 `"SENTINEL"` 断言它不出现在工具结果里） | `core/tests/input/test_timeout.py` |
| A5 | 取消：`turn/input.cancel` → `input_resolved{status:"cancelled"}`，turn 回到 `running` 并继续产出 delta；连调两次只有一条 `input_resolved` | `core/tests/input/test_cancel.py` |
| A6 | 幂等矩阵六行逐条：同值重提 `duplicate:true` 且无第二个事件；异值 `INVALID_ARGUMENT` + `reason:"already_resolved"`；超时后提交返回 `status:"timeout"` 且无事件 | `core/tests/input/test_idempotency.py` |
| A7 | 多端：两个 attach 的 sink 收到同一 `input_id` 的同一 seq；A 提交后 B 在 1 个 RTT 内收到 `input_resolved`；B 的迟到提交得到第 4/5 行结果，**事件计数仍为 1** | `core/tests/input/test_multi_client.py` |
| A8 | 重连两条路：`last_seq` 覆盖时 replay 出 `turn.input_required`；强制 snapshot 时 `snapshot.pending_inputs` 含同一 `input_id`；已超时的 `input_id` 不出现在 snapshot 里 | `core/tests/input/test_snapshot_pending.py` |
| A9 | 互斥与嵌套：`mark_state(waiting_approval)` 后发起输入 → 工具结果 `INVALID_ARGUMENT` + `reason:"blocked_by_approval"`，状态未变；同一 turn 两个并发 `ask_user` → 第二个 `reason:"input_pending"`；子代理在父会话等审批时发起输入（`allow_subagents=true`）→ 两个弹层共存且 `turn_id` 不同 | `core/tests/input/test_state_machine.py` |
| A10 | 中断顺序：`waiting_input` 中 `ui/interrupt` → 先 `input_resolved{status:"cancelled", state:"interrupted"}` 再 `turn.interrupted`，且 `turn.interrupted` 之后该 turn 再无任何事件 | `core/tests/input/test_interrupt_order.py` |
| A11 | 无 UI：`ui_count==0` 时发起 → 立即 `status:"cancelled", reason:"no_ui"`，从请求到返回 ≤50ms，不消耗 `timeout_s` | `core/tests/input/test_no_ui.py` |
| A12 | 只读：显式只读会话调 `turn/input` 与 `turn/input.cancel` 均 `READ_ONLY_SESSION`；同会话 `turn/input` 之外的方法不受影响 | `core/tests/input/test_read_only.py` |
| A13 | 审计：一次提交产生 `input_request` + `input_response` 两行，含 `input_id`/`actor`/`duration_ms>0`/`timed_out=false`；超时场景 `timed_out=true`；两行里 `value_digest` 都存在而**原值字符串不在任何审计字段中** | `core/tests/input/test_audit.py` |
| A14 | 契约同步：`check_contracts.py` 绿（`turn_states == TurnState == turn.state.enum`，两新事件有 emit 点且 `state` 出现在白名单内）；`test_schema_append.py` 的 7 值断言已随 `2.0.0` 更新；旧客户端 fixture 收到两个新事件不报错、`last_seq` 前进 | `core/tests/events/test_schema_append.py` + `core/tests/input/test_event_compat.py` |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿（写本文件时已实测；新文件不参与 C1–C10 计数）。

## 12. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| A. 复用 `turn.approval_required` + `decision:"allow"` 承载输入 | 审计语义失真，且 `scope:"session"` 的缓存会替用户自动回答下一次同类问题（§2.1）——这不是不优雅，是错 |
| B. 复用 `waiting_approval` 状态 | 一个状态两个含义，`turn.input_resolved` 就没有可回写的状态位；需求 D103 已否定（§2.1） |
| C. 不做事件，只在工具结果里阻塞等用户下一条消息 | 模型侧无法区分"用户没答"与"用户没说"，且回答变成新 turn，审计上分不清回答与指令（§2.4） |
| D. 表单 DSL（多字段/嵌套/条件显示） | 校验、审计、三端控件全要跟着长；FR-8.54 只要求一个值。真需要多字段时宁可串行问两遍（N1） |
| E. 超时应用 `default` | 等于超时替用户签字，直接违反 FR-8.54 的验收项（§8.1） |
| F. 无 UI 时排队到超时 | 用户眼里就是"卡住"，且与 `approval.py` 的 `no_approver` 判据不一致（§8.4） |

## 13. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **`free_text` 与选项能否共存**（"选一个，或自己填"） | (a) v1 不做，需要时串行问两次；(b) 加 `allow_other:true`，值仍是字符串但可以是自由文本。**倾向 (a)**：联合类型的校验与三端控件成本远高于收益，而串行两问在 UX 上只多一次点击。`schema` 里已经留了 `allow_other` 键，将来切 (b) 不改事件形状 |
| Q2 | **超时默认值与是否按 `kind` 分档** | 现定 120s 单一默认。(a) 保持单一；(b) `confirm` 60 / `choice` 120 / `free_text` 300。**倾向 (b)**，但要先有一次真机数据：打字时间与选项数的关系目前是猜的。无论选哪个，`max_timeout_s=600` 不动 |
| Q3 | **多端同时提交不同值如何裁决** | (a) 先到 core 者胜，后者 `INVALID_ARGUMENT`（现方案）；(b) 检测到冲突就两个都拒、重开一问；(c) 后者覆盖前者并补发第二条 `input_resolved`。(b) 会把"两端都点了"变成"谁都没答"，(c) 破坏"每条恰好一条 resolve"。**倾向 (a)**，需要真机双端压一次确认没有客户端在收到失败后自动重试 |
| Q4 | **输入是否可流式预览**（一端打字，另一端实时看到） | v1 不做（N5）。要做需要 `turn/input.preview` 事件或 `turn/input` 的 draft 模式，代价是用户未提交的文本离开设备、进 seq、进 replay 窗口——隐私与存储都要重新评估。**倾向永久不做**，除非出现"协作填表"类需求 |
| Q5 | **审计是否保留原值** | 现方案只存 `value_digest` + 长度。(a) 保持；(b) `[ask_user] audit_value=true` 时存原值（客服复盘要"用户当时到底填了什么"）。(b) 的风险是审计表变成秘密收集器。**倾向 (a) + 按会话临时开启**，且开启时必须在 UI 上明示 |
| Q6 | **子代理能否发起 ask-user** | 现默认 `allow_subagents=false`。(a) 保持默认关；(b) 默认开但要求父 turn 显式授权（`task_spawn(allow_ask_user=true)`）。**倾向 (a)**；若产品确认"子代理问路"是核心场景，应走 (b)，因为它把同意权放在派生那一刻而不是被问的那一刻 |
| Q7 | **对话框能否"稍后回答"** | 现方案只有提交/取消/超时（N6）。(a) 保持；(b) 允许最小化，badge 常驻、`expires_at` 照旧。(b) 需要"待答清单"这个新 UI 面与跨会话的 pending 视图，属独立提案。**倾向 (a)**，先看用户是否真的会被 120s 逼到 |

## 14. 影响面与后续

| 文件（实现阶段，非本 RFC） | 改动 |
|---|---|
| `core/vade/input/manager.py`（新增） | `InputManager`：pending 表、`request()` 阻塞等待、`submit()`/`cancel()`、幂等缓存（LRU 256 / 24h）、限流计数、审计写入。结构照 `approval.py` 抄，去掉 grant/scope |
| `core/vade/input/models.py`（新增） | `InputRequest` / `InputValue` 冻结 dataclass、`InputKind` 枚举、`validate_value()` |
| `core/vade/loop/engine.py` + `ports.py` | `ask_user` 内置工具的适配层：调 `InputManager.request()`，把 `CANCELLED` 映射为工具结果，`risk` 固定 `low`；新增 `InputPort` Protocol + `NullInputPort`（无实现时一律 `CANCELLED{reason:"no_ui"}`，与 `NullApprovalPort` 同构） |
| `core/vade/session/models.py` | `TurnState.WAITING_INPUT`（§6.3） |
| `core/vade/session/manager.py` | `snapshot()` 加 `pending_inputs`；`turn/input` 两个方法的入口与只读判定 |
| `core/vade/session/queue.py` | `mark_state(WAITING_INPUT)` 的调用点**必须同时发状态事件**——不再重犯 §2.2 的"状态只落库不发事件" |
| `core/vade/policy/approval.py` | 只抽出 `new_request_id()` 供复用，审批语义零改动 |
| `schema/events.json` / `rpc.json` / `config.schema.json` | §10 的三处追加；`tools/check_contracts.py` 的 `STATE_CHANGE_EVENTS` 加两名字 |
| `core/tests/events/test_schema_append.py` | 7 值列表断言更新为 8 值并钉死 C-06 `2.0.0` |

**不动**：`core/vade/policy/approval.py` 的授权模型与 `scope:"session"` 缓存（§10.3 最后一条）、`core/vade/router/*`、`core/vade/loop/context.py`（`ask_user` 的工具结果按普通 tool 消息进上下文）、`turn.approval_required` 的 `required`（补 `state` 是另一份提案）、C-09 的组件清单（对话框复用 `InputBar`，新增的只是它的 `mode:"asking"` 子态）。

**跨提案依赖**：`docs/agents/50-U1-ui-skeleton.md:458` 的降级规则要求"RFC-0008 未批时 `waiting_input` 分支不写死"；本提案批准后 U1-d 可直接接 `turn.input_required` 与 `turn/input`。RFC-0006 若未先落地，"未知事件类型必须忽略并推进 `last_seq`"这条兼容前提需要在本 RFC 批准时**一并**写进 C-06 规则（两份提案任一方先落都补上，但不能两边都假设对方补了）。

**后续（不在本 RFC）**：给 `turn.approval_required` 补 `state:"waiting_approval"`（§2.2 的欠账，动 `required`，需单独提案）；`turn.approval_resolved` 的 `state` 不再恒为 `running`；输入草稿跨端同步（Q4）；"待答清单"与"稍后回答"（Q7）；多问题合批（N1 的边界，需要先证明单问不会让模型问三轮）。

**参考**：`docs/requirements.md` §8.21 FR-8.54/8.55、§8.21.2 R5、§8.22.2 R6 与 D103；`docs/OVERVIEW.md:150`；`docs/agents/50-U1-ui-skeleton.md:39/448/458`；`core/vade/policy/approval.py`（阻塞、超时、多端、审计的现成模式）；`schema/events.json:22-30/111-128`；`core/vade/session/models.py:26-40`、`queue.py:84-97/279-301/325-332`、`manager.py:272-293`；`tools/check_contracts.py:52-58/140-144/255-258`；RFC-0006（追加块形状与"未知事件忽略"）、RFC-0007（`task.*` 与子代理广播）。
