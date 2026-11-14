# s-usage-inv — `session/usage.get` 的硬不变式，用测试钉死

分支 `tmp/s-usage-inv`（基于 `dev_gingshan`），worktree `D:/HermesWorkspace/Vade-wt/s-usage-inv`。
**未 push、未合并。** 实现代码零改动——这一轮的产出是测试，不是修复。

## 1. 侦察：实现在哪、现有测试钉了哪几条

实现不在 `core/vade/session/usage.py`（该文件不存在），而在 **`core/vade/llm/usage.py`**；
RPC 入口在 `core/vade/session/rpc.py:302-312`（`session_usage_get` → `accounting.report(session)`）。

| 不变式 | 实现位置（`core/vade/llm/usage.py`） | 现有测试 | 判定 |
|---|---|---|---|
| `total_tokens = chat + aux` | L171 `total = chat["total_tokens"] + aux["total_tokens"]` | `test_usage.py:116`（10+0）、`:146`（100+60） | **部分**：只造过 completed + aux，没有三终态混合，也没断言 `budget.used == total` |
| `chat` 汇总所有终态 turn | L31 `TERMINAL_MEASURED`、L145 `[t for t in session.turns if t.state in TERMINAL_MEASURED]` | `test_usage.py:162-176` | **部分**：断言的是 `turns` 数（3）而非 token；failed/interrupted 的 usage 全是 0 token |
| `interrupted` 的 token 计入 `chat` | L31、L145、L155-156 | `test_usage.py:168` `usage_record(0, source="none")` | **未覆盖**：0 token 无法证伪一个漏掉 `interrupted` 的过滤 |
| `failed` 的 token 计入 `chat` | 同上 | `test_usage.py:167` 同样 0 token | **未覆盖**（同上） |
| `cancelled` 不计入 | L31 不含 `CANCELLED` | `test_usage.py:169,174`（999 token 不计） | 已覆盖 |
| `partial_turns` 只数 `source="none"` 的终态 turn | L151-154 `if not usage or usage.get("source") == "none"` | `test_usage.py:176`（2）、`test_budget.py:143`（2） | **部分**：空 `usage={}`（RFC-0005 Q4 的历史行）与"provider 真报 0"都未覆盖 |
| `aux` 来自 audit 且排除 `purpose="chat"` | L123-126 `_purpose`（缺省回落 `"chat"`）、L164 | `test_usage.py:115,145` | **部分**：`purpose` 缺失 / 显式 `null` 两种写法未覆盖 |
| aux 分页越过 reader 的 500 行上限 | L100-121 `_llm_calls` cursor 走页 | `test_usage.py:122-153`（`page=2`） | 已覆盖 |
| `day` 是 UTC 边界 | L133-141 `_day_start`（`gmtime` + `calendar.timegm`） | `test_usage.py:179-197`（今天 / 两天前） | **部分**：秒级边界（00:00:00Z vs 前一毫秒）与本机时区（UTC+8）未覆盖 |
| `day` 只给聚合、不给别会话明细 | L167-169 | `test_usage.py:188-196` | 已覆盖 |
| `limit=0` → `ratio=null`、`state="off"` | L64-72 `budget_state`、L197-209 `_budget_block`（L203） | `test_budget.py:230-231` 逐字段断言 session 侧 | **部分**：day 侧（`core_limit=0` → `off`）没测 |
| `state` 四值边界 | L64-72 | `test_budget.py:184-186`（80=0.8×100 → warn；120>100 → exceeded） | **部分**：`used == limit` 正好相等、79、`ratio=1.0` 未覆盖；无 `budget_state()` 直接边界矩阵 |
| 两个已附加设备得到相同数字与相同 `budget.state` | `rpc.py:302-312`（无状态） | 无 | **未覆盖** |
| 空 session（零 turn）不崩、chat 全 0 | L144-195 | 无（`report()` 从未在零 turn 的会话上调用过） | **未覆盖** |
| `by_model` 的 `model_id: null` 不被丢 | L157-160、L181-186 | `test_usage.py:147-150`（只有非 null） | **未覆盖** |
| RPC 直传 report / NOT_FOUND / UNAVAILABLE | `rpc.py:302-312` | `test_usage.py:200-222` | 已覆盖 |
| `usage.include_aux=false` 只判 chat | L172 | `test_usage.py:155-159` | 已覆盖 |

`grep -rn "usage" core/tests/session/` 只有 `test_queue.py:770,803-825` 的 `turn.completed.usage` 负载键，
没有任何 `session/usage.get` 语义测试；`core/tests/admin/test_config_usage.py` 测的是 admin 的 `config/usage`，不是本方法。

## 2. 新增测试：`core/tests/model/test_usage_invariants.py`（12 个函数 / 24 个用例）

只补上表里"未覆盖"与"部分"的部分，一条不变式对应一条断言来源：

| 测试 | 钉住的不变式 |
|---|---|
| `test_total_is_chat_plus_aux_over_completed_failed_and_interrupted` | `total == chat + aux`（completed 100 + failed 40 + interrupted 60 + aux 40 = 240），且 `budget.used == total_tokens`，`day.session_tokens == 240` |
| `test_an_interrupted_turn_contributes_its_tokens_to_chat` | 70 token 的 interrupted turn：`chat.prompt/completion/total == 50/50/100`，`partial_turns == 0` |
| `test_a_failed_turn_contributes_tokens_and_cancelled_contributes_none` | failed 40 计入、cancelled 999 不计入、`turns == 2` |
| `test_partial_turns_counts_unmeasured_terminal_turns_only` | 5 个终态 turn → `partial_turns == 3`（`source="none"`、空 `usage={}`×2）；provider 真报 0 **不算** partial |
| `test_aux_excludes_a_chat_call_however_its_purpose_is_spelled` | `purpose="chat"` / 缺失 / `null` 都排除；compaction+memory+evolve = 60/3；别的会话不串 |
| `test_the_day_boundary_is_utc_midnight_to_the_second` | `ts == 00:00:00Z` 计入、`-0.001s` 不计入、`+86399s` 计入 → `core_total == 150` |
| `test_the_day_key_is_utc_even_when_the_host_local_date_differs` | UTC 20:30（本地已是次日）：`day == "2031-03-05"`，本地 16:00 的行仍计入（本地日界会丢它） |
| `test_zero_limits_are_off_with_a_null_ratio_on_both_budgets` | `limit=0` → session `ratio is None`/`off`，day `core_limit=0` → `off`，而 `used`/`core_total` 照报 125 |
| `test_budget_state_edges`（12 个参数） | 79→ok / 80→warn（正好 `limit×warn_ratio`）/ 99→warn / 100→exceeded（正好 `== limit`）/ 101→exceeded / limit=0→off（任意 used）/ `warn_ratio=1.0` 时 warn 不可达 |
| `test_the_report_carries_the_state_edges_and_their_ratio` | 同上四条边界经 `report()`：`ratio` 0.79 / 0.8 / 1.0 / 1.01，`enforced` 只在 exceeded 且 block 时为 true |
| `test_two_attached_devices_read_identical_numbers` | 两条连接 `ui/attach` 同一会话后各自 `session/usage.get` → 整个 dict 逐字段相等，`budget.state` 同为 `exceeded`；跨会话 day 聚合 650 |
| `test_an_empty_session_reports_zeros_and_a_ratio_that_follows_the_limit` | 零 turn：chat 全 0、`by_model == []`、`limit=0 → ratio None/off`、`limit=200 → ratio 0.0/ok`，不崩 |
| `test_by_model_keeps_a_null_model_id_bucket` | 两条 `model_id=null` 合并为 `{"model_id": null, "total_tokens": 50, "turns": 2}` 且不被丢掉（按 token 降序排第二） |

## 3. 真 bug：没有

实现没有改动（`git diff` 对 `core/vade/**` 为空）。不是"看着像对"，而是用变异探针确认这些用例确实会咬人：
把实现按下面 5 种方式临时改坏（只在 pytest 进程里 monkeypatch，未落盘），对应测试全部变红：

| 变异 | 结果 |
|---|---|
| `TERMINAL_MEASURED` 去掉 `INTERRUPTED` | `test_total_is_chat...`、`test_an_interrupted_turn...` 2 failed（exit 1） |
| `TERMINAL_MEASURED` 去掉 `FAILED` | `test_a_failed_turn...` 1 failed（exit 1） |
| `_day_start` 换成本地零点（`localtime`+`mktime`） | `test_the_day_key_is_utc...` 1 failed（exit 1） |
| `_purpose` 不把缺失/`null` 回落成 `"chat"` | `test_aux_excludes...` 1 failed（exit 1） |
| `limit=0` 时 `ratio` 给 `0.0` 而不是 `None` | `test_zero_limits...` 1 failed（exit 1） |

另外两条是我自己第一版预期写错、按契约改正的（**不是**放宽断言换绿）：

1. `day.core_total_tokens` 只来自 audit 的 `llm.call` 行（L167-169、RFC-0005 §6.3 表格），
   我漏造了 chat 那一行 `llm.call` 才得到 25 而不是 125 → 补造该行（引擎 `_audit_llm_call` 真实会写），
   断言保持 125 不变。
2. `warn_ratio=1.0` 时 `warn` 数学上不可达（需同时 `used >= limit` 且 `used < limit`；L68-71 先判
   exceeded），我把 `budget_state(99, 100, 1.0)` 的预期从 `"warn"` 改成 `"ok"` 并在参数表里注明原因。

## 4. 门禁

- `pytest core/tests -q`：`1113 passed, 6 warnings in 756.62s (0:12:36)`（基线 1089 + 新增 24）
  - 首次全量跑（与变异探针并发、机器高负载）另有 2 条与本改动无关的时序用例翻红：
    `blob/test_review_fixes.py::test_the_periodic_sweeper_collects_and_stop_leaves_no_task`、
    `task/test_progress.py::test_three_rapid_updates_become_at_most_two_events`；
    二者单独重跑 `2 passed in 26.21s`，空载全量重跑即上面的 1113 passed。
- `python tools/check_contracts.py`：`contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete, RFC-0005 append green, RFC-0010 session metadata green, RFC-0012 subagent family green, RFC-0014 session search green, RFC-0003 layers green, event gate green)`
- `python docs/check_docs.py`：`合计 12 项，失败 0 项。`
- `python tools/check_ledger.py`：`ledger: OK (14 RFC rows; rpc 1.5.0 47 methods in 3 directions, events 2.0.0 34 entries, 34 error codes, appends 10+8+3; impl paths verified, statuses verified, acceptance tables recounted)`

## 5. 变更文件

- 新增 `core/tests/model/test_usage_invariants.py`（唯一改动；无删除、无 skip、无 `schema/**`/`docs/**` 改动）
