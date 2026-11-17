# c1-turninfo 批次结果（规划棒 #3：失败 turn 终态信息完整化）

## 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/session/queue.py` | ① `_finalize()` 真正把 `error_code`/`error_message` 落到 turn 行（`replace(turn, ...)` 后再随 `turns=` 一起提交）。此前两个参数只是签名里收着、从不使用，所以 `turn.failed` 事件有 `code/message`，行上却永远是 NULL。② 失败分支写 usage 改用 `_failed_usage(exc, turn)`：能读到 loop 附在异常上的测量值就用它，读不到才退回 `_unmeasured_usage`。 |
| `core/vade/loop/engine.py` | `run_turn()` 拆成「外壳 + `_run_turn(meter)`」：新增 `_UsageMeter` 持有本次 turn 的 `usage/reported/steps`，累计由 `absorb(call_usage)` 完成（成功调用与中断调用同一入口）。外壳 `except BaseException` 时把 `meter.record()` 挂到异常上（`USAGE_ATTR="vade_usage"` / `attach_usage` / `failure_usage`），再原样 `raise`。异常是失败路径上唯一跨 engine→queue 边界的对象，`TurnOutcome` 在失败时根本不存在。 |
| `core/tests/session/test_queue.py` | +3 测试（真实 `SessionLog` + `LoopEngine` 接线）：error_code 落库 / attach snapshot 可读 / 幂等重试返原 turn；已报账后失败 → `source=provider` 且 token 保留、`usage.get` 与审计 `llm.call` 相等；从未报账 → `source=none`、零计数、`partial_turns=1`。 |

## 关键行为（可直接对上验收）

1. **error_code 落行**：`queue._finalize()` 收到 `error_code/error_message` 就写进 `turns` 行，与 `turn.failed` 同一次提交（`emit(..., turns=(turn,))`）。因此 `ui/attach` 的 snapshot（`session.turns[].error_code`）、`session/get`、重连客户端都能读回失败原因；Vade 异常保留原 C-04 code，非 Vade 异常按既有契约落 `INTERNAL` + 通用文案（不泄露 traceback）。
2. **已报账的 token 不再被覆盖**：provider 报过 12/3/15 后流断 → turn 行 `source=provider`、`prompt/completion/total = 12/3/15`、`steps=1`、`model_id/provider` 与原记录一致；`turn.failed.usage` 同一份。
3. **从未报账不估算**：provider 在第一个 usage delta 前就死 → `source=none`、三个计数为 0，`session/usage.get` 把该 turn 计入 `partial_turns`。审计 `llm.call` 行同口径（total 0 / source none）。
4. **usage.get 与审计对齐**：`UsageAccounting.report()` 的 `chat.total_tokens` 等于该 turn 所有 `llm.call` 行 `extra.total_tokens` 之和（测试里就是 15 == 15），`partial_turns == 0`，`by_model` 也归到同一模型桶。
5. **幂等**：同 `client_msg_id` 重投返回原 turn（`position=0`、无第二行、无第二个 `turn.failed`），错误字段不被重放清空。

## 验证

- `python -m pytest core/tests/session/test_queue.py -q` → **24 passed**（含 3 个新增）
- `python -m pytest core/tests/session core/tests/loop core/tests/llm -q` → **303 passed**
- `python -m pytest core/tests/model core/tests/e2e/test_turn_lifecycle.py core/tests/policy/test_timeout.py core/tests/session/test_replay_catalog.py -q` → **68 passed**
- `python tools/check_contracts.py` → **contracts: OK**（C-03/C-04/C-05/C-06/C-18、event gate 全绿；本次没有新增/改名事件，`turn.failed` payload 形状未变）
- 反向验证：`git checkout ab3f73b -- core/vade/loop/engine.py core/vade/session/queue.py`（实现回退、测试保留）→ **3 failed**（`assert row.error_code == "UNAVAILABLE"` 实得 `None`；usage 实得 `source=none`/0）。恢复后 3 passed，说明新测试不是空转。
- 未跑全量 pytest（按要求）。

## 需要注意 / 后续

- **中断（interrupted）turn 仍然不写 usage**：`interrupt()` 必须在模块回话前就把终态事件写出去，此刻任务还没 unwind，异常对象还不存在，所以读不到 meter。被中断 turn 的 token 仍只在审计 `llm.call` 行里（`session/usage.get` 把它计入 `partial_turns`）。要填平需要「先记 `source=pending`、再由 worker 回填」的两段式写入，属于契约改动，不在本棒范围。
- `_finalize()` 只在参数非 None 时覆盖，所以 `COMPLETED`/`INTERRUPTED` 路径不会把历史 error 字段抹成 NULL；反过来也意味着「一个 turn 从 failed 变 completed」不会被本函数支持（当前没有这种迁移）。
- `core/tests/model/test_budget_trusted_usage.py` 里 `_FailSecondTurn` 场景的断言消息仍写着 “the queue records a failure as unmeasured”：该用例的 executor 是手写的、确实没测量，行为没变，只是措辞现在不够精确（在 3 文件预算外，未改）。
- 依赖方向：`queue._failed_usage()` 用函数内 import 读 `..loop.engine`，避免 session 层在模块作用域反向依赖 loop（否则 `vade.session.queue` → `vade.loop` → `vade.session.models` 的导入链会把引擎拖进每次队列导入）。
- `k7-usage.md` 末尾那条「失败 turn 的用量能否写回 turn.usage 是独立问题」本次已闭合：现在写回，且与审计口径一致。
