---
rfc: RFC-0006
title: turn 语义补充：中断留痕、工具卡字段、重生成/编辑重发、流式多段与单条长度上限
status: approved
approved: 2026-11-14
date: 2026-11-14
proposer: 编排（依据 `docs/requirements.md` §8.21 FR-8.30/8.31/8.32/8.35/8.51/8.52、§8.22 修订；`research/ui-02-messages-input-attachments.md` §3.9 C4/C5/C6/C10/C14）
affects: C-06 `schema/events.json`（5 组字段追加 + 2 个新事件 + 注记级澄清）、C-05 `schema/rpc.json`（新增 `turn/regenerate`、`message/edit_resubmit`，`ui/attach` 的 snapshot 注记）、消息/turn 模型（`core/vade/session/models.py`、`sqlite_store.py` 追加列）、`core/vade/session/queue.py`（中断落库）、`core/vade/loop/engine.py`（工具卡字段与分段）、`core/vade/loop/context.py`（superseded 过滤）
related: RFC-0004（content parts、单 part 32k、`attach.text_max_chars`）；RFC-0003（章节与元信息风格来源）；AMEND-21 §8.21.2 R7/R8/R9；AMEND-22 §8.22.2 R6/R8、§8.22.5；RFC-0008（ask-user，编号已由 AMEND-22 预留，与本文件无字段重叠）
type: 契约追加（C-05/C-06）为主 + 存储列追加；所有新字段对旧客户端均为可选，`required` 数组一律不变
stage: 已实现并合并
---

# RFC-0006 · turn 语义补充

## 1. 摘要

C-06 把 turn 的**状态机**写清楚了，但没写 turn 的**内容语义**：中断时已经生成的半截回答在 core 里没有落点；工具卡只有工具名与一个摘要；重新生成/编辑重发没有 RPC，只能靠"再提交一次"造出重复气泡；`turn.delta` 只有 `text`，无法表达"解释→调用→解释"的分段；单条消息没有 core 侧长度上限。本提案只补这五处，全部按追加权落地。

| # | 决定 | 一句话理由 |
|---|---|---|
| 1 | 中断留痕选**落库 assistant 消息**（`partial=true`），`turn.interrupted` 只带 `partial_message_id`/`partial_chars` 指针 | snapshot 只读 `messages`，事件带文本仍救不了新端 attach；落库后导出、搜索、上下文都只有一份真相 |
| 2 | `turn.tool_call` 增 `args_preview`（按 risk 脱敏）+ `args_preview_mode`/`args_preview_redacted`/`args_preview_truncated`/`started_at`/`batch_id`；`turn.tool_result` 增 `duration_ms`/`truncated`/`result_ref` | 参数在 core 调用前已解析、耗时在 `ToolCallResult` 上已测好，只是没进事件；脱敏预览不能变成第二条泄密路径 |
| 3 | 重生成/编辑重发 = **新 turn + 旧 turn 标 `superseded_by`**，线性版本链，只允许操作会话尾部；`session/fork` v1 不做 | 不引入消息树就能满足 FR-8.31/8.32 的"灰化 + 新回答在下"；树会改存储、snapshot 与多端一致性规则 |
| 4 | `turn.delta` 增 `part_index`/`kind`，**不新增** `turn.part.started` | 分段信息随增量走，旧客户端顺序拼接结果逐字不变；少一个事件类型就少一层 replay 兼容面 |
| 5 | 单条消息文本上限 **32,000 Unicode code points**（所有 text part 之和），超限 `PAYLOAD_TOO_LARGE` + `data.field` | FR-8.45 要求 core 侧校验；按 code point 计数才不会让 emoji 客户端少算一半 |

## 2. 动机与证据

### 2.1 中断后的半截回答，重连即丢

`TurnQueue.interrupt()` 调 `_finalize(..., messages=())`（`core/vade/session/queue.py`）：`_finalize` 虽然接受 `messages` 参数，但中断路径一个都不传。`SessionManager.snapshot()` 返回的 `messages` 只来自 `session.message_tree()`（`manager.py::snapshot`），而 `turn.interrupted` 的 C-06 注记明写"partial text is reconstructable only from turn.delta events"。

于是：A 端看到 128 字后点停止，B 端 attach 拿 snapshot —— 这 128 字在 core 里**不存在**。FR-8.35 已把这条记为"RFC 落地前不得标通过"的已知缺陷；同一缺陷还让导出（FR-8.33）、全文搜索（FR-8.27）与会话续聊都看不到这段回答。UI 无法自救：snapshot 是唯一真相源，delta 不会为它重放。

### 2.2 工具卡是"有名字、没内容"

`schema/events.json` 的 `turn.tool_call` 只有 `turn_id`/`call_id`/`tool`/`args_digest`；`turn.tool_result` 只有 `turn_id`/`call_id`/`ok`/`summary`/`code`。`engine.py` 发出的 `tool_call_payload` 正是这四个键，`_summary()` 把结果硬截到 300 字符，且不告诉客户端"截了"。

但数据其实已经在 core 里：`ToolCallResult.duration_ms` 由 `core/vade/router/dispatch.py` 在调用返回时测好；`engine._tool_content()` 在结果过大时把全文写进 blob store，只在消息里留 `blob_id`。缺的只是**把这些事实搬进事件**。FR-8.51 要求"耗时缺失时省略该项，不显示 `—ms` 占位"——今天耗时**永远**缺失，这条验收无法通过。

### 2.3 重新生成/编辑重发没有协议

`ui_to_core` 只有 6 个方法（`ui/attach`、`ui/detach`、`ui/submit`、`ui/interrupt`、`ui/queue.cancel`、`approval/decide`）。`ui/submit` 只能新建 turn：把同一条用户消息再提交一次会**多出一个用户气泡**，旧 turn 也没有任何字段能标"已被替代"。多端下更糟——A 端重新生成，B 端 attach 看到的仍是旧回答，因为 `superseded` 不在任何持久结构里。`message/edit_resubmit` 同理；FR-8.32 的"灰 + 新两条在另一端一致"必须由 core 给事件与指针。

### 2.4 `turn.delta` 只有 text，分段靠猜

`turn.delta` 的 payload 只有 `{turn_id, text}`。一轮里"先解释、调工具、再解释"会产生多个 assistant 文本段，但事件流只把它们拼成一条；`engine.py` 的 `max_steps` 提示也是直接以 `turn.delta` 混进正文。UI 只能靠 `turn.tool_call` 的位置猜边界，遇到"同一段里既有解释又有提示"就猜不出来。FR-8.52 的"不跨助手文本段聚合"因此没有可靠的段标识。P2 的工具产物图片/markdown 更需要段元数据。

### 2.5 单条消息没有上限

`ui/submit` 目前只校验 `content` 是数组、`client_msg_id` 非空（`manager.py::submit`）；`_text_of()` 把 text 块直接拼起来。RFC-0004 给单个 text part 定了 32k，但没定**整条消息**的上限：20 个 part × 32k 仍是合法输入。FR-8.45 要求"core 侧单条 content 上限校验（建议同为 32k，返回字段级错误）"，而 `PAYLOAD_TOO_LARGE` 至今没有用在 content 上。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 中断后 1 个 attach 周期内，任意端看到的 assistant 部分文本与中断端逐字一致；不依赖 delta 重放 |
| G2 | 工具卡能显示"参数（脱敏）/耗时/是否截断/完整结果指针"，且预览中不出现密钥 |
| G3 | 重新生成/编辑重发在另一端 attach 后仍是"一条用户气泡 + 旧回答灰化 + 新回答"，刷新/重启不丢灰化状态 |
| G4 | 旧客户端零改动：所有既有事件的 `required` 数组不变，未知字段忽略，未知事件类型忽略并推进 `last_seq`；新功能对旧端是可见降级（多一条旧回答/旧用户消息），不是崩溃 |
| G5 | 32,001 code points 的提交被拒且不产生 turn/message 行；32,000 通过 |
| G6 | 多段 delta 对旧客户端顺序拼接后与升级前逐字相同 |
| G7 | 版本链是线性的：每个 root 恰好一个 head，客户端不需要树也能渲染 |
| N1 | 不做消息树 UI、不做 `‹ 2/2 ›` 版本翻页、不做分支切换入口 |
| N2 | 不改变审批语义：`turn.approval_required`/`approval/decide`/`turn.approval_resolved` 一行不改 |
| N3 | 不做 `session/fork`（v1 不注册方法、不出现入口；签名见 §6.5 仅为将来预留） |
| N4 | 不新增 C-04 错误码，不新增 `turn_states` 值（含 `superseded`） |
| N5 | 不做工具结果缓存、不做"重放上次工具调用"；重生成按普通 turn 重新执行 |
| N6 | 不做引用回复的 `quote` part（FR-8.30 的 content schema 属 RFC-0004 的 parts 扩展）；本文件只保证 `message.edited` 能让引用块标"原消息已变更" |

## 4. 中断留痕

### 4.1 选择：落库，不在事件里搬文本

| 方案 | 结果 | 判定 |
|---|---|---|
| A. `turn.interrupted.partial_text` | 只有收到该事件的在线端能拿到；新端 attach 走 snapshot，仍看不到。要补就得让 snapshot 也带一份，变成两处真相 | 不采用 |
| B. 落库 assistant 消息（本提案） | snapshot/导出/搜索/上下文天然可见；事件只带 `partial_message_id` 指针 | **采用** |

选 B 的决定性理由不是"事件更小"，而是**恢复路径只有 snapshot**：C-06 的 replay 窗口是 1000 条/24h，超窗后 `session.attached.snapshot=true`，任何只存在于事件里的文本都会丢。落库还避免把最多 32k 的文本永久写进 `events` 表（每次中断一份）。

### 4.2 落库形状

- 新增 `Message.partial: bool = False`（`models.py`），`to_dict()` 输出 `partial`；`sqlite_store.messages` 追加 `partial INTEGER NOT NULL DEFAULT 0`。旧行读为 `False`。
- 中断时若累计文本非空，`TurnQueue._finalize(..., messages=[Message(role="assistant", turn_id=<turn>, content=<partial>, partial=True)])`。`SessionManager.emit()` 已经把 `messages` 与事件放在**同一个** `store.append()` 事务里（`manager.py::emit`），所以"先落消息再广播"的顺序不需要新机制。
- 部分文本的来源：`SessionManager.emit()` 是每个 `turn.delta` 的必经点，在其中维护 `self._partial[turn_id] += text`；终态事件后清空。`TurnQueue` 通过构造参数 `partial_provider: Callable[[str], str]` 读取，避免 queue 反向依赖 engine 的局部变量。
- 终态 tombstone：`_finalize` 在 emit 锁内先登记该 turn 已终止；此后到达的 `turn.delta`/`turn.tool_*` 在 `manager.emit` 直接丢弃（不分配 seq、不落库）。否则在途 delta 会排在 `turn.interrupted` 之后，既破坏 C-06 的终态规则，也会让 `partial_chars` 与落库消息不一致。
- **原子性**：中断收尾的 `turns` 行（state=interrupted + `partial_message_id`）、partial 消息、`turn.interrupted` 事件必须在**同一个事务**提交。现有 `_finalize` 先 `_save_turn` 再 `_emit`，是两次提交；崩在中间会留下"状态已 interrupted、但没有 partial 消息与事件"的半截状态。实现需给 `sqlite_store` 增加一个同时写 turn + message + event 的入口（或把 turn 行并入 `append`），不能只靠调用顺序。
- 文本为空（首个 delta 之前被中断、或中断发生在工具步且该步无文本）→ **不建消息**，`partial_message_id=null`、`partial_chars=0`。不允许产生空 assistant 气泡。
- 若中断发生在工具步：该步的 assistant(tool_calls) 消息与已完成的 tool 消息早已由 `_turn_emit(..., messages=...)` 落库；本机制只补"最后一段可见文本"，不重复写它们。
- 这条 `partial=true` 消息在**非重生成**的后续 turn 里按普通 assistant 历史参与上下文（用户确实看到过这段）；在重生成的新 turn 里被 §6.6 的 superseded 过滤排除，避免模型看到自己的旧回答。

### 4.3 事件与去重

`turn.interrupted` 追加字段（`required` 仍为 `[turn_id, state, by]`）：

```json
{"turn_id":"t_2","state":"interrupted","by":"user",
 "partial_message_id":"msg:7f3a…","partial_chars":128}
```

> `partial_message_id` 的取值形状与 `result_ref` 一致，都是 `msg:<hex>`；消息行自身存裸 id。审查曾指出此处示例写成 `m_7f3a…` 与契约不符，已订正。

- `partial_chars` 按 Unicode code point 计，与 §8 的长度单位一致；UI 的"已生成 N 字"直接用它，不再自己数 delta（自己数会与 core 差一个在途 delta）。
- 去重规则：**部分文本以落库消息为准**。客户端如果已经按 delta 渲染了该 turn：① 本地字符数等于 `partial_chars` → 保留本地缓冲，只把气泡绑定到 `partial_message_id`；② 不等 → 说明有在途 delta 丢失，先显示"正在同步…"，重新 `ui/attach` 用 snapshot 替换该气泡后再落定；③ 本地已有该 `partial_message_id` 的消息 → 只换指针，不追加。**禁止**把事件与本地缓冲相加（事件没有文本），也禁止同时渲染两条。没有 `partial_message_id` 时保留本地缓冲。
- replay 交叉：snapshot 已含该消息、随后又 replay 到 `turn.interrupted` 时，客户端按 `partial_message_id` 命中同一消息，幂等忽略。
- 同一 turn 的第二次 `ui/interrupt` 是 no-op（`TurnQueue.interrupt` 在 `_finalized` 后返回 `False`），不产生第二个 `turn.interrupted`。

### 4.4 snapshot 恢复

`snapshot.messages[]` 直接包含这条 `partial=true` 的 assistant 消息；`snapshot.turns[]`（§6.1 新增）带 `partial_message_id`。只读客户端不需要理解 delta，按 `turn_id` 分组即可把"已停止"渲染出来。多端一致性因此是**存储级**的，不是"哪端先收到事件"的。

### 4.5 与 C-06 原注记的关系

`turn.interrupted.notes` 原文"partial text is reconstructable only from turn.delta events"在 v1.1 起不再成立。C-06 已冻结且只允许追加：**不改写原 `notes` 字节**，在文件根部 `x-vade.appends` 登记一条 `overrides`，指明"自本 RFC 起，partial 同时落库为 `partial=true` 的 assistant 消息；delta 不再是唯一来源"。契约检查按追加块读，不按原注记读。

## 5. 工具卡字段

### 5.1 字段表

| 事件 | 字段 | 类型 | 何时有 | 旧客户端 |
|---|---|---|---|---|
| `turn.tool_call` | `args_preview` | string，≤512 字节（UTF-8） | 能序列化 args 时 | 忽略 |
| | `args_preview_mode` | `redacted\|typed` | 有 preview 时 | 忽略 |
| | `args_preview_redacted` | string[]，≤16 个点路径 | 有 preview 时 | 忽略 |
| | `args_preview_truncated` | bool | 有 preview 时 | 忽略 |
| | `started_at` | int（unix ms） | 总是 | 忽略 |
| | `batch_id` | string `bat_[0-9a-f]{16}` | 总是 | 忽略 |
| `turn.tool_result` | `duration_ms` | int ≥0 或 null | 工具**实际执行**过 | 忽略 |
| | `truncated` | bool | 总是 | 忽略 |
| | `result_ref` | string `msg:<hex>` 或 null | 有持久化 tool 消息时 | 忽略 |

`args_digest` 保留且语义不变：它仍是审计与审批比对的锚点；`args_preview` 只是给人看的，不参与任何判定。

### 5.2 `args_preview` 脱敏算法（实现必须按序）

1. **canonical**：只接受 `dict`；按键名排序、`ensure_ascii=false`、无空格分隔符序列化。非 dict（数组标量）包成 `{"value": ...}`。
2. **键脱敏**：先对**完整键名**归一化（小写、去除非字母数字）后整串匹配多词名（`apikey`/`accesskey`/`privatekey`/`sessionid`/`connectionstring`，按后缀匹配以覆盖 `x-api-key`、`AWS_SECRET_ACCESS_KEY` 这类前后缀形态），再按 `_`/`-`/`.` 分词做整词匹配（避免 `author` 命中 `auth`，并覆盖 `_token`/`_secret`/`_password` 后缀）；两阶段任一命中即视为密钥键。命中 `pass(word|phrase)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|cookie|authorization|auth|session[_-]?id|dsn|connection[_-]?string` 的键名与值都替换为 `"***"`，点路径记入 `args_preview_redacted`。**只做分词匹配是不合规实现**：分词后 token 内不可能还有 `_`/`-`，带分隔符的备选分支永远匹配不到。
3. **值擦洗**：对所有字符串值做模式替换（PEM `-----BEGIN ... PRIVATE KEY-----`、`sk-[A-Za-z0-9]{16,}`、`ghp_[A-Za-z0-9]{20,}`、`xox[bap]-...`、`AKIA[0-9A-Z]{16}`、`Bearer\s+\S+`、连续 hex/base64 ≥32）→ `"***"`，路径记入 redacted。**先擦洗再截断**，避免截断把未匹配的秘密截成"看起来安全"的前缀。
4. **形状压缩**：字符串 >80 字符 → 前 77 + `…`；数组 >8 项 → 前 8 + `"…(+N)"`；对象 >16 键 → 前 16 + `"…(+N keys)"`；递归深度 ≤3。
5. **体积**：UTF-8 序列化后 ≤512 字节；超出则在字符边界截断并置 `args_preview_truncated=true`。
6. **risk 分级**：`low|medium` → 上述 `redacted` 模式；`high` → **所有字符串值**替换为 `"<str:N>"`，数字/布尔/null 保留，键名保留（键名本身按第 2 步处理），`mode="typed"`。理由：`risk=high` 的完整参数已经由 `turn.approval_required` 广播给所有已 attach 的 UI，工具卡再从 `turn.tool_call` 复述一遍只会让完整参数进入 replay、导出与日志；高风险卡的"完整参数"应读审批事件，而不是预览。

保证：对上述任一模式生成的 `args_preview`，用第 3 步的正则重扫必须 0 命中；这条写成测试（§10 A6）。原始 args 不进 `turn.tool_call`、不进日志、不进导出；审计仍由既有 `args_digest`/`args_redacted` 通道负责。

### 5.3 `started_at` / `duration_ms` / `truncated` / `result_ref` 的语义

- `started_at` = core **发出** `turn.tool_call` 的时刻，不是工具开始执行的时刻。需要审批的调用在发出后还要等审批，所以 `started_at + duration_ms` 不等于结果到达时间；UI 的"耗时"只显示 `duration_ms`，不得用两者相减。
- `duration_ms` 取自 `ToolCallResult.duration_ms`（`router/dispatch.py` 已在调用返回时测好），**不含**审批等待与排队时间。未实际执行（策略拒绝、工具不存在、被中断）→ `null`，UI 省略该项（FR-8.51：不显示 `—ms`）。
- `batch_id` = 同一 assistant 模型步产生的全部调用共享的 id，由 core 在步开始时分配（`bat_` + 16 hex）。它表达"同一批"，**不表达并行**：v1 的 `engine.py` 是 `for index in sorted(calls)` 串行 `await`，UI 不得据 `batch_id` 标"并行"；FR-8.52 的"无 `batch_id` 时不显示并行"在本版收紧为"任何情况都不显示并行，只显示同批 N 个"。
- `truncated` = `summary` 被 300 字符窗口截断，**或** `_tool_content()` 因 `max_tool_result_tokens` 把全文溢出到 blob。注意 `ToolCallResult.to_tool_result()` 的 `truncated` 目前恒为 `false`，所以该字段必须在 `engine` 发送事件时计算，不能直接抄模块返回值。
- `result_ref` = 该次调用持久化的 `role="tool"` 消息 id（`msg:<hex>`），可在 `snapshot.messages[]` 中解析；它是"结果摘要被截断后去哪儿看全文"的唯一稳定指针。若该消息自身也被 core 截断（`max_tool_result_tokens`），`truncated=true` 且全文只在 blob store 中；v1 **不提供** UI 取回 blob 的 RPC，卡片文案必须是"结果已截断"，不得假装可展开全文。

### 5.4 兼容性

新增字段全部 optional，`required` 不变：旧客户端只读 `tool`/`args_digest`/`ok`/`summary`/`code`，行为与升级前逐字一致。`args_preview` 是字符串而非对象，旧 JSON 解析器不会因为嵌套形状变化而崩；新客户端把它当 JSON 文本美化显示。事件体积上限：单条 `turn.tool_call` 增加 ≤512 字节，`turn.tool_result` 增加 ≤80 字节（不含既有 summary）。

## 6. 重生成 / 编辑重发 / 分叉

### 6.1 线性版本模型

| 字段 | 所在 | 含义 | 旧数据 |
|---|---|---|---|
| `root_turn_id` | Turn | 同一用户消息的版本链根；首版 = 自己的 id | 空 → 读取时按 `id` 补齐 |
| `parent_turn_id` | Turn | 被替代的上一版；首版 null | 空 |
| `version` | Turn | 链内 1 起递增 | 空 → 1 |
| `superseded_by` | Turn | 替代它的新 turn；head 为 null | 空 |
| `superseded_reason` | Turn | `regenerate\|edit_resubmit`（`fork` 预留） | 空 |
| `user_message_id` | Turn | 本 turn 回答的用户消息 | 空 → 该 turn 的首条 user 消息 |
| `partial_message_id` | Turn | 中断落库的部分消息（§4） | 空 |
| `superseded_by` | Message | 被编辑重发替代的旧用户消息指向新消息 | 空 |

不变式（core 在事务内保证）：① 每个 root 恰好一个 `superseded_by == null` 的 head；② `version` 连续、无跳号；③ 只有 head 能被 regenerate/edit；④ v1 只允许操作**会话尾部的 root**（`not_tail` 拒绝），因此链是线性的，UI 不需要树。

snapshot 追加（`ui/attach` 注记，`snapshot` 仍是 `object`）：

```json
"turns":[{"turn_id":"t_2","state":"completed","root_turn_id":"t_2","version":1,
          "parent_turn_id":null,"superseded_by":"t_3","superseded_reason":"regenerate",
          "user_message_id":"m_1","partial_message_id":null}],
"messages":[{"id":"m_7","role":"assistant","partial":true,"superseded_by":null,"content":"..."}]
```

旧客户端忽略 `turns[]` 与消息上的两个新键；新客户端不再需要本地"灰化集合"——灰化状态来自 core。

### 6.2 `turn/regenerate`

```
turn/regenerate {session_id, turn_id, client_msg_id}
→ {turn_id, root_turn_id, version, supersedes_turn_id, queue_pos}
errors: NOT_FOUND, INVALID_ARGUMENT, READ_ONLY_SESSION, OVERLOADED, UNSUPPORTED
```

- `turn_id` 必须是**尾部 root 的 head** 且处于终态（`completed|failed|interrupted|cancelled`）。`queued|running|waiting_approval` → `INVALID_ARGUMENT`，`data.reason="turn_active"`；非尾部/非 head → `data.reason="not_tail"|"not_head"`，并带 `data.head_turn_id`。
- 新 turn 复用同一 `user_message_id`：文本从该用户消息重建；RFC-0004 的 parts 落地后按 parts 重建（附件仍指向同一 `file_id`，不重新上传）。**不新增用户消息**，因此不会出现第二个用户气泡。RFC-0004 未落地且旧 turn 含非 text part 时 → `UNSUPPORTED`（`data.capability="parts"`），不静默丢附件（turn 行用 `user_parts` 记住入队时的非 text part 类型，重启后仍能拒绝）。
- `client_msg_id` 必填，作为幂等键：同 id 重复调用返回同一个新 turn，不产生第二个版本（并发双击只有一个赢家，另一个读到赢家）。幂等命中时还要校验该 turn 的 `parent_turn_id == turn_id`；若这个 `client_msg_id` 已被别的操作占用（例如同 id 的 `ui/submit`）→ `INVALID_ARGUMENT` + `data.reason="client_msg_id_conflict"`，不得静默返回无关 turn。
- 旧的 assistant 消息（含 `partial=true` 的那条）保留，不删不改；旧 turn 只写 `superseded_by`/`superseded_reason`。
- 工具是否重跑：本 RFC 不引入缓存，新 turn 按普通 turn 走 `engine`，因此**会重新执行**工具调用。是否允许对已产生副作用的工具重跑列为未决 Q2；若裁定"禁止"，只在 `turn/regenerate` 入口加 `CONFIRM_REQUIRED` 前置，不改本节的字段与事件。

### 6.3 `message/edit_resubmit`

```
message/edit_resubmit {session_id, message_id, content, client_msg_id}
→ {turn_id, message_id (新), supersedes_turn_id, root_turn_id, version, queue_pos}
errors: NOT_FOUND, INVALID_ARGUMENT, READ_ONLY_SESSION, OVERLOADED, PAYLOAD_TOO_LARGE
```

- 只接受 `role="user"` 的消息，且必须是**会话最后一条用户消息**（尾部 root 的 `user_message_id`）。编辑更早的消息等于隐式分叉，v1 拒绝：`data.reason="not_tail"`；该消息已有 `superseded_by` → `data.reason="already_superseded"`，不得二次编辑。
- 消息不可变：**不 UPDATE 旧行**。新建一条 user 消息（新 id、新 content），旧消息写 `superseded_by` 指向新消息；旧 turn 写 `superseded_by` 指向新 turn、`reason="edit_resubmit"`；新 turn 的 `user_message_id` = 新消息，`version` = 旧 + 1。
- `content` 的形状归 RFC-0004；RFC-0004 未落地时只接受单个 `{"type":"text","text":...}`。v1 要求 text 非空；"只有附件没有文本"的编辑等 RFC-0004 的 parts 落地后再开放。
- 旧 turn 必须终态；`turn_active` 拒绝，客户端应先 `ui/queue.cancel` 或 `ui/interrupt`。
- `client_msg_id` 必填、幂等；重复调用返回同一个新 turn 与新消息 id；冲突校验同 §6.2（`parent_turn_id` 必须是 `supersedes_turn_id`）。

### 6.4 事件、顺序与幂等

新增两个事件（`required` 见下）：

```json
{"type":"turn.superseded",
 "payload":{"turn_id":"t_2","superseded_by":"t_3","reason":"regenerate",
            "root_turn_id":"t_2","version":2}}
{"type":"message.edited",
 "payload":{"message_id":"m_1","session_id":"s_1","superseded_by_message_id":"m_9",
            "new_turn_id":"t_4","text":"改过的原文","edited_at":1763200000000}}
```

- `turn.superseded` 的 `required`：`[turn_id, superseded_by, reason]`；`reason` 取 `regenerate|edit_resubmit`（`fork` 预留，v1 不发）。它**不携带 `state`**，也不改变 turn 状态——"被替代"是终态之上的元数据边，不是第八种状态。
- `message.edited` 的 `required`：`[message_id, session_id, superseded_by_message_id, new_turn_id, text, edited_at]`。名字叫 `edited`，但**消息不可变**：它表示旧消息被新消息替代，不是 UPDATE 旧行。`text` 是新文本投影（≤32k），让在线端不必重新 attach 就能更新气泡；RFC-0004 落地后可再加可选 `parts`。`text` 为空只在 `parts` 存在时允许（v1 不允许）。
- 顺序（seq 严格递增，同一事务）：编辑重发 `message.edited` → `turn.superseded` → `turn.queued`；重新生成 `turn.superseded` → `turn.queued`。客户端永远不会先看到新 turn 再知道旧 turn 被替代。
- 幂等：`turn.superseded` 按 `(turn_id, superseded_by)` 去重；`message.edited` 按 `superseded_by_message_id` 去重；重复 replay 不产生第二次灰化/第二个气泡。
- `turn.queued` 追加可选字段：`root_turn_id`、`version`、`parent_turn_id`、`user_message_id`、`reason: submit|regenerate|edit_resubmit`。新客户端据此把新 turn 挂到已有用户气泡下（同一 `user_message_id` 只渲染一个气泡），旧客户端忽略。
- regenerate 的 `turn.queued` **不新建用户消息**（沿用既有 `user_message_id`）；submit/edit 的 `turn.queued` 与新消息同事务。三种 `reason` 都走 persist-before-broadcast。

**与 C-06 终态注记的冲突**：`turn.interrupted.notes` 写"No turn.* event may follow this one for that turn"，而中断后重新生成必然要在其后发 `turn.superseded`。解决方式同样是追加而非改写：`x-vade.appends` 登记豁免——`turn.superseded` 是元数据事件，允许跟在任一终态事件之后；除它之外，原规则不变。`turn.failed`/`turn.cancelled` 后同样适用。

**为什么不用 `turn_states` 的 `superseded`**：状态是"turn 正在做什么"，`completed` 的 turn 仍可能被替代；把"被替代"塞进枚举会让 `turn.completed.state` 的取值随"后来有没有人点重新生成"而变化，破坏"终态事件一旦发出就不改"的既有承诺，旧客户端还会把未知状态渲染成运行中。用独立事件 + `superseded_by` 指针，语义与兼容都更干净。

### 6.5 `session/fork`（v1 不做，仅预留签名）

```
session/fork {session_id, from_turn_id?, from_seq?, title?}
→ {session_id, forked_from, from_seq, messages_copied, turns_copied}
errors: NOT_FOUND, INVALID_ARGUMENT, UNSUPPORTED, OVERLOADED
```

将来语义：复制 `from_seq`（缺省为当前 `last_seq`）之前的 messages/turns 到一个新 session，`forked_from` 记录源；`from_turn_id` 与 `from_seq` 二选一，同给时以 `from_turn_id` 为准（`from_seq` 取该 turn 的终态 seq）。新 session 与原 session 不共享可变状态；附件按 `file_id` 引用并增加引用计数（否则 RFC-0004 的 blob GC 会删掉被分叉引用的文件）。**v1 不把该方法加入 C-05**：FR-8.32 明确"v1 之后"，UI 无入口；客户端调用得到 JSON-RPC `-32601 method not found`，core 不返回 `UNSUPPORTED`（避免把"未实现"伪装成"已定义但能力不足"）。上限（消息数/字节/深度/引用计数）见未决 Q3。

### 6.6 v1 的边界：只做"新 turn + 旧 turn 标 superseded"

- UI 只渲染 head；`superseded_by != null` 的 turn 灰化、不可展开为主视图；旧用户消息（`superseded_by != null`）不再作为活动气泡，可折叠为"已编辑的原消息"。刷新/换端后状态来自 `snapshot.turns[]`，不靠本地标记。
- 没有版本翻页、没有分支切换、没有"选一个版本继续"。版本号只用于排序与幂等，不显示为 `2/2`。
- **上下文重建**：`engine` 组装 history 时必须排除被替代的内容，否则新 turn 会看到自己的旧回答。规则：① user 消息只保留 `superseded_by == null` 的；② assistant/tool 消息只保留其 turn 为 head 的。重新生成复用的那条 user 消息 `superseded_by == null`，所以保留；旧回答及其工具结果被排除。该过滤放在 `context.fit()` 之前（`core/vade/loop/context.py` 的入参是 `Sequence[Message]`，过滤后再传入）。
- 导出（FR-8.33）默认只导出 head 链；可选"包含已替代版本"开关属客户端，不在本 RFC。
- `session.turn`（C-14）会出现旧 turn 与新 turn 两条，这是真实历史；v1 不追加 superseded 字段，evolve 消费者按需自行过滤。
- **旧客户端降级**：不认识 `turn.superseded`/`message.edited` 的端在重新生成/编辑重发后会同时显示旧回答与新回答（编辑场景还会显示两条用户消息）；它不会崩溃，但也不会灰化。这是可接受的降级：触发新功能的端必然是已升级端，未升级端在下次 attach 看到的是完整快照而非坏数据。发布说明要把这条写清楚，避免把"旧端看到两条"误报为重复消息 bug。

## 7. 流式多段（P2）

### 7.1 字段

`turn.delta` 追加两个可选字段：

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `part_index` | int ≥0 | 0 | 本 turn 内的段序号，从 0 起；同一段的所有 delta 相同 |
| `kind` | `text\|notice\|reasoning\|tool_args` | `text` | 段的语义类型；v1 core **只发** `text` 与 `notice` |

### 7.2 语义

- `part_index` 单调不减；新段第一帧带新 index。段与段之间不插入分隔符，`text` 仍是增量。
- 实现上"新段"= 新的 assistant 文本段：`engine` 每进入一个模型步的文本输出就分配下一个 index；`max_steps` 的提示单独成段（`kind="notice"`），不再混进正文段。
- 空段不产生 delta；允许 index 跳号（某步没有文本就没有段），客户端按"出现新 index 就开新段"处理。
- `turn.completed` 关闭所有段；`turn.interrupted` 同样关闭，不再补 delta。

### 7.3 兼容

- 旧客户端忽略两个字段，按 seq 顺序拼接所有 `text`，结果与升级前逐字相同（分段不改变字符流）。
- 新客户端遇到未知 `kind` 按 `text` 渲染，不得丢弃；遇到小于当前 index 的 delta（不应发生）忽略该帧。
- v1 core 不得向任何客户端发 `reasoning`/`tool_args`：当前没有能力协商，旧客户端会把它们拼进正文，等于泄漏推理或工具参数。这两个值只作为枚举预留，等 `ui/attach` 有 capability 字段后再启用。

### 7.4 为什么不是 `turn.part.started`

新增事件要多一次 seq、多一个客户端分支、多一条 replay 兼容规则，而它携带的信息只有一个整数——`part_index` 已经在 delta 上。只有当"段"需要携带非文本载荷（图片工件、可折叠的 markdown 块）时，才值得引入 `turn.part.started`/`turn.part.completed`；那是 P2 的下一阶段，不在本 RFC。多段与 Markdown 增量渲染的关系见未决 Q4：段是**语义**边界，不是 Markdown 块边界。

## 8. 单条长度上限

- **上限**：单条消息所有 text part 的 code point 之和 ≤ **32,000**；单个 text part 也 ≤32,000（RFC-0004 提案中的 `text: 1..32000` 不变）。空文本 + 附件是否合法由 RFC-0004 的 parts 规则决定，本 RFC 不改变 `_text_of()` 的既有行为；本上限只约束 text 总和。
- **计数单位**：Unicode code point（Python `len(str)`、Rust `chars().count()`）。不做 NFC/NFKC 归一化——归一化会改用户原文。客户端不得用 UTF-16 `.length` 判断：16,500 个非 BMP emoji 在 JS 里是 33,000，会被误判超限；验收 A14 专门测这条。
- **校验顺序**（`ui/submit`、`message/edit_resubmit` 共用）：① 会话存在与写权限（`READ_ONLY_SESSION`）；② content 形状；③ 逐 part 文本长度；④ 文本总和；⑤ 附件引用与模型能力（RFC-0004）。任一步失败都**不创建** turn/message 行，不产生半个 turn。
- **错误**：`PAYLOAD_TOO_LARGE`（C-04 既有，HTTP 413，`retriable=false`），`error.data`：

```json
{"field":"content[0].text","limit":32000,"actual":32001,"unit":"codepoints"}
```

总和超限时 `field="content"`、`actual=总和`。不新增错误码；不复用 `INVALID_ARGUMENT`（它是 400，且客户端会显示成"参数错误"而不是"太长"）。
- **助手输出不设上限**：模型可能生成超过 32k 的回答；这是 core 的输出，不是用户输入，拒绝它只会丢回答。UI 按 FR-8.45 折叠（>4000 字符或 >24 行），导出保留全文。
- **配置**：本 RFC 不新增 C-18 键。32,000 是协议常量；RFC-0004 落地后以 `[attach].text_max_chars=32000` 为唯一可调点，两者数值必须一致，`check_contracts.py` 加一条断言。若 RFC-0004 未落地，core 硬编码 32,000 并在发布说明写明。

## 9. 契约变更

### 9.1 汇总（15 项：追加 8、追加并注记 3、纯注记 1、无变更 3；破坏性 0）

| # | 契约 | 改动 | 判定 | 兼容策略 |
|---|---|---|---|---|
| 1 | C-06 `turn.delta` | 追加 `part_index`、`kind` | **追加** | 缺省 0/text；旧端顺序拼接不变 |
| 2 | C-06 `turn.tool_call` | 追加 `args_preview`、`args_preview_mode`、`args_preview_redacted`、`args_preview_truncated`、`started_at`、`batch_id` | **追加**（注记级收窄，见 9.3） | `required` 不变；旧端只读 digest |
| 3 | C-06 `turn.tool_result` | 追加 `duration_ms`、`truncated`、`result_ref` | **追加** | 缺省即省略，不显示占位 |
| 4 | C-06 `turn.interrupted` | 追加 `partial_message_id`、`partial_chars` | **追加**（注记级覆盖，见 9.3） | 旧端保留 delta 缓冲 |
| 5 | C-06 `turn.queued` | 追加 `root_turn_id`、`version`、`parent_turn_id`、`user_message_id`、`reason` | **追加** | 旧端忽略，仍按 `client_msg_id` 去重 |
| 6 | C-06 新事件 `turn.superseded` | 见 §6.4 | **追加** | 旧端忽略未知类型，推进 `last_seq` |
| 7 | C-06 新事件 `message.edited` | 见 §6.4 | **追加** | 同上；`text` 让在线端直接更新 |
| 8 | C-06 规则/注记 | ① 未知事件类型必须忽略；② `turn.superseded` 豁免"终态后不得再有 turn.*"；③ `turn.interrupted` 的"only from delta"收窄；④ `turn.tool_call` 的"Only the digest"收窄为"未脱敏完整参数不进该事件" | **注记追加**，原字节不改 | 旧端行为不变；新端按追加块读 |
| 9 | C-05 新方法 `turn/regenerate` | 见 §6.2 | **追加** | 旧端不调用；未知方法不影响既有方法 |
| 10 | C-05 新方法 `message/edit_resubmit` | 见 §6.3 | **追加** | 同上 |
| 11 | C-05 `ui/attach` 注记 | `snapshot.turns[]`、`messages[].partial`、`messages[].superseded_by` | **追加注记** | `snapshot` 仍为 `object`；旧端忽略新键 |
| 12 | 消息/Turn 存储 | `messages.partial`、`messages.superseded_by`、`turns.root_turn_id/parent_turn_id/version/superseded_by/superseded_reason/user_message_id/partial_message_id/user_parts` | **追加列**（Python API 尾参默认值） | 旧库读默认值；`to_dict()` 只加键 |
| 13 | C-04 `errors.json` | **不改**：复用 `INVALID_ARGUMENT`/`NOT_FOUND`/`READ_ONLY_SESSION`/`OVERLOADED`/`PAYLOAD_TOO_LARGE`；`CONFIRM_REQUIRED` 仅为 Q2 预留，v1 不用 | **无变更** | 码表与 Rust 枚举零改动 |
| 14 | C-06 `turn_states` | **不改**：不增 `superseded`（理由见 §6.4） | **无变更** | 旧端不认识的状态问题不存在 |
| 15 | C-05 `session/fork` | **不加**：v1 不注册；签名仅 §6.5 预留 | **无变更** | 调用得 `-32601`，不是 `UNSUPPORTED` |

### 9.2 C-06 追加块的形状

在 `schema/events.json` 根部追加（不改任何既有条目的字节）：

```json
"x-vade": {
  "append": { "...C-14 既有对象，字节不动..." },
  "appends": [
    {"contract":"C-06","version":"1.1.0","owner":"C3",
     "doc":"docs/rfc/RFC-0006-turn-semantics.md",
     "adds":["turn.superseded","message.edited"],
     "fields":{"turn.delta":["part_index","kind"],
               "turn.tool_call":["args_preview","args_preview_mode","args_preview_redacted",
                                 "args_preview_truncated","started_at","batch_id"],
               "turn.tool_result":["duration_ms","truncated","result_ref"],
               "turn.interrupted":["partial_message_id","partial_chars"],
               "turn.queued":["root_turn_id","version","parent_turn_id","user_message_id","reason"]},
     "overrides":["turn.interrupted.notes: partial 同时落库，delta 不再是唯一来源",
                  "turn.tool_call.notes: 未脱敏完整参数仍只走 turn.approval_required；该事件另带脱敏预览",
                  "terminal rule: turn.superseded 允许跟在任一终态事件之后"],
     "rewrites":"none"}
  ]
}
```

**键名冲突的处理**：`events.json` 现在已有 `x-vade.append`（**单数**对象，C-14 用）。本 RFC **不改这个对象**，而是新增兄弟键 `x-vade.appends`（数组），供后续多个 RFC 依次追加。若复用单数键就必须重写 C-14 的对象，违反"只追加"；`check_contracts.py` 需同时读 `append` 与 `appends`，并断言 `appends[].rewrites == "none"`。`schema/rpc.json` / `config.schema.json` 没有既有 `x-vade` 根键，按 RFC-0004 的 `x-vade.appends` 约定写即可。

实现注记：`required` 数组一律不动；新字段在 `payload` 里以可选键出现；`turn.superseded`/`message.edited` 必须与消息/turn 行在同一事务内落库，再广播（沿用 `manager.emit()` 的 persist-before-broadcast 顺序）。

### 9.3 每条判定说明

- **第 2/4 行是"追加 + 注记级收窄"，第 8 行是纯注记**：C-06 是冻结文件，`notes` 字符串不能改写；但"Only the digest"与"only from turn.delta"是**事实陈述**，v1.1 起事实变了。用 `x-vade.appends.overrides` 登记新读法，原字节保留，审计可追溯。这不是破坏性变更：旧客户端读到的字段与行为完全不变。
- **第 6/7 行是新事件**：C-06 没有"客户端声明支持哪些事件"的协商字段，因此必须约定"未知 `type` 忽略并推进 `last_seq`"。该约定本身也是追加规则，不能假设所有旧实现已经这么做；发布说明要把这条列为升级检查项。
- **第 13 行**：不新增 `SUPERSEDED`/`NOT_HEAD` 之类的码。`INVALID_ARGUMENT` + `data.reason` 足以区分 `not_tail`/`not_head`/`turn_active`，新增码要同步 Rust 枚举、HTTP 映射与 33 条码表，收益只是少一个 `if`。
- **第 14 行**：`superseded` 不是状态（§6.4）；加进枚举会让终态事件的含义依赖后续操作，破坏 C-06 "state is exactly ..." 的承诺。
- **第 15 行**：`session/fork` 若现在进 C-05 而不实现，客户端会得到 `UNSUPPORTED`（501），看起来像"能力不足但已支持"；不注册方法得到 `-32601`，语义更诚实。

## 10. 验收

A1–A15 全部要求真实执行；截图与手写记录不算证据。

| # | 断言 | 承载 |
|---|---|---|
| A1 | 流式中途 `ui/interrupt` → `turn.interrupted.partial_message_id` 非空、`partial_chars == len(拼接后的 delta)`；另一端立即 `ui/attach`，snapshot 中该消息 `partial=true`、内容逐字一致 | `core/tests/session/test_interrupt_partial.py` |
| A2 | 在线端先按 delta 渲染再收 `turn.interrupted`：气泡只出现一条，内容等于落库消息；replay 同一事件两次不产生第二条 | 同上 |
| A3 | 首个 delta 前中断 → `partial_message_id=null`、`partial_chars=0`，snapshot 无空 assistant 消息 | 同上 |
| A4 | 工具执行中中断 → 已完成的 tool 消息仍在；未回结果的卡在 `turn.interrupted` 后显示"结果未知"，5s 内不转圈；不产生空 assistant 部分消息 | `core/tests/session/test_interrupt_tools.py` |
| A5 | 一次 turn 两个调用：`args_preview` ≤512 字节、`args_digest` 不变、`started_at` 单调、`batch_id` 相同；`duration_ms≥0`；结果 >300 字符时 `truncated=true` 且 `result_ref` 能在 snapshot 中解析到 `role=tool` 消息 | `core/tests/loop/test_tool_card_fields.py` |
| A6 | args 含 `password`、`api_key`、`Authorization: Bearer ...`、`sk-...`、32+ hex：`args_preview` 用 §5.2 正则重扫 0 命中；`risk=high` 时 `mode="typed"`、字符串值全为 `<str:N>`；原始秘密不出现在事件与日志 | `core/tests/loop/test_args_redaction.py` |
| A7 | 旧客户端 fixture：新字段全部存在时只读旧键，渲染结果与基线逐字相同；收到 `turn.superseded` 未知类型不报错且 `last_seq` 前进 | `core/tests/session/test_event_compat.py` |
| A8 | 完成后 `turn/regenerate`：旧 turn `superseded_by` 指向新 turn、`version=2`、`user_message_id` 不变；snapshot 只有一个用户气泡；`turn.superseded` 的 seq 小于新 `turn.queued` | `core/tests/session/test_regenerate.py` |
| A9 | 同 `client_msg_id` 连调两次 `turn/regenerate` → 只产生一个新 turn，第二次返回同一 `turn_id`；并发双击同样 | 同上 |
| A10 | 对 running turn regenerate → `INVALID_ARGUMENT` + `reason="turn_active"`；对非尾部 turn → `reason="not_tail"` + `head_turn_id`；均不产生新行 | 同上 |
| A11 | 编辑最后一条用户消息：新消息 id 不同、旧消息 `superseded_by` 指向新消息、旧 turn `reason="edit_resubmit"`；另一端在线收到 `message.edited` 后气泡文本更新且不重新 attach；编辑非尾部消息 → `not_tail` | `core/tests/session/test_edit_resubmit.py` |
| A12 | 重新生成后杀进程重启 → snapshot `turns[]` 的 `superseded_by`/`version` 仍在；旧 turn 灰化、新 turn 在下方；不依赖客户端本地状态 | `core/tests/session/test_superseded_persist.py` |
| A13 | 三段 delta（0,0,1,1,2）：新端渲染 3 段、边界正确；旧端拼接文本与串行全量逐字相同；`part_index` 从不下降 | `core/tests/loop/test_delta_parts.py` |
| A14 | 32,000 个汉字通过；32,001 → `PAYLOAD_TOO_LARGE`、`data.field="content[0].text"`、`limit=32000`、`actual=32001`，且无 turn/message 行；16,500 个非 BMP emoji 通过（UTF-16 长度 33,000 > 32,000，若按 UTF-16 计数会误拒，按 code point 计数为 16,500） | `core/tests/session/test_text_limit.py` |
| A15 | 引用消息被编辑：`message.edited` 到达后引用块仍显示快照文本并标"原消息已变更"；被引用消息的 `superseded_by` 可从 snapshot 读到 | `core/tests/session/test_quote_after_edit.py` |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿；`python tools/check_contracts.py` 需新增断言（§13），在实现批次里变绿。

## 11. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| A. `turn.interrupted` 只带 `partial_text` | snapshot 路径仍丢文本；要补就得 snapshot 再带一份，两处真相，且每次中断把最多 32k 永久写进事件表（§4.1） |
| B. 新增 `turn_states.superseded` | "被替代"是终态之上的边，不是状态；会让 `turn.completed.state` 的取值随之后的操作变化，旧端还会把未知状态渲染成运行中（§6.4） |
| C. 新增 `turn.part.started` | 多一个事件、一条 seq、一条 replay 兼容规则，只为一个整数；`part_index` 已在 delta 上（§7.4） |
| D. `message.edited` 只带 id、不带 `text` | 在线端必须重新 attach 才能看到新文本，编辑重发在多端会出现"旧文本停留到下次刷新"；一次编辑一条事件，32k 成本可接受（§6.4） |
| E. v1 直接做 `session/fork` + 分支切换 | 改存储、snapshot 形状、上下文重建与附件引用计数；FR-8.32 已明确 v1 之后（§6.5） |
| F. 只做单 part 32k、不设消息总和 | 20 个 part × 32k 仍是合法输入，FR-8.45 的"单条上限"落空（§8） |
| G. 所有 risk 都发完整参数预览 | 完整参数进 replay/导出/日志，等于给 `turn.approval_required` 开第二条泄密路径；高风险只给结构（§5.2） |
| H. `regenerate` 重放上次工具结果（缓存） | 会伪造副作用：模块实际没跑，模型却收到"成功"；且审批与审计无法对账。本 RFC 不采用缓存；若最终裁定禁止重跑，只在入口加确认，不改字段（Q2） |

## 12. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **`partial_text` 是否也进 snapshot 的便捷字段？** | 倾向否：snapshot 已有 `messages[].partial`，再加 `turns[].partial_text` 是同一文本的第三份。若移动端解析 messages 成本高，可加只读投影，但要明确"以消息为准" |
| Q2 | **重生成是否允许重跑有副作用的工具？** | 倾向"重跑 + 前置确认"：旧 turn 含 `risk!=low` 的调用时返回 `CONFIRM_REQUIRED`，UI 二次确认后原样重跑；不做结果缓存（§11 H）。需产品拍板 |
| Q3 | **分叉上限？** | 倾向单次 ≤1,000 条消息 / ≤8 MiB 文本、深度 ≤3、附件按 `file_id` 引用计数；超限 `OVERLOADED`。需与 RFC-0004 的 blob GC 一起定 |
| Q4 | **多段与 Markdown 增量渲染的关系？** | 倾向"段是语义边界，不是 Markdown 块边界"：客户端仍只重解析最后一个未闭合块，段切换时强制收尾上一段。若把段当块，代码围栏跨段会渲染错 |
| Q5 | **编辑窗口只到"最后一条用户消息"够不够？** | v1 定案为只允许最后一条（否则隐式分叉）。若用户强烈需要编辑倒数第二条，只能先删后续 turn（另案），不能在本 RFC 偷偷放开 |
| Q6 | **`version` 是否对 UI 可见？** | 倾向只在内部与导出使用，v1 不显示 `2/2`；若将来做版本翻页，再把它映射成 `‹ 1/2 ›`。当前 UI 只显示 head |
| Q7 | **`started_at` 与 `duration_ms` 之间的审批等待是否要单列？** | 倾向暂不列：`duration_ms` 已排除审批等待，卡片显示"执行耗时"；若用户反馈"3.2s 对不上"，再加 `approval_wait_ms`（属于追加字段，不影响本 RFC 结构） |

## 13. 影响面与后续

实现阶段要动的文件：

- `schema/events.json`：§9.2 的 `x-vade.appends` 块 + 两个新事件条目（不改既有字节）。
- `schema/rpc.json`：`ui_to_core` 增 `turn/regenerate`、`message/edit_resubmit`；`ui/attach` 注记 snapshot 新键。
- `core/vade/session/models.py`：`Message.partial/superseded_by`、`Turn` 的七个版本/部分字段、`to_dict()` 加键（尾参默认值，旧构造零改动）。
- `core/vade/session/sqlite_store.py`：`ALTER TABLE` 追加列与读写映射；`turns` 的 `root_turn_id` 等旧行回填逻辑（读取时 coalesce，不做批量迁移）；新增"turn + message + event 同事务"的写入入口（§4.2 原子性、§6.4 顺序）。
- `core/vade/session/queue.py`：`interrupt()` 读取 `partial_provider` 并把部分消息传给 `_finalize`；`_finalize` 的 `messages` 参数已有，无需改签名。
- `core/vade/session/manager.py`：`emit()` 维护 `_partial` 累加器；`submit` 的 `turn.queued` 带新字段；新增 `regenerate()`/`edit_resubmit()`（或放入独立 `turns.py`），同一事务写 turn/message/event。
- `core/vade/loop/engine.py`：`tool_call_payload` 增 `args_preview`/`started_at`/`batch_id`；`tool_result` 增 `duration_ms`/`truncated`/`result_ref`（`result_ref` 需要先拿到持久化消息 id）；`turn.delta` 增 `part_index`/`kind`。
- `core/vade/loop/context.py`（调用方）：按 §6.6 过滤 superseded 内容后再 `fit()`。
- 测试：§10 列出的 11 个测试文件；`tools/check_contracts.py` 增断言（新事件存在、`required` 未变、`turn.tool_call` 新字段、两个新方法、`session/fork` 不存在）。

**不动**：`schema/errors.json`（§9.1 第 13 行）、`schema/config.schema.json`（§8 末尾）、`schema/events.json` 的 `turn_states`（第 14 行）、`core/vade/policy/*`（审批语义，N2）、`docs/design.md` 的 §2.5/§2.6（状态机文字不变；若实现时发现措辞需要补，走方案层 AMEND，不代改）。

**后续（不在本 RFC）**：工具结果全文取回（`result_ref` → blob 的 UI RPC，属 R8 的下一阶段）；`session/fork` 与附件引用计数（Q3）；`reasoning`/`tool_args` 段的 capability 协商（§7.3）；引用回复的 `quote` part（RFC-0004 的 parts 扩展）；ask-user 的 `waiting_input` 与 `turn.input_*`（RFC-0008，AMEND-22 已预留编号）。

**参考**：`docs/requirements.md` §8.21 FR-8.30/8.31/8.32/8.35/8.51/8.52、§8.21.2 R7/R8/R9、§8.22.2 R6/R8、§8.22.5；`research/ui-02-messages-input-attachments.md` §3.2（状态机）、§3.3（停止与重生成）、§3.4（工具卡）、§3.9 C4/C5/C6/C10/C14；`schema/events.json` 的 `turn.*` 现有条目与 `notes`；`schema/rpc.json` 的 `ui_to_core`；`core/vade/session/queue.py::_finalize/interrupt`；`core/vade/session/manager.py::emit/snapshot/submit`；`core/vade/loop/engine.py` 的 delta/tool 发送点与 `_summary`/`_tool_content`；`core/vade/router/dispatch.py::ToolCallResult`；RFC-0003、RFC-0004（格式与 parts 约束）。
