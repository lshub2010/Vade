# RFC-0012 · 子代理事件族 R3 · RESULT

| 项 | 值 |
|---|---|
| 分支 | `tmp/rfc0012-subagent`（基线 `e200772`，已拷 `vade_protocol.dll`） |
| RFC | `docs/rfc/RFC-0012-subagent-events.md`（363 行，`status: proposed`，§1–§12 + `docs/rfc/README.md` 索引行；commit `c5efe27`） |
| 契约 | **C-06 `schema/events.json` 顶层仍 2.0.0**（追加 3 事件 + 顶层 `subagent_states` + `x-vade.appends` 块）；**C-05 `schema/rpc.json` 1.3.0 → 1.4.0**（`ui_to_core` 31 → 33 个方法，追加 `subagent/list` / `subagent/get`） |
| 测试 | `pytest core/tests -q` → **985 passed, 0 failed**（连跑两次；基线 966 + 本批 19，`core/tests/subagent/`） |
| 契约门禁 | `python tools/check_contracts.py` → OK（新增 RFC-0012 检查块，第 12 项） |
| 文档门禁 | `python docs/check_docs.py` → **12/12** |
| 变异自验 | ①状态机闸删除（`_check_transition` 置 no-op）→ `test_result.py::test_illegal_result_transition_spawned_to_completed` 与 `test_status.py::test_terminal_cannot_resume_and_spawned_cannot_finish` 变红（DID NOT RAISE）✓；②`result_digest` 改成全文 → `test_result.py::test_result_digest_is_digest_not_fulltext` 变红（`leaked via 'result_digest'`）+ `test_failed_and_cancelled_digest_the_empty_result` 变红 ✓（均已还原，还原后 19/19 绿） |
| push | 未 push（按任务要求） |

## 一、交付内容

1. **RFC-0012**：§1 摘要+七行决定表（拆族、三事件、digest-不-全文、父会话流、五值状态机、父 turn 收口、只读 RPC）；§2 动机（引 R3 事实行 `requirements.md:1186`、C-06 29 条目 0 个 `subagent.*`、FR-8.56[M]/8.57[S]/8.58[S] 验收句原文）；§3 目标 G1–G6 / 非目标 N1–N6（不做独立 UI 页、`max_spawn_depth=1`、不做跨会话）；§4 `subagent_runs` 表 + `SubagentRun` + 顶层 `subagent_states`；§5 三事件（流归属 `session_id == parent_session_id`、required 冻结、§5.3 脱敏红线）；§6 两只读方法 + `subagent/message` 不动；§7 校验（label ≤64 拒不截断、`^run_[0-9a-f]{32}$`、五值状态机、父 turn 收口两条硬规则）；§8 与 `task.*` 边界（按执行体划族，部分撤销 RFC-0007 §4.2）；§9 契约变更表；§10 五条未决问题；§11 验收 A1–A10；§12 影响面。
2. **`subagent_runs` 表**（`sqlite_store.py` `_SCHEMA`）：11 列 + `idx_subagent_runs_parent`；`CREATE TABLE IF NOT EXISTS` 随 `_SCHEMA` 一次 `executescript`，重开连接幂等（A9 有测试）；`upsert_subagent_run`（`ON CONFLICT DO UPDATE` 整行 swap）/ `get_subagent_run` / `list_subagent_runs`（`created_at ASC, run_id ASC`）。
3. **`session/models.py`**：`SubagentState`（5 值）、`SUBAGENT_TERMINAL_STATES`、`new_run_id()`（`run_`+32hex）、`SubagentRun` 冻结 dataclass（`to_view()` 不携带结果全文，`result_preview` 由调用方注入）。
4. **`core/vade/subagents/` 包**：`manager.py`（`ALLOWED_TRANSITIONS` 状态机闸——非法转移 `INVALID_ARGUMENT` 且记录/事件零写入；同态重放幂等、异态终态拒绝；`spawn` / `set_running` / `report` / `finish`（sha-256 指纹 + `result_chars`，failed 必带 C-04 码，`is_known_code` 校验）/ `cancel_for_turn`（父 turn 收口）/ `list_runs`（keyset 游标）/ `view`（≤500 字 `result_preview` 读时投影）；`rpc.py`（`SubagentRPC.handlers()` = `subagent/list` / `subagent/get`，两方法只读）；`__init__.py` `install(app)` 照 `vade.tasks` 接线：`app.subagents` + `log.subagents` + `app.session_handlers.setdefault`（session 组件不 import 本包，箭头单向）。
5. **`session/manager.py`**：`self.subagents: Any = None` 槽位 + `_turn_ending(session_id)` 钩子链（RFC-0008 输入收口 → `cancel_for_turn`），`_queue()` 的 `pre_terminal_hook` 换成链——`subagent.result(cancelled)` 恒在 turn 终态事件**之前**落 seq（A7 两测试断言 seq 序）。
6. **schema**：三事件 payload/required/notes 按本 RFC §5.2 冻结；顶层 `subagent_states`；C-06 append 块（version 2.0.0，rewrites none，注明脱敏红线与流归属）；C-05 两方法 + append 块（version 1.4.0）。
7. **check_contracts 第 12 项**：枚举零漂移（schema `subagent_states` ↔ 代码 `SubagentState`/`SUBAGENT_TERMINAL_STATES`）、`subagent.state` 条目禁止、三事件 `required` 精确冻结、`subagent.spawned` 无 `state` / 另两个必带（rule 4）、结果文本键（`result_text/summary/output/text`）禁止、两方法声明+错误码 ⊆ C-04+`SubagentRPC.handlers()` 注册、C-05 version=1.4.0、`LABEL_MAX==64`、`RUN_ID_PATTERN`、`ALLOWED_TRANSITIONS` 与 12 组非法转移探针。`STATE_CHANGE_EVENTS`/`PAYLOAD_ONLY_EVENTS` 同步追加；`test_meta.py` 的 C-05 版本钉 1.3.0 → 1.4.0（注释注明 RFC-0012 持有现值）。

## 二、测试（19 条，≥15 达标）

`core/tests/subagent/`（helpers + 5 文件）：

- `test_spawned.py` ×4：spawned 自述（required 全键、`session_id==parent_session_id`、落库）、label 校验（空/非串/65 字符拒、64 过、`data.field=label`）、run_id 格式（`run_`+32hex、两失败分开 INVALID_ARGUMENT/NOT_FOUND）、store 可重入（同文件二次打开读写）。
- `test_status.py` ×4：spawned→running 事件（step/tool_name 可选键）、运行期刷新（3 条 status、step ≤120 截断）、非法转移零痕迹（闸删除即红 = 变异①目标）、终态不可复活 + spawned 不能直跳终态 + 异态终态拒绝/同态幂等。
- `test_result.py` ×5：**digest 非全文脱敏**（sha-256、`result_chars`、5000 字全文零泄漏、payload 无多余键 = 变异②目标）、failed 必带 C-04 码（缺失/伪造码都拒）、failed/cancelled digest = `sha256("")`、终态幂等（同态 no-op 无第二条 result + 异态拒绝）、`spawned→completed` 拒绝且记录零变化。
- `test_parent_turn_cancel.py` ×2：父 turn 完成收口（两个 run 取消、旁观会话不动、恰好一条 result/run）、**seq 序**（`subagent.result` < `turn.completed`；interrupt 路径 `< turn.interrupted`）。
- `test_rpc.py` ×4：list（成员/过滤/会话范围/分页无重漏/只读可调）、list 参数拒绝（states/limit/cursor/session_id 四探针）、get（全视图、preview=子会话末条助手消息前 500 字、无子会话 preview=null、只读可调）、get NOT_FOUND vs 格式 INVALID_ARGUMENT 分开。

## 三、changed（文件 + md5）

| 文件 | md5 |
|---|---|
| `docs/rfc/RFC-0012-subagent-events.md` | `3b5c57e4e9f2e2eee912f97a612a4d9b` |
| `docs/rfc/README.md` | `325624554386967db60888ecec3f98ca` |
| `core/vade/session/models.py` | `4dc04e81c4ce297ca81b7e0788bb2399` |
| `core/vade/session/sqlite_store.py` | `95d20c98737cc88bbac0c6c90cefda3c` |
| `core/vade/session/manager.py` | `f56a3424988b162082d426f68fa4820c` |
| `core/vade/subagents/__init__.py` | `c61287ddf7879cf68ad9efa22394d436` |
| `core/vade/subagents/manager.py` | `1d037959de9905af8dc45cf5c4def7f1` |
| `core/vade/subagents/rpc.py` | `b77df45d9c37a4df3c64181dd76e80db` |
| `schema/events.json` | `eab0e21b5d9fb8b469248586474eb3ca` |
| `schema/rpc.json` | `a548327d2b23dc9edacede13920a27d5` |
| `tools/check_contracts.py` | `aadff268360db075a914f2e65b88075e` |
| `core/tests/session/test_meta.py` | `91796b178c329f102e1029ee01557ce7` |
| `core/tests/subagent/helpers.py` | `a033623ec91be7ce9d12f5d8fe0d4e20` |
| `core/tests/subagent/test_spawned.py` | `68a56a3efbf0abe2399d74073140e2a8` |
| `core/tests/subagent/test_status.py` | `dae18ef4c267899b4f204c9cb8239a0f` |
| `core/tests/subagent/test_result.py` | `31cd0373bf7314b1205ca2024698378d` |
| `core/tests/subagent/test_parent_turn_cancel.py` | `bf3407f6ec0638e3e82a9d028695bc01` |
| `core/tests/subagent/test_rpc.py` | `e14393183f42a172f198ef562e699978` |

## 四、硬约束核对

- 未改 `turn_states`（7 值）/ `task_states` / C-04 错误码（仍 34；新拒绝全落 `INVALID_ARGUMENT` / `NOT_FOUND` + `data.field`）。
- 未改 `core/vade/blob/` `input/` `modulepage/` `prompt/` `loop/`；`core/vade/tasks/` 未动（`task.*` 四事件与 `subagent/message` 原样）；`queue.py` 未动（收口走既有 pre_terminal_hook 参数）。
- 三事件都带 `session_id`（= `parent_session_id`，父会话 seq 流），与既有事件流归属一致；均无 `x-vade.push`、无 `x-vade.coalesce`。
- `schema/events.json` 顶层 version 仍 2.0.0（append 块记录，无枚举加宽——`subagent_states` 是新顶层键，非 widen）。

## 五、实现侧裁定（RFC 未尽事项）

1. **终态语义**：同态重放 `finish` = 幂等 no-op（RFC-0007 §5.5 R1 先例）；异态终态目标 = 非法转移拒绝。RFC §7.2 已按此措辞修订。
2. **`result_preview`**：只对 `completed` 且子会话可读的 run 给出（末条助手消息前 500 字）；运行中/spawned/无子会话 → null。读时投影，不落库、不进事件。
3. **注册路径**：两方法经 `subagents/install()` 并入 `app.session_handlers`（`session/rpc.py` 发布、`vade.tasks` 扩展的同一张表）——RFC-0007 既有模式；`session/rpc.py` 本体未加条目，避免双重注册导致 setdefault 死代码。
4. **并发/深度闸**：`max_spawn_depth=1` 是代码侧约定（N2），本批未加运行时闸（spawn 入口属 loop 另案，本包的 `spawn()` 无派生方时闸无从触发）。
