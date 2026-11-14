# RFC-0006 · turn 语义补充 · RESULT

分支 `tmp/rfc0006`，基于 `3e369c0`。

## 实现点

| 区域 | 内容 |
|---|---|
| 中断留痕（§2/§3） | `Message.partial` / `superseded_by`；`manager._partial[turn_id]` 累积 delta；`queue.partial_provider`；`_finalize_interrupted()` **只在文本非空时**写 `partial=true` 的 assistant 消息，`turn.interrupted` 只带指针（`partial_message_id` / `partial_chars`） |
| 终态墓碑 | `OrderedDict`（上限 1024）：终态事件后到达的 `turn.delta` / `turn.tool_*` 直接丢弃，**不分配 seq** |
| 单事务收尾 | `_finalize` 把 turn 行 + 消息 + 事件放进同一事务（`emit(..., turns=(...))`） |
| 工具卡字段（§4） | 新增 `core/vade/loop/toolcard.py`：canonical → 键脱敏（整词切分）→ 值擦洗 → 形状压缩 → **512 字节上限** → 按 risk 选模式（low/medium `redacted`，high `typed`，未知工具按 `high`）；`args_digest` **未动** |
| 工具结果 | `duration_ms` / `truncated` / `result_ref`（`msg:<id>`） |
| 重生成/编辑重发（§5） | `SessionLog.regenerate()` / `edit_resubmit()`；`turn.superseded` → `turn.queued`（编辑还先发 `message.edited`）同一 `emit_batch`；线性版本链（`root_turn_id`/`parent_turn_id`/`version`/`superseded_by`）；拒绝用 `INVALID_ARGUMENT` + `data.reason`（`turn_active`/`not_tail`/`not_head`/`already_superseded`/`client_msg_id_conflict`）；**`session/fork` 不注册** |
| 流式多段（§6） | `_PartCursors`：每个 `turn.delta` 带 `part_index` / `kind`；`max_steps` 提示与无能力提示为 `kind="notice"` |
| 长度上限（§7） | `core/vade/session/limits.py`：`TEXT_LIMIT=32000`（按 **code point** 计），`submit` 与 `edit_resubmit` 共用；超限 `PAYLOAD_TOO_LARGE` + `{field, limit, actual, unit}`，**失败不落库** |
| 契约 | `schema/events.json`：两个新事件 + 五组字段，冻结字节不动，增量记在 `x-vade.appends`（version `1.1.0`，`rewrites:"none"`）；`schema/rpc.json` 两个方法 + appends；`errors.json` 与 `turn_states` **未动**；`tools/check_contracts.py` 扩了 required 数组钉死、字段集合、appends/rewrites、`session/fork` 缺席、`TEXT_LIMIT == attach.text_max_chars` |

## 测试

新增 7 个文件：`session/test_interrupt_partial.py`、`test_regenerate.py`、`test_edit_resubmit.py`、`test_text_limit.py`、`loop/test_tool_card_fields.py`、`test_args_redaction.py`、`test_delta_parts.py`；另更新 `test_queue.py` 里一个 fake `emit` 的签名。

```
pytest core/tests -q      → 530 passed, 5 warnings（489 基线 + 41 新）
tools/check_contracts.py  → contracts: OK (… 33 error codes …)
docs/check_docs.py        → 合计 12 项，失败 0 项
```

## 与 RFC 的偏差 / 缺口（诚实记录）

1. `filter_superseded` 在 `loop/context.py`，但由 session 的 history provider（`session/__init__._history_of`）调用，不是 `engine.run_turn` 直接调。
2. `args_preview_truncated` 只在 512 字节兜底触发时为真；**形状压缩**（§5.2 第 4 步）不置这个标志。
3. 验收 A15（编辑后引用旧内容）与完整 A7（旧客户端 fixture）**没写成测试**；A4 是队列级工具步测试，没走真实模块。
4. base64/hex 擦洗规则故意偏激进（`[A-Za-z0-9+/]{40,}`），长字母数字串会被打码成 `***`。
