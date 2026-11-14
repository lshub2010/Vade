# RFC-0010 · 会话元数据（分组 / 图标 / 颜色 / 置顶）· RESULT

| 项 | 值 |
|---|---|
| 分支 | `tmp/rfc-session-meta`（基线 `68dbcdd`，已拷 `vade_protocol.dll`） |
| RFC | `docs/rfc/RFC-0010-session-metadata.md`（233 行，`status: proposed`，§1–§12 + 索引行） |
| 契约 | **C-05 `schema/rpc.json` 1.2.0 → 1.3.0**（`ui_to_core` 21 → 31 个方法）；**C-06 `schema/events.json` 顶层仍 2.0.0**（`x-vade.appends` 追加一条） |
| 测试 | `pytest core/tests -q` → **966 passed, 0 failed**（基线 934 + 本批 32，`core/tests/session/test_meta.py`） |
| 契约门禁 | `python tools/check_contracts.py` → OK（新增 RFC-0010 检查块） |
| 文档门禁 | `python docs/check_docs.py` → **12/12** |
| 变异自验 | ①删组连删会话 → `test_group_delete_keeps_sessions_and_unassigns_them` 变红 ✓；②置顶排序子句删除 → `test_pinned_ordering_dominates_updated_at` 变红 ✓（均已还原） |
| push | 未 push（按任务要求） |

## 一、交付内容

1. **RFC-0010**：分组=文件夹（一层、禁嵌套、非标签）、图标=Tabler 白名单、颜色=`#RRGGBB`、存服务端、排序=置顶>`updated_at` 倒序（FR-8.26 原文）；§5 十方法、§6 两事件、§7 校验、§8 删除语义（删组不删会话/删会话客户端二次确认 + `turn_active` 保护闸）、§10 五条未决问题（导出动词、标记未读入口、图标白名单下发、颜色主题自适应、归档是否只读）。
2. **两张表 / 七列迁移**：`session_groups`（`group_id/name/icon/color/sort_order/created_at`）；`sessions` 追加 `group_id/icon/color/pinned_at/archived_at/unread/updated_at`（`PRAGMA table_info` 可重入 + `updated_at` 回填 `created_at` + `idx_sessions_order`）。
3. **10 个 `session/*` 方法**（`ui_to_core`）：`session/list`（offset cursor 分页 + `group_id`（UNSET/null/串三态）+ `archived` 过滤）、`session/rename`、`session/pin`、`session/archive`、`session/delete`（硬删四表、活跃 turn 拒绝 `INVALID_ARGUMENT data.reason=turn_active`，不加新错误码）、`session/group.list`（含 `session_count`）、`session/group.create`（`parent_id` → `nesting_forbidden`）、`session/group.update`（部分更新，`icon/color` 显式 null=清除）、`session/group.delete`（同事务 `UPDATE group_id=NULL`，返回 `unassigned`）、`session/group.assign`。
4. **两个 C-06 事件**：`session.updated`（走变更会话自己的流，`required=[session_id, by]`，带整行元数据，无 `state`）；`session.list_changed`（向每个挂载会话扇出，`module.status` 先例；attach 自动建会话发 `added`、delete 发 `removed`；payload 不带行内容，客户端重拉）。
5. **校验常量（§7）**：`ICON_NAMES`（39 个 Tabler 语义名白名单 + `^[a-z][a-z0-9-]*$` + ≤64）、`COLOR_PATTERN`（`^#[0-9a-fA-F]{6}$`）、组名 ≤64、会话标题 1..200 —— core 一处执行，`check_contracts.py` 直接调用 validate 函数断言。
6. **check_contracts 追加（第 11 项）**：方法存在且错误码 ⊆ C-04、handler 注册齐、icon/color 白名单与格式（含 `zz-not-an-icon`/`#fff` 等探针）、事件形状（`required` 精确、无 `state`、非 push topic）、两处 `x-vade.appends` 块、C-05 version=1.3.0。

## 二、测试（32 条，≥25 达标）

- 每 ≥2 条/方法：`session/list`×3（分页逐页无重漏+参数边界、行形状、组/归档过滤+未知组）、`rename`×2、`pin`×3（含**置顶排序**变异目标 ②）、`archive`×2、`delete`×2（活跃 turn 保护闸→放行+四表清空；removed 扇出+未知会话）、`group.list`×2、`group.create`×2（含图标/颜色/名称/排序/嵌套全拒绝矩阵）、`group.update`×2、`group.delete`×2（**删组不删会话**变异目标 ①）、`group.assign`×2。
- store 级 5 条：可重入迁移+`updated_at` 回填、`updated_at` 只随消息/turn 推进（attach 不推进）、meta 白名单+NOT_FOUND、删会话清四表、组 CRUD 重开往返。
- 契约级 3 条：events.json 两事件形状、rpc.json 1.3.0+handlers 一致、`session.updated` 重连重放（seq 连续）。

## 三、changed（文件 + md5）

| 文件 | md5 |
|---|---|
| `docs/rfc/RFC-0010-session-metadata.md` | `1062a849fcbb92fe3ac92fc61085d296` |
| `docs/rfc/README.md` | `5a3cbe032263fd96fb3a195dd6e53f77` |
| `core/vade/session/models.py` | `8d6d04aea48e79216e8404e4e9acb8ea` |
| `core/vade/session/store.py` | `f8a546801bf1b85cbd7c1f84a887af5f` |
| `core/vade/session/sqlite_store.py` | `507d714512fe09189161238808d68b19` |
| `core/vade/session/manager.py` | `638cbcc53ad52e0a074145bc76b0d536` |
| `core/vade/session/rpc.py` | `77b15a71ff0c77561efc02d62834287e` |
| `schema/rpc.json` | `e07a34d367ada2f265583821a5f90cca` |
| `schema/events.json` | `9b20397ce3b4e5a48a0c6d12b150cc8c` |
| `tools/check_contracts.py` | `561cc79cae55c4565a4086d88f94f067` |
| `core/tests/session/test_meta.py` | `62e5b78e9d797f32afca90f271b3c21d` |

## 四、硬约束核对

- 未改 `turn_states` / `task_states` / 错误码（仍 34；新拒绝全部落在 `INVALID_ARGUMENT` / `NOT_FOUND` + `data.reason`/`data.field`）。
- 未改 `core/vade/blob/` `tasks/` `input/` `modulepage/` `prompt/` `loop/`；`queue.py` 未改（delete 保护闸只读队列状态）。
- 排序 = `(pinned_at IS NULL) ASC, updated_at DESC, created_at DESC, id ASC`（置顶 > 组内 `updated_at` 倒序，`group_id` 是过滤不是排序组）。
- `events.json` 顶层 version 仍 2.0.0（append 块记录，无枚举加宽）；`docs/agents/01-contracts.md` 未动（C5 仍 18 契约）。

## 五、unverified / 边界

- 多设备并发写同一会话元数据是 last-write-wins（无版本向量）——RFC §10 Q4/Q5 已留口。
- `unread` v1 只存不同步置 1 入口（无 `session/read` 方法，见 §10 Q4）；`session.updated` 只到挂载该会话的设备（未挂载靠重拉），§6.1 已写明。
- 客户端三端渲染 / Tabler 图标 / 删除确认弹窗不在本批（RFC §12「明确不在本批」）。
