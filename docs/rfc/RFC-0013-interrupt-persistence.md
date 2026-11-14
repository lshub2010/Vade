---
rfc: RFC-0013
title: 中断留痕落库（R9）：partial 消息进 snapshot，删掉「只能从 turn.delta 重建」的旧注记
status: proposed
date: 2026-11-14
proposer: 编排（依据 `docs/requirements.md` §8.21.2 R9 / FR-8.35 的「需 RFC-NNNN（待编号）· `turn.interrupted` 携带 `partial_text` 或落库」标注）
affects: C-06 `schema/events.json`（`turn.interrupted.notes` 就地改写：删除「reconstructable only from turn.delta」句）；C-05 `schema/rpc.json`（`ui/attach` result 注记补 `snapshot.messages[].partial`，append 1.1.0 → 1.2.0）；`core/vade/session/`（存储/模型/中断落库路径**复用 RFC-0006 §4 已有实现**，本 RFC 补契约缺口并锁验收测试）
related: RFC-0006（§4 已选定「落库 assistant 消息」方案并在 `core/vade/session/` 落地：`queue.py::_partial_message` / `_finalize_interrupted`、`messages.partial` 列、`Turn.partial_message_id`；本 RFC 不改其机制）；RFC-0008（C-06 顶层 version 2.0.0 的归属）；FR-8.28（停止后「已生成 N 字」的呈现）；FR-8.35（本 RFC 的需求源）
type: 契约注记改写（C-06 notes）+ C-05 注记/append 补齐；不改 C-04 错误码（仍 34）、不改 `turn_states`（仍 7 值）、不改 delta 流式机制
stage: 本 worktree 同批落地：core 侧中断落库复用既有实现 + schema 两处契约修订 + 验收测试 13 条（见 `core/vade/session/RESULT-rfc0013.md`）
---

# RFC-0013 · 中断留痕落库（R9）

## 1. 摘要

FR-8.35 要求「中断后已生成文本保留并**在其他端可见**」，验收句是「中断后在另一端 attach，已生成的 N 字仍可见」。目前（HEAD `e200772`）协议与实现是**半拉子**：

- 存储侧**已经落库**——RFC-0006 §4 的实现已在 `core/vade/session/`：中断收尾时 `_finalize_interrupted()` 把 `partial_text(turn_id)` 写成一条 `partial=true` 的 assistant 消息，`turn.interrupted` 只带 `partial_message_id` / `partial_chars` 指针，`messages` 表有 `partial` 列（可重入迁移），snapshot 经 `Message.to_dict()` 已输出 `partial` 字段；
- 但**契约层还说反话**——C-06 `turn.interrupted` 的 `notes` 仍写着「an interrupted assistant's partial text is reconstructable only from turn.delta events」，这句话自落库实现存在起就是**假的**；C-05 `ui/attach` 的 result 注记也从未声明 `snapshot.messages[].partial` 这个键，客户端按契约实现就看不到它。

本提案把 R9 收口：**契约说真话，验收锁测试**。C-06 注记就地改写（删掉假句，写明落库真相）；C-05 注记补 `partial` 键并在 `x-vade.appends` 登记 RFC-0013 块（1.1.0 → 1.2.0，追加级）；用 ≥12 条测试锁住「另一端 attach 可见」这条 FR-8.35 验收句。

| # | 决定 | 一句话理由 |
|---|---|---|
| 1 | 中断留痕 = **落库一条 `partial=true` 的 assistant 消息**（复用 RFC-0006 §4 方案与实现，不另起炉灶） | snapshot 只读 `messages`；落库后新端 attach、导出、搜索、上下文只有一份真相 |
| 2 | `turn.interrupted` 继续只带**指针**（`partial_message_id`/`partial_chars`），**不带文本** | 事件带文本 = 第二份真相，重放去重立刻变成客户端的活；指针 + 落库行让 dedupe 按 id 幂等 |
| 3 | C-06 `turn.interrupted.notes` **就地改写**：删除「only from turn.delta」句，写明「已落库为 `partial=true` 消息、新端 attach 可见」 | 冻结文件里留一句假注记比改写字节更危险——按旧注记实现的客户端会**主动丢弃**落库消息；RFC-0006 的 `overrides` 块保留作审计痕迹 |
| 4 | C-05 `ui/attach` result 注记补 `snapshot.messages[].partial`（bool，默认 false），`x-vade.appends` 登记 RFC-0013 块 **1.2.0** | 字段已在 wire 上（RFC-0006 实现），但契约从未声明；「追加字段，非破坏」按 append 权走 |
| 5 | 存储模型不新增列/字段：`messages.partial`（INTEGER DEFAULT 0）与 `Turn.partial_message_id` **沿用 RFC-0006 已落地的形状** | 列已存在、迁移已可重入（`_APPENDED_COLUMNS`）、加载已回读；任务书里的 `interrupted_partial_message_id` 命名让位给既有 `partial_message_id`——改名的破坏面（事件键、DB 列、C-06 append 记录）买不到任何收益 |
| 6 | 不改 `turn_states`（7 值）、不改错误码（34）、不改 delta 流式机制、不动 `core/vade/loop/` | R9 是「留痕可见」问题，不是状态机或流式问题；`interrupted` 状态已存在 |

## 2. 动机与证据

### 2.1 FR-8.35 的原文与「不得标通过」注记

`docs/requirements.md` FR-8.35 [M]（AM21-A 最后一行）：

> **队列与中断的呈现**：会话内显示排队位置（"排队中 #2"）与"取消排队"；队首等待审批时明示"等待确认"；中断后已生成文本保留并**在其他端可见** | 并发提交 3 条 → 位置依次 1/2/3，取消第 3 条后剩余位置前移；**中断后在另一端 attach，已生成的 N 字仍可见**。**当前 C-06 注记"partial 只能从 `turn.delta` 重建"、新端 attach 走 snapshot 不重放 delta，故此项在 RFC 落地前记为已知缺陷，不得标通过** | **需 RFC-NNNN（待编号）· `turn.interrupted` 携带 `partial_text` 或落库**（C-06 变更）

需求册 §8.21.2 R9 同义重复：「`turn.interrupted` 携带 `partial_text` 或落库，snapshot 可恢复 | FR-8.35 | C-06 注记"partial 只能从 delta 重建"」。

### 2.2 C-06 注记仍是假话

`schema/events.json` `turn.interrupted.notes`（HEAD `e200772` 原文，逐字）：

> Emitted after tools/cancel is fired; core does not wait for the module. No turn.* event may follow this one for that turn; **an interrupted assistant's partial text is reconstructable only from turn.delta events**. RFC-0005 append: optional `usage`, usually `source="none"` with zero counts because the provider had not sent a final usage yet — core does not estimate.

加粗句与存储现实冲突（见 §2.3）。RFC-0006 落地时选择不改冻结注记字节，只在 `x-vade.appends[0].overrides` 登记「partial 同时落库为 partial=true 的 assistant 消息，turn.delta 不再是唯一来源」。结果是：**读原注记的客户端**（不看 append 块的、或三端里先写好的）会把 snapshot 里的 partial 消息当非法数据丢弃——假注记从「过时」升级成了「有害」。本 RFC 按编排序的指令就地改写，`overrides` 保留作历史。

### 2.3 存储侧已经落库（复用清单）

grep `partial_message_id` 与通读 `core/vade/session/` 的结论——**落库链路在 HEAD 已完整**，本 RFC 全部复用，不重写：

| 环节 | 现状 | 位置 |
|---|---|---|
| 文本累积 | `manager._partial[turn_id]` 随 `turn.delta` 累积；`partial_text(turn_id) -> str` 已存在 | `manager.py`（`_record_delta`/`partial_text`），并作为 `partial_provider` 传给 `TurnQueue` |
| 中断收尾 | `_finalize_interrupted()` 调 `_partial_message()`：文本非空才建 `Message(role="assistant", content=text, partial=True)`，空文本不建消息（不留空气泡） | `queue.py` §4.2 |
| 原子性 | 终态 turn 行 + partial 消息 + `turn.interrupted` 事件**同一事务**（`_emit_batch → store.persist_batch`） | `queue.py` / `sqlite_store.py` |
| 指针 | `partial_message_id = "msg:" + message.id`（与 `result_ref` 同一 `msg:<hex>` 约定）；空文本时为 null | `models.py::message_ref` |
| 存储 | `messages.partial`（INTEGER NOT NULL DEFAULT 0）已在建表与 `_APPENDED_COLUMNS` 可重入迁移里；写入/回读都带该列 | `sqlite_store.py` |
| 模型 | `Message.partial: bool = False`、`Turn.partial_message_id: str \| None` 已存在，`to_dict()` 已输出 | `models.py` |
| snapshot | `manager.snapshot()` 的 `messages = session.message_tree()` → `m.to_dict()`，已含 `partial` 键 | `manager.py` / `rpc.py::ui_attach` |

**缺口只有两处**：§2.2 的假注记，和 §2.4 的未声明字段 + 未锁验收。

### 2.4 C-05 从未声明 `partial`，验收从未被测试锁住

`schema/rpc.json` `ui/attach` 的 result 只有 `{"session_id", "from_seq", "snapshot"}` 三个键，`snapshot` 是松散 `object`；`snapshot.messages[]` 里有哪些键全靠 notes 口口相传（parts、pending_inputs、tasks 都有各自句子），**唯独没有 `partial`**。RFC-0006 的 C-05 append 块只写了一句「messages[].partial … are appended keys」，没说它**何时为 true、新端能不能看到**。同时现有测试（`test_interrupt_partial.py`）锁的是**同一端**的 snapshot/存储路径——「**另一端** attach」这个 FR-8.35 验收句没有对应用例。

## 3. 目标 / 非目标

**目标**

1. G1：任意客户端在中断发生后**新 attach**（`last_seq=null`，走 snapshot、不重放 delta），snapshot 里能看到该 turn 已生成的文本，`partial=true`，内容与被中断端渲染的逐字一致（FR-8.35 验收句）。
2. G2：契约层与存储现实一致：C-06 注记不再说「only from turn.delta」；C-05 注记声明 `snapshot.messages[].partial` 及其语义；两处都以 `x-vade.appends` 的 RFC-0013 块登记（1.2.0）。
3. G3：中断语义不变质：指针事件（不带文本）、空文本不建消息、单事务原子性、tombstone 行为全部维持 RFC-0006 §4 现状，测试锁死。
4. G4：落库可持久：关库重开后 partial 消息仍在（`partial=true` 从列里回读），不是内存对象。
5. G5：与版本链兼容：被中断 turn 可 `turn/regenerate`，旧 turn `superseded_by` 指向新 turn，partial 消息留在旧 turn 上（灰化可见，不消失、不复制）。

**非目标**

1. N1：**不改 `turn_states`**——7 值（queued/running/waiting_input/completed/failed/interrupted/cancelled）一个不动，`interrupted` 已是正式状态，无需新增。
2. N2：**不改 delta 流式机制**——`turn.delta` 的形状、batch/合并、tombstone、`_partial` 累积逻辑一律不动；partial 落库是中断收尾的一次性写，不是流式通道的改动。
3. N3：不给 partial 消息做编辑/续写/重新流式（v1 语义见 §8）。
4. N4：不动 `core/vade/loop/`——引擎只负责流 delta，收尾落库在 `session/queue.py`，边界不变。
5. N5：不改 C-04 错误码（34）、不加配置键、不改三端渲染。

## 4. 方案

中断收尾（`TurnQueue._finalize_interrupted`）已经按 RFC-0006 §4 落库，本 RFC **确认该方案为 R9 的最终实现**并只补契约：

1. **写入**：`interrupt` 命中运行中的 turn → 队列用 `partial_provider`（`manager.partial_text`）取已流出的文本；非空则构造 `Message(id=new_id(), role="assistant", content=text, partial=True)`，与 `state=interrupted` 的 turn 行（`partial_message_id="msg:<id>"`）和 `turn.interrupted` 事件（`partial_message_id` + `partial_chars=len(text)`，**不带 text**）在同一事务提交。空文本（首 delta 前中断、或工具步无文本）→ 不建消息、指针 null、`partial_chars=0`。
2. **读出**：`ui/attach` 的 snapshot（`manager.snapshot()`）经 `message_tree()` 自然包含该消息，`partial=true` 标记它；新端按 `turns[].partial_message_id` 命中同一行（RFC-0006 §4.4 的去重规则不变：按 id 幂等，禁止事件+本地缓冲相加）。
3. **契约修订**（§6）：C-06 注记改写 + C-05 注记补键；两个 append 块落地。
4. **测试**（§9）：13 条新用例锁 A1–A8，含「另一端 attach」与两个变异靶子。

对任务书两处提名的**命名/复用裁定**：

- 任务书 §5 写「`Turn` 新增 `interrupted_partial_message_id`」——实现里 RFC-0006 已用 `partial_message_id`（事件键、DB 列、`x-vade.appends[0].fields` 三处一致）。新增第二个同义字段会造出双真相；本 RFC 沿用 `partial_message_id`，见 §5。
- 「PRAGMA 可重入迁移」——`sqlite_store._APPENDED_COLUMNS` + `_ensure_columns` 已是该机制（SQLite 无 `ADD COLUMN IF NOT EXISTS`，按列存在性检查），`messages.partial` 已登记其中；无需新迁移代码。

## 5. 数据模型

存储形状**全部沿用现状**（RFC-0006 §5 已冻结的追加列），本 RFC 不新增列、不改名：

**`messages` 表**（`core/vade/session/sqlite_store.py`）

```
partial INTEGER NOT NULL DEFAULT 0      -- bool；1 = 中断落库的部分回答
```

- 建表骨架含该列；旧库经 `_APPENDED_COLUMNS["messages"]` 可重入迁移补列（重复打开同一库不重复 ALTER）；旧行读回 `partial=False`。
- 写入：`persist_batch` 落 `int(bool(m.partial))`；只有 `_finalize_interrupted` 路径写 `True`。
- 回读：`_message_from_row` 按列存在性回读 `bool(row["partial"])`。

**`Turn`（`turns` 表 / `models.Turn`）**

```
partial_message_id: str | None          -- "msg:<hex>" | null
```

- 任务书提名的 `interrupted_partial_message_id` **不采纳**：该字段在事件 payload（C-06 append 已登记）、`turns.partial_message_id` 列、`Turn.to_dict()` 三处已用现名；改名是纯破坏面（旧端按 `partial_message_id` 去重的客户端会失配），收益为零。
- `state="interrupted"` 且有部分文本时非 null；`completed` / `failed` / `cancelled` 恒为 null（终态收尾不带 partial）。

**`Message`（`models.py`）**：`partial: bool = False` 已存在；`to_dict()` 输出 `"partial": bool`，即 snapshot 行。不变。

## 6. 契约变更

### 6.1 C-06（`schema/events.json`）——注记就地改写

`turn.interrupted.notes` 中删除句子：

> an interrupted assistant's partial text is reconstructable only from turn.delta events

替换为：

> the interrupted assistant's partial text is persisted as a `partial=true` assistant message that `partial_message_id` points at, so a fresh `ui/attach` snapshot contains it — replaying `turn.delta` is no longer the only source (RFC-0013, FR-8.35)

其余句子（tools/cancel 时序、RFC-0005 usage 句）逐字保留。payload 键、`required`、`turn_states`、规则全部不动。

同时在 `x-vade.appends` **追加** RFC-0013 块（不修改 RFC-0006 的 `appends[0]`）：

```json
{
  "contract": "C-06", "version": "1.2.0", "owner": "C3",
  "doc": "docs/rfc/RFC-0013-interrupt-persistence.md",
  "adds": [], "fields": {},
  "overrides": ["turn.interrupted.notes: the 'only from turn.delta' sentence was deleted in place — the partial text is persisted as a partial=true assistant message and a fresh ui/attach snapshot contains it (RFC-0013 §6; FR-8.35/R9)"],
  "rewrites": "turn.interrupted.notes only. No payload key, required array, rule, enum or turn_state changed. Top-level C-06 stays 2.0.0 (the enum bump is RFC-0008's); this block continues RFC-0006's 1.1.0 partial-pointer lineage at 1.2.0 — append-level, non-breaking."
}
```

**版本裁定**：C-06 顶层 `version` 保持 2.0.0 不动（枚举扩宽是 RFC-0008 的所有权，契约门钉死该值）；RFC-0013 的 append 块标 **1.2.0**，续接 RFC-0006 落下 partial 指针时的 1.1.0 血统（任务书「version 升 1.2.0（追加字段，非破坏）」落在 append 块上）。这是对 RFC-0006 §9.3「不改写 notes 字节」惯例的一次**明示偏离**：理由见 §2.2——假注记已从过时升级为有害，`overrides` 不足以阻止按原文实现的客户端丢弃落库消息；偏离本身登记在本 append 块的 `overrides` 里，RFC-0006 的旧块原样保留作审计。

### 6.2 C-05（`schema/rpc.json`）——`ui/attach` 声明 `partial` 字段

`ui/attach.notes` 末尾追加一句（既有 notes 的追加惯例，RFC-0005/0008 同法）：

> RFC-0013: `snapshot.messages[]` carries `partial` (bool, default false) — true for the persisted partial answer of an interrupted turn (`turn.interrupted.partial_message_id` points at it), so a fresh attach renders the N already-generated characters without replaying `turn.delta` (FR-8.35).

并在 `x-vade.appends` 追加：

```json
{
  "contract": "C-05", "version": "1.2.0", "owner": "C2",
  "doc": "docs/rfc/RFC-0013-interrupt-persistence.md",
  "adds": [],
  "notes": ["ui/attach: `snapshot.messages[].partial` (bool, default false) is now a documented result key — the write side landed with RFC-0006 §4; RFC-0013 freezes the read side so a fresh attach shows the interrupted turn's partial text (FR-8.35)", "no method, error code or frozen field changed; the wire key predates this append"],
  "rewrites": "none"
}
```

C-05 顶层 `version` 保持 1.3.0（RFC-0010 的 append 门钉死该值）；RFC-0013 块标 **1.2.0**（RFC-0006 的 C-05 块为 1.1.0，本块续接同一条 `partial` 血统）。

### 6.3 兼容性

- 旧客户端：`snapshot.messages[]` 多一个已存在的 bool 键——「追加字段，非破坏」，忽略即可；C-06 注记改写不改变任何字段/行为，旧端读到的新注记只会让它**不再丢弃** partial 消息。
- 新客户端：按 `partial_message_id` + `partial` 渲染灰气泡/「已停止 · 已生成 N 字」（FR-8.28 的呈现键已在 C-06）。

## 7. 与「重新生成」（`turn.superseded`）的关系

被中断的 turn 是终态，允许 `turn/regenerate`（RFC-0006 §6.2）。partial 消息与版本链的交互规则：

1. **可被 supersede**：regenerate 在中断 turn 上创建新 turn（同 `root_turn_id`，`parent_turn_id` 指旧 turn，`version+1`），旧 turn 置 `superseded_by` + `turn.superseded` 事件；**partial 消息原样留在旧 turn**（`turn_id` 不变、`partial=true` 不变、不复制到新 turn）。
2. **灰化可见**：snapshot 里旧 turn 的 partial 消息继续出现——「重新生成前的那半截」属于历史，客户端按旧 turn 的 `superseded_by` 灰化；新 turn 的回答来自新一轮流式，与 partial 无关。
3. **不复活**：新 turn 不从 partial 消息续写、不把它当上下文注入（引擎照常从会话消息构建 prompt；partial 消息作为普通 assistant 行参与，是否计入上下文是 §8 的未决问题，不改变本节规则）。
4. **编辑重发同理**：`message/edit_resubmit` 走用户消息的版本链，partial 消息不是用户消息，不参与链头判定；旧 turn 灰化后 partial 随之灰化，行为同 1–2。

## 8. 未决问题

1. **partial 消息是否计入 usage / 上下文？** 中断 turn 的 usage 已按 RFC-0005 §6.1 落 `source="none"` 零值，partial 文本本身不再计费。但它已是一条 assistant 行：下一 turn 构建上下文时是否包含（模型能否「接着自己的半截话继续」）尚未裁定——包含则续写连贯但可能放大被否定的方向，不包含则每次都从零开始。v1 按现状（普通行、计入）不动，待产品裁定后另立 RFC。
2. **partial 消息允许编辑吗？** 用户长按半截回答改错字，语义上是「编辑历史」还是「伪造模型输出」？v1 拒绝（`message/edit_resubmit` 只收 user 角色，`INVALID_ARGUMENT` 已覆盖）。若开放，`superseded_by_message_id` 的指向规则需要先裁定。
3. **partial 是否该在 `snapshot.turns[]` 里带内联长度？** turn 行已带 `partial_message_id` 指针，`partial_chars` 只在事件里；只读 snapshot 的客户端要算「已生成 N 字」得自己 `len(content)`。指针 + 内容推导足够（code point 数即 `len(str)`），但若三端出现计数分歧（代理对/组合字符），再考虑把 `partial_chars` 提升为 turn 行列。
4. **部分文本的截断标记**：超长（如 32k 上限附近）被截断的 partial，客户端如何区分「这就是全部」与「还在写时被打断」？`partial=true` 已表达后者；前者不需要标记。是否需要「不完整感」的更细粒度（如尾块未闭合的 markdown 虚拟闭合提示）交由三端渲染规范。

## 9. 验收

实现层（`core/tests/session/test_rfc0013_partial_persistence.py`，与既有 `test_interrupt_partial.py` 互补）：

| # | 验收 | 用例 |
|---|---|---|
| A1 | **FR-8.35 验收句**：中断后，**另一个端**（同一库、全新的 `SessionLog` + 全新 attach，`last_seq=null`）的 snapshot 含该 partial 消息：`partial=true`、`role=assistant`、内容与被中断端流出的逐字一致 | `test_fr835_a_fresh_attach_on_a_second_log_sees_the_partial_text` |
| A2 | 走生产 RPC 层：`SessionRPC.ui_attach` 的 result `snapshot.messages[]` **每条**都带 `partial` 键；partial 消息为 `true`，普通消息为 `false` | `test_rpc_ui_attach_snapshot_carries_the_partial_flag` |
| A3 | 字符数三方一致：`turn.interrupted.partial_chars == len(partial 消息 content) == 拼接后的 delta 总长`（多段 delta，含中文） | `test_partial_chars_equals_the_persisted_content_length` |
| A4 | 未中断不产生 partial：`completed` 与 `failed`（含流过 delta 的失败）turn 的 snapshot 无任何 `partial=true` 行，turn 行 `partial_message_id=null` | `test_a_completed_turn_never_produces_a_partial_message` / `test_a_failed_turn_never_produces_a_partial_message` |
| A5 | 落库持久：关闭 store 后**重新打开**（新 `SQLiteStore` 同路径），partial 消息仍在且 `partial=true`（列回读，非内存） | `test_the_partial_message_survives_a_store_reopen` |
| A6 | 指针一致：turn 行 `partial_message_id == "msg:" + 消息 id`，与事件 payload、snapshot 三方同值 | `test_the_turn_row_pointer_matches_the_partial_message` |
| A7 | §7 关系：中断 turn 被 regenerate 后，旧 turn `superseded_by` 指新 turn，partial 消息仍在 snapshot（`partial=true`、仍在旧 turn），全库恰一条 partial | `test_regenerating_an_interrupted_turn_keeps_the_partial_visible` |
| A8 | 契约文本：events.json 的 `turn.interrupted.notes` 不再含 "only from turn.delta" 且写明落库真相；rpc.json 的 `ui/attach.notes` 含 `partial` 声明；两文件 `x-vade.appends` 各有恰一个 RFC-0013 块且 `version=="1.2.0"` | `test_events_json_no_longer_claims_delta_is_the_only_source` / `test_rpc_json_documents_the_partial_field` |

既有 `core/tests/session/test_interrupt_partial.py`（RFC-0006 §4 的 A1–A4/S2/S4）继续覆盖：中断落库+指针形状、重放不双气泡、首 delta 前中断无空气泡、墓碑带走消息、工具步不产 partial。回归护栏（本 RFC 新增）：普通消息 `partial` 默认 `false`（构造器与 to_dict）、双 turn 场景 partial 只落在被中断的那个 turn。

**变异自验**（§10 之外单独跑）：

- M1「不落库 partial 消息」：`_finalize_interrupted` 丢弃 message（`messages=()`）→ A1/A2/A5/A6/A7 变红；
- M2「partial 恒为 false」：`_partial_message` 把 `partial=True` 改成 `False` → A1/A2/A5/A7 变红（A3/A6 因指针仍在而绿，恰证明两变异打击面不同）。

## 10. 影响面

| 文件 | 变更 | 性质 |
|---|---|---|
| `docs/rfc/RFC-0013-interrupt-persistence.md` | 新增（本文件） | 文档 |
| `docs/rfc/README.md` | 索引追加一行 | 文档 |
| `schema/events.json` | `turn.interrupted.notes` 改写（删一句、换真相句）；`x-vade.appends` 追加 RFC-0013 块（C-06 1.2.0）；顶层 version 2.0.0 **不动** | 契约注记 |
| `schema/rpc.json` | `ui/attach.notes` 追加 `partial` 句；`x-vade.appends` 追加 RFC-0013 块（C-05 1.2.0）；顶层 version 1.3.0 **不动** | 契约注记 |
| `core/vade/session/sqlite_store.py` | **无代码变更**——`messages.partial` 列与可重入迁移已存在（RFC-0006） | 复用确认 |
| `core/vade/session/models.py` | **无代码变更**——`Message.partial` / `Turn.partial_message_id` 已存在 | 复用确认 |
| `core/vade/session/queue.py` | **无代码变更**——`_partial_message` / `_finalize_interrupted` 已存在 | 复用确认 |
| `core/vade/session/manager.py` | **无代码变更**——`partial_text` / `partial_provider` / snapshot 输出已存在 | 复用确认 |
| `core/vade/session/rpc.py` | **无代码变更**——`ui_attach` 透传 snapshot，`partial` 随 `message_tree()` 已在 | 复用确认 |
| `core/tests/session/test_rfc0013_partial_persistence.py` | 新增 13 条 | 测试 |
| `core/vade/session/RESULT-rfc0013.md` | 新增实施报告 | 文档 |

**不触碰**：`turn_states`（7 值）、错误码（34）、delta 流式机制、`core/vade/loop/`、`core/vade/blob/ tasks/ input/ modulepage/ prompt/`、三端渲染。
