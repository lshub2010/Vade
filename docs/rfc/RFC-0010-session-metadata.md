---
rfc: RFC-0010
title: 会话元数据：分组（文件夹）/ 图标 / 颜色 / 置顶，与 `session/*` 十方法
status: proposed
date: 2026-11-14
proposer: 编排（依据 `docs/requirements.md` FR-8.26 的「需 RFC-NNNN（待编号）」标注；分组=文件夹、图标+颜色可改、存服务端、置顶>分组内 `updated_at` 倒序四条为用户已拍板决定）
affects: C-05 `schema/rpc.json` 的 `ui_to_core` 追加 10 个 `session/*` 方法（1.2.0 → 1.3.0）；C-06 `schema/events.json` 追加 `session.updated` / `session.list_changed` 两个事件（顶层 version 不动，仍 2.0.0）；`core/vade/session/*` 四个文件 + 两张表
related: RFC-0005（`session/model.get|set`、`session/usage.get` 是 C-05 里仅有的 `session/*` 先例，命名风格沿用）；RFC-0006 / RFC-0008（C-06 追加惯例：`x-vade.appends` 数组、`required` 不动、未知事件类型必须被忽略）；RFC-0007（`module.status` 的「向每个挂载会话扇出」是 `session.list_changed` 的传输先例）；FR-8.26（动词集合与排序口径的原文）
type: C-05 / C-06 追加；不改 C-04 错误码（仍 34）、不改 `turn_states` / `task_states`、无破坏性改名
stage: 已实现（本 worktree 同批落地 core 侧：store / manager / rpc / schema / 测试；客户端三端渲染不在本批）
---

# RFC-0010 · 会话元数据

## 1. 摘要

会话在协议里只有一条「附件流」（`ui/attach` + 事件重放），**没有一条列表通道**：客户端要展示会话列表，只能自己记名字。本提案给会话补上用户侧的元数据层——**分组（文件夹）、图标、强调色、置顶、归档、未读、`updated_at`**——全部存 core 的 SQLite（多设备同步，D98 的「真相只存 core」不破例），并用 10 个 `session/*` 方法与 2 个 C-06 事件把它变成契约。分类是**分组（文件夹）**，不是多对多标签：用户建组、会话拖进组、组可折叠，**只有一层，禁止嵌套**。

| # | 决定 | 理由 |
|---|---|---|
| 1 | 分类 = **分组（文件夹）**，会话↔组是**多对一**（`sessions.group_id` 单列），**不是标签** | 用户已拍板。文件夹有唯一的「拖进/拖出」，标签需要管理界面、冲突合并与「按标签聚合」的第二套列表语义——FR-8.26 只要求数据夹 |
| 2 | 组只有**一层**，禁止嵌套：`session_groups` 没有 `parent_id`，`group.create` 遇到 `parent_id` 参数直接拒绝 | 嵌套树需要递归展开、环路检测与拖拽进深处的 UX；FR-8.26 的「分组在列表内联展开」是一层展开 |
| 3 | 元数据全部**存服务端**（core SQLite），客户端不落本地副本 | 多设备同步是需求本体（用户拍板第 3 条）；客户端副本一旦存在，「改了图标另一台没变」就是缺陷 |
| 4 | 排序 = **置顶 > 组内按 `updated_at` 倒序**（FR-8.26 原文） | 已拍板。`updated_at` 是新列，由真实活动（消息/turn 落库）推进；`ui/attach` 这类非活动写不推进，否则重连一次就顶到最上 |
| 5 | `icon` = **Tabler 语义名白名单**（core 内置常量集），`color` = **`#RRGGBB`**，两者都是**客户端装饰**，core 不解释含义 | 白名单让三端渲染一致（Tabler 是 C-19 页面图标的同族）；`#RRGGBB` 不做 alpha、不做主题 token——主题自适应是 §10 Q3 |
| 6 | 删组**绝不删会话**：组内会话 `group_id` 置 null；删会话是**硬删**（sessions/turns/messages/events 四表同行），二次确认是**客户端责任** | 用户的数据不该因为一个容器的删除而消失（FR-8.26 的「删除（二次确认）」针对的是会话本身）；core 无弹窗能力，确认只能在客户端 |
| 7 | `session/list` 是**轻列表**：只回元数据行，不回消息/turn | 列表页滚动时每页 ≤200 行，若每行都 `load()` 整棵消息树，一次翻页就是全库扫描；正文永远走 `ui/attach` 快照 |
| 8 | 分页用**不透明整数 cursor**（= 偏移量），`next_cursor` 为 null 即到底 | 会话列表 ≤几百条（个人规模），offset 足够；keyset 分页要按 `(pinned, updated_at, id)` 复合键，复杂度买不到收益 |
| 9 | 新事件只有两个：`session.updated`（单条变更，走该会话自己的流）与 `session.list_changed`（新增/删除，向**每个挂载会话**扇出，`module.status` 先例） | 元数据变更天然有「变更的那条流」可挂；列表级变更没有归属流，扇出是唯一能让每台设备的列表都刷新的办法，且 payload 不带行内容，客户端重拉 `session/list` |
| 10 | **不加新错误码**（仍 34），不加 `[sessions]` 配置键 | 「有活跃 turn 的会话不可删」用 `INVALID_ARGUMENT` + `data.reason=turn_active` 表达足够；限额（icon ≤64、name ≤64、color 定长 8）是契约常量不是部署参数，进配置只会制造两处真相 |

## 2. 动机与证据

### 2.1 FR-8.26 的动词集合在协议里一个都没有

`docs/requirements.md` FR-8.26 原文（AMEND-21）：

> **会话列表**：会话入口只有"列表 → 详情"两级，不做分类页；排序固定为 置顶 > 今天 > 昨天 > 7 天内 > 更早，组内按 `updated_at` 倒序；分组在列表内联展开。列表项动词集合三端一致：**置顶、重命名、归档、删除（二次确认）、导出、标记未读**——**需 RFC-NNNN（待编号）· `session/list`、`session/rename`、`session/pin`、`session/archive`、`session/delete`**（**C-05 现无任何 `session/*` 方法**；ui-00 的 A1 标"否"过于乐观）

这行标注写下的那天起就有一个前提：这些方法不存在。核对现状，三处缺口全部成立：

- `schema/rpc.json`（C-05 1.2.0）的 `ui_to_core` 有 21 个方法，其中 **`session/*` 只有 `session/model.get` / `session/model.set` / `session/usage.get`**（RFC-0005 的模型面）——**没有列表、没有重命名、没有置顶、没有归档、没有删除，更没有分组**。
- `core/vade/session/models.py` 的 `Session` 只有 `id / created_at / title / seq_counter / state / messages / turns / model_id`：没有 `group_id`、没有 `icon` / `color`、没有 `pinned_at` / `archived_at` / `unread`，也没有 FR-8.26 排序要用的 `updated_at`——客户端连「上次活动时间」都拿不到，时间桶（今天/昨天/7 天内）无从谈起。
- `core/vade/session/sqlite_store.py` 的 `list_sessions(limit)` 按 `created_at DESC` 排序且**逐条 `load()` 整棵消息树**——它存在是为了恢复与 C-14 的 transcript 收集（`events/deliver.py:424`），不是给列表页用的：一次 50 行的列表要把 50 个会话的全部消息/turn 读进内存。

### 2.2 没有这一层，三端列表各自发明

列表项动词「三端一致」是 FR-8.26 的验收口径。协议不定义动词时，一致只能靠口头约定：A 端把「置顶」实现成本地 flag，B 端把「归档」实现成隐藏——同一账号在两台设备上看到两个不同的工作区。FR-8.26 的验收造了 6 个会话跨 3 个时间桶 + 2 个置顶，断言「首屏顺序为置顶 2 项（组内按 `updated_at` 倒序）→ 今天 → 昨天 → 更早」——这个断言只有在**排序真相在服务端**时才可能对三台设备同时成立。

### 2.3 分组不是标签（已拍板）

用户拍板四条里的第 1 条：**分类 = 分组（文件夹）**。这与常见的「多对多标签」方案有两条硬差异：标签需要「给会话打多个标签」与「按标签交叉筛选」，于是列表排序失去唯一键（按哪个标签的时间桶排？）；文件夹要求「拖进/拖出」这个单一动作，正好映射到 `sessions.group_id` 一列的置值与置空。选文件夹不是因为它简单，是因为 FR-8.26 的排序条款（置顶 > 时间桶）**只有**在每条会话最多属于一个容器时才有定义。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 客户端能分页拉取会话列表（`session/list`），行内含全部分组/图标/颜色/置顶/归档/未读元数据与 `updated_at`（FR-8.26 列表页） |
| G2 | 置顶、重命名、归档、删除四个动词可用（FR-8.26）；排序固定 置顶 > `updated_at` 倒序，重启与跨设备一致 |
| G3 | 用户能建组、改组（名称/图标/颜色/排序）、删组、把会话指派进组或拖出（null），组只有一层 |
| G4 | 元数据变更通过两个 C-06 事件可达每台设备；删会话/建会话让所有挂载中的列表知道要重拉 |
| G5 | icon / color / name / title 的校验在 core 一处执行，三端共享同一份白名单与格式（§7） |
| N1 | **不做标签 / 多对多分类**（§2.3）；不做组嵌套；不做「智能分组/自动归类」 |
| N2 | **不做会话导出**（FR-8.26 六动词之一，涉及格式与脱敏，另起 RFC；本 RFC 只留动词位）与**不做「标记未读」的写入口**——`unread` 字段已入模型与同步面，但 v1 没有任何方法把它置 1（§10 Q4） |
| N3 | **不做软删除 / 回收站**：`session/delete` 是硬删；删会话的确认弹窗是客户端责任（§8） |
| N4 | **不做主题自适应颜色 / alpha / 渐变**：color 只收 `#RRGGBB`（§10 Q3） |
| N5 | **不接 `[sessions]` 配置段**：限额是契约常量（§7），不是部署参数（决定 #10） |
| N6 | **不动** `turn_states` / `task_states` / C-04 错误码表（仍 34）；不动 `core/vade/blob/` `tasks/` `input/` `modulepage/` `prompt/` `loop/` |

## 4. 数据模型

### 4.1 新表 `session_groups`（core SQLite，随 `sessions.db`）

| 列 | 类型 | 语义 |
|---|---|---|
| `group_id` | TEXT PK | core 分配的 `uuid4().hex` |
| `name` | TEXT NOT NULL | 1–64 字符（strip 后）；组内显示名 |
| `icon` | TEXT | 可空；Tabler 语义名（白名单，§7.1） |
| `color` | TEXT | 可空；`#RRGGBB` |
| `sort_order` | INTEGER NOT NULL DEFAULT 0 | ≥0；组在列表中的手工排序（拖拽排序是 §10 Q1） |
| `created_at` | INTEGER NOT NULL | unix ms |

**没有 `parent_id`**：一层是拍板决定（决定 #2），嵌套由 `group.create` 在入口拒绝，数据模型上无处可挂。

### 4.2 `Session` 追加列（`sessions` 表，全部可空/有默认，`PRAGMA table_info` 可重入迁移）

| 列 | 类型 | 语义 |
|---|---|---|
| `group_id` | TEXT | 所属组；NULL = 未分组。**唯一**的会话↔组通道（多对一） |
| `icon` | TEXT | 会话图标（白名单语义名，§7.1）；NULL = 用客户端默认 |
| `color` | TEXT | 会话强调色 `#RRGGBB`；NULL = 用客户端默认 |
| `pinned_at` | INTEGER | 置顶时刻（unix ms）；NULL = 未置顶。排序主键之一 |
| `archived_at` | INTEGER | 归档时刻；NULL = 未归档。默认列表只回 NULL 行 |
| `unread` | INTEGER NOT NULL DEFAULT 0 | 未读标记（0/1）。v1 只存不同步置 1 入口（N2 / §10 Q4） |
| `updated_at` | INTEGER NOT NULL DEFAULT 0 | **最后活动时刻**（unix ms）：消息或 turn 行落库时由 store 推进（`append()`），元数据写（改名/置顶/归档/分组）**不**推进——改个名不该把会话顶到「今天」；旧库迁移时回填为 `created_at` |

`model_id`（RFC-0005）是这批列之前唯一的会话级追加列，迁移模式沿用：`ALTER TABLE ADD COLUMN` 前先 `PRAGMA table_info` 探测，重入安全；旧行读到文档默认值。

### 4.3 `SessionGroup` dataclass（`models.py`）

与 `Session` 同风格：frozen、slots、`to_wire()` 产出 C-05 行形状 `{group_id, name, icon, color, sort_order, created_at}`；`session/list` 的行形状由 `Session.to_wire()` 产出：`{session_id, created_at, updated_at, title, icon, color, group_id, pinned_at, archived_at, unread, state}`。

## 5. RPC 方法（C-05 追加 10 个，1.2.0 → 1.3.0）

全部在 `ui_to_core` 方向，沿用 C-01/C-02 的设备鉴权，不新增鉴权面；错误码只用 C-04 现有 34 个中的 `INVALID_ARGUMENT` / `NOT_FOUND`（§7/§8 的每条拒绝都落在这两个码上，`data.reason` / `data.field` 细分）。

| 方法 | 参数 | 结果 | 备注 |
|---|---|---|---|
| `session/list` | `limit`（1–200，默认 50）、`cursor`（≥0，默认 0）、`group_id`（可省略 = 全部；`null` = 仅未分组；字符串 = 该组）、`archived`（bool，默认 false） | `{sessions: [session_row], next_cursor: int\|null}` | 轻列表（决定 #7）；`group_id` 指向不存在的组 → `NOT_FOUND`（`data.reason=unknown_group`）；排序见 §7.4 |
| `session/rename` | `session_id`、`title`（strip 后 1–200） | `{ok, session: session_row}` | FR-8.26 动词；内容不动，只改标题；发 `session.updated` |
| `session/pin` | `session_id`、`pinned`（bool，必填） | `{ok, pinned_at: int\|null}` | 置顶 → `pinned_at=now`，取消 → `null`；排序主键（决定 #4） |
| `session/archive` | `session_id`、`archived`（bool，必填） | `{ok, archived_at: int\|null}` | 归档后离开默认列表（`archived=false`），进「已归档」筛选 |
| `session/delete` | `session_id` | `{ok}` | 硬删四表（§8.2）；有排队/运行中 turn → `INVALID_ARGUMENT`（`data.reason=turn_active`）；发 `session.list_changed{change:"removed"}` |
| `session/group.list` | `{}` | `{groups: [group_row + session_count]}` | 按 `sort_order, created_at, group_id` 排序；`session_count` 供折叠行展示 |
| `session/group.create` | `name`（必填）、`icon`、`color`、`sort_order`（≥0，默认 0） | `{group: group_row}` | `parent_id` 参数出现即 `INVALID_ARGUMENT`（`data.reason=nesting_forbidden`，决定 #2）；`group_id` 由 core 分配 |
| `session/group.update` | `group_id`（必填）+ `name`/`icon`/`color`/`sort_order` 至少一个 | `{group: group_row}` | 部分更新：只动出现的键；`icon`/`color` 显式 `null` = 清除，省略 = 不动；`name: null` → `INVALID_ARGUMENT` |
| `session/group.delete` | `group_id` | `{ok, unassigned: int}` | **删组不删会话**（§8.1）：返回被移回「未分组」的会话数 |
| `session/group.assign` | `session_id`、`group_id`（`null` = 拖出） | `{ok, group_id}` | 组必须存在（`NOT_FOUND`）；发 `session.updated` |

命名沿用 C-05 现状的两段式：`session/` 前缀 + 动词或 `group.<动词>`，与 `session/model.get`、`ui/queue.cancel` 同风格（点号已在契约里活着，不再发明第三种风格）。

## 6. 事件（C-06 追加两个，顶层 version 仍 2.0.0）

追加走 `x-vade.appends` 数组（RFC-0006 / RFC-0008 惯例），不动任何既有条目、不动枚举；两个事件都**不是** C-14 push topic（无 `x-vade.push`），都**不带 `state`**（元数据边，不是 turn 状态变化，不适用 rule 4 的状态事件约束）。客户端不认识这两个类型时按既有规则忽略并推进 `last_seq`（C-06 rule 6 / RFC-0008 §6.4）。

### 6.1 `session.updated` — 单条变更

```json
{"type": "session.updated", "seq": 42,
 "payload": {"session_id": "…", "title": "重构方案", "icon": "code", "color": "#4a90d9",
             "group_id": "…", "pinned_at": 1731…, "archived_at": null, "unread": 0,
             "updated_at": 1731…, "by": "dev-01"}}
```

`required: ["session_id", "by"]`；payload 携带**变更后的整行元数据**，挂在该会话**自己的流**上——rename / pin / archive / group.assign 每次成功写后各发一条。已挂载该会话的客户端原地更新；**没挂载的客户端不会收到**（这是刻意收窄：列表刷新由 `session.list_changed` 或用户主动重拉驱动，不给每台设备无差别广播的常驻成本）。

### 6.2 `session.list_changed` — 新增 / 删除

```json
{"type": "session.list_changed", "seq": 7,
 "payload": {"session_id": "…", "change": "removed", "by": "dev-01"}}
```

`required: ["session_id", "change", "by"]`；`change ∈ {added, removed}`。向**每个挂载中的会话**扇出（`module.status` 的传输先例，RFC-0007 之前就有）：`ui/attach` 自动建新会话后发 `added`，`session/delete` 落库后发 `removed`（不含正在删除的那条流本身）。**payload 刻意不带行内容**——每台设备重拉一次 `session/list`，不在事件里复刻一份随时会过期的副本。`group.*` 三个方法 v1 不发事件（§10 Q4）。

## 7. 校验规则

### 7.1 `icon`：白名单语义名

- 形状：`^[a-z][a-z0-9-]*$` 且长度 ≤64（`ICON_NAME_MAX`）；
- **形状合格还不够**：必须是 core 内置白名单 `ICON_NAMES` 的成员（Tabler 语义名起步集，如 `chat` / `folder` / `code` / `database` / `search` / `terminal-2`…，常量在 `core/vade/session/manager.py`，供 `check_contracts.py` 断言非空且每个成员都过形状闸）；
- 白名单外的一切（含形状不合格的 `1bad`、`Chat`、空串）→ `INVALID_ARGUMENT`（`data.field=icon`）。**未知值不降级、不回落**：写进去就是同步出去的契约数据，客户端三端都要渲染同一个名字。
- 可空：`null` = 清除/未设置，合法。

### 7.2 `color`：`#RRGGBB`

`^#[0-9a-fA-F]{6}$`（`COLOR_PATTERN`）。不做 `#RGB`、不做 8 位 alpha、不做 `rgb()`、不做主题 token 名（N4）。不合 → `INVALID_ARGUMENT`（`data.field=color`）。可空同上。

### 7.3 名称与组

- 组 `name`：strip 后 1–64（`GROUP_NAME_MAX`）；空串/全空白/超长 → `INVALID_ARGUMENT`；
- 会话 `title`：strip 后 1–200；`session/rename` 的 `title` 不可为空（清空标题用未来的专用语义，v1 不提供）；
- `group_id` 必须存在：`session/group.assign` / `session/list` 的过滤 / `group.update` / `group.delete` 遇到未知组 → `NOT_FOUND`（`data.reason=unknown_group`）；
- **组嵌套禁止**（决定 #2）：数据模型无 `parent_id`，`group.create` 见到 `parent_id` 键 → `INVALID_ARGUMENT`（`data.reason=nesting_forbidden`）。

### 7.4 排序（FR-8.26 原文）

`session/list` 固定排序：`(pinned_at IS NULL) ASC, updated_at DESC, created_at DESC, id ASC`——置顶在前，置顶与未置顶内部都按 `updated_at` 倒序，`id` 兜底保证同刻稳定。`group_id` 是**过滤**不是排序组：客户端拿行内的 `group_id` 内联展开分组（FR-8.26「分组在列表内联展开」），每条会话只属于一个组，排序键在全表唯一。

## 8. 删除语义

### 8.1 删组：不删会话

`session/group.delete` 在**同一事务**里做两件事：`UPDATE sessions SET group_id=NULL WHERE group_id=?` 然后 `DELETE FROM session_groups WHERE group_id=?`。返回 `unassigned` = 被移回「未分组」的会话数。**组的消失永远不等于会话的消失**——这是 §1 决定 #6，也是本 RFC 唯一的变异自验目标 ①（删组时连会话一起删 → 测试必须变红）。

### 8.2 删会话：硬删 + 客户端二次确认

- `session/delete` 硬删 `sessions` / `turns` / `messages` / `events` 四表中该会话的全部行（一个事务）；随后向其余挂载会话扇出 `session.list_changed{removed}`、拆掉它的 sink、弹出内存队列。
- **二次确认是客户端责任**（FR-8.26 原文「删除（二次确认）」）：core 无弹窗，不做软删/回收站（N3）——确认发生在 RPC 发出之前。
- 保护闸：该会话仍有排队或运行中的 turn → `INVALID_ARGUMENT`（`data.reason=turn_active`）。不新增错误码（决定 #10）：这不是新失败类别，是参数与状态不符。运行中 turn 的正确动词是先 `ui/interrupt`。

## 9. 契约变更

| # | 契约 | 变更 | 类型 | 风险 |
|---|---|---|---|---|
| 1 | C-05 `schema/rpc.json` | `version` 1.2.0 → **1.3.0**；`ui_to_core` 追加上表 10 个方法；`x-vade.appends` 追加一条（`adds` = 10 个方法，`rewrites: "none"`） | 追加 | 新能力面：客户端可改工作区元数据（§10 安全） |
| 2 | C-06 `schema/events.json` | `events` 追加 `session.updated` / `session.list_changed`；`x-vade.appends` 追加一条 | 追加 | **顶层 version 不动**（仍 2.0.0）：无枚举加宽、无状态新增，旧客户端按 rule 6 忽略未知类型 |
| 3 | C-04 | **不改** | — | 仍 34 个码；全部拒绝落在 `INVALID_ARGUMENT` / `NOT_FOUND`，细分走 `data.reason` / `data.field` |
| 4 | C-18 `config.schema.json` | **不改** | — | 无 `[sessions]` 配置段：限额是契约常量（决定 #10 / N5） |
| 5 | C-03 | **不改** | — | 会话元数据不进 manifest |
| 6 | `core/vade/session/store.py` | `SessionStore` 协议追加可选扩展方法（`list_session_rows` / `update_session_meta` / `delete_session` / `session_groups` 族） | 追加 | Postgres 实现按同一端口补齐即可 |
| 7 | `tools/check_contracts.py` | 追加 RFC-0010 断言：10 方法存在且错误码合法、icon/color/组名校验常量、事件形状、两处 `appends` 块 | 追加 | 门禁变严，无放行 |

`docs/agents/01-contracts.md` **不动**：C-05/C-06 的契约表行数不变（追加不新建契约），`check_docs.py` 的 C5 口径（18 个契约）不受影响。

## 10. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **分组排序是否用户可拖？** | 现状：`sort_order` 是 `group.create/update` 的普通整数字段，客户端可以自行维护（每次拖动算一次重排并写回）。倾向：v1 就这样（服务端不感知拖拽语义）；若三端都要「拖完即同步」，在 `group.update` 上加批量 `sort_order` 数组形态，而不是发明新方法 |
| Q2 | **图标白名单要不要下发？** | 现状：白名单是 core 常量，客户端硬编码同一份。问题在于 core 与客户端版本错位时，客户端会对一个合法名字显示默认图标。三选：①`session/group.list` 顺带返回白名单（会话列表也要用它，位置不对）；②新方法 `session/icons`；③客户端内置 + 容忍未知名（只丢图标不丢数据）。倾向 ③ 先跑，等真实名字集稳定再考虑 ① |
| Q3 | **颜色是否支持主题自适应？** | 现状只收 `#RRGGBB`，三皮肤（material/miuix/glass）各自决定怎么用它（背景色/描边/圆点）。若要「暗色下自动调亮」，可以追加可选 `color_scheme` 字段（`"fixed"` 默认 / `"adaptive"`），但这会改变三端渲染契约，且玻璃皮肤的对比度规则还没冻结——倾向保持 `#RRGGBB`，让皮肤层自己调 |
| Q4 | **「标记未读」动词与 group.* 变更事件** | `unread` 字段已在模型/同步面里，但 v1 没有任何方法把它置 1（`session/attach` 也不清它——避免给 attach 加隐藏写）。候选：`session/read`（`read: bool`）或塞进一个未来统一的 `session/update`；`group.*` 的变更目前靠客户端操作后自拉，若多设备并发改组出现视图漂移，再评估给 `group` 族配一条扇出事件 |
| Q5 | **归档是否算「会话关闭」？** | 现状：`state` 列（open/closed，RFC-0005 的 `READ_ONLY_SESSION` 语义）与 `archived_at` 是两个正交维度——归档的会话仍可提交消息。若产品口径是「归档 = 只读」，`session/archive` 需要级联 `state='closed'`，这会改变现有 closed 语义的观察者（模型解析、审批）——倾向保持正交，等产品拍板 |

## 11. 验收

A1–A10 要求真实执行；落点为计划路径，全部在本 worktree（`core/tests/session/test_meta.py` + `tools/check_contracts.py`）。

| # | 断言 | 落点 |
|---|---|---|
| A1 | 10 个方法全部可达：`SessionRPC.handlers()` 含全部 10 个名字；`schema/rpc.json` `ui_to_core` 声明一致且 version = 1.3.0；每个方法的 `errors` ⊆ C-04 | `tools/check_contracts.py`（check_session_meta） |
| A2 | `session/list` 分页：造 7 个会话，`limit=3` 逐页拉取无重复无遗漏，末页 `next_cursor=null`；`limit` 越界（0 / 201）/ `cursor` 负数 → `INVALID_ARGUMENT` | `core/tests/session/test_meta.py` |
| A3 | 置顶排序：置顶会话（`updated_at` 更旧）排在未置顶（`updated_at` 更新）之前；取消置顶后回到 `updated_at` 序 | 同上（变异目标 ②：删掉排序的置顶子句 → 变红） |
| A4 | 删组不删会话：建组、指派 2 个会话、删组 → 2 个会话仍在默认列表且 `group_id=null`，`unassigned=2`；`session/group.list` 不再含该组 | 同上（变异目标 ①：删组连删会话 → 变红） |
| A5 | icon 非法被拒：形状合格但不在白名单（`zz-not-an-icon`）、形状不合格（`1bad`、`Chat`、空串）→ `INVALID_ARGUMENT`；白名单内（`chat`）通过 | 同上 |
| A6 | color 非法被拒：`#fff`（短）、`#GGHHII`（非 hex）、`red`、缺 `#` → `INVALID_ARGUMENT`；`#4A90D9` 与 `#4a90d9` 通过 | 同上 |
| A7 | 组名校验：空串 / 全空白 / 65 字符 → `INVALID_ARGUMENT`；`group.create` 带 `parent_id` → `INVALID_ARGUMENT`（`nesting_forbidden`） | 同上 |
| A8 | 未知组被拒：`group.assign` / `group.update` / `group.delete` / `list` 过滤遇未知 `group_id` → `NOT_FOUND`（`unknown_group`）；未知会话 → `NOT_FOUND` | 同上 |
| A9 | 删会话保护闸：有运行中 turn 的会话 → `INVALID_ARGUMENT`（`turn_active`），四表行都在；空闲会话 → 四表行全删，其他挂载会话收到 `session.list_changed{removed}` | 同上 |
| A10 | 事件形状与追加块：两事件 `required` ⊆ payload、不带 `state`、非 push topic；C-06 顶层 version 仍 2.0.0；两处 `x-vade.appends` 各含 RFC-0010 块 | `tools/check_contracts.py` + `core/tests/session/test_meta.py` |

文档级：`python docs/check_docs.py` 仍 12 项全绿；`python tools/check_contracts.py` OK；`pytest core/tests -q` 全绿（934 基线 + 本批新增）。

## 12. 影响面

**本批已动**：`docs/rfc/RFC-0010-session-metadata.md`（本文件）+ `docs/rfc/README.md` 索引行；`core/vade/session/models.py`（`Session` 7 列 + `SessionGroup` + `to_wire()`）；`core/vade/session/sqlite_store.py`（`session_groups` 表 + `sessions` 追加列迁移 + `updated_at` 推进 + 列表/元数据/组/删会话方法）；`core/vade/session/manager.py`（校验常量与 4 个 validate 函数 + 10 个方法实现 + 两事件发射 + attach 建会话的 `added` 扇出）；`core/vade/session/rpc.py`（10 个 handler 注册）；`schema/rpc.json`（1.3.0 + 10 方法 + appends）；`schema/events.json`（2 事件 + appends）；`tools/check_contracts.py`（check_session_meta）；`core/tests/session/test_meta.py`。

**明确不动**：`schema/errors.json`（34 个码）、`schema/config.schema.json`（无 `[sessions]`）、`schema/manifest.schema.json`、`docs/agents/01-contracts.md`、`turn_states` / `task_states`、`core/vade/blob/` `tasks/` `input/` `modulepage/` `prompt/` `loop/`、`core/vade/session/queue.py`（删会话保护闸只**读**队列状态）。

**客户端（不在本批）**：三端列表页消费 `session/list` 行形状与两事件；Tabler 图标渲染；删除确认弹窗（客户端责任，§8.2）；`admin-ui` 若要展示组管理，走同一组方法。

**参考**：`docs/requirements.md` FR-8.26（AMEND-21 §AM21-A）；`core/vade/session/models.py` `Session`（现状字段）；`schema/rpc.json`（C-05 1.2.0，`session/*` 仅 model/usage 三个）；`core/vade/session/sqlite_store.py`（`_ensure_columns` 迁移模式）；RFC-0005（`session/*` 命名先例）；RFC-0007（`module.status` 扇出先例）；RFC-0006 / RFC-0008（C-06 `x-vade.appends` 惯例）。
