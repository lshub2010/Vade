# M4a · `memory` 记忆模块（存储与召回）

| 项 | 值 |
|---|---|
| Agent 代号 | `m4-memory` |
| 波次 | W5 |
| 依赖 | C4 的 SDK v0.1 |
| 配对批次 | `M4b`（evolver）依赖本批的存储接口 |
| 目录所有权 | `modules/memory/`（含 `manifest.json`、建表 SQL 含 `consolidate_run`、`evolve/` 空包预置） |
| 出口 | 五道门 + 两层记忆隔离 + 重启后召回一致 + 去重/冲突/淘汰生效 |

## 一、一句话任务

两件事：**存得住**（两层记忆、去重、冲突不覆盖、超限淘汰、重启不丢）和**查得回**（语义召回）。
**不依赖 model 模块**做 embedding。

> 定位是「过程记忆」，不是知识库（D37）：这里存模型与 evolver 产生的东西（偏好、结论、上下文），
> 人工整理的沉淀知识走外置 MCP 知识库，两边不重复存。

## 一·五、两层记忆（D40）

| | `session` 层 | `global` 层 |
|---|---|---|
| 分区 | `session_id`（**core 注入，模型不可指定**） | `namespace`（`user:default` / `agent:x` / `project:y`） |
| 写 | 只由 evolver 写（本批留接口，M4b 填） | `remember` 工具 + evolver 晋升 |
| 读 | **core 直接注入 prompt**，不走 recall | `recall` 检索 |
| 生命周期 | 会话关闭 +7 天 TTL | 永久，受配额 |
| 类型 | `kind ∈ {fact, preference, lesson}`；lesson 带 `evidence` / `verified_at` | 同左 |
| 上限 | **双闸**：≤200 条 **且** ≤`session_inject_char_limit`（默认 4200 字符），先到先限 | 5000 条；`recall` 单次 ≤`recall_token_limit`（默认 800 token） |

- `remember` 的 `scope` 只接受 `global` / `session`；后者由 core 在调用时注入 `session_id`——**工具参数里没有 session_id 字段**，模型无法伪造。
- `recall` 默认只查 `global`；`include_session=true` 时叠加当前会话层（session_id 同样由 core 注入）。
- **session 层是按预算注入，不是全量**（AMEND-19/D79）：按 `importance × recency` 排序，累计字符数到 `session_inject_char_limit` 即停。被裁掉的条目仍能通过 `recall(include_session=true)` 捞到。理由：注入是每轮付费，200 条全量 ≈ 8k token/轮，100 轮就是 80 万——比自进化本身贵一个量级。
- 本模块读的配置键（`memory.*`）权威定义在 `design.md §7.9.1` / 契约 **C-18**。**不要在 `config.py` 里自造默认值**——core 的 schema 校验会以「未知键」拒绝启动，两边默认值分叉时更难查。

## 二、必读

- `docs/design.md` §16.1（两层记忆）、§16.3（熵增对策）、§15.2（memory 规格）
- `docs/requirements.md` D37、D40、D44、A26、A28（§8.10）
- `sdk/PYTHON_SDK.md`

## 三、目录

```
modules/memory/
├── manifest.json               # 你独占；M4b 只读，不得改
├── src/memory_module/
│   ├── tools.py                # 纯函数，零 SDK import
│   ├── store.py                # SQLite + 向量；MemoryBackend 接口的唯一实现
│   ├── embed.py                # embedding 客户端
│   ├── server.py
│   └── evolve/                 # ← 你只建空包（__init__.py + README），M4b 在 W6 填
├── tests/{test_tools.py,test_persistence.py}
├── Dockerfile
└── README.md

schema/memory.schema.json       # ← 契约 C-12，你交付（目录表已授权）
schema/memory-admin.json        # ← 契约 C-16，你交付（memory/admin_* 六个私有 RPC，不进工具表）
```

**为什么 `evolve/` 要你先建**：M4b 的目录所有权是 `evolve/` 内，但父目录 `modules/memory/` 归你。如果你不预置，M4b 第一次提交就要动你的目录，合并门禁会以「越界」拒绝。空包 + `manifest.json` 里预置 `"role": "evolve"` 与 `"subscriptions": ["session.turn", "session.closed"]`，M4b 只填实现。

## 四、工具规格

| 工具 | 参数 | risk | timeout |
|---|---|---|---|
| `remember` | `text, tags=[], scope="global", namespace="user:default"` | low | 15s |
| `recall` | `query, k=5, namespace="", include_session=false, include_superseded=false` | low | 15s |
| `forget` | `id` | **high** | 10s |

`scope ∈ {global, session}`。`session` 时 core 注入 `session_id`（参数里没有这个字段）；`namespace` 仅在 `global` 下有效，正则 `^[a-z][a-z0-9:_-]{0,63}$`。

> 设计 §15.2 把 `memory` 整体标为 low；这里把 `forget` 单独标 **high**——删除不可逆，宁可多一次审批。README 要写明这处「更保守」的偏离。

- **私有 RPC `memory/read_session`**（core→模块，不在工具表里，模型不可见）：`{session_id, limit}` → `{items:[{id,text,tags,created_at}]}`。C3 的 `MemoryPort.read_session` 调它。**必须实现**——不实现，session 层记忆永远进不了 prompt。

**管理用私有 RPC（AMEND-16，同样不进工具表）**：

配置页的记忆管理通过 core→模块的私有 RPC 落到底层，**不直连 SQLite**：

| 方法 | 入参 | 出参 |
|---|---|---|
| `memory/admin_list` | `{layer?, namespace?, status?, from?, to?, q?, limit=50, cursor?}` | `{items:[{id,layer,namespace,status,text,tags,created_at,expires_at,hit_count,supersedes}], next_cursor}` |
| `memory/admin_get` | `{id}` | 单条详情 + `supersedes` 前后链（`chain:[{id,status,created_at}]`） |
| `memory/admin_promote` | `{id, by}` | `{ok, id}` —— `staging → active`，写 `promoted_by="admin:<name>"` |
| `memory/admin_archive` | `{id, reason, by}` | `{ok}` —— 软归档，可恢复 |
| `memory/admin_restore` | `{id, by}` | `{ok}` —— `archived → active` |
| `memory/admin_stats` | `{}` | `{global_count, global_limit, staging_count, archived_count, oldest_at}` |

三条硬约束：

1. **不做权限判断**。只有 core 能连模块（FR-5.7），权限在 core 侧判（只有 admin 会话能触发）。模块不要在这里再写一套角色逻辑，两套迟早不一致。
2. **`admin_list` 不返回原文对话**。返回的是记忆条目正文与 `session_id`/`turn_id` 指针，**没有** `user_text`/`assistant_text` 字段。这不是省事，是隐私边界（§7.9）。
3. **没有「编辑」RPC**。要改就 `archive` 旧的 + `remember` 新的，`supersedes` 链保留溯源。不要为了「顺手改个错字」加 `admin_update`。

- `admin_list` 的关键词检索 `q` 用 SQLite `LIKE`（`%q%`，对 `text` 与 `tags`），**不走向量**：管理页面要的是「我大概记得有这句」，不是语义相似。走向量会让结果不可预测，且每次翻页都要重算 embedding。
- `concurrency = "exclusive"`（D13：记忆存储一能力一实例，第二实例注册应被 core 拒）。
- `namespace` 正则 `^[a-z0-9][a-z0-9:_-]{0,63}$`，默认 `user:default`；`recall` 的 `namespace` 为空表示不限。**参数名统一用 `namespace`**，`scope` 只表示 `session|global` 两层，两者不要混。
- 单条 `text` ≤ 8KB（超限 `PAYLOAD_TOO_LARGE`）；`tags` ≤ 16 个，每个 ≤ 32 字；`k ∈ [1,20]`。

## 五、实现

### 存储

**存储走接口，SQLite 只是默认实现**（与 C3 的 `SessionStore` 同一手法）：

```python
class MemoryBackend(Protocol):
    # 工具路径
    def put(self, rec: MemoryRecord) -> str: ...            # 去重，返回 id
    def search(self, vec: list[float], k: int, namespace: str,
               include_session: str | None = None) -> list[MemoryRecord]: ...
    def delete(self, mem_id: str) -> bool: ...              # 软删，写 archive
    def all_vectors(self) -> tuple[list[str], "np.ndarray"]: ...   # 启动时载入内存矩阵

    # core 专用（不进工具表，模型看不见）
    def read_session(self, session_id: str, limit: int = 50) -> list[MemoryRecord]: ...
    # 管理面专用（同样不进工具表；AMEND-16）
    def admin_list(self, *, layer: str | None, namespace: str | None, status: str | None,
                   t_from: int | None, t_to: int | None, q: str | None,
                   limit: int, cursor: str | None) -> tuple[list[MemoryRecord], str | None]: ...
    def admin_get(self, mem_id: str) -> tuple[MemoryRecord, list[MemoryRecord]]: ...
    def admin_archive(self, mem_id: str, reason: str, by: str) -> bool: ...
    def admin_restore(self, mem_id: str, by: str) -> bool: ...
    def admin_stats(self) -> dict: ...

    # M4b 专用（evolve 包要读写，但它只能通过这个接口，不能碰 SQLite）
    def staging_put(self, text: str, namespace: str, session_id: str,
                    embedding: list[float], confidence: float) -> str: ...
    def staging_list(self, namespace: str | None = None) -> list[StagingRecord]: ...
    def staging_promote(self, staging_id: str) -> str: ...
    def staging_drop(self, staging_id: str) -> None: ...
    def supersede(self, old_id: str, new_id: str) -> None: ...
    def archive_row(self, mem_id: str, reason: str) -> None: ...
```

**为什么这几个方法必须由你实现，而不是 M4b 自己写 SQL**：M4b 的目录所有权只有 `evolve/`，`store.py` 归你。它绕过接口直连 SQLite 就等于改你的文件，合并门禁会拒。接口不够就让它提 RFC，不要把 `connection` 漏出去。

- v1 只实现 `SqliteBackend`。接口存在的意义：将来换真向量库或改成远程共享服务时，**不改 `tools.py`**。
- 注意：当前实现把全量向量读进内存（20,000 条上限），这是**单实例**设计。多 core 共享不要直接复用，见 README 的「已知限制」。

`data/memory.db`（SQLite WAL）：

```sql
CREATE TABLE memories(
  id TEXT PRIMARY KEY,          -- ULID
  layer TEXT NOT NULL,          -- 'session' | 'global'
  namespace TEXT NOT NULL,      -- global 层用；session 层固定 ''
  session_id TEXT NOT NULL DEFAULT '',   -- session 层用；global 层固定 ''（不用 NULL！见下）
  text TEXT NOT NULL,
  tags TEXT NOT NULL,           -- JSON array
  content_hash TEXT NOT NULL,
  embedding BLOB NOT NULL,
  importance REAL NOT NULL,     -- 1–5，显式 remember 固定 5
  confidence REAL NOT NULL,     -- 0–1
  source TEXT NOT NULL,         -- 'user' | 'evolve' | 'import'
  kind TEXT NOT NULL DEFAULT 'fact',  -- fact | preference | lesson（AMEND-19）
  evidence TEXT,                -- lesson 必填：{turn_id, tool, result_digest, claim}
  verified_at INTEGER,          -- lesson 二次验证时间；NULL = unverified
  supersedes TEXT,              -- 我替代了谁
  superseded_by TEXT,           -- 我被谁替代（软失效）
  hit_count INTEGER DEFAULT 0,
  last_hit_at INTEGER,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,           -- session 层 TTL
  deleted_at INTEGER,
  UNIQUE(layer, session_id, namespace, content_hash)
);
CREATE TABLE staging(           -- 未晋升的候选（evolver 写，consolidate 消费）
  id TEXT PRIMARY KEY, text TEXT NOT NULL, namespace TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'fact',   -- fact | preference | lesson（AMEND-19）
  evidence TEXT,                       -- lesson 必填：{turn_id, tool, result_digest, claim}
  content_hash TEXT NOT NULL, seen_sessions TEXT NOT NULL,  -- JSON list
  embedding BLOB,               -- 写入时就算好，consolidate 聚类要用；缺了它只能做字符串匹配
  confidence REAL, created_at INTEGER,
  UNIQUE(kind, namespace, content_hash)   -- kind 进唯一键：同一句话作为 fact 与 lesson 是两条
);
CREATE TABLE archive(           -- 淘汰与软删的落点，可恢复
  id TEXT PRIMARY KEY, payload TEXT NOT NULL, archived_at INTEGER, reason TEXT
);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);   -- embedding_model, dim
CREATE TABLE consolidate_run(                     -- M4b 的 consolidate 幂等水位（M4a 建表，M4b 只读+写 run 行）
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  last_run INTEGER NOT NULL,                      -- 上次 consolidate 完成时间（unix ts）
  kind TEXT NOT NULL,                             -- 'scheduled' | 'startup_backfill' | 'manual'
  stats TEXT                                       -- {promoted, merged, archived, decayed} JSON
);
```

> **`consolidate_run` 表由 M4a 在 W5 建好**（v1.18 明确）：M4b 的 `scheduler.py` 启动补跑要读 `consolidate_run.last_run` 判断是否 >24h 需补跑，但 M4b 的目录所有权只有 `evolve/`、不改 `store.py`。所以建表 SQL 与读写接口由 M4a 提供（`store.py` 增 `get_last_consolidate()` / `record_consolidate(run_id,kind,stats)`），M4b 只调用。不建此表，M4b 在 W6 会卡死。
>
> 维度写进 `meta`；启动时若配置的 embedding 模型维度与库不一致 → **拒绝启动**并给出可读原因（不要静默重算，会把库写坏）。
- 向量用 `numpy.float32.tobytes()` 存 BLOB。
- 召回复现：启动时把未删除的向量读进内存矩阵（`np.ndarray`），`recall` 做矩阵乘 + 排序；`remember`/`forget` 同步更新内存矩阵。
- 配额：`global` 5000 条；`session` **双闸**——200 条 且 `session_inject_char_limit`（默认 4200 字符）。1536 维下 5000 条约 30MB 内存。
- 超配额：**先跑一次评分淘汰**（见下），腾不出空间才返回 `OVERLOADED`，不静默丢新数据。
- **`session` 层写超字符闸返回 `MEMORY_FULL`**（AMEND-19/D79）：这是给 evolver 的信号，让它当轮合并/淘汰后重试，而不是等每日 consolidate。`MEMORY_FULL` 与 `OVERLOADED` 不同——前者是“该整理了”，后者是“真的满了”。
- 淘汰进 `archive` 表，**不物理删除**（D44）；只有 `forget` 与 session TTL 到期才 `DELETE`。

### 唯一键为什么必须带 session_id

原设计是 `UNIQUE(layer, namespace, content_hash)`，session 层的 `namespace` 恒为 `''`、hash 也不含 `session_id`。后果不是「去重不准」，是**跨会话串数据**：A 会话写「我偏好深色主题」，B 会话写同一句，命中同一行——B 读到的是 A 的记忆，两个会话的 200 条配额也合成了一个。

另外，SQLite 的 `UNIQUE` 里 **`NULL` 互不相等**。如果 `session_id` 允许 `NULL`，global 层的去重会整个失效（每行都是"新"的）。所以字段是 `NOT NULL DEFAULT ''`，不用 `NULL` 表示"不适用"。

### session 层生命周期

- 写入时 `expires_at = now + 7d`；`session_id` 由 core 注入（`tools/call` 的 `ctx.session_id`），进程内校验格式（ULID），**不信任模型传入**。
- 会话关闭（core 发 `session.closed`）→ 该会话所有条目 `expires_at = now + 7d`（**从关闭时刻起算 7 天，不是置为 now**）。置为 now 会让下一次启动的清理直接删掉，会话记忆提前丢失，这是审查抓到的一处算错。
- 启动时执行一次 `DELETE FROM memories WHERE layer='session' AND expires_at < now`。
- `session` 层**不参与** `recall` 的默认检索，只有 `include_session=true` 才叠加，且必须带当前 `session_id`。

### 去重

- `content_hash = sha256(layer + "\0" + session_id + "\0" + namespace + "\0" + normalized_text)`；`normalized_text` 只做 `strip()` 与连续空白折叠，**v1 不做语义去重**（语义合并由 M4b 的 consolidate 处理，且只合并不删原文）。
- 命中唯一约束 → 返回已存在的 `id` + `deduped: true`，不重复调 embedding；**若新文本与旧文本不同但哈希相同（几乎不可能），只保留旧条并告警**。

### embedding

- OpenAI 兼容 `POST {base_url}/embeddings`，`api_key_env` 指定环境变量名（默认 `MEMORY_API_KEY`）。
- 超时 10s，重试 1 次（仅 429/5xx）；失败 → `UNAVAILABLE`，**不写入半条记录**。
- 批处理：`remember` 一次一条，不做批量接口（保持简单）。
- 日志只记 `text_digest` 与长度，**不记原文**。

### 评分与淘汰（D44）

```
score = 0.45*importance + 0.30*exp(-(now-created_at)/30d)
      + 0.20*log1p(hit_count) - 1.00*(superseded_by IS NOT NULL)
```

- 每次 `recall` 命中 → `hit_count += 1`、`last_hit_at = now`（批量异步写，不阻塞返回）。
- 超配额时按 `score` **升序**淘汰到 90% 水位，每条写 `archive(reason='quota')`。
- `superseded_by IS NOT NULL` 的条目 **score 恒为负**，优先淘汰，但保留在库里供 `include_superseded` 查。
- 淘汰不看 `source='user'`？**看**，但用户的显式记忆 `importance=5`，实际很难被淘汰；不做"用户记忆永不淘汰"的特权，否则库会只进不出。

### forget

- 软删 `deleted_at`，同时写 `archive(reason='forget')`，立即从内存矩阵移除；`recall` 不再返回。
- 不提供「恢复」；物理清理在启动时对 `deleted_at < now-7d` 执行 `DELETE`。
- 不存在的 id → `NOT_FOUND`（不要静默成功）。

### recall 返回

```json
{"ok": true, "result": {"items": [
  {"id": "01J...", "text": "...", "tags": ["x"], "layer": "global",
   "namespace": "user:default", "confidence": 0.9, "score": 0.83}
], "count": 1}}
```

`score` 为余弦相似度，保留 3 位；`k` 大于实际条数时返回实际条数，不报错。

## 六、验收（五道门）

```bash
cd modules/memory

python -m memory_module --standalone remember --args '{"text":"用户偏好深色主题","tags":["pref"]}'
python -m memory_module --standalone recall  --args '{"query":"界面主题","k":3}'
python -m memory_module --standalone forget  --args '{"id":"<上一步的 id>"}'

# 重启后仍在
python -m memory_module --standalone remember --args '{"text":"项目代号 Orion"}'
pkill -f memory_module; python -m memory_module --standalone recall --args '{"query":"项目代号"}'
# 期望仍返回 Orion

pytest tests -q
```

必测：
1. 同一文本写两次 → 1 条，第二次 `deduped: true`；
2. 不同 scope 同文本 → 2 条（唯一约束含 scope）；
3. 重启后 recall 结果一致（同 query 同 k，顺序一致）；
4. `forget` 后 recall 不再返回；重复 forget → `NOT_FOUND`；
5. embedding 服务不可用 → `UNAVAILABLE`，库中无残留；
6. 维度不匹配 → 启动失败且错误可读；
7. 20,001 条 → `OVERLOADED`（可用小上限配置测）；
8. `concurrency=exclusive`：第二个实例注册被 core 拒（e2e）；
9. **两层隔离**：写一条 `session` 一条 `global` → 默认 `recall` 只返回 global；`include_session=true` 且带正确 session_id 才返回两条；
10. **伪造 session_id**：直接调 SDK 传 `session_id` 参数 → 被拒（参数不接受这个字段）；
11. **supersedes**：手工造新条指向旧条 → 默认 recall 不返回旧条；`include_superseded=true` 返回两条且链可追；
12. **配额淘汰**：把上限配成 5，写 8 条 → 保留 score 最高的 5 条，被淘汰的 3 条在 `archive` 里能查到，`reason='quota'`；
13. **TTL**：把某条 `expires_at` 改成过去 → 重启后被物理删除，`archive` 里没有（TTL 不走 archive）。
14. **字符闸**：`session_inject_char_limit=100`，连写 4 条（每条 40 字符）→ 第 4 条返回 `MEMORY_FULL`；删一条后重试成功；期间无静默丢弃（对比写入前后条数）；
15. **注入预算**：灌 200 条，`session` 层注入返回的条目累计字符 ≤ 4200，且顺序为 `importance × recency` 降序；`recall(include_session=true)` 能捞到被裁掉的条目；
16. **lesson 字段**：写一条 `kind=lesson` 但 `evidence` 为空 → 拒绝（`INVALID_ARGUMENT`）；补齐后写入成功，`verified_at` 为 NULL。

## 七、禁止

- 禁止依赖 `model` 模块做 embedding。
- 禁止把原文写日志。
- 禁止静默重算向量（维度/模型变化必须显式失败）。
- 禁止用"语义相似"做去重（误删不可逆）。
- 禁止在 `tools.py` import SDK。
- 禁止接受模型传入的 `session_id`（只能 core 注入）。
- 禁止物理删除未过 TTL 的记录（一律先进 `archive`）。
- 禁止把 `session` 层条目晋升到 `global`（晋升是 M4b 的 consolidate 的事，本批只留 `staging` 表）。
- 禁止用 `importance` 做特权豁免（用户记忆也会被淘汰，否则库只进不出）。

## 八、交付格式

按 §三，额外附：

1. 重启前后 recall 的原始输出对比；
2. **两个会话写同一句话 → 得到两条记录**（唯一键含 `session_id` 的证据），以及 global 层同句仍只存一条；
3. 维度不匹配的启动报错；
4. Docker 卷挂载说明（`data/` 必须持久化）；
5. **`schema/memory.schema.json`（C-12）**：`MemoryRecord` / `StagingRecord` / `ArchiveRecord` 三张表的字段与枚举，M4b 与 C5a 都照它写。不交这份，M4b 无法开工。
6. **`schema/memory-admin.json`（C-16）**：`memory/admin_*` 六个私有 RPC 的入参出参与 `MemoryQuery` 定义（**不进工具表**），与 C-12 同批（W5 中）冻结。C3 / C5a 照它实现管理面记忆浏览。
