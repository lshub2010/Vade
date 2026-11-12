# M4b · `evolve` 自进化（memory 的后台角色）

| 项 | 值 |
|---|---|
| Agent 代号 | `m4b-evolve` |
| 波次 | W6 |
| 依赖 | M4a 的存储接口与 `staging` 表；C3b 的 skill schema；C3d 的事件推送 |
| 目录所有权 | `modules/memory/src/memory_module/evolve/` |
| 出口 | 五个触发器各自可验；关闭时零动作；提案永不直接生效 |

## 一、一句话任务

让记忆自己长大：会话中抽候选、会话后总结、每天巩固晋升、任务成功后提炼 skill——**全部确定性触发，全部可审计，全部可关闭**，且绝不把模型的一次推断当事实。

> **不新建模块**（D41）。演化是记忆的写路径，读写同一个库；拆成两个模块会立刻撞上「模块间不能互调」的墙（演化要写记忆，记忆要读库）。

## 二、必读

- `docs/design.md` §16（分层、流水线、熵增五对策、与 Hermes 的差异、权限）
- `docs/requirements.md` D40–D46、A26–A31（§8.10）、AC-23～AC-27
- `docs/agents/43-M4-memory.md`（存储与表结构）
- `docs/agents/23-C3b-skill-loader.md`（skill schema 与 pending）
- `docs/agents/24-C3d-events-skill-proposal.md`（订阅与提案协议）

## 三、目录

```
modules/memory/src/memory_module/evolve/
├── config.py        # 开关、预算、redact
├── rules.py         # turn_end 规则抽取（纯函数）
├── review.py        # turn_review：骨架化 + 旁路复盘 + 输出校验
├── summarize.py     # session_close：rolling summary + 严格 JSON 解析
├── consolidate.py   # 聚类 → 冲突 → 晋升 → 衰减 → 淘汰
├── skillgen.py      # 轨迹 → SKILL.md 全文
├── scheduler.py     # 定时（每日 03:00）+ 手动触发
└── worker.py        # 消费 events/deliver 的队列消费者
```

`manifest.json` **由 M4a 在 W5 预置好**，你只读不写：

```json
{ "role": "evolve", "subscriptions": ["session.turn", "session.closed"] }
```

- 你**不改** `manifest.json`、不改 `store.py`、不改建表 SQL——它们都在 M4a 名下，改了就过不了合并门禁。
- 需要新的表（如 `consolidate_run`），由 M4a 在 W5 建好；你只在 `evolve/` 内写查询。
- 需要读写记忆，只走 M4a 的 `MemoryBackend` 接口；接口不够就提 RFC，不要直接连 SQLite。

> 主题就两个：`session.turn`（每轮）与 `session.closed`（会话结束）。**不要自造 `session.transcript` 这种合并主题**——权限要能按主题分别授予，合并了就粗了。

## 四、五个触发器

| 触发 | 输入 | 动作 | LLM | 预算 |
|---|---|---|---|---|
| `turn_end` | **本轮 `role=user` 的文本** + 工具结果里的环境事实 | 规则抽取 0–2 条候选 → `session` 层 | **不用** | 2 条/轮 |
| `turn_review` | 最近 `nudge_interval` 轮的**结构化骨架**（≤120 token/轮） | 复盘 → ≤3 条候选 + ≤1 lesson → `staging` | 1 次（便宜模型） | 共享 `daily_token_budget` |
| `session_close` | **rolling summary + 最后 10 轮** + 每轮结构化字段 + skill 目录快照 | 生成 summary + 候选 → `session` 层 + `staging` | 1 次（便宜模型） | 5 条 |
| `consolidate` | `staging` + `global` | 聚类、冲突、晋升、衰减、淘汰 | 批量 1 次（仅冲突判定用） | — |
| `skill_evolve` | 该会话成功轨迹 | 生成提案 → `skill/proposal` | 1 次 | 1 个/会话，5 个/天 |

**`turn_review` 不新增订阅主题**：它复用 `session.turn` 的计数（每收到 `nudge_interval` 个 turn 触发一次）。不要为此加 `session.review` 事件——C3d 的 C-14 已经冻结，加主题要重开契约，收益不值。

调度：`turn_end` 由事件驱动；`turn_review` 由 `session.turn` 计数触发（每 `nudge_interval` 个）；`session_close` 由事件驱动且受 `flush_min_turns` 门控；`consolidate` 每日 03:00 + **启动补跑**（距上次 >24h）+ 手动 CLI；`skill_evolve` 在 `session_close` 之后同步判定、异步生成。

**启动补跑**（AMEND-19）：`scheduler.py` 在进程启动时读 `consolidate_run.last_run`，超过 24h 就先跑一次再进入定时循环。设备关机、容器停摆都会错过 03:00——没有补跑，consolidate 在桌面端基本等于不执行。consolidate 已保证幂等（§七），补跑不会重复晋升。

**所有 LLM 调用失败都不得阻塞会话**：拿不到结果就只存规则抽取的部分，日志记 `evolve_degraded`。

### 五个触发器的数据从哪来（别自己猜）

| 触发器 | 事件来源 | 载荷里必须有的字段 |
|---|---|---|
| `turn_end` | `events/deliver` 的 `session.turn` | `{session_id, turn_id, state, user_text, assistant_text, tool_calls:[{tool,args_digest,ok}]}`；**只有 `user_text` 才抽候选** |
| `turn_review` | 同一条 `session.turn` 流，自己维护 per-session 环形缓冲（最近 `nudge_interval` 轮骨架） | 每轮骨架 `{turn_id, state, tool_names[], first_sentence, last_sentence, error_flag}`，**不存原文** |
| `session_close` | `events/deliver` 的 `session.closed` | `{session_id, turns:[{turn_id, state, tool_calls, negative}], closed_at}`；**rolling summary 由你自己维护**，不要指望 C3 传 |
| `consolidate` | 启动补跑 / 每日 03:00 / `python -m memory_module.evolve consolidate` | — |
| `skill_evolve` | 由 `session_close` 处理完时**顺带判定**，不单独订阅 | `turns[]` 里的 `state=completed`、**有效** `tool_calls>=5` 且工具种类 ≥2、`negative=false`、`self_corrected=false` |

**两个坑**：

1. **不要指望从摘要反推 `tool_calls`**。摘要由 LLM 生成，数字不可信。`tool_calls` 必须来自 C3d 的结构化字段；C3d 不给你就提 RFC，别自己数。
2. **不要保留完整转录超过一次处理周期**。这是你自己定的约束（§九），但和 `skill_evolve` 的「≥5 次工具调用」冲突——解法是 `turn` 到达时就把 `{turn_id, tool_calls, state}` 累计到内存里的 per-session 计数器，close 时只用计数器，不回头翻原文。

## 五、turn_end：规则抽取

不调 LLM。只认五种信号（正则 + 关键词，命中即候选，原文截取所在句）：

> **只从 `user_text` 抽**（AMEND-19）。载荷把 `user_text` / `assistant_text` 分开了（C-14），不要合并后再猜角色。assistant 文本只用作「上一轮是否被纠正」的上下文，**绝不能作为候选源**——否则模型自己写的「结论是…」会被存成记忆，再被 `session_close` 抄进 staging，两次推断叠加就晋升成“事实”，直接违反 §一 的「绝不把模型的一次推断当事实」。
>
> 唯一例外：**环境事实**可以来自工具结果的 `args_digest` / `ok` 结构（比如 `path=/srv/app`），但仍不读 `role=assistant` 的正文。

| 信号 | 例 | 落到 |
|---|---|---|
| 显式记忆指令 | 「记住…」「以后都用…」「不要再…」 | `session`，importance=5 |
| 偏好陈述 | 「我喜欢…」「我习惯…」「我们统一用…」 | `session`，importance=4 |
| 用户纠正 | 「不对」「不是这样」「改成…」 | `session`，importance=5，并标记上一轮为负反馈 |
| 明确结论 | 「结论是…」「最终决定…」 | `session`，importance=3 |
| 环境事实 | 路径、端口、版本号、仓库名 | `session`，importance=2 |

- 单轮最多 2 条，按 importance 降序取；超出的丢弃（不排队）。
- 同轮重复文本先做哈希去重。
- 只写 `session` 层，**绝不直接写 `global`**。

## 六、session_close：总结与 staging

**门控**：会话总轮数 `< flush_min_turns`（默认 6）→ 不调 LLM，只落规则抽取的部分，记 `evolve_skipped_low_signal`。两轮寒暄不值得一次模型调用。

**输入不是全文**：`rolling_summary`（**evolver 自己维护**：每次 `turn_review` 顺带把最近 N 轮压成一段增量，累计成会话摘要，≤ `ctx × summary_token_ratio`）+ **`tail`（最后 10 轮原文，core 已强制截断）** + skill 目录快照。不要在这里重读整段转录——100 轮会话约 8 万 token，一次就吃光日预算。

> 为什么摘要归你、不归 C3：C3 的模型是主模型，贵；你这里是 `model="cheap"`，而且 `turn_review` 本来每 10 轮就要调一次——**同一次调用顺带产 `summary_delta`**，边际成本接近零。C3 的 `session.closed` 只给结构化 `turns[]`，不给文本。

```python
# 输入：rolling summary + 最后 10 轮（已按 redact 裁剪）+ skill 目录快照
# 输出必须是严格 JSON，不接受 markdown 包裹
{
  "summary": "≤ 300 字，这个会话解决了什么、结论是什么",
  "facts": [
    {"text": "...", "namespace": "user:default", "importance": 4, "confidence": 0.8}
  ],
  "lessons": [
    {"when": "...", "do": "...", "pitfall": "...", "verify": "...",
     "evidence": {"turn_id": "...", "tool": "...", "claim": "..."}}
  ],
  "negative_feedback": false,
  "skill_candidate": true
}
```

- `temperature=0`；解析失败重试 1 次（附上错误原因），再失败则**只存 summary 原文**，`facts=[]`、`lessons=[]`。
- `facts` / `lessons` 写 `staging`（**不是** `global`）；`lessons` 额外带 `kind="lesson"`、`evidence`、`confidence ≤ lesson_confidence_floor`。
- `summary` 写 `session` 层，importance=4，供本会话后续追问时注入。
- `skill_candidate=true` 只是“值得考虑”，最终仍由 §八 的四条硬条件裁决。
- **`negative_feedback` 判的是“用户有没有否定”**，不是“这次任务顺不顺”。模型自己发现错误并改正，是 `self_correction`，不是负反馈——两者要分开记（见 §八）。

## 六·五、turn_review：旁路复盘（AMEND-19 新增）

每 `nudge_interval`（默认 10）轮触发一次。**这是“无人干预也能学”的落点**：不依赖用户说“记住”，看的是状态变化。

**输入**：环形缓冲里的骨架，不是原文。

```python
[{"turn_id": "...", "state": "completed|failed|interrupted",
  "tool_names": ["fs__read", "shell__run"],
  "first_sentence": "…", "last_sentence": "…",
  "error_flag": false}]
```

**输出**：严格 JSON，≤3 条候选 + ≤1 lesson。

```json
{
  "candidates": [{"text": "...", "kind": "fact|preference", "importance": 4}],
  "lesson": {
    "when": "...", "do": "...", "pitfall": "...", "verify": "...",
    "evidence": {"turn_id": "...", "tool": "...", "claim": "..."}
  },
  "summary_delta": "≤200 字，只写这 N 轮新增的结论/转折，不重复已有摘要"
}
```

`lesson` 可以为 `null`；**多数 review 应该什么都不产出**，产出率高说明阈值太低。

**规则**

- 输入 ≤120 token/轮 × 10 轮 ≈ 1.2k，加 prompt 总 ≤1.5k。超了先修骨架（截断首尾句），不要截 turn 数。
- **只在旁路跑**：独立任务，`asyncio.wait_for(..., 30s)`；超时/失败记 `evolve_degraded`，**绝不阻塞 `turn_end`**。
- 输出一律进 `staging`，**不直接写 `session`**——否则等于每 10 轮把模型推断塞进工作台。
- 同一 session 的多次 review 结果按 `content_hash` 去重，避免同一件事产出 3 条。
- `lesson` 一次只出 1 条，取最具体、带 `evidence.turn_id` 的那条。宁少勿滥。
- `summary_delta` 增量追加到 per-session rolling summary；累计超过 `ctx × summary_token_ratio` 时**先丢最早的过程细节、保留结论与未决项**——摘要的价值在“后来定了什么”，不在“当时怎么试的”。
- 与 `session_close` 的分工：review 抓「中途决策、换方案、纠错」，close 抓「整体结论」。两者都写 `staging`，靠去重合并。

**为什么规则层做不了这件事**：正则能抓“记住/不对/结论是”，抓不到“第三次重试才成功”“把方案从 A 改成 B”——而那才是复盘该学的东西。

## 七、consolidate：巩固

```
1. 取出 staging 全部候选，按 (kind, namespace) 分组
2. 组内语义聚类：embedding 余弦 > 0.92 归为一簇
3. 每簇合并：取最长文本为代表，seen_sessions 取并集
4. 晋升判定（分 kind）：
   fact / preference → len(seen_sessions) >= 2 才写 global
   lesson            → 1 次 + evidence 非空即可写 agent:lessons，
                        confidence = min(簇内均值, lesson_confidence_floor)，
                        verified_at = NULL（标 unverified）
5. 冲突检测：与同 namespace 的 global 条目相似度 > 0.90 且命中否定词
   （不/别/取消/改成/不是/停止）→ 新条 supersedes=旧id，旧条 superseded_by=新id
   lesson 不参与 supersede：教训是累积的，新教训不推翻旧教训，只追加
6. 衰减：重算全部 score，超配额按升序淘汰到 90% 水位 → archive
7. 清空已晋升的 staging 行
```

**lesson 簇的合并规则**：`when` 段相似（余弦 > 0.92）视为同一条教训，合并时**保留 evidence 最多的那条**，其余 evidence 并集进 `evidence[]`。不要按“最长文本”取——教训的价值在证据，不在字数。

- **冲突判定不准就两条都留**（D44）。宁可冗余，不可误删——误删一条真偏好比多存一条废话贵得多。
- 聚类只做"合并展示"，**不删除任何原文**。
- consolidate 必须**幂等**：同一天跑三次，结果一致（用 `consolidate_run` 表记 `run_id` 与水位）。

## 八、skill_evolve：提炼

**四条**硬条件同时满足才启动（缺一不触发）：

1. `turn.state == completed`（终态名统一为 completed，不是 done）；
2. **有效** `tool_calls >= 5`，且 `distinct_tools >= 2`（读 5 个文件不是“复杂流程”）；
3. `negative_feedback == false`；
4. `self_corrected == false`（AMEND-19）——轨迹里出现过“推翻前序结论”的，是纠错案例，不是最佳实践。

**失败 / 自纠轨迹的去处**：不产 skill，但可以产 `lesson`（`[evolve] failure_learning=true` 时）。条件是 `evidence` 可定位——哪个工具、返回什么、哪条断言被推翻。这样“失败学不到”被补上，同时不把错误流程写成 skill。

生成流程：

```
轨迹（工具序列 + 参数摘要 + 结果摘要）
   ↓
判断是否命中已有 skill（用 core 推来的 skill_catalog 做 name/description 匹配）
   ├ 命中 → action="patch"，必须带 base_revision（取快照里的当前值）
   └ 未命中 → action="create"
   ↓
生成完整 SKILL.md（frontmatter 符合 C-11 + 四节正文）
   ↓
自检四节：When to Use / Procedure / Pitfalls / Verification
   ├ 缺段 → 重写一次
   └ 再缺 → 丢弃，记 evolve_schema_invalid
   ↓
skill/proposal 发给 core
```

**四节是硬要求**（AMEND-19/D82）：`Pitfalls` 至少 1 条、`Verification` 必须可执行（命令或可观察结果）。写不出 `Pitfalls` 说明这次任务本来就没难度，不该提案；写不出 `Verification` 说明成功不可判定，更不该提案。

- 正文只写**可复用的步骤与判据**，不写这一次的具体路径/ID（那是参数不是流程）。生成后要自检：正文里是否含绝对路径、ULID、邮箱——命中则重写一次。
- `derived_from` 必填：`{session_id, turn_ids[], model}`。
- 预算：每会话 ≤ 1 个提案，每天 ≤ 5 个；超了记 `evolve_budget_exceeded` 并丢弃，**不排队**。
- 提案被 core 拒绝（schema 不合法 / base_revision 过期）→ 记日志，**不自动重试**（避免死循环刷提案）。

## 九、权限与开关

```toml
[evolve]
enabled = false              # 默认关
allow_transcript = false     # 与 manifest 的 subscriptions 双许可
allowlist = []               # module_id + 公钥指纹（core 侧白名单）
nudge_interval = 10          # 每 N 轮一次 turn_review；可调 5–50
flush_min_turns = 6          # 低于此轮数不跑 session_close 的 LLM
session_char_limit = 4200    # 写入侧字符硬顶，超了返回 MEMORY_FULL
summary_token_ratio = 0.05   # rolling summary 预算 = ctx × 该值
lesson_confidence_floor = 0.6
failure_learning = true      # 失败轨迹是否产 lesson
daily_token_budget = 200000  # 四个 LLM 触发器共享；0 = 不限（不推荐）
daily_budget_skills = 5
session_budget_skills = 1
redact = ["tool_args", "file_content"]
model = "cheap"              # 走 M4a 的 LLM 配置，不复用 core 的 llm-adapter
```

> 这些键的**权威定义**在 `design.md §7.9.1` / 契约 C-18，这里只是 M4b 读到的子集。加键先改 C-18，不要在 `config.py` 里自己加——否则 core 的 schema 校验会以「未知键」拒绝启动。

- `enabled=false` → 不订阅、不消费、不产生任何写入；日志打一行 `evolve disabled`。
- `nudge_interval` 热改：新值从**下一个计数周期**起算，已累计计数不重置——否则改小后可能立刻连触两次。
- `daily_token_budget` 耗尽 → 四个 LLM 触发器全部降级为纯规则，日志 `evolve_budget_exhausted`，次日 00:00 重置；**不排队补跑**（补跑等于把今天的账推到明天）。
- 收到未订阅主题 → 丢弃并告警，不处理。
- 转录落盘前按 `redact` 裁剪；**内存里也不保留原始转录超过一次处理周期**。
- 审计只记 `{module_id, session_id, turns, bytes}`，不记内容。

## 十、验收

| # | 场景 | 期望 |
|---|---|---|
| 1 | `enabled=false` 跑完整会话 | 零 `events/deliver`、零新记忆、零提案；日志有 `evolve disabled` |
| 2 | 用户说「以后都用中文回答」 | `turn_end` 抽出 1 条进 `session` 层，importance=5，**不进 global** |
| 3 | 会话结束 | `staging` 有候选，`global` 无新增；`summary` 写入 session 层 |
| 4 | 两个不同会话都说了同一偏好 → consolidate | 晋升 `global`，且只有 1 条，`seen_sessions` 含两个 id |
| 5 | 只在一个会话说过 → consolidate | 仍在 `staging`，`recall` 查不到 |
| 6 | 新事实与旧 global 矛盾 → consolidate | 新条 `supersedes` 指向旧条，默认 recall 只返回新条 |
| 7 | 6 次工具调用的成功任务 | 生成 `create` 提案；core 落在 `pending/`，**prompt 里没有它** |
| 8 | 同类任务再跑一次 | 生成 `patch` 提案，带正确 `base_revision` |
| 9 | 一天提 6 个提案 | 第 6 个被预算拒绝，日志 `evolve_budget_exceeded`，不排队 |
| 10 | LLM 服务不可用 | 会话正常结束，只写规则抽取部分，日志 `evolve_degraded` |
| 11 | consolidate 连跑 3 次 | 结果一致（幂等），无重复晋升、无重复 archive |
| 12 | 转录含文件内容 | 落盘与日志中均为 digest，grep 原文 0 命中 |
| 13 | `nudge_interval=10`，跑 25 轮 | 恰好 2 次 `turn_review`；每次输入 ≤1.5k token；产出 ≤3 条 + ≤1 lesson |
| 14 | 3 轮寒暄会话 | 不调 LLM，日志 `evolve_skipped_low_signal`，`staging` 为空 |
| 15 | 持续写入至字符数 > 4200 | 返回 `MEMORY_FULL`；当轮合并后重试成功；无静默丢弃、无跨轮遗留 |
| 16 | 失败任务但有证据 | 产 1 条 `lesson`（`unverified`，confidence ≤0.6），**不产 skill 提案**；`recall` 能查到且带 `evidence` |
| 17 | 自纠轨迹（先采信错误报告、后核对改正） | `self_corrected=true`；**不产 `create` 提案**；可产 lesson，且 lesson 的 `when` 段写明触发条件 |
| 18 | 生成缺 `Pitfalls` 的 SKILL.md | 自检失败重写一次；仍缺则丢弃，日志 `evolve_schema_invalid`，不提交提案 |
| 19 | 15:00 关机、次日 09:00 启动 | 启动即补跑一次 `consolidate`，`last_run` 更新，无重复晋升（幂等） |

## 十一、禁止

- 禁止把 `staging` 的候选直接写 `global`（必须过 consolidate 的 ≥2 次门槛）。
- 禁止在 `turn_end` 调 LLM（成本和延迟都不允许）。
- 禁止让演化失败影响会话：所有异常吞掉并记日志。
- 禁止自动审核自己的提案（`auto_approve` 只能由 core 配置，模块无权设置）。
- 禁止把原始转录写进任何持久化位置（处理完即丢）。
- 禁止一次会话提多个 skill 提案。
- 禁止用「模型觉得重要」替代四条硬条件。
- 禁止把 assistant 正文当候选源：`turn_end` 只从 `role=user` 抽（唯一例外是工具结果里的环境事实）。
- 禁止 `turn_review` 直接写 `session` 或 `global`——一律进 `staging`，走同一条晋升路径。
- 禁止在 `turn_review` 里读原文：只读骨架。要读原文的场合是 `session_close`，且也要先走 rolling summary。

## 十二、交付格式

按 §三，额外附：**五个**触发器各一次的真实日志（含时间戳与耗时）、一次 `turn_review` 的输入骨架与 token 计数、一条 `lesson` 从产生到 `recall` 的完整链路（含 `evidence`）、一次 `MEMORY_FULL` 触发的整理日志、一次 consolidate 前后库内容对比、一个 `create` 与一个 `patch` 提案的原文、`enabled=false` 时的完整会话日志片段、redact 生效的 grep 结果。
