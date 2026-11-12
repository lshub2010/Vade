# M6 · `model` 模块

| 项 | 值 |
|---|---|
| Agent 代号 | `m6-model` |
| 波次 | W5 |
| 依赖 | C4 的 SDK v0.1；C3 的 `ModuleLLMProvider`（已在 C3 T5 实现，默认关闭） |
| 目录所有权 | `modules/model/` |
| 出口 | 五道门 + core 切到模块后行为不变 |

## 一、一句话任务

证明「模型也可以是模块」：把 LLM 调用搬出 core，core 一行不改就能换供应商；换完之后对话行为与内置适配器一致。

## 二、必读

- `docs/design.md` §1（`llm-adapter` 可被替换）、§15.2（model 规格）
- `docs/requirements.md` FR-6.2 / FR-6.3、D5、A14
- `sdk/PYTHON_SDK.md`

## 三、目录

```
modules/model/
├── manifest.json
├── src/model_module/
│   ├── tools.py        # 纯函数：请求构造、响应规整（零 SDK import）
│   ├── provider.py     # OpenAI 兼容客户端
│   └── server.py
├── tests/{test_tools.py,test_provider.py}
├── Dockerfile
└── README.md
```

## 四、工具规格

| 工具 | 参数 | risk | timeout |
|---|---|---|---|
| `chat` | `messages: list, tools: list = [], temperature: float = 0.7` | low | 120s |

- `concurrency = "exclusive"`，manifest 带 `priority = 100`（数值越大越优先；core 有多个 model 模块时取最高，FR-6.3）。
- `messages` 校验：≤ 200 条；每条 `role ∈ {system,user,assistant,tool}`；单条 ≤ 64KB；总量 ≤ 512KB；超限 `INVALID_ARGUMENT`。

## 五、返回契约（必须与内置适配器一致）

```json
{"ok": true, "result": {
  "message": {"role": "assistant", "content": "...", "tool_calls": [
    {"id": "call_1", "type": "function", "function": {"name": "fs__read_file", "arguments": "{\"path\":\"/a\"}"}}
  ]},
  "usage": {"prompt_tokens": 12, "completion_tokens": 34},
  "finish_reason": "stop"
}}
```

- `tool_calls[].function.arguments` 必须是**字符串**（OpenAI 的形状），不是对象——core 侧按字符串解析，形状不一致会静默丢调用。
- 上游返回非 OpenAI 形状时，在 `provider.py` 里规整成上面的结构；**不要**把供应商字段透传给 core。

## 六、实现

- 配置：`[model] base_url / model / api_key_env / timeout_s / max_retries`；key 只从环境读。
- 重试：429/5xx/连接错误最多 2 次，指数退避 + 抖动；4xx 不重试。
- 超时：单次 120s；超时 → `TIMEOUT`（`retriable: true`）。
- 日志：只记 `messages_digest`（sha256 前 16 位）、条数、token 用量、耗时。**绝不记内容**。
- v1 不做流式：模块返回完整响应，core 把它作为**一个 delta** 推给 UI。README 必须写明这个限制与影响（首字延迟 = 整段生成时间）。
- 不在模块内做工具执行——只返回 `tool_calls`，执行是 core 的事。

## 七、core 侧切换（由 C3 提供，不由本模块改 core）

```toml
[llm]
provider = "module"          # builtin | module
model_module = "com.example.model"
fallback_to_builtin = false  # 模块不在线时是否回落
```

`ModuleLLMProvider` 通过 router 调 `model/chat`；模块不在线：`fallback_to_builtin=true` 时回落，否则 `turn.failed(UNAVAILABLE)`。

## 八、验收（五道门）

```bash
cd modules/model

python -m model_module --standalone chat \
  --args '{"messages":[{"role":"user","content":"用一句话说明什么是模块化"}]}'

pytest tests -q
```

必测：
1. 与内置适配器对比：同一 mock 上游、同一输入 → `message.content` 与 `tool_calls` 结构一致；
2. `arguments` 是字符串；
3. 429 → 重试 2 次后成功；400 → 不重试；
4. 消息超 512KB → `INVALID_ARGUMENT`；
5. 日志中 grep 用户输入原文 → 0 命中；
6. core 切换 e2e：`provider=module` 下完成一轮含工具调用的对话；关掉模块 → `fallback_to_builtin=false` 时 `turn.failed` 且错误可读；
7. 两个 model 模块（priority 100 / 50）→ core 选 100。

## 九、禁止

- 禁止记录 prompt / completion 原文。
- 禁止把供应商特有字段透传给 core。
- 禁止在模块里执行工具。
- 禁止用 `priority` 抢多个实例（必须 `exclusive`）。
- 禁止在 `tools.py` import SDK。

## 十、交付格式

按 §三，额外附：与内置适配器的响应 diff（应为空）、两次重试的时间戳日志、core 切换前后的对话事件流对比。
