# f15 · core 快照契约 / 待审批截断标记

基线 `fcf638e`（`tmp/f12-attach` 顶端，含 P0-1 防死循环），分支 `tmp/f15-contract`。
修的是 `batch-results/f12-attach-review.md` 的 P1-2 与 P1-3：

| 审查项 | 本棒处理 |
|---|---|
| P1-2 第 ④ 步静默丢 `pending_*`、`truncated` 仍为 false、无审计、注入空 `tasks` | 已修：④ 先丢 `tasks` 再**重测**，只有仍超才丢 pending；`pending_truncated` + `omitted_approvals`/`omitted_inputs` 计数 + `truncated=true` + 审计；`tasks` 三键只在原本存在时写 |
| P1-2 `pending_approvals[].args` 是 Mapping，裁剪器处理不了 | 已修：`_clip_one_text` 递归找 `args` 里最长的字符串叶子并折半截断，行级 `args_truncated`/`args_omitted_chars`；裁剪前 `deepcopy`，审批端原对象不被改写 |
| P1-3 `parts` / `user_text` 删除未按 C-05 登记，notes 自相矛盾 | 已修：`schema/rpc.json` 升 **1.8.0** + 第 **13** 块 append（`adds=[]`、`rewrites` 显式登记两处删除、notes 登记全部新键）；ui/attach notes 改成 `content` 是最小渲染字段，读 `parts` 的客户端必须回退 |
| P1-1 replay 重启后非空 buffer 仍 replay-only | **不在本棒范围**（任务只点名上述三项），见文末「未做」 |

## 1. 第 ④ 步：先 tasks，重测，再 pending；丢 pending 必须可听见

`core/vade/session/manager.py::_bound_snapshot`

改前：

```python
if over_budget:
    snapshot["tasks"] = []
    snapshot["tasks_truncated"] = True
    snapshot["pending_inputs"] = []
    snapshot["pending_approvals"] = []
```

四个问题一起修：

1. **先只丢 `tasks`，然后重新 `_reply_bytes`。** 任务列表有 `task/list` 恢复通道，且不阻塞
   turn；审批/输入是阻塞态。只有丢完 tasks 仍超预算才进入 ④b 丢 pending —— 一条巨大的
   task 列表不会再顺手把审批对话框带走。
2. **丢 pending 有独立信号**：`snapshot.pending_truncated=true`、
   `omitted_approvals`、`omitted_inputs`（基快照里恒为 `false/0/0`，形状稳定）。
3. **`truncated` 不再漏报**：最终判定加上 `pending_truncated` 与 `omitted_tasks`。即使
   一条 message/turn 都没丢，只要 ④ 清过阻塞态，`truncated=true` 且写
   `session.snapshot_truncated` 审计。审计 `extra` 增加 `pending_truncated` /
   `omitted_approvals` / `omitted_inputs` / `omitted_tasks`。
4. **`tasks` 三键只在原本存在时写**：`_drop_tasks()` 只对「键存在且非空」的列表生效；
   没有 tasks 组件的 core 不会在裁剪时凭空长出 `tasks=[]`/`tasks_truncated=true`/
   `omitted_tasks`。⑥ 兜底也走同一对 helper，不再无条件 `snapshot["tasks"] = []`。

第 ④b 之后的截断循环保留，并仍带「重算后字节没变小就退出」的防自旋保护。

## 2. `pending_approvals[].args`：Mapping 也要能裁

`_clip_one_text()` 重写成「候选 = (容器, 路径, 原文, 要打标记的行)」：

- message 的 `parts[]` 文本按 part 裁剪（保留 P0-1 的按 part 折半，绝不再出现
  「content 折半后被未截断的 parts 重算回来」的死循环）；
- `running_partial` 仍保留尾部；
- pending 行的 `prompt/detail/title/question/summary` 照旧；
- **新增** `args`：`_longest_arg_text()` 在 Mapping/序列里递归找最长字符串叶子
  （深度上限 8），命中后 `_set_at()` 按路径截断为前一半。行级写
  `truncated=true`、`args_truncated=true`、`args_omitted_chars`（累计）。

安全点：`snapshot()` 复制 pending 行时原来只做 `dict(row)`，`args` 仍与审批端口共享；
裁剪会改写**活的**待审批参数（其 digest 决定审批结果）。现改为 `_copy_pending_row()`：
`args` 是 Mapping/list 时 `copy.deepcopy`，端口原对象保持不变（有测试断言）。
若 args 里没有 >64 字符的字符串叶子（例如全是短字符串的大结构），④ 仍会把它计入
`omitted_approvals` —— 能裁就裁，不能裁就计数，绝不再「静默消失且 truncated=false」。

## 3. C-05 契约登记：1.8.0 + 第 13 块 append

`models.Message.to_snapshot()` 对纯文本消息不写 `parts`、`Turn.to_snapshot()` 不写
`user_text`，是**线上字段删除**，此前只改 notes、没有 append 块（审查 P1-3）。
本棒按 append-per-minor 纪律补齐：

- `schema/rpc.json` 顶部 `version` **1.7.0 → 1.8.0**；
- `x-vade.appends` 追加第 **13** 块（`contract=C-05`、`version=1.8.0`、
  `adds=[]`、`doc=batch-results/f15-snapshot-contract.md`）：
  - notes 逐项登记 `truncated` / `omitted` / `omitted_turns` /
    `truncated_before_seq` / `content_truncated` / `running_partial`（含
    `truncated?`/`omitted_chars?`）/ `pending_truncated` / `omitted_approvals` /
    `omitted_inputs` / `omitted_tasks` / `args_truncated` / `args_omitted_chars`；
  - `rewrites` 显式写明：纯文本 `messages[].parts` 与 `turns[].user_text` 不再下发，
    客户端必须回退 `content` / 通过 `user_message_id` 找用户消息；
- `ui/attach.notes` 删掉「`parts` 是三端必须渲染的最小字段集」的旧句，改为
  「`parts` 只在含非 text part 时出现；`content` 是最小渲染字段，读 `parts` 的客户端
  对纯文本消息必须回退 `content`」，并补 pending/tasks/args 的截断语义。

配套：

- `tools/check_contracts.py` 新增 `check_snapshot_contract()`：版本必须是 1.8.0、
  append 块存在、`adds=[]`、12 个键都在 notes、`rewrites` 含 `parts`+`user_text`、
  旧矛盾句不得回潮、`content` 最小字段与 `pending_truncated` 必须在 attach notes 里；
- 三处既有版本钉追加 1.8.0（旧版本保留），`check_blob_token` 的 head 钉改 1.8.0；
- `core/tests` 三处「C-05 顶部版本」断言同步 1.8.0（沿用 RFC-0014/0015/0016 的仅改
  版本号与相邻注释的惯例）；
- `docs/rfc/LEDGER.md` aggregate（`rpc_version=1.8.0`、`appends_rpc=13`）与 §1/§4
  口径、`docs/WHERE-ARE-WE.md` 当前数字同步。

## 4. 新增测试（`core/tests/session/test_attach_bounds.py`）

| 用例 | 断言 |
|---|---|
| `test_a_32k_approval_argument_is_clipped_not_dropped` | 32 000 汉字的嵌套 `args` 被折半，审批行仍在；`args_truncated`/`args_omitted_chars`；`pending_truncated=false`；`truncated=true`；审计一行；端口原 args 未被改写；信封 ≤ `MAX_REPLY_BYTES` |
| `test_a_huge_task_list_does_not_take_the_approvals_with_it` | 20×4 000 字 tasks 先被丢（`omitted_tasks=20`），小审批**保留**；证明 ④ 丢 tasks 后重测 |
| `test_dropped_pending_is_counted_flagged_and_audited` | 400 条审批（detail 200 字，低于裁剪阈值）被丢：`pending_truncated=true`、`omitted_approvals=400`、`truncated=true`；`tasks`/`tasks_truncated`/`omitted_tasks` **三键都不存在**；审计带计数 |
| `test_rpc_json_registers_the_snapshot_contract` | schema gate：1.8.0 + 第 13 块 + 12 个键登记 + `rewrites` 含 `parts`/`user_text` + 旧矛盾句已消失 + `content` 最小字段 |

## 5. 门禁与全量

```
python3 tools/check_contracts.py     # contracts: OK (… RFC-0016 blob token green …)
python3 docs/check_docs.py           # 合计 12 项，失败 0 项
python3 tools/check_ledger.py        # ledger: OK (16 RFC rows; rpc 1.8.0 49 methods …,
                                     #            appends 13+9+3)
python3 -m pytest core/tests/session -q   # 245 passed
python3 -m pytest core/tests -q           # 见下
```

全量：`__FULL_PYTEST__`

## 6. 线上 diff（`ui/attach.result.snapshot`）

| 键 | 变化 | 说明 |
|---|---|---|
| `truncated` / `omitted` / `omitted_turns` / `truncated_before_seq` / `content_truncated` / `running_partial` | f12 新增，本棒补登记 | 旧客户端忽略 |
| `pending_truncated` / `omitted_approvals` / `omitted_inputs` | 本棒新增，恒出现 | 阻塞态被丢的可见信号 |
| `omitted_tasks` | 本棒新增，仅当有 `tasks` 键 | tasks 被丢的行数 |
| `pending_approvals[].args_truncated` / `args_omitted_chars` | 本棒新增，仅裁剪时 | args 被裁的标记 |
| `messages[].parts` | **删除**（纯文本消息） | 需 C-05 1.8.0 登记；客户端回退 `content` |
| `turns[].user_text` | **删除** | 通过 `user_message_id` → `messages[]` |
| `tasks` / `tasks_truncated` | 只在 core 有 tasks 组件时出现（不变） | 裁剪不再注入 |

## 7. 未做 / 留给下一棒

- **P1-1（replay）**：重启后 buffer 非空、客户端游标在重启前，`covers()` 仍可能
  replay-only，丢掉 `session/model.set` 这类「只落库不落事件」的状态。需要
  `ReplayBuffer.complete_from`（进程内第一次 add 的 seq）或等价 generation；本棒未动。
- P2 清单（`pop(0)` O(n²)、`truncated_before_seq` 仅在丢 message 时有值、
  第 ⑤ 步可能丢最新一条、`_wire_size` 重复实现、审计限频等）保持原状，不在任务范围。
- 三端客户端未在本仓，`parts`/`user_text` 回退只能由契约注记约束；需要在客户端仓
  做一次兼容性确认（本报告已把要求写进 C-05 notes）。
