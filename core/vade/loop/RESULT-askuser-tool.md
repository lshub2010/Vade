# RESULT · W1-askuser：`ask_user` 注册为 engine 内置工具

批次：`tmp/w1-askuser`（worktree `D:/HermesWorkspace/Vade-wt/w1-askuser`，基于 `68dbcdd`）
日期：2026-11-14 · 状态：**完成，待审查**

## 1. 交付了什么

RFC-0008 的通道（`InputPort`/`InputManager`、`turn/input` RPC、`waiting_input`）已合入但模型调不到 `ask_user`。本批把它注册为 loop engine 的内置工具：

| 文件 | 改动 |
|---|---|
| `core/vade/loop/builtins.py`（新增） | 内置工具机制：`BuiltinTool` / `BuiltinContext` / `BuiltinRegistry` + `ask_user` 定义（schema、描述、fail-closed 校验、handler）。**具体的工具名只活在这个文件里**——engine.py 保持 name-blind（22-C3 §T4，source-grep 测试仍绿） |
| `core/vade/loop/engine.py` | `builtins: BuiltinRegistry` 参数；工具面 = 模块快照 + 内置（LLM tools 数组与 prompt 工具清单都含 `ask_user`）；`no_capability` 门把内置算作能力；派发时快照没有的名字查内置，经 `_call_builtin` 本地执行（不走 router）；`VadeError` → 错误工具结果、turn 继续（§4.4.4） |
| `core/vade/session/__init__.py` | install 时传 `builtins=default_builtins(lambda: getattr(app, "input_port", None))`（惰性解析，input 组件未装也不挂） |
| `core/vade/input/manager.py` | 新增 `ui_attached(session_id)`（§8.4 公开探针，坏探针 fail-closed 读 0）与 `ask(InputPrompt)`（`InputPort` 协议适配：`InputRequest` + `InputOutcome`→`InputAnswer` 映射） |
| `core/tests/loop/test_ask_user_tool.py`（新增） | 10 条测试（七条验收 + handler 映射契约 + prompt/schema 面 + 模块工具共存） |

**C-18 未动**：`[ask_user]` 的 6 个既有键够用，本批没有加任何配置键。schema/ 契约零改动（`check_contracts.py` OK）。

## 2. 工具 schema（发给 LLM 的 parameters）

```json
{"type": "object", "additionalProperties": false, "required": ["kind", "prompt"],
 "properties": {
   "kind":      {"type": "string", "enum": ["free_text", "single_choice", "multi_choice", "confirm"]},
   "prompt":    {"type": "string", "maxLength": 500},
   "options":   {"type": "array", "minItems": 2, "maxItems": 20,
                 "items": {"type": "object", "additionalProperties": false, "required": ["id", "label"],
                           "properties": {"id": {"type": "string", "maxLength": 64},
                                          "label": {"type": "string", "maxLength": 120},
                                          "description": {"type": "string"}}}},
   "default":   {},                     // 仅 UI 预填，超时不采用（§8.1）
   "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600}}}
```

描述明示「需要用户提供信息才能继续时调用；不要确认显而易见的事、不要用它请求授权」。校验 fail-closed：形状走 `validate_arguments`（与模块工具同一道边界），`kind×options` 组合、prompt 空/超长、选项数/重复 id/空 label、`timeout_s` 类型、未知参数全部 `INVALID_ARGUMENT` 拒绝——拒的是问题，不是 turn。

## 3. 执行语义（实现到位的点）

- 调用 → `InputManager.request()` → `turn.input_required`（带 `state:"waiting_input"`）→ turn 进 `waiting_input` → asyncio 阻塞等待（不占 worker 线程）→ 答复后回 `running` → 工具结果。
- **结果形状**：答复 → `ok:true`，模型可见内容恰为 `{"status":"answered","value":...}`；超时/取消 → 事件面 `ok:false, code:"CANCELLED"`（RFC-0008 §5.3/A4 原文），`ToolCallResult.result` 同时携带结构化 `{"status":"timeout"|"cancelled","value":null}` 供调用方读取。`default` 只作为 UI 预填下发，任何路径都不会替用户作答。
- **值是数据**（§9.1）：handler 只把 value 原样放回工具结果，不拼接、不执行；审计只落 `value_digest`/`value_chars`（§9.4，manager 侧已实现，测试 ⑦ 钉死）。
- **无 UI**：handler 先探可用性（`None`/`NullInputPort`/`ui_attached==0`）→ **立即** `UNAVAILABLE`，`ask` 一次都不调（测试里有「探针说没 UI 但 ask 会睡 120s」的端口，预检被删就红）。manager 自己的 `no_ui` 立即取消路径（RFC A11）原样保留，双层兜底。
- **S5 顺序**：传给 manager 的 `emit` 是**裸** session writer，不是 `_turn_emit`——turn 被打断时 `_turn_emit` 的中断守卫会吞掉 `turn.input_resolved`，对话框就永远关不上；裸写保证 resolved 先于 `turn.interrupted` 落 seq。
- 审批检查照走 `_check_approval`（risk 固定 low，与模块工具同一审计口径）；限流/单 turn 单问题/终态拒绝全部由 manager 既有闸门承担。

## 4. 验收（原始输出见提交前的运行记录）

- `pytest core/tests -q`：**944 passed, 0 failed**（基线 934 + 新增 10）≥ 934 ✓
- `tools/check_contracts.py`：**OK** ✓ · `docs/check_docs.py`：**12/12** ✓
- 七条验收测试（`core/tests/loop/test_ask_user_tool.py`）：
  1. **事件 + waiting_input + 答复**：`turn.input_required{state:"waiting_input"}` 落 seq、`log.turn_state==waiting_input`；提交后 `input_resolved{submitted}` 先于 `turn.tool_result`，模型可见内容 `== {"status":"answered","value":"main.py"}`，turn 完成 ✓
  2. （并入 1 + handler 契约测试）答复返回 `{status:"answered", value}` ✓
  3. **超时**：`input_resolved{status:"timeout", by:"system", value:null}`、事件 `code:"CANCELLED"`、模型收到的内容与 answer 槽位均无 `SENTINEL` default ✓
  4. **取消**：`turn/input.cancel` → `input_resolved{status:"cancelled", by:"user"}`、`code:"CANCELLED"`、turn 继续到完成 ✓
  5. **无 UI**：manager ui=0 / 永不返回的端口 / `NullInputPort` 三形态全部立即 `UNAVAILABLE`（<2s、无 `turn.input_*` 事件、pending=0、`ask` 未被调）✓
  6. **非法组合**：13 种坏参数逐条 `INVALID_ARGUMENT` 且 `ask` 未触达；e2e 里模型收到错误结果、turn 继续完成 ✓
  7. **审计脱敏**：`input_response` 行有 64 位 hex `value_digest` + `value_chars`，全审计 JSON 中原值与 prompt 文本 0 命中 ✓
- 变异自验 2 条（改实现→跑新测试→还原，均有测试转红）：
  - **M1 超时返回 default**：handler 在 timeout 分支返回 `{"status":"answered","value":default}` → `test_timeout_returns_timeout_with_null_value_and_never_the_default` + `test_handler_maps_every_answer_to_status_and_value` **2 failed** ✓
  - **M2 无 UI 不立即返回**：删掉 handler 的 `ui_attached<=0` 预检 → 委托进「ask 睡 120s」的端口 → `test_no_ui_answers_unavailable_immediately_and_never_asks` **1 failed**（wait_for 5s 超时）✓
  - 还原后 10/10 绿，`grep MUTATION|mutated|if False` 在本批文件 0 命中。

## 5. 需要审查注意的两个口径

1. **无 UI 的工具结果码是 `UNAVAILABLE`（批次任务书口径），不是 RFC-0008 §8.4 写的 `CANCELLED`**。manager 通道行为未动（A11 的 `cancelled/no_ui` 原样，`core/tests/input/test_no_ui.py` 仍绿）——只有工具层把「没人可问」映射成 `UNAVAILABLE`，让模型区分「没人能问」与「用户拒答」。若审查认为应回归 RFC 原文，改 `builtins.py` 的 `_unavailable` 一处即可。
2. **超时/取消的工具结果**：事件面按 RFC A4 是 `ok:false, code:"CANCELLED"`（模型看到 C-05 错误形状），批次要求的 `{status, value}` 结构化形状落在 `ToolCallResult.result` 与答复场景的模型可见内容上。两处口径在测试里各自钉死。

其余决定：内置名冲突时模块工具优先（llm 名带 `prefix__`，实际不可能撞）；`ctx.emit` 用裸 writer 的理由见 engine `_call_builtin` docstring；`input/manager.ask` 内惰性导入 `loop.ports`，保证 `vade.input` 可先于 `vade.loop` 被导入。
