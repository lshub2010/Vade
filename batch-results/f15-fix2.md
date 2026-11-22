# f15 返工（fix2）：深嵌套审批 P0 + 顺序/契约 P1

基线 `1a1574b`（`tmp/f15-contract`，f15 首轮），本报告对应返工提交。审查依据 `batch-results/f15-review.md`。

## 结论

- **P0 已修**：`_copy_pending_row` 不再无条件 `deepcopy`；改为有界复制（深度 64 / 节点 50 000 / 字符 4 000 000），逐行 `try`，失败时**保留原行**并写 `pending_copy_failed` + `pending_truncated=true`，该行的 `args` 被保护、永不就地裁剪，另写 `session.snapshot_truncated` 审计。真实 `ApprovalManager + PolicyAdapter`、900 层 args（约 6.3 KB）实测：审批保留（1 行）、`pending_copy_failed=1`、`pending_truncated=true`、`truncated=true`、审计在案；同会话另一条浅 args 审批不受影响。
- **P1-1 已修**：`tasks` 在 `_bound_snapshot` 的**最前面**丢（⓪，早于 messages/turns/args 裁剪），丢完重新测字节。40×2000 字符消息 + 20×4000 字符 task：修前保留 1 条、正文裁到 64 字；修后保留 **14 条完整 2000 字消息**（本版保留 `parts`，同预算下比 review 的 28 条少，但不再吃对话）。20×4000 task + 32 000 汉字审批 args：修前 args 只剩 64 字；修后保留 **16 000 字**（`args_truncated=true`）。
- **P1-2 已修（合规路径 a：保留字段）**：撤销 `schema/rpc.json` 的 1.8.0 升版与第 13 块 append（`adds=[]` + 无 RFC 的 field removal 不合法）。`Message.to_snapshot` / `Turn.to_snapshot` 恢复 `messages[].parts` 与 `turns[].user_text`，C-05 保持 **1.7.0**、appends 12 块。`check_snapshot_contract()` 改成反向门禁：版本必须 1.7.0、不得出现 f15 append 块、notes 必须保留 `parts`/`user_text` 且不得出现 “no longer sent”/“omits `user_text`”。
- **P1-3 已修**：`ui/attach.notes` 重写——删除自相矛盾表述（现在 “no key was renamed or removed” 为真）；明确 `content` 与 `parts` 都下发、`turns[].user_text` 保留；补 legacy turn 回退链：`user_message_id` 为 NULL 时用 `messages[].turn_id == turn.id`（`client_msg_id` 仅辅助）。
- **P1-4 部分收紧**：审计 `extra` 增加 `args_truncated` / `args_omitted_chars` / `pending_copy_failed`；notes 明确“参数被裁的审批必须提示不完整、不得拿快照 args 重发 stage two”。
- **P1-6 已修**：README / ALPHA / LEDGER / WHERE-ARE-WE 的版本与计数同步。README 服务端测试 1203 → **1557**；ALPHA 的 appends 口径 `12+8+3` → **`12+9+3`**（events 9 块）；LEDGER 顶部 1.8.0 → **1.7.0**、`appends_rpc` 13 → **12**、f15 行改为“无 append 块、字段保留”；WHERE-ARE-WE `13+9+3=25` → `12+9+3=24`。

## 代码改动

`core/vade/session/manager.py`

1. `snapshot()`：`pending()` 调用与每行复制分开；每行单独 `try`；`protected_args` 记录 args 未复制的行；新增 `pending_copy_failed`（基快照恒在）；复制失败即 `pending_truncated=true`。
2. `_bound_snapshot()`：新增 ⓪ 步，先 `_drop_tasks()` 再重测；①–⑥ 顺序保留；`_clip_one_text` 接收 `protected_args`，对共享 args 的行跳过 args 裁剪（prompt 等行级字段仍可裁）。
3. `_bounded_copy()` / `_copy_pending_row()`：显式深度/节点/字符预算，超限抛 `_ArgsTooComplex` 并保留整行；不再依赖 `copy.deepcopy`（900 层即 RecursionError）。
4. `_audit_snapshot_truncated()`：补 `pending_copy_failed`、`args_truncated`、`args_omitted_chars`。

`core/vade/session/models.py`：两个 `to_snapshot()` 恢复完整字段（C-05 1.7.0 冻结）。

`schema/rpc.json`：版本 1.7.0；删除 f15 1.8.0 append 块；重写 `ui/attach.notes`（字段保留 + 回退链 + 预算顺序 + 计数键）。

`tools/check_contracts.py`：三处版本钉回退到 1.7.0（保留 RFC-0016 钉）；`check_blob_token()` 版本断言回 1.7.0；`check_snapshot_contract()` 改为合规门禁（见上）。

`docs/agents/22-C3-session-loop.md`：快照规则改为“不删字段；先丢 tasks，再丢对话，最后丢 pending；args 太深/太大整行保留并计数”。

## 测试

`core/tests/session/test_attach_bounds.py`（18 → 22 例）：

- 新增 P0：`test_a_deeply_nested_approval_is_kept_and_counted_not_cleared`（真实 `ApprovalManager` + `PolicyAdapter`，900 层 args，另加一条浅审批验证不连带清空）。
- 新增/加强 P1-1：`test_a_huge_task_list_does_not_eat_the_conversation`（40 条完整消息 + 20 条大 task）、`test_a_huge_task_list_does_not_shrink_approval_args`（args 从 64 → ≥8000 字）。
- 新增 `test_both_pending_blocks_are_counted_when_dropped`（approvals + inputs 同时丢，双计数）。
- 修假绿：`endswith("字")` 改为 32 000 个**不同**字符的精确尾部断言；`_approval_row` 去掉真实 `_row()` 不存在的 `detail` 字段；预算断言统一到 `MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE`；schema 测试改为断言 1.7.0 / 无 f15 块 / `parts`&`user_text` 保留 / legacy 回退。
- `test_parts.py` 的 legacy 行断言改为 `parts` 为合成 text part；版本断言三处回 1.7.0。

## 复跑证据（本机，Python 3.14.9，复用 `protocol/core/python/libvade_protocol.so`）

```
PYTHONPATH=core:protocol/core/python python3 -m pytest core/tests -q
  -> 1557 passed, 14 warnings in 481.82s (0:08:01)
PYTHONPATH=core:protocol/core/python python3 -m pytest core/tests/session -q
  -> 250 passed in 141.74s
PYTHONPATH=core:protocol/core/python python3 -m pytest core/tests/session/test_attach_bounds.py -q
  -> 22 passed in 6.36s
python3 tools/check_contracts.py  -> contracts: OK
python3 tools/check_ledger.py     -> ledger: OK (16 RFC rows; rpc 1.7.0 49 methods, appends 12+9+3)
python3 docs/check_docs.py        -> 合计 12 项，失败 0 项
```

审查探针复跑：

```
# P0：真实 ApprovalManager + 900 层 args
manager pending 1 snapshot rows 1 pending_trunc True omitted 0 truncated True

# P1-1：40x2000 消息 + 20x4000 task
kept messages: 14 omitted: 26 omitted_tasks: 20 truncated: True
final bytes: 60170 budget: 63471 kept content len 2004

# P1-1b：20x4000 task + 32k 审批 args
tasks 0 omitted_tasks 20 args_len 16000 args_trunc True pending_trunc False truncated True content_trunc True
```

## 未做 / 仍在范围外

- 三端客户端实测本仓不可核（只保证契约字段未删；`parts`/`user_text` 对旧客户端是原样）。
- P2 项（`_drop_pending` 全有全无、args 顶层 list 不裁、`truncated_before_seq` 仅 omitted>0 时设值等）未在本棒处理。
- `pending()` 端口自身抛异常时仍只能返回空 pending（有 warning，无审计）；`deepcopy` 根因已消灭，但端口完全不可用不在本棒范围。
