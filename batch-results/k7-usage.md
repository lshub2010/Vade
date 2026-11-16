# k7-usage 批次结果

## 范围修正

任务写的是 `core/vade/session/usage.py`，仓库里没有这个文件：用量统计与预算门禁实际在 **`core/vade/llm/usage.py`**（`core/vade/session/__init__.py` 组装 `UsageAccounting` 时也是从 `..llm.usage` 导入的）。本棒改的是 `core/vade/llm/usage.py`，没有新建 `session/usage.py`。

## 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/llm/openai_compat.py` | 流式请求体加 `stream_options: {"include_usage": true}`（仅 `stream=True`；非流式请求带它会在严格服务端 400）。`parse_chunk` 对 `choices: []` + `usage` 的末尾 usage-only chunk 保留测量值并显式注释：它是 `Delta(usage=...)`，不是空结束。 |
| `core/vade/llm/usage.py` | `_trusted()` 改为「从新到旧找证据」：带真实 `source` 的 turn → 可信（后续 failed/interrupted 的无测量 turn 不会把可信账本推翻）；最近一个 **completed** turn 的 source 缺失或 `none` → 不可信；failed/interrupted 且无测量 → 跳过继续看；空会话 → 可信。`gate()` 的候选排序改为 `(severity, 超限比例, day 优先)`：session/day 同时超限时不再总报 session——先报超出比例更大的那个；比例完全相等时归 day（日预算拒绝所有会话，是更外层的约束）。 |
| `core/tests/llm/test_openai_compat.py` | +3：流式请求必须带 `include_usage`；非流式不带 `stream_options`；`finish_reason` 之后的 usage-only chunk 走通整条流（文本不重复、usage 保留、`reasoning_tokens` 被 flatten）。 |
| `core/tests/model/test_budget_trusted_usage.py`（新） | +7：真实 `LoopEngine` + `OpenAICompatProvider`（`httpx.MockTransport`，服务端只有收到 `include_usage` 才发 usage chunk）→ turn.usage=`source:"provider"`/42 tokens → `_trusted()==True` → 下一次 submit 真的抛 `BUDGET_EXCEEDED`（data.scope=session, used=42, limit=10），且被拒的 turn 没打到 provider；`_trusted` 的四种证据边界；failed turn 不抹掉既有可信测量；三种 scope 平局（day 超得更多→day；完全平局→day；session 超得更多→session）。 |

## 关键行为（可直接对上验收）

1. **include_usage**：请求体只在流式时出现 `stream_options`；末尾 `choices:[]`+`usage` 的 chunk 会被 `parse_chunk` 转成只带 usage 的 `Delta`，engine 本来就累加 `delta.usage`，所以 `turn.usage.source` 从 `none` 变成 `provider`。
2. **预算真生效**：`_trusted()` 现在把「provider 是否报账」当作证据问题，而不是「最后一条 turn 运气如何」。一次网络错误/失败 turn 不会再让整个会话的硬阻断永久降级。端到端测试证明 `BUDGET_EXCEEDED` 确实在 submit 前抛出（不入队、不落用户消息）。
3. **平局 scope**：`max()` 原来在 severity 相等时取第一个候选（session），现在按超限比例裁决、精确平局归 day。

## 验证

- `python -m pytest core/tests/llm/test_openai_compat.py -q` → **7 passed**
- `python -m pytest core/tests/model/test_budget_trusted_usage.py -q` → **7 passed**
- `python -m pytest core/tests/model core/tests/llm -q` → **74 passed**
- `python -m pytest core/tests/loop core/tests/test_skill/test_catalog_message.py core/tests/session -q` → **285 passed**
- 反向验证：`git stash push -- core/vade/llm/usage.py core/vade/llm/openai_compat.py` 后跑两个新测试文件 → **6 failed, 8 passed**（失败点分别是缺 `stream_options`、usage 记录成 none 后门禁不触发、failed turn 抹掉可信账本、平局报 session）。`stash pop` 后全绿，说明新测试不是空转。
- 未跑全量 pytest（按要求）。

## 需要注意 / 后续

- `stream_options` 是 OpenAI 标准字段，但并非所有「OpenAI 兼容」服务都接受；不接受的会返回 400，而 4xx 按当前策略不重试。如果以后接入这类服务，需要按 provider 配置开关（本次不做，避免把兼容性问题带进配置面）。
- `stream=False` 时适配器仍然用 SSE 解析路径（既有行为，本次未改）；这次只是不再往非流式请求体里塞 `stream_options`。
- 若一次 turn 中途失败，queue 会用 `_unmeasured_usage`（`source=none`）覆盖该 turn 的真实用量；审计 `llm.call` 行仍保留已花的 token。`_trusted()` 现在不会再被这条记录误导，但「失败 turn 的用量能否写回 turn.usage」是 queue/engine 的独立问题，不在本棒文件范围内。
