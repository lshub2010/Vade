# C3 · 会话 / 队列 / 路由 / Agent 循环

| 项 | 值 |
|---|---|
| Agent 代号 | `c3-loop` |
| 波次 | W3 |
| 依赖 | C2 的 registry 查询面、`schema/rpc.json`、`schema/errors.json` |
| 目录所有权 | `core/vade/session/`、`router/`、`loop/`、`llm/`、`core/vade/slots.py` |
| 产出 | 一个能真正把「用户消息 → LLM → 工具 → 回复」跑完的 core |
| 出口 | turn 全生命周期、队列、中断、replay、熔断 测试全绿；`schema/events.json` 冻结 |

## 一、一句话任务

实现会话的单一真相源：一条消息进来，排队、执行、调模型、调工具、流出结果、记账，全程产生一条带 `seq` 的事件流，任何端接上来看到的都一样。

## 二、边界

| 做 | 不做 |
|---|---|
| session / turn / queue / seq / replay | 不做审批判定（C5a），只定义端口 |
| 工具名解析、实例选择、超时、熔断、取消 | 不做注册与 manifest 校验（C2） |
| Agent 循环、终止条件、上下文拼装 | 不做内置 UI（U 组） |
| 内置 OpenAI 兼容适配器 | 不做 model 模块（M6） |

**目录冲突预防**：审批与审计的接口由你在 `loop/ports.py` 定义（`ApprovalPort` / `AuditPort`），C5a 实现适配器。**不要 import `vade.policy` 或 `vade.audit`**——它们还不存在，且方向反了。

## 三、必读

- `docs/design.md` §2.5（RPC 与事件表）、§2.6（seq/replay）、§4（会话模型）、§5（队列与中断）、§6（审批时序）
- `docs/requirements.md` FR-1 / FR-6.2 / FR-7 / FR-8.5–8.12（A5、A8）
- `docs/agents/01-contracts.md` §二、§三、§五

## 四、任务分解

### T1 · 数据模型与持久化（1 天）

**包入口**：`core/vade/session/__init__.py` 暴露 `install(app)`，把 session-store、queue、router、loop、UI RPC 挂到 `app` 上（C2 的 `app.py` 会调用它）。不要改 `__main__.py`。

```
core/vade/session/
├── models.py     # 不可变 dataclass：Session / Turn / Message / QueueItem
├── store.py      # SessionStore 抽象 + SQLiteStore 实现
├── sqlite_store.py
├── seq.py        # per-session 计数器
└── replay.py     # deque(maxlen=1000) + 24h 过期
```

**存储必须走接口（D36）**，SQLite 只是默认实现：

```python
class SessionStore(Protocol):
    def append(self, sess_id: str, msgs: list[Message], events: list[Event]) -> None: ...
    def read_since(self, sess_id: str, seq: int) -> list[Event]: ...
    def update_turn(self, sess_id: str, turn: Turn) -> None: ...
    def next_seq(self, sess_id: str) -> int: ...          # 必须原子
    def load(self, sess_id: str) -> Session | None: ...
```

- 调用方（loop / router / queue）**只依赖接口**，不出现 SQL 字面量。
- `append` 是「一批写」而不是「一条一写」——换 PG 时这里是事务边界。
- `next_seq` 必须在存储侧原子分配（SQLite 用 `UPDATE ... RETURNING`），不能「读 + 1 + 写」。
- SQLite 实现另有 `append_sequenced()`：把这一批的 seq 分配放进 `append` 的同一个事务（一次提交而不是两次）。写路径优先用它（有则用、无则退回 `next_seq` + `append`），`append` 的语义不变。
- 换 PG 时只需新增 `pg_store.py`，**不改 loop**。v1 不实现 PG，但接口签名冻结。

表结构（SQLite，`data/sessions.db`）：

```sql
CREATE TABLE sessions(id TEXT PRIMARY KEY, created_at INTEGER, title TEXT, seq_counter INTEGER);
CREATE TABLE turns(id TEXT PRIMARY KEY, session_id TEXT, state TEXT, started_at INTEGER, ended_at INTEGER);
CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT, role TEXT, content TEXT, seq INTEGER);
CREATE TABLE events(session_id TEXT, seq INTEGER, type TEXT, payload TEXT, ts INTEGER,
                    PRIMARY KEY(session_id, seq));
```

规则：
- `seq` **只由 core 分配**，在一个 session 内严格 `+1`，不允许跳号、不允许客户端指定。
- 每个对外事件（`turn.*` / `queue.updated` / `module.status`）都先落 `events` 再广播；**先落盘后推送**，否则重连补发会缺条。
- replay buffer 内存里保留最近 1000 条或 24h（先到为准）；`attach(last_seq)`：
  - 覆盖 → 从 `last_seq+1` 补发，再切实时；
  - 不覆盖 → 返回 `snapshot`（消息树 + 队列 + 待审批），`from_seq` = 快照点。
- 客户端可能收到重叠片段，**客户端按 seq 去重**（写进 `schema/events.json` 的说明）。

### T2 · 队列与中断（1 天）

状态机：

```
queued ──start──> running ──ok──> completed
   │                 ├──fail──> failed
   │                 ├──interrupt──> interrupted
   └──cancel──> cancelled
```

| 规则 | 实现要点 |
|---|---|
| 每会话同时 1 个 running | 用 `asyncio.Lock` 或单 worker task，**不要用信号量猜** |
| `client_msg_id` 幂等 | session 内唯一索引；重复提交返回原 `turn_id`，不新建 |
| 入队顺序 | 按 core 收到顺序，不按客户端时间戳 |
| `interrupt(turn_id)` | 标记 → 向 router 发 cancel → **立即**把队列释放并启动下一个（不等在途返回） |
| `cancel(queued)` | 只对 queued 有效；running 返回 `INVALID_ARGUMENT` |
| 审批阻塞 | 只卡队首，后续可入队；事件里 `turn.state = waiting_approval`，UI 显示"等待确认" |

`schema/events.json` 必须包含 `docs/design.md` §2.5 的全部事件，字段一个不少，另加 `turn.state`（枚举）供 UI 渲染。

### T3 · router（1.5 天）

```
core/vade/router/
├── names.py      # 双向映射：module_id/tool <-> ns__tool
├── dispatch.py   # 实例选择 + 调用 + 超时
└── breaker.py    # 熔断器
```

**命名映射**：
- `com.example.fs` + `read_file` → LLM 侧 `fs__read_file`；
- 前缀取 `module_id` 最后一段；冲突加序号 `fs2__`；映射表对**当前 turn 快照**稳定，不随注册表实时变化；
- LLM 返回未知工具名 → 不崩溃，回一条 `tool` 角色消息说明「工具不存在，可用工具：…」，让模型自己改。

**实例选择**：

| concurrency | 选择 |
|---|---|
| `exclusive` | 唯一实例；不在线 → `UNAVAILABLE` |
| `pooled` | 在途数最少者；并列取最近心跳更新者；全部熔断 → `UNAVAILABLE` |
| `mirrored` | **不可路由工具**；若有 tools 声明，注册时就该拒（C2 负责） |

**调用**：发 `tools/call {call_id, tool, args, deadline_ms}`；`deadline_ms = min(manifest.timeout_ms, 配置上限)`。超时 → 发 `tools/cancel`，返回 `TIMEOUT`，**不等待**模块确认。

**会话上下文注入（关键）**：`tools/call` 的载荷里带 `ctx: {session_id}`，**由 core 填，模型无法指定**：

| 情况 | 行为 |
|---|---|
| manifest 的 `injects` 含 `session_id` | `ctx.session_id` = 当前 turn 的会话 id；模块只读 |
| 未声明 | **不发 `ctx`**。不是发空值——发了空值，模块分不清「没注入」和「空会话」 |
| 模型在 args 里自己塞 `session_id` | router 先按 manifest 的 `input_schema` 校验，多余字段（`additionalProperties:false` 时）直接拒；schema 宽松时也**强制覆盖**为 core 的值 |

这条不做，M4a 的「模型不能伪造 session_id」就是纸面承诺——loop 现在是原样透传 args。

**熔断**：滑动窗口 30s 内失败 ≥ 5 次或超时率 > 20% → open 30s → half-open 放 1 个探测。熔断状态要能被 registry 读到（`degraded` 态），提供只读方法，不要反向 import registry。

### T4 · Agent 循环（2 天）

```
core/vade/loop/
├── ports.py      # ApprovalPort / AuditPort / EventSink 协议
├── engine.py     # 循环本体
├── context.py    # 上下文拼装与裁剪
└── tools.py      # 工具快照与 schema 转换
```

循环：

```
turn.start
  snapshot = registry.tool_snapshot()          # 本轮固定，中途 update 不影响
  loop (max_steps = 8, 可配):
    resp = llm.chat(messages, tools=snapshot.schemas, stream=True)
    流式 delta -> turn.delta (逐段)
    if 无 tool_calls: break
    for call in tool_calls:
      decision = approval.check(tool_id, args)  # C5a 实现
      if need_approval: 挂起等 decision（超时拒绝）
      if deny: 回填拒绝原因给模型，continue
      result = router.call(...)
      emit turn.tool_call / turn.tool_result（args 只发 digest 给 UI，完整参数走审批事件）
      append tool message
  turn.completed(usage)
```

硬规则：
- **`agent-loop` 不认识任何具体工具名**。只认 `snapshot` 里的条目和 `router.call(tool_id, args)`。这条会有测试：grep 源码不出现 `fs__` / `shell__` 字面量。
- 工具快照在 turn 开始时取一次；`capabilities.update` 只在下一轮生效。
- 终止条件：无 tool_call / 达到 max_steps / 用户中断 / 不可恢复错误。达到 max_steps 要回一条可读消息，不是静默停。
- 上下文裁剪：参数全部来自 `[compaction]`（`threshold=0.50` / `target_ratio=0.20` / `protect_last_n=20` / `protect_first_n=3` / `tail_mode`），**不要硬编码**。触发条件是 token 数 > `threshold × ctx`，压到 `target_ratio × ctx` 以下。
- 保留优先级：system + 前 `protect_first_n` 条 + 最近 `protect_last_n` 条 + **当前 turn 的 tool 结果**；先裁最早的 assistant 文本。`tail_mode` 改了就打断 prefix cache，所以它是 `restart` 项（§7.9.1）。
- 无 LLM 剪枝优先：`compaction.proactive_prune_tokens > 0` 时，先把超过 `min_result_chars` 的**旧**工具结果替换成摘要 + `blob_id`，再考虑调摘要模型。工具输出是上下文污染的最大来源，先剪它最划算。
- **会话级 rolling summary 不归你**：那是 M4b 的 `turn_review` 产出的，你只做当前上下文窗口内的压缩。两边都做会重复付费，且摘要口径不一致。
- 工具结果超过 `max_tool_result_tokens`（默认 4000）时，存全文到 `data/blobs/`，只把摘要 + `blob_id` 给模型（模型可再用 `fs` 读，若装了）。
- 中断：`CancelledError` 必须能穿透到 router，且**不把 traceback 写进用户可见消息**。

### T4.5 · prompt 组装点（C3b 在此接入）

loop 每轮拼 prompt **只调一个函数**，不内联字符串拼接——否则 C3b 没有地方插：

```python
# core/vade/loop/prompt.py
@dataclass(frozen=True)
class PromptParts:
    persona: str
    tool_block: str          # 由工具快照渲染
    skill_catalog: str = ""          # C3b 填：目录行
    skill_bodies: str = ""           # C3b 填：已激活正文
    session_memory: str = ""         # M4a 的 session 层，core 读后填
    user_message: str = ""           # 当轮用户原文，供 skill 激活匹配
    activated_skills: tuple[str, ...] = ()   # C3b 回填，loop 只读，写进审计
    context: str = ""

def assemble(parts: PromptParts) -> list[Message]: ...
```

- 顺序固定：`persona → tool_block → skill_catalog → skill_bodies → session_memory → context`（方案 §2.7.3）。
- **`user_message` 必须由 loop 在调 hook 前填好**。不填，C3b 的 `@mention` / `triggers` 匹配就是死代码。
- **`session_memory` 由 loop 通过 `MemoryPort.read_session(session_id)` 读**；M4a 未部署时返回空串，不阻塞 turn。C3 只认端口，不 import memory 包。
- **`activated_skills` 是回传通道**：hook 返回新 parts 时一并带出，loop 写进 `turn.completed.skills`。loop 不判断内容。
- C3 阶段两个 skill 字段恒为空串；**C3 不得 import skill 包**，只留字段，避免两批互相阻塞。
- `assemble` 必须是纯函数：同样入参 → 逐字节相同输出（C3b 的快照测试依赖这点）。
- 预留装配点：`app.prompt_hooks: list[Callable[[PromptParts], PromptParts]]`，`assemble` 前依次调用。C3b 的 `install(app)` 往里 append 一个填充 skill 字段的 hook——**C3b 不改 loop 代码**。

### T5 · LLM 适配器（1 天）

```
core/vade/llm/
├── base.py            # LLMProvider 协议：chat(messages, tools, stream) -> AsyncIterator[Delta]
└── openai_compat.py   # /v1/chat/completions，SSE 流式，tool_calls 增量拼接
```

- 配置：`[llm] base_url / model / api_key_env / timeout_s / max_retries`；**key 只从环境变量读**，不写配置文件、不进日志。
- 重试：仅对 429 / 5xx / 连接错误，最多 2 次，指数退避 + 抖动；4xx 不重试。
- 流式：必须支持 `delta.tool_calls[].function.arguments` 分片拼接（这是最容易写错的地方，要有单测）。
- 用量：`usage` 写进 `turn.completed`，并按 module_id 归因（工具部分归 0，模型部分归 `builtin` 或 model 模块）。
- 可替换：`LLMProvider` 由配置注入。**你还要实现一个 `ModuleLLMProvider`**：先从 registry 查 `capabilities` 含 `model` 的在线实例，按 `priority` 降序取第一个，再通过 router 调它的 `chat`，把返回的 OpenAI 形状转成统一 `Delta` 流（v1 只有一个 delta）。默认关闭，`[llm] provider="module"` 时启用；`fallback_to_builtin` 控制模块离线时是否回落。M6 只负责模块侧，**不改你的代码**。

### T6 · UI 侧 RPC（1 天）

实现 `ui/attach`、`ui/detach`、`ui/submit`、`ui/interrupt`、`ui/queue.cancel`、**`approval/decide`**，以及把事件推给所有 attached 连接。

**`approval/decide` 由你注册，C5a 只提供 `ApprovalPort`**（两边都不写就没人接，这是审查抓到的接线缺口）：

```python
# T6 里必须有的分发分支
async def on_approval_decide(conn, params):
    req_id, decision, scope = params["request_id"], params["decision"], params.get("scope", "once")
    # 1) 身份校验：只有 device_key 或 loopback admin 能批 high
    if conn.auth_mode not in ("device_key", "admin_local"):
        raise RpcError("APPROVER_INVALID", "browser_session cannot approve high-risk calls")
    # 2) 交给 C5a 的端口做「第一个生效」
    ok, resolved = await self.approval_port.decide(req_id, decision, scope, by=conn.device_id)
    # 3) 无论成功与否，都要广播关闭事件，否则其余端一直转圈
    await self.broadcast_all(resolved.session_id,
        "turn.approval_resolved",
        {"turn_id": resolved.turn_id, "request_id": req_id,
         "decision": decision, "by": conn.device_id or "admin", "scope": scope})
```

- 一个 session 可被多端 attach（`mirrored` 语义在会话层体现，不依赖模块 concurrency）。
- `ui/attach` 必须返回 `session.attached{from_seq, snapshot?}` 作为**第一个**事件。
- 推送失败（连接已断）不得影响会话执行；发送队列满 → 断开该 UI 连接，不阻塞 turn。
- `module.status` 事件由 registry 变更触发，经同一事件总线广播（带 seq，便于补发）。
- **每轮结束发 `session.turn`**（M4b 的 `turn_end` 触发器只认这个）：

```json
{"session_id": "...", "turn_id": "...", "state": "completed",
 "user_text": "...", "assistant_text": "...",
 "tool_calls": [{"tool": "fs__read_file", "args_digest": "sha256:...", "ok": true}]}
```

  不发这个，`turn_end` 与 `turn_review` 都永远不触发——审查发现「五个触发器里只有 consolidate 有数据来源」，这是其中一处。

- 会话结束（用户关闭/删除会话、core 正常关闭）时依次做四件事，**顺序不能反**：
  1. `emit session.closed`，载荷**必须够 M4b 判定 `skill_evolve`，且只带最后 10 轮原文**：
```json
{"session_id": "...", "closed_at": 1730000000, "turns_total": 87,
 "turns": [{"turn_id": "...", "state": "completed", "tool_calls": 6,
            "negative": false, "self_corrected": false}],
 "tail":  [{"turn_id": "...", "user_text": "...", "assistant_text": "..."}]}
```
  - `tail` **由 core 强制截到最近 10 轮**，不是模块请求多少给多少。整段转录不进事件——100 轮约 8 万 token，M4b 的日预算会被一个会话吃光。
  - `self_corrected`：同一会话内后轮推翻前轮结论时为 `true`；M4b 靠它挡住“把纠错轨迹学成最佳实践”。判据见 `24-C3d` §四。
  2. 遍历 `app.on_session_end` 回调（C5a 的清放行、C3d 的投递都挂在这里）；
  3. `ApprovalPort.on_session_end(session_id)` —— 清掉 `scope=session` 的放行记录；
  4. 标记 `session.state = closed`，保留数据不删（TTL 由 M4a 管）。
  **不要**把放行表存在 C3 里。

- `turns[]` 里的 `tool_calls` 是**计数**不是明细——M4b 只用它判 `>=5`，给明细等于把整段转录再复制一遍。
- `state` 的终态值统一为 **`completed`**，不是 `done`。M4b 与 design §5 都按 `completed` 判。

### T0.5 · `app` 扩展位（别人 append，你遍历）

C3 是「宿主」，四个扩展位必须**在 T0 就建好**，默认值给全，否则 C3b/C3d/C5a 的 `install` 会 `AttributeError`。

**文件归属**：`app.py` 归 C2（见 `01-contracts` 目录所有权表），你不改它。你把槽定义放自己的 `core/vade/slots.py`，C2 的 `app.py` 只做 `from .slots import install_slots` + `install_slots(app)`。

> **波次时序（v1.18 明确，解决 W2 早于 W3 的依赖倒置）**：C2 是 W2、C3 是 W3。所以 **C2 在 W2 先建一个 `core/vade/slots.py` 空壳**（四个槽都初始化为空列表 + `def install_slots(app): ...` 占位，只 `pass`），让 C2 自己的 `app.py` 能 import 而不 `ModuleNotFoundError`；**C3 在 W3 填真实实现**。所有权仍是 C3，C2 只在 W2 建空壳、之后不再改。C2 的 `app.py` 示例（`21-C2` T3）必须包含 `install_slots(app)` 调用。

```python
# core/vade/slots.py（C3 所有；C2 的 app.py 只调用 install_slots(app)）
app.prompt_hooks: list[Callable[[PromptParts], PromptParts]] = []
app.tool_failure_hooks: list[Callable[[list[str]], None]] = []
app.on_session_end: list[Callable[[SessionClosed], None]] = []
app.approval_port: ApprovalPort | None = None      # C5a 的 install 里赋值
```

另外两个**端口**（C3 消费，C5a / M4a 实现）：

```python
# core/vade/loop/ports.py
class MemoryPort(Protocol):
    async def read_session(self, session_id: str, limit: int = 50) -> list[MemoryItem]:
        """读当前会话的 session 层记忆，供 prompt 注入。M4a 未部署时返回 []。"""

    # 管理面专用（AMEND-16）；全部转 M4a 的 memory/admin_* 私有 RPC
    async def admin_list(self, query: MemoryQuery) -> tuple[list[MemoryItem], str | None]: ...
    async def admin_get(self, mem_id: str) -> tuple[MemoryItem, list[MemoryItem]]: ...
    async def admin_promote(self, mem_id: str, by: str) -> bool: ...
    async def admin_archive(self, mem_id: str, reason: str, by: str) -> bool: ...
    async def admin_restore(self, mem_id: str, by: str) -> bool: ...
    async def admin_stats(self) -> dict: ...
```

- 管理方法**只被 C5a 的 admin 层调用**，loop 与 router 都不碰。C3 只做转发（`MemoryPort` → `memory/admin_*` RPC），**不做权限判断**：权限在 admin 层（有没有登录、有没有 CSRF），到了这里就是「已授权的请求」。
- M4a 未部署时这些方法抛 `NOT_FOUND`（而不是返回空），否则页面会显示「一条记忆都没有」，让人误以为记忆丢了。

- `MemoryPort` 是 **core 内部端口**，不是工具、不进模型工具表。M4a 侧对应一个私有 RPC `memory/read_session {session_id, limit}`，**只有 core 能调**（模块侧不可见）。
- 不定义这个端口，`session_memory` 永远是空串，M4a 的「自动注入」就是一句空话——审查发现 C3 引用了 `MemoryPort.read_session` 但全仓无定义。

## 五、验收

```bash
pytest core/tests/session core/tests/router core/tests/loop -q

# 端到端（用 C2 的假模块 + 一个假 LLM，不花钱）
pytest core/tests/e2e/test_turn_lifecycle.py -q -s
```

必须覆盖：

| 用例 | 判据 |
|---|---|
| 正常一轮 | 事件序列 `queued→started→tool_call→tool_result→delta→completed`，seq 连续无洞 |
| 三端各发 3 条 | 执行顺序 = 到达顺序；每端看到相同 seq 流 |
| `client_msg_id` 重复提交 | 只产生一个 turn，返回同一 `turn_id` |
| interrupt | 500ms 内 `interrupted`，下一个 turn 立刻开始；模块不响应 cancel 也不阻塞 |
| replay 覆盖 | `attach(last_seq=N)` 补发 N+1 起，无重复无缺失 |
| replay 不覆盖 | 返回 snapshot，消息树与队列与当前一致 |
| 熔断 | 连续 5 次超时 → 后续快速失败 `UNAVAILABLE`；30s 后 half-open |
| 工具名冲突 | `fs__read_file` 与 `fs2__read_file` 各自路由正确 |
| 未知工具 | 不崩，模型收到可读错误并继续 |
| max_steps | 第 8 步停止，`turn.completed` 带 `stopped_reason=max_steps` |
| 无模块 | 空跑：发消息得到「无可用能力」而非异常（FR-1.4） |

## 六、禁止

- 禁止在 loop 里写任何具体工具名、模块 id、路径。
- 禁止把会话状态放进程外（Redis 等）——v1 单 core，SQLite 足够。
- 禁止「先推事件后落盘」。
- 禁止为通过中断测试而在 cancel 上 `sleep` 等待。
- 禁止把 LLM key 写进 `core.toml` 或日志。
- 禁止直接 import `vade.policy` / `vade.audit`（用 ports）。

## 七、交付格式

按 `00-orchestration.md` §三，额外附：
- 一轮完整 turn 的 `events` 表 dump（含 seq 与 type）
- 三端并发的时序日志
- max_steps / 熔断 / replay 的原始测试输出
- `schema/events.json` 的 sha256
