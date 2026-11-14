---
rfc: RFC-0003
title: 提示词注入分层：技能目录消息化、工具渐进披露与 stable/context 分段
status: approved
approved: 2026-11-14
date: 2026-11-13
proposer: 编排（借 dsh / Claude Code 的注入做法；依据 `research/agent-01-injection-comparison.md` R1 / R3 / R6）
affects: C-11 `schema/skill.schema.json`、C3b `core/vade/skill/*`、C-05 `schema/rpc.json`、C-06 `schema/events.json`（只追加）、C-18 `schema/config.schema.json`（附带）
related: RFC-0001（已实施）；RFC-0002（待合入，在 `tmp/debt-a` 分支）
type: 行为破坏性（C3b 注入位置、C-11 顺序语义）；文件层面以追加为主
stage: 已实现并合并
---

# RFC-0003 · 提示词注入分层：技能目录消息化、工具渐进披露与 stable/context 分段

## 1. 摘要

技能目录改为会话内持久 user 消息（digest 变则整条重发，空目录发退休条），正文由 `skill_load` 按需取；工具热常驻、冷工具走 `tools/search`；system prompt 拆 stable/context 两段并用中央序号表锁序。默认 legacy。

## 2. 动机与证据

### 2.1 每轮重算，前缀随时失效

`LoopEngine.run_turn()` 每轮把整条前置消息重新拼一遍（行号为 `a0a5062`）：

```
engine.py:80   snapshot = self._snapshot()                    # 每轮冻结工具表
engine.py:87   parts = PromptParts(persona, tool_block, session_memory, user_message)
engine.py:93   parts = await apply_hooks(parts, hooks)        # C3b 的 loader.render 在这里
engine.py:94   system_messages = assemble(parts)              # 固定六段 system 消息
engine.py:109  self.llm.chat(messages, tools=snapshot.schemas())   # 每一步全量 schema
```

`skill/loader.py:715` 的 `render()` 每轮重跑 `catalog()` → `select()` → `render_catalog()` → `render_bodies()`，把目录和已激活正文重新写进 `skill_catalog` / `skill_bodies`。`skill/render.py` 保证排序确定（name 字典序），但**确定性 ≠ 稳定**：内容变一个字符，`assemble()` 产出的 `prompt-2-skill_catalog` 就换字节，整条前缀从该点起失效。

### 2.2 技能全量注入挤爆预算

`schema/skill.schema.json` 现状是 `l0_token_budget: 3000`（目录）、`body_budget_chars: 12288`、`body_max_chars: 8192`、`max_activated: 3`。三篇正文按上限就是 12 288 字符，与 `memory.session_inject_char_limit: 4200` 同处一个 32k 上下文；再加 tool_block 与 tools schema，留给历史的余量不足一半（估算见 §6）。

### 2.3 工具数线性增长，且每步全量序列化

`loop/tools.py:46` 的 `schemas()` 把 `snapshot.entries` 全量映成 OpenAI `tools` 数组，`render_block()`（同文件 60 行）再把同一批工具写一遍到 system prompt —— 同一份信息付费两次。`engine.py:109` 在 for 循环内调用 `schemas()`，即每一步都重发全量定义。M5 `mcp-bridge` 上线后，工具数 = 模块数 × 每模块工具数 + 上游 MCP 工具数，没有上限。Claude Code 官方给的经验阈值是 30–50 个工具后选择准确率下降[官方]，Vade 的 M1–M6 规划已经越线。

### 2.4 外部实测：同序号 = 缓存炸弹

dsh 社区 #2136 / #2141：`tool:workflow` 与 `tool:cordis` 都拿到 `order=115`，重启后注册顺序翻转，**前缀缓存命中率塌到 0–3%**[社区]。这不是「优化没做」，而是「顺序没定义」直接变成账单。Vade 的 `_SECTIONS` 是硬编码元组（`prompt.py:36`），暂时没这个问题；但 C-11 的 `injection_order` 是 `const` 六元组、**没有段序号概念**，一旦出现第二个写入者（模块 prompt 片段、M4a 记忆段）就会重演。

### 2.5 三条结论

1. **前缀稳定性必须是契约属性**：靠代码自觉必然回退，需要唯一序号 + 测试断言。
2. **会变的东西不能放在不变的东西前面**：技能目录、正文、运行事实都属于「会变」。
3. **少发不等于不发**：目录必须仍对模型可见（否则技能发现归零），只是换个位置发。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 会话内 system 前缀（stable 段 + tools 定义）在「技能安装/禁用/正文变化」时**字节不变** |
| G2 | 技能目录仍每轮对模型可见，且**变化时可被感知**（含空目录的退休语义） |
| G3 | 常驻 `tools` 条目数与会话活跃需求相关，**不随模块总数线性增长** |
| G4 | 每个 prompt 段有唯一序号与唯一写入者，序号冲突由 `check_contracts.py` 挡住 |
| G5 | 全部改动可用一个开关退回 `a0a5062` 的字节级行为 |
| N1 | **不改工具执行语义**：`router.call` / `tools/call` / `tools/cancel` / 实例选择 / 超时 / 幂等不动 |
| N2 | **不改审批**：`ApprovalPort`、`turn.approval_required` / `turn.approval_resolved`、`risk` 判定不动 |
| N3 | **不做子代理**（R4 另立提案）：不引入 child session、`subagent.*` 事件、`parent_id` |
| N4 | 不改 C-06 已冻结条目；不改 `docs/design.md` §9 的分层与 Skin 接口（只引用） |
| N5 | 不做语义检索：`tools/search` 是词法匹配，不引入 embedding（`memory.embedding_model` 属记忆层） |
| N6 | 不引入插件总线 / 运行时能力注册表（dsh 那条路抄不动，见 research §3.4） |

## 4. 方案

### 4.1 装配总览

```
改造前: snapshot() → PromptParts(persona, tool_block, memory, user_message)
        hook: loader.render()   ── 每轮重拼 catalog + bodies 进 system
        assemble()              ── 六段 system 消息，顺序即元组顺序
        step 循环: chat(messages, tools=snapshot.schemas())      # 每步全量

改造后: snapshot()                                   # 表仍冻结，语义不变
        hot = resolve_hot(snapshot, config, session)  # 纯函数，§4.3
        parts = PromptParts(persona, tool_block=render(hot), memory, runtime_facts)
        hook: 只回填 activated_skills（layered 下不再写 catalog/bodies）
        system = assemble(parts)                      # stable + context，按 §4.4 序号
        messages = system + history + catalog_update(states, last)   # §4.2
        step 循环: chat(messages, tools=schemas(hot ∪ discovered))   # 发现只在 step 边界扩张
```

A/B/C 可独立回滚：A 只动 skill 包与历史，B 只动 `loop/tools.py` 的选取，C 只动 `loop/prompt.py` 的排序。但**只有 A+C 同时开才有缓存收益**；B 的收益是上下文体积与选错率。

### 4.2 A · 技能目录改为会话内持久消息

#### 4.2.1 消息形态

首轮请求前（以及每次 digest 变化后）追加一条 **user-role** 消息，只有索引、没有正文：

```text
<system-reminder>
Skills available in this session (names and one-line summaries only):
<available_skills>
- `kb-guide`: 项目知识库的读写规则
- `legacy`: 旧笔记 [unavailable]
</available_skills>
Call `skill_load` with an exact name before following a skill's instructions.
Do not infer a skill's body from its summary.
If a skill was invoked with @name, its body is already in this conversation — do not load it again.
</system-reminder>
```

1. **user role，不是 system。** 现有 `Role = str`（`session/models.py:22`），无 enum 约束，落库不改表；写成 user 与 dsh 一致（模型对 user-role 的持久提醒遵循度更高），更重要的是它排在 messages 里，**追加不触碰 system 前缀**。
2. **不含正文。** 正文只经 `skill_load` 的 tool 结果进入（§4.2.4）。
3. **外壳文案不参与 digest。** digest 只对 entries（`name` + 截断 `description` + 状态 marker）按 name 排序后的 canonical 文本取 sha256；改措辞不重发。
4. **每次重发都是完整替换，不是 diff。** 历史保留旧目录，最后一条生效，文案里明确「以最后一条为准」。

#### 4.2.2 注入时机与重发判定

在 `run_turn()` 里、`assemble()` 之后、`context.fit()` 之前：

```
last = 会话日志里最后一条 skill-catalog 消息（按 seq 倒序）
if last is None:                     append(catalog(state))        # 首次必发
elif digest(state) != last.digest:   append(catalog(state))        # 变化：整条替换
else:                                                              # 不变：一个字都不发
```

- **判定基准是「日志里最后一条可见的目录消息」**，不是内存缓存：进程重启、断线重连、压缩之后都不会误判。
- **空目录也要发**（退休条）：entries 为空时发空 `<available_skills>`，并写明 `No skills are available in this session; ignore any skill names from earlier in this conversation.` 否则历史里的旧目录永远有效 —— 这是 dsh 踩过的坑[官方]，也是本方案唯一必须「多发」的场景。
- **触发源**：install / approve / uninstall / enable / disable / `status` 迁到 `stale` / frontmatter 变化。正文变化不触发（§4.2.4）。

#### 4.2.3 与激活语义的关系（必须显式承认的行为变化）

现在 `render()` 用 `select(user_message, states, limit=max_activated)` 同时做 `@name` 与 `triggers` 子串匹配，命中就把正文塞进 system。layered 下拆成两条路：

| 通道 | 现状 | layered |
|---|---|---|
| `@name` 显式调用 | 正文进 system 的 `skill_bodies` | 正文以 **user-role** 消息注入（`<skill_content name=… revision=…>`），并告知模型不要再调 `skill_load` |
| `triggers` 子串命中 | 正文自动进 system | 不注入正文，只在目录里给该技能加 `[match]` marker，提示模型自己加载 |
| `max_activated` | 每轮最多 3 篇正文进 system | 目录里最多 3 条 `[match]` 标注 |
| `body_budget_chars` | 每轮正文总预算 | 单次 `skill_load` 返回上限；同轮累计仍受同值约束 |

**取舍**：`triggers` 从「自动注入」退化为「提示模型去加载」，弱模型可能不调工具，导致技能不生效。缓解是目录里那句 `Call skill_load …`，加上 `skill_load` 常驻热工具（§4.3）。若 U1-0 验收发现激活率下降，回退路径是「触发器命中仍预加载，但正文追加为一条 user 消息而不是进 system」——仍然不破前缀，只是把决定权从模型拿回 core。

#### 4.2.4 正文按需加载：`skill_load`

- 每次返回一篇正文，带 `revision`；沿用 `body_max_chars` / `body_max_lines` 截断，超限返回 `truncated: true`。
- 正文每次从磁盘重读（现状也是每轮重读），所以改正文**不需要**版本号或目录刷新 —— 目录与正文生命周期分离，与 dsh 一致。
- **工具名核查（已查）**：全仓无 `skill_load`；C-05 的 `tools/*` 只有 `tools/call` / `tools/cancel`；模型侧名字由 `router/names.py:64` 生成为 `f"{prefix}__{tool}"`，**恒含双下划线**，而 `skill_load` 只有单下划线 —— 模块工具在结构上撞不到。建议写成断言（§8 A13），并约定「core 内置工具 = 单下划线，模块工具 = 双下划线」。
- `skill_load` 是 **core 消费**的工具，不进 `router.call()`。这要求 `ToolSnapshot` 能承载 core-native 条目（Q3），本提案不假装它已存在。

### 4.3 B · 工具渐进披露：热常驻 + 冷搜索

#### 4.3.1 热工具名单：四层合并

全部取自本轮 `_snapshot()` 的冻结条目，**绝不重读 registry**（否则 `capabilities.update` 会在 turn 中途改工具集，破坏 C3 的快照冻结不变量）：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `CORE_TOOLS` | `skill_load`、`tools/search` 自身；常量，不可配置 |
| 2 | 已加载技能的 `requires_tools` | 正文进来后它声明的工具随之升热（正是 C-11 现有字段的用途，不改 schema） |
| 3 | 配置 `tools.hot` | C-18 新增，默认 `[]`；**模块不能自荐**（Q4） |
| 4 | 会话粘性 | 本会话内被发现过、或最近一次成功调用过的工具，上限 K=8 |

合并后**按 `llm_name` 升序**，不按时间、不按优先级 —— 优先级只用于截断。理由：插入位置可预测，tools 数组的 diff 只有一个插入点，缓存从插入点起才失效。上限 `tools.hot_max`（C-18，默认 12）；截断后在 `tool_block` 末尾追加一行：`+{N} more tools are available; call tools/search to find one by name or description.` 这一行只在 N 变化时才变，而 N 不随工具总数变（热集合有上限）。

#### 4.3.2 `tools/search`：签名与确定性

```json
{"name": "tools/search",
 "params": {"session_id": "string", "query": "string(1..200)", "limit": "int(1..8), default 5"},
 "result": {"matches": [{"name": "fs__read", "tool_id": "m1.fs/read",
                         "description": "string<=200", "risk": "low|medium|high",
                         "digest": "string(12)"}],
            "digest": "string(12)", "total": "int"}}
```

- **词法匹配**：name 精确 > name 前缀 > description 子串 > `module_id` 子串；同档按 name 字典序。不打相关性分（打分带来不可复现的排序）。
- **只搜本轮快照**：命中但不在快照里的名字（已下线 / 未注册）**不返回**，避免「搜到却调不了」。
- **digest** = `sha256(canonical_json(sorted(matches)))` 前 12 位十六进制；同一 query 两次调用必须字节一致（UI 去重与测试断言的抓手）。
- **描述截断 200 字符**：搜索结果不是第二份 system prompt。

#### 4.3.3 发现后何时可见（关键：不多一轮）

`schemas()` 在 `engine.py:109` 是**每一步**求值，而 `snapshot` 是每轮冻结 —— 这个组合正好允许「step 边界扩张，但扩张源已冻结」：命中项加入本轮 `discovered` 集合（session 级记忆，下一轮沿用），下一步的 `schemas() = hot ∪ discovered`，全部仍取自本轮冻结快照。即同一轮内搜到就能调，不需要多一轮往返；代价是 tools 数组在该 step 变一次，前缀从该点起失效一次。`turn.tool_discovered`（C-06 追加，可选发送）在工具首次进入 schema 时发一次，同 turn 同工具不重复。

### 4.4 C · stable / context 两段 + 中央 SECTION_ORDERS

| 段 | 允许内容 | 变化频率 | 缓存语义 |
|---|---|---|---|
| `stable` | persona、策略常量、热工具清单（`tool_block`） | 会话级 / 工具集变化 | 字节不变即命中；它是整条请求的最前缀 |
| `context` | `session_memory`、`runtime_facts`（cwd、provider/model、时间、设备名、剩余预算） | 每轮可写 | 只影响自身之后的 token；前缀匹配是最长公共前缀，稳定部分仍命中 |

**硬规则**：任何随轮次变化的事实（cwd、模型名、时间戳、token 余量、端口、PID、会话 id）**只允许**写 `context`，禁止写 `stable`，由测试断言（§8 A8）保证而非 review。

| order | section | 通道 | 写入者 | 变更频率 |
|---|---|---|---|---|
| 100 | `persona` | `system.stable` | core | 版本级 |
| 110 | `policy` | `system.stable` | core | 版本级 |
| 200 | `tool_block` | `system.stable` | loop | 热工具集变化 |
| 300 | `skill_catalog` | `history.user` | C3b | 技能集变化 |
| 400 | `skill_bodies` | `tool_result` | C3b | 每次 `skill_load` |
| 500 | `session_memory` | `system.context` | M4a | 每轮 |
| 600 | `runtime_facts` | `system.context` | core | 每轮 |

三个设计点：**300 / 400 保留原序号位**，于是 legacy 六段（`context` 在新表里改名为 `runtime_facts`）的相对次序仍是升序，layered 是 legacy 的超集而非另一套语义，迁移不需要重排历史；`policy`（110）是新插入段，落在 persona 与 tool_block 之间，不改变旧段相对次序。**步长 10 留插入空间**，新段取空位（150、250）永不重排既有段。**同号禁止**，不把「同号按 name 兜底」当特性 —— dsh 的 0–3% 就是同号导致的。

`tools/check_contracts.py` 现有 `check(cond, msg)` 一类断言足够，追加一组即可：

```python
orders = skill["injection"]["orders"]
LEGACY = ["persona", "tool_block", "skill_catalog", "skill_bodies", "session_memory", "context"]
ALIAS = {"context": "runtime_facts"}          # 旧名 → 新段
check(len(set(orders.values())) == len(orders), "C-11: SECTION_ORDERS 有重号")
check(set(skill["injection"]["stable"]) | set(skill["injection"]["context"]) == set(orders),
      "C-11: 段清单与序号表不一致")
check([ALIAS.get(s, s) for s in LEGACY] == sorted(ALIAS.get(s, s) for s in LEGACY),
      "C-11: legacy 六段在新序号表里必须保持升序")
check(max(orders[s] for s in skill["injection"]["stable"])
      < min(orders[s] for s in skill["injection"]["context"]), "C-11: context 必须在 stable 之后")
```

## 5. 契约变更

### 5.1 汇总

| # | 契约 | 改动 | 判定 | 兼容策略 |
|---|---|---|---|---|
| 1 | C-11 `schema/skill.schema.json` | 新增 `injection` 对象（`schema_version/stable/context/channels/orders`）；`injection_order` 保留为 legacy 六段的升序投影 | **追加字段**；语义破坏（顺序含义变两段式） | 老读者仍读 `injection_order` 且值不变；新读者读 `injection`；一致性断言挡漂移 |
| 2 | C3b `core/vade/skill/*` | `render()` 不再写两个 slot；新增 `catalog_digest` / `catalog_message` / `load_body` | **破坏性**（行为） | `prompt.injection=legacy` 走旧路径，逐字节不变 |
| 3 | C-05 `schema/rpc.json` | 登记 `skill_load` / `tools/search` 为 **core 保留工具名**（模型可见、不路由到模块）；若 Q3 裁定走 RPC 再追加方法 | **追加**（保留名表） | 未实现方不受影响；保留名不得被任何 manifest 声明 |
| 4 | C-06 `schema/events.json` | 追加 `turn.tool_discovered` | **追加**（C-06 只可追加） | 未知事件由客户端忽略；必须同 PR 有 `emit()` 点，否则 CI 红 |
| 5 | C-18 `schema/config.schema.json` | 新增 `prompt.injection`（`legacy|layered`，默认 `legacy`）、`tools.hot`（默认 `[]`）、`tools.hot_max`（默认 12） | **追加，但 root `additionalProperties:false`** | 与 C2 同批发版；默认值等价现状 |
| 6 | C-06（条件条） | `turn.skill_catalog {turn_id, digest, entries, empty}` | **条件性追加**，取决于 Q1 | 若裁定「仅快照可见」则不做 |

### 5.2 C-11：两段式 + 段序号

现状（`schema/skill.schema.json:14` 与 `:131`，同一份 const 写了两遍）是 `["persona","tool_block","skill_catalog","skill_bodies","session_memory","context"]`。提议追加：

```json
"injection": {
  "schema_version": 2,
  "stable": ["persona", "policy", "tool_block"],
  "context": ["session_memory", "runtime_facts"],
  "channels": {"persona": "system.stable", "policy": "system.stable", "tool_block": "system.stable",
               "skill_catalog": "history.user", "skill_bodies": "tool_result",
               "session_memory": "system.context", "runtime_facts": "system.context"},
  "orders": {"persona": 100, "policy": 110, "tool_block": 200, "skill_catalog": 300,
             "skill_bodies": 400, "session_memory": 500, "runtime_facts": 600}
}
```

`injection_order` **不删不改**，语义降级为「legacy 六段在新序号表里的升序投影（`context` → `runtime_facts`）」，所以老客户端、老测试、老 loader 读到的仍是那个数组；新增的 `policy`（110）与改名后的 `runtime_facts`（600）不改旧段相对次序。破坏性体现在**行为**：`skill_catalog` 从 system 第 3 段变成历史消息，任何断言「catalog 在 system 里」的测试都会红 —— 属于 C3b 的测试面，不是外部契约。`injection.schema_version` 缺失时按 1 处理（等价 legacy），因此**不需要**批量改存量技能。

### 5.3 C3b：接口表

| 能力 | 现状 | layered |
|---|---|---|
| `render(parts)` | 每轮填 `skill_catalog` + `skill_bodies` | 返回 parts 不变（两 slot 空），只回填 `activated_skills` |
| `catalog_digest(states)` | 无 | 新增；只算 entries，不含模板文案 |
| `catalog_message(states, last)` | 无 | 新增；`None` = 不重发 |
| `load_body(name, budget)` | 无（逻辑在 `render_bodies`） | 新增；返回 `(text, revision, truncated)` |
| `max_activated` | 每轮最多 3 篇正文进 system | 目录里最多 3 条 `[match]` |
| `body_budget_chars` | 每轮正文总预算 | 单次 load 上限 + 同轮累计上限 |

`render()` 的签名与返回类型**不变**，只是 layered 下两 slot 为空；这样 C3d 的 `skill/proposal`、C5a 管理面、现有 `core/tests/test_skill/*` 都不用改签名。

### 5.4 C-05：`tools/search` 的登记位置

两条路，取决于 Q3。**默认（推荐）**：`tools/search` 与 `skill_load` 作为 core 内置工具进 C-05 的**保留工具名表**，由 core 拦截、不路由模块 —— `tools/call` 是 core→module 方向，而搜索发生在 core 内部，塞进 `client_to_core` 会让「UI 能搜工具」变成无意义的公开能力。**备选**：若 U1 想做「工具发现」面板，则追加 `client_to_core` 方法 `tools/search {session_id, query, limit}`。无论哪条，签名以 §4.3.2 为准，两条只是入口不同。

### 5.5 C-06：只追加

```json
"turn.tool_discovered": {
  "payload": {"turn_id": "string", "tool": "string", "tool_id": "string",
              "query_digest": "string(12)", "total_matches": "int"},
  "required": ["turn_id", "tool", "tool_id"],
  "notes": "只在工具首次进入本 turn 后续 step 的 schema 时发送；同一 turn 内同一工具不重复。"
}
```

两条硬约束：**不能只改 schema** —— `check_contracts.py` 的 `emitted_event_names()` 扫源码里的 `emit(<session>, "domain.action")` 字面量，追加了事件却没有发送点，CI 会以「declared events nobody emits」失败，实现 PR 必须同时有 `emit()`。**C-06 现在没有「客户端必须忽略未知事件」这条明文规则**（现状 6 条 rules 里没有），所以追加事件在协议上安全（不知道就不处理），但建议 C3 在同一次追加里补一条规则；若不补，U1 解析器要显式忽略未知 `turn.*`，不能抛错。

## 6. 缓存与成本分析

> 全部为**估算**。假设：一个工具 schema ≈ 120–200 token（description 20–40 + JSON Schema 80–160）；目录上限取 `l0_token_budget=3000`；正文上限 12 288 字符 ≈ 3.1k token（4 字符/token，见 `skill/render.py:estimate_tokens`）；记忆上限 4 200 字符 ≈ 1.05k；persona ≈ 45 token；`tool_block` 一行 ≈ 35 token。命中按 **0.25×** 计价（OpenAI 自动缓存口径，保守；Anthropic 口径约 0.1×，收益更大）。

| 组成 | 改造前（token，估算） | 改造后（token，估算） |
|---|---|---|
| persona | 45 | 45（stable） |
| `tool_block` 文本 | 840（24 工具 × 35） | 280（8 热工具 × 35） |
| `skill_catalog` | 0–3000，**每轮重拼** | 0（移出 system；历史里一条 400–1500，只在变化时新增） |
| `skill_bodies` | 0–3072，**每轮重拼** | 0（改走 tool 结果，按需） |
| `session_memory` | ≤1050 | ≤1050（context） |
| `runtime_facts` | 现状散落无约束 | ≤120（context） |
| tools 数组 | 3.0k–4.8k（24 工具全量） | 1.0k–1.6k（8 热工具） |
| **合计（不含历史）** | **8.0k–12.0k** | **1.5k–3.1k 常驻 + 按需** |

分场景（全价当量 = 未命中 token + 0.25 × 命中 token；下表数字均为估算）：

| 场景 | 改造前 | 改造后 | 变化 |
|---|---|---|---|
| 会话首轮（无缓存可命中） | 12.0k | 12.5k（多一条目录消息） | **+4%** |
| 技能/工具稳定期第 N 轮（前 40% / 后 85% 命中） | 8.4k | 4.6k | **−45%** |
| 技能安装后的下一轮 | 12.0k（整条前缀失效） | 2.1k（前缀全命中，只有新目录消息全价） | **−83%** |
| 工具集 6 → 24（每轮全量 schema） | 12.0k | 约 2.6k（热 8 + 偶发搜索 0.3k/次） | **−78%** |

失效范围：

| 事件 | 改造前 | 改造后 |
|---|---|---|
| 安装 / 启用 / 禁用技能 | system 全段 → 其后全部历史 | **0**；追加一条 ~0.4k 历史消息 |
| 技能正文修改 | system 全段（bodies 每轮重拼） | **0**（下次 `skill_load` 才读新字节） |
| 记忆更新 | system 从 memory 段起 | context 段起 |
| 模块上线（+3 工具） | system 全段 + tools 数组 | tools 数组 + `tool_block` 那一段（见下） |
| 冷工具首次被发现 | — | tools 数组插入点之后一次（之后因进入粘性集合而稳定） |

**诚实口径（三条，不掩饰）**：① `tool_block` 在 stable 段，**工具集变化仍会破前缀**，缓解不是假装不破，而是让热集合低频变化（上限 12 + 配置固定 + 发现项只进 tools 数组、不写 `tool_block` 文本）；② 冷工具首次发现**必然破一次** tools 数组前缀，这是 B 的固有代价，按「每月几次」量级可接受，若某模块工具被高频发现，说明它本该进 `tools.hot`；③ 目录消息仍占上下文（≈0.4–1.5k），省的**不是 token 总量，而是重算导致的 cache miss**，首轮反而 +4%。

**观测缺口（必须先补）**：`core/vade/llm/*` 把 provider 的 `usage` 原样透传，`turn.completed.usage` 是自由 `object` —— 即现在无法知道命中率。建议归一化 `cached_tokens` / `prompt_tokens` / `completion_tokens` 并保留原始键；`usage` 是 `object`，不改 C-06，属观测而非契约。

## 7. 迁移、灰度与回滚

开关（C-18 新增，`apply: restart`，不做热切换 —— 热切换会让同一会话两轮走不同装配路径，前缀必然破且难复现）：

```toml
[prompt]
injection = "legacy"   # legacy | layered；默认 legacy
```

**默认 legacy，直到 U1-0 验收。** U1-0 是本提案提议的「U1 0 号前置任务」占位（仓库当前没有这份任务书：`docs/agents/50-U1-ui-skeleton.md` 只有 T1–T7）。legacy 下所有代码路径与 `a0a5062` 逐字节一致，由 golden fixture 断言（§8 A9）。

| 阶段 | 内容 | 出口条件 |
|---|---|---|
| P0 | 只加 `SECTION_ORDERS`、`prompt.injection` 键、`check_contracts` 断言、golden fixture | legacy 输出字节不变；`check_docs` 仍 12 项全绿 |
| P1 | A + B 在 layered 下实现，内部 dogfood | §8 的 A1–A13 全绿；单会话 50 轮无重复目录消息 |
| P2 | 默认切 layered | U1-0 验收：三端把目录消息渲染成「系统提示条」而非用户气泡；老会话 replay 无重复 |

| 场景 | 行为 |
|---|---|
| 老会话（无目录消息）打开 layered | 首轮补发一条；该会话此前缓存的前缀失效一次，之后稳定 |
| 已写入目录消息后关回 legacy | 消息**不删**（删了破 seq 与 replay），legacy 读取时按 `role=user` + `<system-reminder>` 前缀过滤，既不进 LLM 请求也不渲染 |
| 老客户端收到 `turn.tool_discovered` | 未知事件，忽略；C-06 未强制报错（建议补规则，§5.5） |
| UI 把 user-role 一律渲染成气泡 | 目录消息会显示成「用户说了一段 XML」—— Q1 必须回答 |
| replay（`session.attached` snapshot / `from_seq`） | 目录消息是普通 message，带 seq，天然被 replay 带上；重复投递由「按 seq 去重」规则兜住 |

回滚四步：① `prompt.injection=legacy` + 重启，立即回旧装配路径；② `skill_load` / `tools/search` 在 legacy 下**不注册**，模型看不到，不会出现「调了没人处理」；③ 历史里的目录消息保留但被过滤，不写迁移脚本、不回滚 schema（`injection` 字段留着不用）；④ 若 C-11 的 `injection` 字段引发争议，整体 revert 该文件 diff 即可 —— 它只是追加，除 `check_contracts` 新断言（需同批回滚）外无依赖。

## 8. 验收

A1–A13 全部要求真实执行；图表/截图不算证据。

| # | 断言 | 承载 |
|---|---|---|
| A1 | 技能安装后**下一轮 stable 段字节不变**（安装前后各 render 一次，比较 sha256） | `core/tests/loop/test_prompt_layers.py` |
| A2 | 同一会话连续两轮 render，stable + context 字节一致（纯函数性质未破） | 同上 |
| A3 | digest 不变**不重发**；改 description 追加一条完整替换；清空后追加退休条且含「ignore any skill names」 | `core/tests/test_skill/test_catalog_message.py` |
| A4 | 目录消息是 **user role**，且 `assemble()` 输出里不含 `available_skills` | 同上 |
| A5 | 冷工具**不在**常驻 `schemas()`；`tools/search` 命中后**下一步**的 `schemas()` 含它，且仍取自本轮快照 | `core/tests/loop/test_tool_discovery.py` |
| A6 | `tools/search` 同一 query 两次调用，`matches` 顺序与 `digest` 字节一致 | 同上 |
| A7 | `orders` 无重号、段清单与序号表一致、`stable` 全早于 `context`、legacy 六段投影保持升序 | `tools/check_contracts.py` |
| A8 | stable 段扫描不含运行事实（IPv4、ISO 时间戳、绝对路径、`cwd`、token 余量），命中即失败 | `core/tests/loop/test_prompt_layers.py` |
| A9 | `legacy` 下 `assemble()` 输出与 `a0a5062` golden fixture 字节一致 | `core/tests/loop/test_prompt_legacy.py` |
| A10 | layered 下加载技能后，正文**不在** system 段，只出现在 tool 结果 / user 注入消息里 | `core/tests/test_skill/test_catalog_message.py` |
| A11 | 空配置时 `prompt.injection == "legacy"`；`layered` 必须显式开启 | `core/tests/config/test_prompt_switch.py` |
| A12 | 安装技能 → 从 seq 0 replay → 目录消息条数与 digest 一致，无重复 | `core/tests/session/test_replay_catalog.py` |
| A13 | 所有模块工具名匹配 `__`，且无一等于 `skill_load` / `tools/search` | `core/tests/router/test_name_namespace.py` |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿（写本 RFC 时已实测，新文件不参与 C1–C10 计数）。

## 9. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| 只做 C（分层）不做 A/B | catalog/bodies 仍每轮重拼进 system，前缀照样破，分层收益被抵消 |
| 目录留 system，用 provider 的 `cache_control` 打点 | `core/vade/llm/*` 没有缓存指令通道（只有 `provider/base_url/model/api_key_env`），且 OpenAI 自动缓存不可打点 → 不可移植 |
| 工具只做 `restrict` 作用域（按 turn 类型裁剪） | 判定边界不可解释（「看看这个文件」属于哪类？），模型也无法自我纠正 |
| 目录用 assistant 消息 | 模型会把自己说过的话当既定事实；部分 provider 拒绝连续 assistant 消息 |
| 正文继续放 system 但加 `revision` 版本号 | 版本号变了照样破前缀，只让失效可解释，不减少失效 |

## 10. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **目录消息对用户可见吗？** | (a) 渲染成系统提示条 —— 需要一条 C-06 事件/字段告诉客户端「这条不是用户说的」，靠 `<system-reminder>` 嗅探不可接受；(b) 完全隐藏 —— replay 时客户端仍会当普通 user 消息；(c) 只在管理页技能面板显示。**倾向 (a) + §5.1 第 6 条条件条**，但那是新增事件，需 C3 点头 |
| Q2 | **冷工具搜索多一次往返能接受吗？** | 移动端 RTT 100–300ms；同轮内可见（§4.3.3）所以只多一次工具调用、不多一轮。倾向接受，并在 stable 段常驻一句「没有合适工具时先 `tools/search`」（约 20 token，值得） |
| Q3 | **`tools/search` / `skill_load` 是模型工具还是 RPC 方法？** | 模型必须能调 → 必须是工具；但 core 现在没有「内置工具」通道（`snapshot` 全部来自 registry），需要 `ToolSnapshot` 支持 core-native 条目并定一条 C3 边界。**这是本提案最大的实现风险** |
| Q4 | **热工具名单谁定？** | 配置（运维）/ 技能 `requires_tools`（已有）/ 会话历史（自动）。本提案**拒绝模块自荐**（否则每个模块都标自己热，等于没做）。若要模块自荐需 C-03 追加 `capabilities.hot`，并接受「manifest 决定缓存成本」 |
| Q5 | **压缩时目录消息怎么处理？** | 压缩丢它 → 下一轮发现「没有可见目录」→ 必重发，代价是每次压缩后一次全价。可选：把它纳入 `protect_first_n`（当前 3）保护，但它不是第 1 条消息，位置会漂 |
| Q6 | **正文从 system 移到 tool 结果，信任层级变了吗？** | 变了且方向是好的（工具输出信任更低，注入面缩小）；但 `@name` 注入仍是 user-role，信任反而更高。是否给 `<skill_content>` 一个「非用户原话」标记？ |
| Q7 | **存量会话灰度？** | layered 打开后老会话首轮必补目录 → 破一次缓存。按 `session_id` 白名单灰度，还是接受「一次性破」？倾向接受一次性破，但发布说明要写明 |

## 11. 影响面与后续

| 文件（实现阶段，非本 RFC） | 改动 |
|---|---|
| `core/vade/loop/prompt.py` | `_SECTIONS` → `SECTION_ORDERS`；`assemble()` 分两段；`runtime_facts` 约束 |
| `core/vade/loop/engine.py` | 目录消息注入 + `resolve_hot()` + `schemas(hot ∪ discovered)` |
| `core/vade/loop/tools.py` | `ToolSnapshot.select(hot)` / `render_block(hot)` / core-native 条目 |
| `core/vade/skill/loader.py` | `catalog_digest` / `catalog_message` / `load_body`；`render()` 在 layered 下退化 |
| `schema/skill.schema.json`、`rpc.json`、`events.json`、`config.schema.json` | §5 的四条追加 |
| `tools/check_contracts.py` | §4.4 的四条断言 |

**不动**：`core/vade/policy/*`（审批）、`core/vade/router/*`（路由语义）、`docs/design.md` §9（分层与 Skin）、`core/vade/skill/proposal.py`（C3d 提案流）。

**后续（不在本 RFC）**：R4 子代理（需 `subagent.*` 事件与 child session，与 A/B/C 无耦合，可并行）；R2 `turn.tool_result.locations`（正交，但同一个「工具结果当消息用」的承载面）；R7 压缩 cache-safe forking（依赖本提案的分层先落地）。

**参考**：`research/agent-01-injection-comparison.md` R1/R3/R6 与 §3.2/§3.3/§3.4（dsh 目录注入、Section/Context 双通道、中央 order 表、社区 #2136/#2141 的 0–3% 实测）；RFC-0001（章节与元信息风格来源）；`docs/agents/23-C3b-skill-loader.md`（L0/L1 预算、激活上限、稳定排序）；`docs/agents/01-contracts.md`（C-05 / C-06 / C-11 / C-18 的所有者与冻结时点）。
