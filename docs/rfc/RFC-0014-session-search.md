---
rfc: RFC-0014
title: 会话搜索：`session/search`（标题 + 正文 LIKE 扫描、两档排序、片段硬截断）
status: proposed
date: 2026-11-14
proposer: 编排（依据 `docs/requirements.md` FR-8.27 的「需 RFC-NNNN（待编号）· `session/search`」标注；标题匹配与当前会话内全文查找是 FR 已写死的最低面）
affects: C-05 `schema/rpc.json` 的 `ui_to_core` 追加 1 个 `session/search` 方法（1.4.0 → 1.5.0）；`core/vade/session/` 追加 `search.py`（新建，语义与常量）并在 `store.py` / `sqlite_store.py` / `manager.py` / `rpc.py` 各加一段；无新表、无新列、无新索引、无新事件（C-06 不动）、无新错误码（C-04 仍 34）
related: RFC-0010（`session/list` 的轻行、`updated_at` 只随真实活动推进的语义、FR-8.26 排序是本方法排序的底座）；RFC-0006（`messages.superseded_by` 划出「活正文」边界；§8 `TEXT_LIMIT`=32 000 码点是片段必须硬截断的原因）；FR-8.27（列表内覆盖层、输入 3 字符触发防抖、当前会话内全文查找、「已缓存」降级路径——本方法是该降级的解除条件）
type: C-05 追加；不改 C-04 错误码（仍 34）、不改 C-06（无新事件）、无破坏性改名
stage: 已实现（本 worktree 同批落地 core 侧：search / store / sqlite_store / manager / rpc / schema / 测试；客户端三端搜索框与命中高亮渲染不在本批）
---

# RFC-0014 · 会话搜索

## 1. 摘要

RFC-0010 落地后协议有了一条「列表通道」，但仍没有「查找通道」：消息正文只进不出（`ui/attach` 按会话整棵给快照），要找「那条讨论过分页策略的消息」只能靠人肉翻会话。FR-8.27 早已写死口径——**搜索是列表内的覆盖层，不是一级 tab**；core 不提供 `session/search` 时，客户端只能搜本地已缓存的消息并在结果页标注「范围：已缓存」。本提案把这件事变成契约：一个只读方法 `session/search`，扫**会话标题 + 活正文**，参数化 LIKE、两档排序、片段硬截断——**不做 FTS5、不加索引、不加新错误码、不发事件**。

| # | 决定 | 理由 |
|---|---|---|
| 1 | 搜索范围 = **会话标题（`sessions.title`）+ 活正文（`messages.content` 文本）**；`superseded_by` 非空的行是编辑史不是活正文，**不搜**（RFC-0006 §6.3）；`parts` 里的图片/附件、`turns.user_text`（用户消息本体已在 `messages` 里）不在范围 | 「搜得到摘要、点进去空白」是 FR-8.27 明令禁止的结果；编辑旧文本被搜出来指向一条已经不存在的正文，正好制造这种结果 |
| 2 | 匹配方式 = **参数化 LIKE 扫描（`LIKE ? ESCAPE '\'`），不是 FTS5** | 见 §4.2：个人规模（≤5 000 会话 / ≤200 000 消息）内亚秒级；FTS5 默认 unicode61 分词器对中文整段切词，必须换 trigram——索引体积与写入放大换不来这个规模下的收益；LIKE 零维护（无 shadow 表、无 trigger、无重建），逃逸通道留在 §7 Q1 |
| 3 | 大小写 = **SQLite LIKE 原生语义：ASCII 折叠，非 ASCII 逐码点精确** | 中文没有大小写，「unicode 大小写折叠」要 `lower()` UDF 或 FTS5 才能做对，而它对中文用户毫无收益、对 ASCII 用户免费存在——照抄 LIKE 语义并文档化边界，不假装 unicode-CI（§4.3） |
| 4 | 排序 = **两档相关性：标题命中档 > 正文命中档；档内 `updated_at` DESC、`id` ASC 兜底**（FR-8.26 序） | 标题是用户自己起的名字，命中即「就是这个会话」；正文命中只说明「提过一嘴」。两档足够，不引入打分引擎（§4.4）；`updated_at` 语义与排序稳定性直接沿用 RFC-0010 §4.2/§7.4 |
| 5 | 结果按**会话去重**：一个会话一行；标题与正文都命中 → `matched_in: "title"` 一行，`total` 计一次 | 搜索框的点击目标是「进入会话」，同一会话出 5 行是列表噪音；`total` 若按消息计数，翻页语义就没有稳定总量 |
| 6 | 分页 = **显式 `limit`（1..100，默认 20）+ `offset`（≥0）+ `total`**；短页即末页；**不做**不透明 cursor | 搜索是可重跑查询（FR-8.27：3 字符触发、200ms 防抖），query 一变 cursor 全废；offset 对可重跑语义是诚实的。`total` 要给——「找到 3 条」是结果页第一行文案；limit 上限 100 是片段行（~120 码点/行）比 `session/list` 轻行重约一个量级的对价（§4.5） |
| 7 | **空查询 = `INVALID_ARGUMENT`**（缺参 / 非字符串 / strip 后为空，含全角空白）；>200 码点同样拒绝 | 回车返回全量列表是 UX bug 且扫描成本无界；200 码点对齐 `session/rename` 的标题上限，搜索框不是粘贴板（§4.6） |
| 8 | 片段 = **≤120 码点硬截断窗口**（首命中前后取窗，截断处以 `…` 标注）+ `snippet_offset` = 命中在**原文**里的码点下标；正文命中才有片段，标题命中片段为 null | 正文可达 32 000 码点（RFC-0006 §8），整段回传是 payload bug；高亮渲染是客户端的事，core 只给定位（N2） |
| 9 | 注入安全 = **全程参数化**；`%` `_` `\` 三个 LIKE 元字符按 `ESCAPE '\'` 转义为字面量；`'` `--` `;` **不是** LIKE 元字符，不转义（参数化天然覆盖），转义它们反而毁掉诚实查询（"it's"、"a--b"） | 字符串拼 SQL 是唯一不可接受项；通配符不转义则用户搜「100%」得到全库——测试按 §8 A9 钉死（§4.10，变异目标 ①） |
| 10 | 权限 = 与 `session/list` **同一信任层**（C-01/C-02 设备鉴权，`ui` 通道）；`read_only` 连接**可搜**（读动词）；可选 `session_id` 参数把范围**钉在单会话**（FR-8.27「当前会话内全文查找」），未知会话 → `NOT_FOUND` | 本方法读的是与 `ui/attach` 快照同源的数据，不出现任何一条已挂载客户端看不到的内容，无越权面可开口；单会话范围让「会话内查找」不用客户端再过滤一遍 |
| 11 | **不加新索引** | 前导通配 `%q%` 的 LIKE 用不上 B-tree，`idx_messages_session` 已覆盖 `session_id` 范围路径；为全表扫描建索引是 cargo cult（§4.9） |
| 12 | **不加新错误码**（仍 34），**不发事件**：全部拒绝落在 `INVALID_ARGUMENT` / `NOT_FOUND`；搜索是纯读，无状态变更可广播 | 与 RFC-0010 决定 #10 同口径；读方法发事件等于给每个按键广播 |

## 2. 动机与证据

### 2.1 FR-8.27 在协议里没有落点

`docs/requirements.md` FR-8.27 原文：

> **会话搜索**：搜索是列表内的覆盖层，不是一级 tab；至少支持标题匹配与当前会话内全文查找；core 未提供 `session/search` 前，跨会话全文搜索只搜本地已缓存消息，并在结果页标注"范围：已缓存" …… **需 RFC-NNNN（待编号）· `session/search`**（可选；缺失时降级为本地索引，降级路径必须可见）

核对现状，缺口成立：

- `schema/rpc.json`（C-05 1.4.0）的 `ui_to_core` 有 33 个方法，`session/*` 13 个——**没有 `session/search`**；
- 客户端唯一的正文来源是 `ui/attach` 快照，于是 FR-8.27 描述的降级（只搜本地缓存、标注范围）是当前唯一实现——多设备上「另一台搜得到、这台搜不到」，而 FR 的验收行明令「不得出现搜得到摘要、点进去空白」的结果；
- 「当前会话内全文查找」同样没有协议落点：客户端只能对快照做内存过滤，长会话（32 000 码点/条 × 数百条）在低端设备上是可感知的卡顿。

### 2.2 降级路径必须有解除条件

FR-8.27 要求「降级路径必须可见」，这隐含着：降级不能是永久状态。本 RFC 就是那个解除条件——`session/search` 出现后，客户端检测到方法存在（否则 `-32601`，与 RFC-0007 对 `task/*` 的降级判定同法）即切服务端搜索并去掉「范围：已缓存」标注。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 客户端能跨会话搜索**标题与活正文**（解除 FR-8.27 的「已缓存」降级），结果按会话去重、两档排序、带 `total` |
| G2 | `session_id` 参数把搜索**钉在单会话**（FR-8.27「当前会话内全文查找」），未知会话 `NOT_FOUND` |
| G3 | 正文命中自带**≤120 码点片段**与 `snippet_offset`（原文码点下标），客户端可直接渲染与高亮，无需二次拉取 |
| G4 | 注入样本（`%` `_` `'` `--` `\` `; DROP TABLE`）全部**字面化**：要么字面命中要么空结果，永不报错、永不动表 |
| N1 | **不做 FTS5 / 倒排索引 / 分词 / trigram**：规模边界与逃逸通道见 §4.9、§7 Q1 |
| N2 | **不做命中高亮标记**：core 返回 `snippet_offset`，染色是渲染层的事 |
| N3 | **不搜** `parts` 附件文本 / 图片 OCR / 工具调用结构化字段；只搜 `messages.content` 文本投影（§7 Q2） |
| N4 | **不做**搜索历史 / 联想建议 / 拼写纠错 / 语义检索 |
| N5 | **不动** C-06（无事件）、C-04（无新码）、无新表 / 新列 / 新索引；`turn_states` / `task_states` 不动 |
| N6 | **不动** `core/vade/loop/`、`core/vade/input/`、`queue.py`；`docs/requirements.md`、`PROGRESS.md`、`docs/status.md` 由编排统一改 |

## 4. 语义

### 4.1 搜索范围与「活正文」边界

两个扫描面：

| 面 | 表 / 列 | 过滤 |
|---|---|---|
| 标题 | `sessions.title` | `LIKE ?`（`title` 为 NULL 的行天然不命中） |
| 正文 | `messages.content` | `LIKE ?` **且** `superseded_by IS NULL`（RFC-0006 §6.3：被编辑重发的旧消息是编辑史） |

- **用户消息不重复搜**：`turns.user_text` 是 `messages` 里同一条 user 消息的投影副本，搜两遍只会让同一句话命中两次；
- **`partial` 消息在范围内**：中断留痕（RFC-0006 §4 / RFC-0013）是活正文——用户记得「回答被打断前说了什么」，搜不到才是缺陷；
- **归档会话在范围内**：行内 `archived: true` 让客户端标注来源，搜索不替用户决定归档不可见（RFC-0010 Q5 保持正交）；已删除会话硬删四表，天然不出现。

### 4.2 匹配方式：LIKE，不是 FTS5（写出代价再选）

| | 参数化 LIKE（本 RFC） | FTS5 + trigram |
|---|---|---|
| 中文 | 逐码点精确，天然正确 | 默认 unicode61 把整段中文当一个 token，**必须**换 trigram，否则中文查询全灭 |
| 写入放大 | 零 | messages 每行进 shadow 表（trigram 索引 ≈ 3× 文本体积），且 `append()` 批量路径要加同步触发器或双写 |
| 维护面 | 无 | schema 迁移、重建索引、`optimize` 时机、外部内容表一致性 |
| 规模内延迟 | ≤200 000 消息全扫亚秒级（SQLite 每秒可 LIKE 扫 ~10⁸ 码点量级） | 毫秒级，但这个规模下差距买不回上表两行成本 |
| 结果相关性 | 两档（标题 > 正文）+ `updated_at` | bm25——但两档语义已经覆盖 FR-8.27 的验收（标题命中高亮），打分是超工程 |

**决定**：LIKE。FTS5 的真实收益（相关性打分、亚毫秒延迟）在本规模与验收口径下不存在，而它的真实代价（中文必须 trigram、写入路径加同步、迁移面）全部要付。规模边界与触发重评的信号见 §4.9 / §7 Q1。

### 4.3 大小写与中文

SQLite LIKE 的内建折叠语义：**ASCII `A-Z` ↔ `a-z` 折叠，其余码点逐字节精确**。本 RFC 照抄并文档化，理由与后果：

- 中文（以及任何非 ASCII）没有大小写，「精确」就是正确行为——搜「重构方案」命中「重构方案讨论」；
- ASCII 侧免费获得大小写无关：搜 `todo list` 命中标题 `TODO LIST`；
- 边界（写进契约，不当 bug 修）：`É` ≠ `é`、`Ａ`（全角）≠ `A`。要跨过这条线需要 `lower()` UDF 或 FTS5，两者的复杂度在 §4.2 已被否决；
- `snippet_offset` 的定位器（`find_ascii_ci`）**逐字符镜像 LIKE 折叠**，不用 Python 的 `str.lower()`——后者折叠 `ß`、`İ`、全角等 LIKE 不认识的字符，会让 offset 指向一个 LIKE 根本没命中的位置（或错过它命中的）。

### 4.4 排序

固定两档，档内沿用 RFC-0010 §7.4 的键：

```
标题命中档：updated_at DESC, id ASC
正文命中档：updated_at DESC, id ASC        ← 排在标题档之后
```

- 一个会话同时命中标题与正文 → 只进标题档（决定 #5）；
- `updated_at` 只随消息/turn 落库推进（RFC-0010 §4.2），搜索结果因此天然「最近动过的在前」；
- `id` ASC 兜底：同刻会话的页序可复现，三端一致；
- 不做 bm25 / 词频打分——两档是 FR-8.27 能验收的最小语义（§4.2）。

### 4.5 分页与 total

| 参数 | 规则 |
|---|---|
| `limit` | int 1..100，默认 20；越界（0 / 负数 / 101+ / bool）→ `INVALID_ARGUMENT`（`data.field=limit`） |
| `offset` | int ≥0，默认 0；无上界——越界 = 空页（`results: []`，`total` 照实返回），负数 → `INVALID_ARGUMENT`（`data.field=offset`） |
| `total` | **去重后的命中会话数**（标题命中计一次，即使正文也命中）；每页都返回，客户端用 `offset + len(results) >= total` 判末页 |

- 上限 100 而不是 `session/list` 的 200：命中行带 ~120 码点片段，比轻行重约一个量级，一页 200 行片段 ≈ 24 000 码点，是列表页一页的十倍负载；
- 不做不透明 cursor：查询可重跑（防抖后 query 每次都变），offset 是诚实语义（决定 #6）。

### 4.6 空查询与非字符串

- 缺 `query`、非字符串（int/list/null）、strip 后为空（含全角空格 U+3000 等所有 Unicode 空白）→ `INVALID_ARGUMENT`（`data.field=query`）；
- strip 后 >200 码点 → `INVALID_ARGUMENT`（`data.field=query`，`data.limit=200`，`data.actual=N`）；
- **永不**把空查询解释成「返回全部会话」。

### 4.7 片段与 snippet_offset

- 窗口：`start = max(0, offset − 40)`，从 `start` 起取足 `120 − 省略号数` 个码点；`start > 0` 加前导 `…`，窗口未达文末加尾 `…`；**整条片段 ≤120 码点**；
- `snippet` 只在正文命中时非 null（标题命中时标题本身就是命中，片段为 null）；
- `snippet_offset` = 首命中起点的**原文码点下标**（不是片段内下标）——客户端拿它与 `ui/attach` 快照里的原文对位渲染；定位器逐字符镜像 LIKE 折叠（§4.3）；
- 每会话取**第一条**命中消息（`rowid` 最小 = 落库最早），一页至多 `limit` 条片段查询，成本有界。

### 4.8 权限与范围

- 与 `session/list` 同一信任层：C-01/C-02 设备鉴权后的 `ui` 通道，本方法不做第二套 ACL——它读的数据与 `ui/attach` 快照同源，不新增任何可见性；
- `read_only` 连接可搜（读动词，与 `session/list` / `ui/attach` 同判）；不做 `READ_ONLY_SESSION` 拒绝；
- `session_id`（可选）：给了就把标题与正文两个扫描面都钉在该会话（`WHERE id = ?` / `WHERE session_id = ?`，仍是参数绑定）；未知会话 → `NOT_FOUND`（与 `session/list` 过滤未知组的判法一致）；
- 纯读方法：不发 `session.updated`、不发 `session.list_changed`、不写审计以外的任何行。

### 4.9 性能边界与索引

- **舒适区**：≤5 000 会话且 ≤200 000 消息。两个扫描面各一次全扫（LIKE 逐码点，无索引可走也无需索引），SQLite 单进程亚秒级完成；`total` 从同一次扫描的分组结果取，不加第二次往返；
- **超界**：先到的是消息数——正文档按「每会话首命中」分组后仍是 O(命中会话数) 的内存（每行无正文，正文只对 ≤limit 的页行回查）；越过舒适区时 §7 Q1 的 FTS5-trigram 是既定逃逸通道，触发信号 = 真实部署的搜索 P95 > 500ms，而不是拍脑袋；
- **不加索引**（决定 #11）：前导通配 LIKE 用不上 B-tree；`idx_messages_session`（RFC-0006 时代已有）恰好服务 `session_id` 范围路径；新索引只会拖慢 `append()`。

### 4.10 SQL 注入与通配符转义

- **参数化是唯一通路**：模式串（`%` + `escape_like(query)` + `%`）与所有筛选值都走 `?` 绑定；SQL 文本里没有第二个来自用户的字符；
- `escape_like` 只转义三个 LIKE 元字符：`\` → `\\`、`%` → `\%`、`_` → `\_`，配 `ESCAPE '\'` 恢复字面量；
- `'`、`--`、`;` **不转义**：它们不是 LIKE 元字符，参数化已让它们只能是数据；画蛇添足地转义会把诚实查询（`it's`、`a--b`）变成搜不到；
- 双保险由 §8 A9 的样本组钉死：字面命中正确、全库误命中为零、表行数不变、响应恒为 200 语义（命中或空页）。

## 5. RPC 方法（C-05 追加 1 个，1.4.0 → 1.5.0）

`ui_to_core` 方向，沿用 C-01/C-02 设备鉴权；错误码只用 C-04 现有 34 个中的 `INVALID_ARGUMENT` / `NOT_FOUND`。命名沿用两段式 `session/` + 动词，与 `session/list` 同风格。

| 方法 | 参数 | 结果 |
|---|---|---|
| `session/search` | `query`（string，strip 后 1..200，必填；缺参/非串/空白 → `INVALID_ARGUMENT`）、`limit`（int 1..100，默认 20）、`offset`（int ≥0，默认 0）、`session_id`（string，可选；钉在单会话，未知 → `NOT_FOUND`） | `{results: [hit], total: int}` |

`hit` 行形状（与 `schema/rpc.json` 逐字一致）：

| 键 | 类型 | 语义 |
|---|---|---|
| `session_id` | string | 命中会话 |
| `title` | string\|null | 当前标题（null = 未命名会话按正文命中） |
| `updated_at` | int (unix ms) | 最后活动时刻（RFC-0010 §4.2 语义），档内排序键 |
| `matched_in` | `title`\|`body` | 命中档；标题与正文同中 → `title` |
| `message_id` | string\|null | 正文命中的首条消息；标题命中为 null |
| `snippet` | string\|null | ≤120 码点窗口（含 `…`）；标题命中为 null |
| `snippet_offset` | int\|null | 命中在**原文**的码点下标；标题命中为 null |
| `archived` | bool | 会话是否已归档（客户端据此标注来源） |

## 6. 契约变更

| # | 契约 | 变更 | 类型 | 风险 |
|---|---|---|---|---|
| 1 | C-05 `schema/rpc.json` | `version` 1.4.0 → **1.5.0**；`ui_to_core` 追加 `session/search`；`x-vade.appends` 追加一条（`adds=["session/search"]`，`rewrites:"none"`） | 追加 | 只读面加宽；无既有方法/字段变动 |
| 2 | `core/vade/session/store.py` | `SessionStore` 协议追加 `search_sessions(query, *, limit, offset, session_id) -> (hits, total)` | 追加 | Postgres 实现按同一端口补齐（LIKE → `ILIKE`，ASCII 折叠语义文档化偏差） |
| 3 | `core/vade/session/search.py`（新建） | 常量（`QUERY_MAX=200` / `LIMIT_MAX=100` / `LIMIT_DEFAULT=20` / `SNIPPET_MAX=120`）、`escape_like`、`validate_query`、`find_ascii_ci`、`make_snippet`、`SearchHit` | 追加 | 语义一处定义，store 只做 SQL 黏合 |
| 4 | `tools/check_contracts.py` | 追加 RFC-0014 断言（方法声明/handler/常量/转义函数/store 参数化标记/appends 块）；版本钉随头（1.4.0 → 1.5.0） | 追加 | 门禁变严，无放行 |
| 5 | C-04 / C-06 | **不改** | — | 仍 34 个码；无新事件，无 `state` 面变化 |

`docs/agents/01-contracts.md` 不动（追加不新建契约）。

## 7. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **FTS5 何时接手？** | 触发信号 = 真实部署搜索 P95 > 500ms（§4.9）。届时 trigram 分词（中文唯一正确选择）+ 外部内容表 + `append()` 双写同步另起 RFC；`search_sessions` 端口不变，SQLiteStore 内换实现，调用方零改动 |
| Q2 | **要不要搜 `parts` 附件文本 / 工具结构化字段？** | v1 只搜 `content` 文本投影（所有读者消费的同一份）。附件文本涉及 OCR 与脱敏、工具字段涉及模块边界，各自另议 |
| Q3 | **搜索要不要进审计？** | 纯读、无状态变更；现有审计面（C-16）记录的是动作与写。若合规要求「谁搜过什么」，加一条只读审计事件比把 RPC 变更有成本低——倾向不动 |

## 8. 验收

A1–A14 要求真实执行；落点为 `core/tests/session/test_rfc0014_search.py` + `tools/check_contracts.py`。两个变异自验目标：

- **变异 ①**：把 store 的参数化改成字符串拼接（去掉 `escape_like` 与 `?` 绑定）→ A9 的注入样本组变红；
- **变异 ②**：删掉 handler 里 `limit ≤ 100` 的上限检查 → A7 的 `limit=101` 用例变红。

| # | 断言 | 落点 |
|---|---|---|
| A1 | 方法可达：`schema/rpc.json` 声明 `session/search`（version = 1.5.0）、`SessionRPC.handlers()` 注册、`errors` ⊆ C-04、appends 块 `adds=["session/search"]` 且 `rewrites` none | `tools/check_contracts.py`（check_session_search）+ 测试 A1 |
| A2 | 空查询拒绝：缺参 / `""` / 全空白（含 U+3000）/ 非字符串 → `INVALID_ARGUMENT`（`data.field=query`）；201 码点拒绝、200 码点放行 | test_absent_empty_and_blank_query_are_rejected、test_query_length_bounds_are_enforced |
| A3 | 标题命中行形状：`matched_in="title"`、`message_id/snippet/snippet_offset` 全 null、`archived` 正确 | test_title_hit_row_shape |
| A4 | 正文命中行：`message_id` 指向首条命中、`len(snippet) ≤ 120`、`snippet_offset` 处原文以查询（ASCII 折叠后）开头、长文窗口含 `…` | test_body_hit_row_carries_clipped_snippet_and_offset |
| A5 | ASCII 折叠：`todo list` 命中 `TODO LIST` 标题、`parseerror` 命中正文 `ParseError` | test_ascii_case_folding |
| A6 | 中文精确：`重构方案` 命中标题/正文中文；无关中文不误中 | test_chinese_matches_exactly |
| A7 | limit 边界：0 / −1 / 101 / `true` / `"20"` → `INVALID_ARGUMENT`；缺省 = 20（**变异目标 ②**） | test_limit_bounds_are_rejected |
| A8 | offset 边界：−1 → `INVALID_ARGUMENT`；999（越界）→ 空页且 `total` 不变 | test_offset_must_be_non_negative、test_offset_beyond_total_returns_empty_page |
| A9 | 注入样本：`100%` 不当通配符、`case_name` 不匹配 `caseXname`、`it's` / `--` / `\` 字面命中、`'; DROP TABLE sessions; --` 无错无中且表行数不变（**变异目标 ①**） | test_percent_is_literal、test_underscore_is_literal、test_quote_and_sql_comment_are_literal、test_backslash_is_literal |
| A10 | 排序：标题档在正文档前；正文档内 `updated_at` DESC、同刻 `id` ASC | test_title_hits_rank_before_body_hits、test_body_tier_orders_by_updated_at_desc_with_id_tiebreak |
| A11 | 去重与 total：标题+正文同中 → 一行 `matched_in="title"`、`total` 计一次；跨页无重无漏、`total` 恒定、短页即末页 | test_session_matching_both_title_and_body_appears_once_as_title、test_pagination_pages_have_no_duplicates_or_gaps |
| A12 | 范围与权限：`session_id` 钉住单会话、未知 → `NOT_FOUND`；`read_only` 连接可搜 | test_session_id_scope_pins_results_to_one_session、test_read_only_connection_can_search |
| A13 | 边界排除：`superseded_by` 行不命中；`session/delete` 后不再出现 | test_superseded_messages_are_not_searched、test_deleted_session_disappears_from_results |
| A14 | 无 FTS5 依赖：`sqlite_master` 无 fts 表、重开库可搜（降级路径 = 无处可坏） | test_no_fts_infrastructure_is_required |

文档级：`python docs/check_docs.py` 仍 12 项全绿；`python tools/check_contracts.py` OK；`pytest core/tests -q` 全绿（1041 基线 + 本批新增）。

## 9. 影响面

**本批已动**：`docs/rfc/RFC-0014-session-search.md`（本文件）+ `docs/rfc/README.md` 索引行；`core/vade/session/search.py`（新建：常量 + `escape_like` / `validate_query` / `find_ascii_ci` / `make_snippet` / `SearchHit`）；`core/vade/session/store.py`（Protocol 追加 `search_sessions`）；`core/vade/session/sqlite_store.py`（`search_sessions`：三段参数化 SQL + 两档归并 + 页内片段回查，全程 `?` 绑定）；`core/vade/session/manager.py`（`search()`：query 规范化 + 范围解析）；`core/vade/session/rpc.py`（handler 注册 + limit/offset 类型检查——上限检查只此一处，是变异目标 ② 的钉子）；`schema/rpc.json`（1.5.0 + 方法 + appends 块）；`tools/check_contracts.py`（check_session_search + 版本钉同步）；`core/tests/session/test_rfc0014_search.py`（22 条）；`core/tests/session/test_meta.py` / `test_rfc0013_partial_persistence.py`（仅版本钉随头 1.5.0）。

**明确不动**：`schema/events.json`、`schema/errors.json`、`schema/config.schema.json`（无 `[sessions]` 搜索键——限额是契约常量，RFC-0010 决定 #10 同口径）、`docs/agents/01-contracts.md`、`core/vade/session/models.py`（无新列）、`queue.py`、`core/vade/loop/`、`core/vade/input/`、`docs/requirements.md`、`PROGRESS.md`、`docs/status.md`。

**客户端（不在本批）**：三端搜索覆盖层消费 `session/search`（FR-8.27：3 字符触发、200ms 防抖、`snippet_offset` 高亮、`archived` 来源标注、`-32601` 时回退本地缓存并保留「范围：已缓存」标注）。

**参考**：`docs/requirements.md` FR-8.27；RFC-0010 §4.2/§7.4（`updated_at` 与排序）；RFC-0006 §6.3/§8（`superseded_by`、`TEXT_LIMIT`）；`core/vade/session/sqlite_store.py`（现有参数化先例：`list_session_rows` 的 `group_id` 三态过滤）；`schema/rpc.json`（C-05 1.4.0，`session/*` 13 个）。
