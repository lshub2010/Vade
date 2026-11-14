# RFC-0005 模型能力与用量（C-03/C-04/C-05/C-06/C-18 追加）

状态：**已实现并通过门禁，未提交合并**。分支 `tmp/rfc0005`（worktree `D:/HermesWorkspace/Vade-wt/rfc0005`）。
范围：RFC-0005 §4（`model/list` 目录）、§5（选择序 + 会话级覆盖 + 下一 turn 生效）、§6（用量账本与预算门禁）、§7（五份契约追加）。
**不含**：客户端/UI 改动（`model/list` 的模型切换面板、横幅、徽章渲染）、G-1 真机验收。core 侧提供的是数据面。

## 1. 实施面

| 文件 | 内容 |
|---|---|
| `core/vade/llm/capabilities.py`（新） | 能力块解析：`null` = UNKNOWN，永不与 `false`/`[]` 互换；单字段坏值只降为 `null` 并告警，不整条拒；`MAX_MODELS=16` |
| `core/vade/llm/catalog.py`（新） | `ModelCatalog`（`<provider>/<model>` 目录、`revision`、`<module_id>/*` synth）、`ModelResolver`（session > global > priority，同优先级取最新心跳再按 id） |
| `core/vade/llm/usage.py`（新） | `session/usage.get` 账本（`chat` 来自 `turns.usage`，`aux` 来自审计 `llm.call`，`day` 为 UTC 聚合）+ `ui/submit` 预算门禁 |
| `core/vade/llm/base.py` | `flatten_usage()`：`*_details` 提升为 `cached_tokens`/`reasoning_tokens`，只让整数进入总量；`chat(..., model=, attribution=)` 协议说明 |
| `core/vade/llm/module.py` | `select(model=)` 按 id 精确路由（`/<module>/*` = 模块自己的默认），`attribution` 回填 `module_id`/`instance_id` |
| `core/vade/llm/openai_compat.py` | `builtin/<name>` 前缀剥离后才上 wire |
| `core/vade/loop/engine.py` | 每次模型调用写一条 `llm.call`（无 prompt/completion/base_url，仅计数与归属）；`turn.completed.usage` 九键（三键不动 + 六键追加），`source="none"` 时计数全 0 且不估算 |
| `core/vade/session/{manager,queue,models,store,sqlite_store,rpc}.py` | 会话级 `model_id`（含旧库 `ALTER TABLE` 幂等迁移）、dequeue 时解析并钉在 turn 上、`turn.started` 带 `model_id/model_source`、失败/中断 turn 也记 `usage`、四个新 RPC |
| `core/vade/registry/{manifest,table}.py` | `Manifest.models`（`capabilities.models[]`）+ `revision()` 单调计数（注册/能力更新/断线/心跳恢复/启停/吊销都 bump） |
| `core/vade/admin/app.py` | `config/usage` 的 `tokens_used` 只统计非 `purpose="chat"` 行，并翻页取完（见 §4.3） |
| `tools/check_contracts.py` | 33→34；新增 `[usage]` 六键默认值、`[llm]` 四键、`capabilities.models` 形状 + `Manifest.models` 接线、`usage.budget` 六 required、四方法落 `ui_to_core` |

## 2. 契约变更（全部为追加）

| 契约 | 变更 | 位置 |
|---|---|---|
| C-04 | 新增错误码 `BUDGET_EXCEEDED`（http 402，`retriable=false`），33→**34** | `errors.json` + `01-contracts.md` §二（新增规则行） |
| C-05 | 新方法 `model/list`、`session/model.get/set`、`session/usage.get`（均在 `ui_to_core`）；`events_core_to_ui` 加 `usage.budget`；`ui/attach` 只加 `notes` 描述 `snapshot.session.model` | `rpc.json` |
| C-06 | 新事件 `usage.budget`（六 required）；`turn.started`/`turn.failed`/`turn.interrupted` 只加**可选** payload 键；`turn.completed.usage` 加六键 | `events.json` |
| C-18 | 新段 `[usage]`（六键，全 `hot`）；`[llm]` 加 `model_module`/`fallback_to_builtin`/`default_model`/`capabilities`；`x-vade.version` 1.0.0→**1.1.0** | `config.schema.json` |
| C-03 | `capabilities.models[]`（`maxItems:16`，仅 `name` 必填，`$defs.model`） | `manifest.schema.json` |

四份追加都写在自己的兄弟键（`x-vade.rfc0005_append`），不共用 `x-vade.append`/`appends` 数组，两个 RFC 可以任意顺序落地。

## 3. 偏差（有意为之，不是遗漏）

**D-1｜C-06 与 C-05 的顶层 `version` 仍是 `1.0.0`。**
RFC §7.1 第 1/7 行与 §7.4 都写「v1.0.0 → 1.1.0」。实际未改顶层 `version`：

- `core/tests/events/test_schema_append.py::test_c06_is_untouched` 显式断言 `events.json["version"] == "1.0.0"`；改它等于为了标版本去动冻结头，且会让第二个追加方（RFC-0006/0007）无从判断自己该写哪个版本。
- 顶层 `version` 是被追加文件**自己的**字段，不是追加方的。追加方无权替它升版。
- 真实记录方式是 `events.json` / `rpc.json` 的 `x-vade.rfc0005_append.version_note`：RFC 提议 1.1.0、未采用、原因如上，并指向本文件。
- `config.schema.json`（C-18）**升了** 1.1.0：那一份是本批直接修改的契约本体，`check_contracts` 也不钉它的 version 字符串（只钉 `x-vade.contract`）。

**D-2｜新增 `[usage]` 不进 `check_contracts` 的 `required_sections`。**
`required_sections` 是“C-18 必须有的段”清单（core/tls/policy/…），`[attach]`/`[usage]` 走各自的专用断言（键集 + 默认值 + apply）。把 `usage` 塞进 `required_sections` 只是重复，且会让“段存在但键错”仍绿。

**D-3｜用例文件路径与 RFC 验收表的写法不同。**
RFC A10/A10b/A11 写的是 `core/tests/session/test_budget.py`、`core/tests/model/test_dangling.py`；本批集中在 `core/tests/model/`（目录名就是“模型面”，六个块按职责分文件），A12 按 RFC 原路径 `core/tests/admin/test_config_usage.py` 落地。内容与验收句一一对应，见 §5 的映射表。

## 4. 本期修掉的三处（都由测试逼出来，不是顺手重构）

| # | 位置 | 症状 | 修法 |
|---|---|---|---|
| 1 | `llm/usage.py` `_budget_block` | `budget.enforced` 只看 `state=="exceeded" and enforce=="block"`，不看 `enabled`。`usage.enabled=false` 时 `gate()` 不拒、`session/usage.get` 却报 `enforced:true` → UI 会显示“你被拦住了” | 加上 `and self.budget.enabled`，让报告与门禁同源 |
| 2 | `llm/usage.py` `_trusted` | 只把 `source=="none"` 判为不可信；`usage={}`（旧库行、没填 source 的 provider）被当成可信 → 用编出来的数去拒用户的 turn | 改为“有 source 且不是 none”。缺 source 与 `none` 同为“没测到” |
| 3 | `admin/app.py` `config_usage` | 本批开始每个 chat turn 都写 `llm.call` 行（`purpose="chat"`），而 `tokens_used` 汇总**所有**`llm.call` 行且只读一页 → 日常聊天会吃掉 `[evolve].daily_token_budget`，把自进化悄悄关掉；且 500 行上限会截断 | 跳过 `purpose=="chat"`（未标 purpose 的老行照旧计入，不改变既有行为）+ 用 `next_cursor` 翻完 |

第 3 条超出“只做 rfc0005 新文件”的字面范围（`admin/` 属 C5a），但它是本批**新引入的 chat 审计行**造成的回归，且正是 RFC §6.5/A12 的验收句（“`tokens_used` 只等于 evolve 部分；当天 chat 行超过 500 条也不截断”），故一并修掉并补用例。若审查认为归属应留给 C5a，可整体回退这三行 + 删 `core/tests/admin/test_config_usage.py`，其余不受影响。

## 5. 测试

`core/tests/model/`（新目录，`helpers.py` 只用真配置/真 manifest/真 Registry，假的是 LLM 与审计 reader）：
**4 个文件 29 个用例**；另有 `core/tests/admin/test_config_usage.py` 2 个用例。

| 块 | 文件 | 用例 | 钉住的东西 |
|---|---|---|---|
| ① `model/list` 形状 + 模块自报 + revision | `model/test_catalog.py` | 8 | 11 个 wire 键（`model` 不上线）、`<module_id>/<name>`、`<module_id>/*`（声明为空的 chat 模块不被隐藏）、无 chat 工具不算模型、离线仍列出 `available:false`、未声明能力一律 `null`（`false` 保留为 `false`）、schema 拒掉的坏值由 `parse_models` 兜底、`revision` 随注册/断线递增、`if_revision` 命中返回三键且 `models:null`、非整数 revision → `INVALID_ARGUMENT` |
| ② 选择序 + 悬空 + 无选择不阻塞 | `model/test_selection.py` | 8 | 会话 > `default_model` > priority；悬空显式选择 `resolved=false`/`reason=model_unavailable`/原样回显请求 id/给出 `global_default`；离线同样悬空；无显式选择且目录为空时 turn 照常完成（`model_id` 不写入、`turn.started` 无该键）；悬空选择 → turn `failed` + `UNAVAILABLE` + `data.reason`，且**不调用** provider；`session/model.set` 置空/非法/未知/只读会话四态；`ui/attach.snapshot.session.model` 直接点亮徽章 |
| ③ `BUDGET_EXCEEDED` 真不入队（+ 幂等先于门禁） | `model/test_budget.py` | 2 | 拒绝发生在入队前：无 turn 行、无 `turn.queued`、无用户消息、队列空、provider 没跑；`retriable=false`、`data={state,scope,used,limit}`；同一 `client_msg_id` 的重放返回原 turn 且不抛（预算正是那条已被接受的 turn 花掉的） |
| ④ `source="none"` 降级 | `model/test_budget.py` | 2 | 最后一轮 `source="none"` → 不拒，`usage.budget(degraded=true)`，`usage.get.budget.state` 仍 `exceeded`、`enforced:true`（额度仍然生效，只是这一轮豁免），`partial_turns` 计入；同一 `(scope,state,period)` 只通知一次 |
| ⑤ `session/usage.get` 与审计逐 turn 对账 | `model/test_usage.py` | 6 | 真 `LoopEngine` + 真 `Registry`：turn.usage.total == 该 turn 全部 `llm.call` 行之和（引擎两处写点不一致就红）；`chat`/`aux`/`total`/`by_model`/`day` 的分工；`aux` 只统计非 chat 且**翻页**读满（reader 的 500 上限不截断）；failed/interrupted 计入、cancelled 不计；UTC 日界 + 跨会话只给聚合；RPC 结果 == 报告原文，未知会话 `NOT_FOUND`、未接线 `UNAVAILABLE`；`flatten_usage` 只提升 details 不放大总量 |
| ⑥ `enforce="warn"` 只提示 | `model/test_budget.py` | 1 | warn→exceeded 两态各通知一次、三 turn 全完成、`degraded` 恒 false、`enforced:false`。（同文件另 2 条：`enabled=false` 只记账不拒也不通知；`session_tokens=0` 即 `off`） |
| 附（A12） | `admin/test_config_usage.py` | 2 | chat 行不进 evolve 预算；600 条 chat 行之后的 evolve 行仍被读到 |

### 变异验证（9 + 2 条，全部变红后还原）

| 变异 | 结果 |
|---|---|
| M1 选择序忽略会话覆盖 | RED（4 失败） |
| M2 幂等检查挪到门禁之后 | RED |
| M3 门禁永不拒绝 | RED（2） |
| M4 悬空选择静默忽略 | RED |
| M5 账本永远可信 | RED（2） |
| M6 `degraded` 恒 false | RED |
| M7 `llm.call` 行 token 记 0 | RED |
| M8 审计只读一页 | RED |
| M9 `revision` 不递增 | RED |
| M10 admin 不过滤 chat 行 | RED（2） |
| M11 admin 不翻页 | RED |

契约门禁同类变异（7 条，均变红）：`usage.session_tokens` 默认值、`warn_ratio` 默认值、`default_model` 的 apply、`models.maxItems`、`$defs.model.required`、`model/list` 改名、`usage.budget.required` 去掉 `degraded`。

## 6. 门禁原始输出（本机 2026-11-14，Windows）

```
$ D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe -m pytest core/tests -q
530 passed, 5 warnings in 149.37s (0:02:29)
# 基线 499 + 本批 31（model 29 + admin 2），0 failed / 0 error

$ ... -m pytest core/tests/model -q
29 passed in 0.42s

$ ... -m pytest core/tests/admin/test_config_usage.py -q
2 passed in 2.41s

$ ... tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete, RFC-0005 append green, event gate green)

$ ... docs/check_docs.py
合计 12 项，失败 0 项。
```

（本批未改 `docs/` 正文，`check_docs` 12 项仍全绿；`01-contracts.md` 只加了一行错误码规则，不在 C1–C10 的常量里。）

## 7. 已知限制 / 后续

1. **只有一个模块能当模型模块**：`Registry._build_index` 把同名工具跨模块视为 `TOOL_CONFLICT`，而模型模块的判定工具固定是 `chat`。所以今天不可能同时在线两个模型模块，“manifest priority 决定默认”只能在同一模块的多实例间生效。这是 C-03 既有规则，不在本 RFC 范围内，但 `model/list` 的多 provider 场景要真跑起来必须先解决它（建议另开 RFC：工具索引按 `module_id` 命名空间化，或给模型模块专用通道）。
2. **`revision` 是进程内计数**：重启归 0，客户端只能把“不相等”当“全量刷新”，不能假设跨进程单调。已写进 `catalog.py` 与 `rpc.json` notes。
3. **客户端未做**：模型切换面板、`resolved=false` 横幅、能力徽章、`usage.budget` 去重展示、`session/usage.get` 面板。core 侧的 `UNAVAILABLE`（老 core 无此方法）降级路径已在 `rpc.json` 写明，需客户端按 -32601 处理。
4. **`[llm.capabilities]` 只覆盖内置模型**：模块模型的能力来自 manifest，两者不合并（设计如此：`<provider>/<model>` 决定来源）。
5. **`turn.failed.usage` 恒为 `source="none"`**：core 不估算，provider 在失败路径上也通常没报 usage。若后续要让模块在错误响应里带 usage，需要 C-01 的 `tool_result.error` 扩字段（另开 RFC）。
