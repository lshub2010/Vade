# C3 · RFC-0007 子代理与后台任务 · RESULT

| 项 | 值 |
|---|---|
| 分支 | `tmp/rfc0007`（基线 `94f5995`） |
| RFC | `docs/rfc/RFC-0007-subagents-and-tasks.md`（approved） |
| 测试 | `pytest core/tests -q` → **601 passed**（基线 549，新增 52；含 `RingLog.tail` 的 head 截断修复与一条时序 flaky 修正） |
| 契约门禁 | `python tools/check_contracts.py` → `contracts: OK` |
| 文档门禁 | `python docs/check_docs.py` → 12 项全通过 |
| 未 push | 是（按任务要求） |

## 一、交付清单（逐条）

| # | 要求 | 落点 | 状态 |
|---|---|---|---|
| 1 | 统一 `task.*` 四事件 + 顶层 `task_states` 7 态（终态三值），`turn_states` 不动 | `schema/events.json`、`core/vade/tasks/registry.py`、`core/vade/tasks/models.py` | ✅ |
| 2 | 进度 ≤2/s 合并（窗口内只发最后一次）+ `x-vade.coalesce`；结构事件不合并不丢 | `TaskRegistry.progress/_flush_progress`、`Event.coalesce`、`SessionLog.emit(coalesce=)` | ✅ |
| 3 | `task/list`（游标/过滤/`running_count`/`limits`）、`task/log_tail`（≤200 行且 ≤64 KiB、`follow`、`lost`/`eof`/`truncated`）、`task/cancel`（幂等、3 s、`forced`）、`subagent/message`（默认 `UNSUPPORTED`） | `core/vade/tasks/rpc.py`、`schema/rpc.json` | ✅ |
| 4 | 审批归属 `task_id`/`task_name`/`depth` + 补登记 `session_id`；`scope:"session"` 不向子代理继承 | `core/vade/policy/approval.py`、`core/vade/loop/ports.py`、`schema/events.json` | ✅ |
| 5 | C-18 `[tasks]` 13 键 + `check_contracts.py` 同步 | `schema/config.schema.json`、`tools/check_contracts.py` | ✅ |
| 6 | 进程内 `TaskRegistry`：并发上限、取消令牌、日志环形缓冲、终态广播；取消 3 s 内转终态，超时 `forced` | `core/vade/tasks/{registry,logs}.py` | ✅ |
| 7 | 测试（事件字段与 7 态 / 进度合并 / list 游标 / log_tail 上限与 follow / cancel 幂等+3 s / 审批字段 / UNSUPPORTED / 并发上限） | `core/tests/task/*`（8 文件 50 项） | ✅ |

验收 A1–A16 的 core 侧承载：`test_lifecycle.py`（A1、A2 事件/状态）、`test_progress.py`（A5、A6、A7）、`test_cancel.py`（A3、A4、A11）、`test_log_tail.py`（A14）、`test_list.py`（A16 + snapshot §5.6）、`test_approval.py`（A9、A10）、`test_limits.py`（A12）、`test_rpc.py`（A15、§6.4）。A8（子代理失败隔离）由「每个任务独立记录 + 父 turn 不受 task 事件影响」的字段与 `failed` 必带 `code` 的断言覆盖；A13（客户端通知去重）属 UI 三端，不在本仓库 core 范围。

## 二、变更文件

```text
新增  core/vade/tasks/__init__.py      组件 install（注册表 + 四个 handler）
新增  core/vade/tasks/models.py        TaskState / TERMINAL_TASK_STATES / Task / TaskView
新增  core/vade/tasks/logs.py          RingLog + 不透明游标（行数/字节双上限）
新增  core/vade/tasks/registry.py      TaskRegistry（状态机、限流、取消、级联、历史、snapshot）
新增  core/vade/tasks/rpc.py           task/list|log_tail|cancel、subagent/message
新增  core/tests/task/{__init__,helpers}.py + 8 个测试模块（50 项）
改    schema/events.json               4 事件 + task_states + coalesce 标记 + 审批可选字段
改    schema/rpc.json                  4 方法 + events_core_to_ui.names + ui/attach 注记
改    schema/config.schema.json        [tasks] 13 键
改    tools/check_contracts.py         两组事件集合、task_states↔TaskState、[tasks] 默认值、审批可选字段
改    core/vade/session/manager.py     emit(coalesce=)、snapshot.tasks/tasks_truncated、interrupt 级联
改    core/vade/session/models.py      Event.coalesce + 线上 `x-vade.coalesce`
改    core/vade/session/replay.py      add() 跳过 coalesce 事件（sqlite 仍全量）
改    core/vade/session/rpc.py         approval_resolved 透传归属字段
改    core/vade/policy/approval.py     PendingApproval 归属字段、payload 四键、grant 不向下继承
改    core/vade/loop/ports.py          ApprovalRequest.task_id/task_name/depth
```

**未动**（硬约束核对）：`turn_states`（git diff 为空）、`core/vade/blob/**`、`schema/errors.json`、`schema/events.json` 里既有条目的 `required` 列表。

## 三、实现要点

- **事件只有一个出口**：`TaskRegistry` 的四个 `_emit_*` 都走 `SessionLog.emit()`，因此 seq 分配、persist-before-broadcast、重连 replay 全部复用 C-06 既有机制；`check_contracts.py` 的静态扫描（`emit(<x>, "task.started")`）能看到四个名字，`missing_handlers` 因此保持绿色。
- **coalesce 的语义边界**：`task.progress` 落库、广播、`read_since` 照旧返回，只是不再充当 `ReplayBuffer.covers()` 的连续性证人（§9.4）。1000 条进度后 `ui/attach(last_seq=0)` 仍走 replay；同样的 1001 条普通事件会退化为 snapshot（对照测试）。
- **进度双重闸门**：先看会话是否有 sink（无 attach 只更新记录），再看 `event_burst` 会话总闸（只丢 progress，状态事件永不丢），最后才是每任务 `progress_hz` 的尾沿合并。
- **取消是截止时间**：`task/cancel` 立即返回 `cancelling`，`cancel_grace_ms` 到点由定时器强制 `cancelled{forced:true}`；协作退出走 `finish(..., forced=False)`，先到者生效，另一个见到终态即返回——因此只有一个 `task.finished`。
- **级联**：`ui/interrupt` → `SessionLog.interrupt` → `registry.cancel_turn`（只管子代理，不动作业）；父任务终态递归取消子代理；`close_session` 两种 kind 都取消。
- **审批归属**：`turn.approval_required` 的 payload 补 `task_id`/`task_name`/`depth` 并登记早已发送的 `session_id`，`required` 一字未动；`scope:"session"` 的 grant 只在 `depth==0` 时查询与写入，子代理一律重新询问（并因此不产生 grant）。

## 四、偏差（与 RFC 文本逐条对照）

1. **包名/位置**：RFC §13 写 `core/vade/task/*` 与 `session/models.py`；本实现用 `core/vade/tasks/*`（与任务书要求的结果文件路径一致），`Task`/`TaskState` 也放在 `tasks/models.py`，避免 `session` 反向依赖 `tasks`。`check_contracts.py` 扫描新位置。
2. **未建 sqlite `tasks` 表**：RFC §13 提到该表，任务书第 6 条明确「进程内 `TaskRegistry`」。v1 的任务记录不跨重启（日志本就只在内存，Q3）；重启后活动页为空，与「不伪造进度」一致。若后续要跨重启历史，补表即可，事件面无需改。
3. **C-06 `rules[]` 未追加**：RFC §9.2 自己标注「追加规则需 C3 点头」。因此没有改冻结的 rules 文本，而是把同一约束写进 `task.progress.notes`、`envelope.x-vade` 与新事件条目，并在 `replay.add()` 落地。
4. **append 块命名**：`x-vade.append` 已被 C-14 占用（不可改写），RFC-0007 登记为 `x-vade.append_rfc0007`，字段（contract/owner/adds）与 RFC 片段一致。
5. **`task/log_tail` 越界参数**：`lines`/`max_bytes` 超过上限时按上限夹紧（配置的 `log_lines` 可能小于 200，客户端默认 200 必须先被夹紧），只有 ≤0 或非整数才 `INVALID_ARGUMENT`。
6. **`task/list` 游标编码**：RFC 举例 `base64(ended_at|task_id)`；混合排序（非终态按 `started_at`、终态按 `ended_at`）需要 phase，故用 `base64url("k1|phase|primary|task_id")`。仍是不透明 keyset，越界/非法游标 `INVALID_ARGUMENT`。
7. **`forced` 的返回时机**：`task/cancel` 同步返回时 `forced=false`（此时只是 `cancelling`），强制终态在 `task.finished.forced=true` 上体现——RFC 的字段表把 `forced` 归在「仅 cancelled」。
8. **进度窗口是尾沿合并**：窗口内只保留最后值并由定时器补发；窗口外的第一个值立即发（否则 2 次/秒会退化成固定 500 ms 延迟）。测试按「3 次快速上报 → ≤2 事件且末值为最新」断言。
