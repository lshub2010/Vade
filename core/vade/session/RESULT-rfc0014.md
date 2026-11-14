# RFC-0014 · 会话搜索（`session/search`）· RESULT

| 项 | 值 |
|---|---|
| 分支 | `tmp/rfc0014-search`（基线 `bf2080b`，worktree `D:/HermesWorkspace/Vade-wt/rfc0014-search`） |
| RFC | `docs/rfc/RFC-0014-session-search.md`（`status: proposed`，§1 摘要+决定表 12 条 → §2 动机 → §3 目标/非目标 → §4 语义（范围/匹配/大小写/排序/分页/空查询/片段/权限/性能/注入 十节）→ §5 RPC → §6 契约变更 → §7 未决 3 条 → §8 验收 A1–A14 → §9 影响面）+ `docs/rfc/README.md` 一行索引 |
| 契约 | **C-05 `schema/rpc.json` 1.4.0 → 1.5.0**（`ui_to_core` 33 → 34 个方法，`session/*` 13 → 14）；`x-vade.appends` 追加 1 条（9 → 10，`adds=["session/search"]`、`rewrites:"none"`，既有 9 条逐字未动）；C-06 / C-04 未动（无新事件、无新错误码，仍 34） |
| 测试 | `pytest core/tests -q` → **1065 passed, 0 failed**（基线 1041 + 本批 24，`core/tests/session/test_rfc0014_search.py`） |
| 契约门禁 | `python tools/check_contracts.py` → `contracts: OK (… RFC-0014 session search green …)`，exit 0（新增 `check_session_search`：方法声明/handler 注册/常量/escape 探针/store 参数化标记/appends 块/版本 1.5.0） |
| 文档门禁 | `python docs/check_docs.py` → **合计 12 项，失败 0 项** |
| 变异自验 ① | 参数化 → 字符串拼接（`LIKE '%{query}%'` 直插 SQL、去掉 `escape_like` 与 `?` 绑定）→ `test_percent_is_literal_not_wildcard` / `test_underscore_is_literal` / `test_quote_and_sql_comment_are_literal` / `test_backslash_is_literal` 全红：**4 failed, 20 passed**；已还原，24 passed |
| 变异自验 ② | 删掉 handler 的 `1 <= limit <= LIMIT_MAX` 上限子句 → `test_limit_bounds_are_rejected` 红：**1 failed, 23 passed**；已还原（逆 patch），24 passed |
| push | 未 push、未合并（按任务要求） |

## 一、语义决定（详见 RFC §4）

1. **范围**：`sessions.title` + `messages.content` 活正文（`superseded_by IS NULL`，RFC-0006 编辑史不搜）；`parts` 附件、`turns.user_text`（user 消息的投影副本）不在范围；归档会话在范围内（行内 `archived: true` 标注），已删会话天然不出现。
2. **匹配方式 = 参数化 LIKE（非 FTS5）**：个人规模（≤5 000 会话 / ≤200 000 消息）亚秒级；FTS5 默认 unicode61 对中文整段切词、必须换 trigram（索引体积/写入放大 ×3）且要动 `append()` 写入路径——收益在这个规模买不回成本。逃逸通道写进 RFC §7 Q1（触发信号：搜索 P95 > 500ms）。
3. **大小写与中文**：照抄 SQLite LIKE 原生语义——ASCII 折叠、非 ASCII 逐码点精确；`find_ascii_ci` 逐字符镜像 LIKE 折叠（不用 `str.lower()`，否则 `snippet_offset` 会指向 LIKE 没命中的位置）。
4. **排序**：两档相关性——标题命中档 > 正文命中档，档内 `updated_at` DESC、`id` ASC 兜底（沿用 RFC-0010 §7.4）；不做打分引擎。
5. **去重**：一会话一行，标题命中压过正文命中（`matched_in: "title"`）；`total` = 去重后命中会话数。
6. **分页**：`limit` 1..100（默认 20，片段行比轻行重一个量级）、`offset` ≥0（越界 = 空页 + total 照实）、显式 offset 不做 cursor（搜索可重跑，防抖下 cursor 全废）。
7. **空查询**：缺参/非串/strip 后空（含 U+3000）→ `INVALID_ARGUMENT data.field=query`；>200 码点拒绝（对齐 `session/rename` 标题上限）。
8. **片段**：≤120 码点硬窗（省略号计入预算）+ `snippet_offset` = 命中在原文的码点下标；正文可达 32 000 码点（RFC-0006 §8），永不整段回传；每会话取首条命中（`MIN(rowid)`），页内 ≤limit 次回查。
9. **注入**：全程 `?` 绑定；`escape_like` 只转义 `%` `_` `\` 三个 LIKE 元字符；`'` `--` `;` 不转义（不是元字符，参数化天然字面化，转义反而毁掉诚实查询）。
10. **权限**：与 `session/list` 同一信任层（C-01/C-02），`read_only` 连接可搜（读动词）；可选 `session_id` 钉住单会话（FR-8.27「当前会话内全文查找」），未知 → `NOT_FOUND`。
11. **不加索引**：前导通配 LIKE 用不上 B-tree；`idx_messages_session` 已覆盖 scope 路径。
12. **不加错误码 / 不发事件**：拒绝只落 `INVALID_ARGUMENT` / `NOT_FOUND`；纯读无状态可广播。

## 二、交付内容（文件）

| 文件 | 变更 |
|---|---|
| `docs/rfc/RFC-0014-session-search.md` | 新建（本 RFC） |
| `docs/rfc/README.md` | +1 行索引 |
| `core/vade/session/search.py` | 新建：常量（`QUERY_MAX=200`/`LIMIT_MAX=100`/`LIMIT_DEFAULT=20`/`SNIPPET_MAX=120`/`SNIPPET_CONTEXT=40`）+ `escape_like` / `validate_query` / `find_ascii_ci` / `make_snippet` / `SearchHit`（frozen dataclass + `to_wire()`） |
| `core/vade/session/store.py` | Protocol 追加 `search_sessions(query, *, limit, offset, session_id) -> (hits, total)` |
| `core/vade/session/sqlite_store.py` | `search_sessions`：标题档 SQL（`ORDER BY updated_at DESC, id ASC`）+ 正文档 `GROUP BY session_id + MIN(rowid)` 一次取每会话首命中 + 页内片段回查；三次读共享一把 `_lock`；模式串与 scope 全部 `?` 绑定 |
| `core/vade/session/manager.py` | `SessionLog.search()`：`validate_query` + 可选 scope 的 `_require_session` + 组装 `{results, total}` |
| `core/vade/session/rpc.py` | `session_search` handler + 注册；limit/offset 类型与范围检查**只此一处**（变异目标 ② 的钉子）；read_only 不拒绝（读动词） |
| `schema/rpc.json` | 文本拼接式手术编辑（全文 CRLF 与既有字节保持）：version → 1.5.0、`ui_to_core` 追加 `session/search`、appends 追加 RFC-0014 块；`git diff --stat` = 43 insertions / 1 deletion |
| `tools/check_contracts.py` | 新增 `check_session_search` + 常量 `SESSION_SEARCH_METHODS` / `SESSION_SEARCH_HIT_KEYS`；版本钉同步（`check_session_meta` 三元组、`check_subagent_events` 二元组、本检查 == 1.5.0）；docstring 第 13 项；OK 行追加 |
| `core/tests/session/test_rfc0014_search.py` | 新建 24 条 |
| `core/tests/session/test_meta.py` | 仅版本断言 1.4.0 → 1.5.0（注释：RFC-0014 拥有 1.5.0） |
| `core/tests/session/test_rfc0013_partial_persistence.py` | 仅版本断言随头 1.4.0 → 1.5.0（同性质同步，注释已更新；不属「不许改」清单） |
| `core/vade/session/RESULT-rfc0014.md` | 本文件 |

## 三、测试（24 条，≥15 达标）

- 校验 3：空/空白/非串查询、201 码点（含 `data.limit/actual`）、无结果空页。
- 行形状 5：标题命中（无片段）、正文命中（片段 ≤120 + offset 回指原文 + 双省略号）、ASCII 折叠、中文精确（含同会话双命中去重）、契约回显（schema 字段与常量逐项对齐）。
- 分页 4：limit 0/负/101/bool/str/float 全拒 + 默认值、offset 负拒、limit=2 跨 3 页无重无漏 + total 恒定 + 短页即末页、offset=999 越界空页。
- 注入 4（变异目标 ①）：`100%` 字面、`case_name` 不匹配 `caseXname`、`it's`/`--bear--`/`'; DROP TABLE sessions; --` 无错无中且表完好、`C:\Users` 与 `it\'s` 字面。
- 排序 2：标题档在正文档前、正文档内 `updated_at` DESC + 同刻 `id` ASC（测试先插消息再钉 updated_at——append 会推进活动时刻，RFC-0010 §4.2 语义）。
- 范围/权限 2：`session_id` 钉单会话 + 未知 NOT_FOUND、read_only 连接可搜。
- 边界排除 3：superseded 行不搜（`message_updates` 打标）、删会话即消失、无 FTS 基建（`sqlite_master` 无 fts 表 + 重开可搜 = 降级路径「无处可坏」）。

## 四、变异记录

| # | 变异 | 观察 | 还原证据 |
|---|---|---|---|
| ① | `sqlite_store.search_sessions`：`like = f"LIKE '%{query}%'"`（直插 SQL，去 `escape_like`/`?` 绑定），三处查询参数元组同步去 pattern | `pytest core/tests/session/test_rfc0014_search.py -q` → **4 failed, 20 passed**（红 = percent / underscore / quote+comment / backslash-quote；percent→`%100%%` 全库误中，quote→`SQL` 语法错 INTERNAL，`it\'s`→backslash 吞引号破 SQL） | 逆 patch 还原后 **24 passed**；`git diff` 对工作区（还原前后唯一差异即变异四行） |
| ② | `rpc.session_search`：`not 1 <= limit <= LIMIT_MAX` 子句删除（保留类型检查） | 同命令 → **1 failed, 23 passed**（红 = `test_limit_bounds_are_rejected`：limit=0/101 不再拒绝） | 逆 patch 还原（diff 可见单行复原）后 **24 passed** |

过程事故与修复：变异 ① 的还原最初用 `git checkout -- sqlite_store.py`，把本批对该文件的**全部**改动一并还原到了分支基线（21 failed）——随即按原 patch 重放 import + `search_sessions`，24 passed 恢复；最终文件 md5 `bb8b985d…` 已计入门禁重跑（契约门禁 exit 0 + 全量 1065 passed 均在重放之后执行）。

## 五、门禁原始输出尾部

```
$ python -m pytest core/tests -q
1065 passed, 6 warnings in 59.60s

$ python tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete,
RFC-0005 append green, RFC-0010 session metadata green, RFC-0012 subagent family green,
RFC-0014 session search green, RFC-0003 layers green, event gate green)     [exit 0]

$ python docs/check_docs.py
合计 12 项，失败 0 项。
```

## 六、未做项（明确不在本批）

- 客户端三端搜索覆盖层（FR-8.27 的 3 字符触发 / 200ms 防抖 / `snippet_offset` 高亮 / `-32601` 回退本地缓存）——不在 core 侧范围。
- FTS5-trigram 逃逸通道（RFC §7 Q1，触发信号：搜索 P95 > 500ms）；`parts` 附件文本与工具结构化字段检索（Q2）；搜索审计（Q3，倾向不做）。
- Postgres store 的 `search_sessions`（端口已定义：LIKE → `ILIKE`，ASCII 折叠语义偏差需在实现时文档化）。
- 未 push、未合并；`docs/requirements.md`、`PROGRESS.md`、`docs/status.md` 未动（父代统一改）。
