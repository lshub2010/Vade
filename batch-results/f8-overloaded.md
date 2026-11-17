# f8-overloaded 批次结果

范围：`core/vade/session/manager.py` + `core/tests/session/test_k1_closeout.py`。
分支 `tmp/f8-overloaded`，未 push。

## 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/session/manager.py` | `_enqueue_or_rollback` → `_enqueue_or_fail`，`_rollback_unstarted` → `_fail_unstarted`；docstring 明确写 **roll-forward**（事件 append-only，`turn.queued` / `turn.superseded` / `message.edited` 已持久化，不能撤回；v2 以 `failed{OVERLOADED}` 终态留在版本链里，绝不回到 `queued`）。新增 `_refused_before_start()` 与 `_prior_overloaded()`，并在 `submit` / `regenerate` / `edit_resubmit` 的 `find_turn_by_client_msg` 幂等分支里，命中「`FAILED` + `error_code==OVERLOADED` + `started_at is None`」时抛 `OVERLOADED`（`data.reason="prior_attempt_failed"`，带 `turn_id` / `client_msg_id`），不再 200 返回从未入队的尸体。regenerate/edit 的容量预检注释改为「预检只挡住已满的队列；emit 之后的竞态走 roll-forward」，不再声称「拒绝不会留下指向未入队 v2 的指针」。`submit` 步骤 2 的幂等注释补充了唯一例外。 |
| `core/tests/session/test_k1_closeout.py` | 扩充 4 个 OVERLOADED 用例并新增 2 个：<br>• `test_submit_overloaded_refuses_before_anything_is_written`：预检拒绝不写任何行；腾出容量后**同一 `client_msg_id` 照常提交成功**（没有尸体 → 不触发拒绝）。<br>• `test_submit_retry_after_an_enqueue_race_is_overloaded_not_200`：竞态回滚后同键重试 → `OVERLOADED` + `prior_attempt_failed`，`turn.queued` 只有一条；换新键同一文本真正入队并 `completed`。<br>• `test_overloaded_after_a_started_turn_is_still_an_idempotent_result`：模型解析阶段抛 `OVERLOADED`（`started_at` 非空）仍按普通幂等结果返回原 turn，证明判别条件没有过宽。<br>• `test_regenerate_rolls_forward_to_a_failed_version_when_enqueue_fails`：`turn.superseded` → `turn.queued` → `turn.failed` 顺序；v1.`superseded_by` == failed v2；v2 `parent_turn_id`/`version=2`/`started_at=None`；同键重试 `OVERLOADED`；对 failed v2 换新键 regenerate 得到 v3 且复用同一 `user_message_id`。<br>• `test_edit_resubmit_rolls_forward_to_a_failed_version_when_enqueue_fails`：`message.edited.new_turn_id` / `superseded_by_message_id` 与 queued v2 一致；旧消息 `superseded_by` == 新消息 id；v1.`superseded_by` == failed v2；同键重试 `OVERLOADED`；编辑 failed edition 的消息换新键可继续成 v3。<br>• 原 `test_submit_rolls_back_...` 更名为 `test_submit_terminates_...`，模块 docstring 记录两个后续缺陷。 |

## 语义选择（P1-1）

选 **roll-forward**，不做同事务撤销。理由：`turn.queued` / `turn.superseded` / `message.edited` 在 `enqueue` 之前已经提交并广播；events 是 append-only，撤销行状态而不撤销事件会让已经收到事件的客户端永久指向一个不存在的 turn，比留一个可渲染的 `failed` 版本更糟。因此：

- 预检（队列已满）仍然发生在任何 supersede 写入之前 —— 这种情况不会产生任何 v2；
- emit 与 enqueue 之间的竞态（并发 submit 抢走最后一个槽）承认 roll-forward：v2 是 `failed{OVERLOADED}` 的终态 head，v1 的 `superseded_by` 和 `message.edited` 指针保留并指向真实存在的 v2 行/消息；
- 恢复路径：对 failed head 换新 `client_msg_id` 再 regenerate/edit，或换新键重发 submit。测试逐条钉住。

## 幂等重试（P1-2）

`find_turn_by_client_msg` 命中 `FAILED && error_code=="OVERLOADED" && started_at is None`（只有 `_fail_unstarted` 会产生：没有引擎跑过）时返回 `OVERLOADED`，`data={"reason":"prior_attempt_failed","turn_id":...,"client_msg_id":...}`，message 明确要求换新 `client_msg_id`。`started_at` 是判别键：真正出队后失败的 turn（例如模型解析/Provider 压力）`started_at` 非空，仍按幂等结果返回原 turn。终态不能复活（`TurnState` 文档：terminal states are never left again；C-06 禁止终态后再发该 turn 的 `turn.*`），所以没有采用原地重新入队。

## 验证

- `python -m pytest core/tests/session/test_k1_closeout.py -q` → **10 passed**
- `python -m pytest core/tests/session -q` → **188 passed**
- `python -m pytest core/tests/session/test_k1_closeout.py core/tests/session/test_regenerate.py core/tests/session/test_edit_resubmit.py core/tests/session/test_queue.py -q` → **44 passed**
- `python -m pytest core/tests/e2e/test_turn_lifecycle.py -q` → **4 passed**
- `python -m py_compile`（两个改动文件）→ 通过
- 未跑全量 pytest（按要求）。`core/tests/integration/test_core_wiring.py` 在 setup 阶段因缺少 `protocol/core/python/libvade_protocol.so` 报 5 个 ERROR，是环境/预存在问题和本次改动无关。

## 发现（未在允许文件范围内修）

- `core/vade/session/queue.py::_finalize` 接受 `error_code` / `error_message` 参数却从不写回 turn 行，因此引擎侧失败（含 Provider `OVERLOADED`）落库后 `error_code` 为 `NULL`。本次判别只依赖 `_fail_unstarted` 自己写的 `error_code`，不受影响；但「失败原因入库」本身是另一个缺陷，应由 queue 棒处理。
- `_prior_overloaded` 的 `data.reason="prior_attempt_failed"` 是现有 `OVERLOADED` 之外的新 reason 值（C-04 码不变）。若上游希望 `data` 严格限定，可改为携带 `retryable:true` 的既有字段；当前测试按此形状钉住。
