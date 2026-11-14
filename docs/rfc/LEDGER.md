# RFC ↔ 契约 ↔ 实现 ↔ 验收 总账（LEDGER）

> **真相口径**：契约列以 `schema/*.json` 的 `x-vade.appends` 为准，**不以 RFC 正文自述为准**；status 以各 RFC frontmatter 为准（无 frontmatter 的以正文「状态」表为准）；实现落点必须在本仓真实存在（由 `tools/check_ledger.py` 校验）；测试数只引用有出处的数字，没有出处写「未记录/未核实」。
> 重跑自检：`D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe tools/check_ledger.py`（基于本文件末尾 JSON 事实块 + 三份契约文件 + RFC frontmatter 重算比对）。
> 快照基线：分支 `tmp/w-ledger` @ `320384a`（2026-11-14）。发现的口径冲突见本文 §4 与 `docs/rfc/LEDGER-RESULT.md`。

## 1. 契约真实状态（脚本重算，非引用）

| 项 | 值 | 出处 |
|---|---|---|
| C-05 rpc.json 顶部 version | **1.5.0** | `schema/rpc.json` |
| C-05 方法数 | ui_to_core **34** / core_to_module **7** / module_to_core **6**，三方向合计 **47** | 同上 |
| C-06 events.json 顶部 version | **2.0.0** | `schema/events.json` |
| C-06 事件数 | **34**（含 `turn.state` 枚举条目；不含则 33） | 同上 |
| C-04 错误码 | **34** | `schema/errors.json` |
| append 登记块 | rpc.json **10** 块 / events.json **8** 块 / manifest.schema.json **3** 块，合计 **21** | 三文件 `x-vade.appends` |
| 服务端测试 | **1089 passed**（`pytest core/tests -q`，2026-11-14 在 `dev_gingshan` 合入 RFC-0003 Q5 后复跑：`1078 passed, 6 warnings in 86.21s`；RFC-0002 落地后在 worktree s-rfc0002 复跑：`1089 passed, 6 warnings in 65.79s`，+11 = RFC-0002 新增用例）。本总账初版记录的 **1065** 是 RFC-0014 落地时、RFC-0003 Q5 未合时的数（出处 `core/vade/session/RESULT-rfc0014.md`；+13 = `core/tests/loop/test_compaction_catalog.py`） | 本人复跑 |
| 契约门禁 | `tools/check_contracts.py` exit 0（`contracts: OK …RFC-0014 session search green…`） | 本人复跑 |
| docs 门禁 | `docs/check_docs.py` exit 0（12 项，失败 0 项） | 本人复跑 |

## 2. 主表（每条 RFC 一行）

| RFC | 标题 | status（frontmatter） | 实现状态（核实） | 契约方法（C-05 adds） | 契约事件（C-06 adds） | 其他契约登记 | append 版本 | 实现落点（本仓已验证存在） | 测试数（来源） | 验收（行/勾） |
|---|---|---|---|---|---|---|---|---|---|---|
| RFC-0001 | 在 C-03 `manifest` 中追加 `subscriptions` | 已实施（C3d，待 C2 追认）〔无 frontmatter，正文表〕 | 已实现 | 无 | 无 | manifest.appends：C-14 1.0.0 `subscriptions` | manifest 1.0.0 | `core/vade/registry/manifest.py` | 5 passed（RFC-0001 §6 自述：`test_c3d_registration_chain.py` 5 例） | 无 A 表（命令式验收）· 0/0 |
| RFC-0002 | 在 C-06 `turn.completed` 中追加 `skills` | 草案（实施已落，待批准追认）〔无 frontmatter，正文表〕 | 已实现 | 无 | 无（adds=[]；`turn.completed` payload 追加可选字段 `skills`，required 未动） | 无 | C-06 1.3.0（append 级；顶部 version 保持 2.0.0） | `core/vade/loop/engine.py`、`core/vade/session/queue.py` | 新增 11 passed（worktree s-rfc0002：`test_engine.py` 7 例 + `test_queue.py` 4 例）；全量复跑 1089 passed（见 §1） | 无 A 表 · 0/0 |
| RFC-0003 | 提示词注入分层：技能目录消息化、工具渐进披露与 stable/context 分段 | approved | 已实现（frontmatter stage「草稿，未实现」已过时） | 无 | `turn.skill_catalog`、`turn.tool_discovered` | C-11 `skill.schema.json` x-vade.injection；C-18 `prompt.injection`/`tools.hot`/`tools.hot_max` | C-06 1.1.0 | `core/vade/loop/prompt.py`、`core/vade/skill/loader.py`、`core/vade/loop/engine.py` | 967 passed（`core/vade/prompt/RESULT-rfc0003.md`：基线 934+33） | 13/0 |
| RFC-0004 | 附件与多模态：`POST /ui/blob` 上传协议、压缩图单 `file_id` 与 content parts 消息结构 | approved | 已实现（frontmatter stage 已过时） | 无方法名——adds 原文：`ui/submit.content typed part union`、`http_endpoints` | 无 | C-18 `[attach]` 17 键 | C-05 1.1.0 | `core/vade/blob/http.py`、`core/vade/session/parts.py`、`core/vade/session/models.py`、`core/vade/session/sqlite_store.py` | 539 passed（`core/vade/session/RESULT-parts.md`）；0004a 记 489（`docs/status.md` rfc0004a 行） | 13/0（验收号 A1–A14，A2/A3 合并一行） |
| RFC-0005 | 模型能力查询与用量：`model/list`、会话级选择优先级与两级用量口径 | approved | 已实现（frontmatter stage 已过时） | `model/list`、`session/model.get`、`session/model.set`、`session/usage.get` | `usage.budget`（另 `turn.started`/`turn.failed`/`turn.interrupted` 三事件追加字段） | manifest.appends：`capabilities.models`（块 contract 字段写的是 "RFC-0005"，非契约号）；C-18 `[llm]` 4 键 + `[usage]` 6 键 | C-05 1.2.0；C-06 2.0.0；manifest 1.0.0 | `core/vade/llm/catalog.py`、`core/vade/llm/usage.py`、`core/vade/llm/capabilities.py` | 530 passed（`core/vade/llm/RESULT-rfc0005.md`）；status.md 记 860（合并后全量口径） | 13/0（验收号 A1–A12 + A10b） |
| RFC-0006 | turn 语义补充：中断留痕、工具卡字段、重生成/编辑重发、流式多段与单条长度上限 | approved | 已实现（frontmatter stage 已过时） | `turn/regenerate`、`message/edit_resubmit` | `turn.superseded`、`message.edited`（另 5 个既有事件追加字段） | — | C-05 1.1.0；C-06 1.1.0 | `core/vade/session/queue.py`、`core/vade/session/models.py`、`core/vade/loop/engine.py`、`core/vade/loop/context.py`、`core/vade/session/limits.py` | 552 passed（`core/vade/loop/RESULT-rfc0006.md` 返工后）；status.md 记 670 | 15/0 |
| RFC-0007 | 子代理与后台任务：统一 `task.*` 事件族、查询/取消/日志尾部与审批归属 | approved | 已实现（frontmatter stage 已过时） | `task/list`、`task/log_tail`、`task/cancel`、`subagent/message` | **无登记块**（但 `task.started/status/progress/finished` 四事件与顶层 `task_states` 实际存在于 events.json——见 §4-5） | C-18 `[tasks]` 13 键 | C-05 1.1.0 | `core/vade/tasks/models.py`、`core/vade/tasks/rpc.py`、`core/vade/session/replay.py` | 601 passed（`core/vade/tasks/RESULT-rfc0007.md`）；status.md 记 607（两处不一致→§4-6） | 16/0 |
| RFC-0008 | agent 要求输入：`waiting_input` 状态、`turn.input_*` 事件对与 `turn/input` 取值通道 | approved | 已实现（frontmatter stage 已过时） | `turn/input`、`turn/input.cancel` | `turn.input_required`、`turn.input_resolved`（`turn_states` 拓宽 `waiting_input`，C-06 升 2.0.0） | C-18 `[ask_user]` 6 键 | C-05 1.1.0；C-06 2.0.0 | `core/vade/input/manager.py`、`core/vade/loop/builtins.py`、`core/vade/session/models.py` | 611 passed（`core/vade/input/RESULT-rfc0008.md`）；`ask_user` 内置工具批次 944（`core/vade/loop/RESULT-askuser-tool.md`）；status.md 记 829 | 14/0 |
| RFC-0009 | 模块 UI 页面：原生声明式 schema（新契约 C-19）、只读数据绑定与三类动作 | approved | 已实现（frontmatter stage 已过时） | `ui/page.get`、`ui/state.get`、`ui/tool.call`、`state/get`、`page/get` | 无 | manifest.appends：C-19 1.0.0 `ui`（`ui.pages[]`）；C-18 `[ui]` 4 键（append 块缺 `contract` 字段→§4-7） | C-05 1.2.0；manifest 1.0.0 | `core/vade/registry/uipage.py`、`core/vade/modulepage/service.py`、`core/vade/modulepage/rpc.py`、`core/vade/modulepage/auth.py`、`core/vade/registry/manifest.py` | 710 passed（`core/vade/registry/RESULT-rfc0009.md`）；安全返工后 934（`core/vade/modulepage/RESULT-c19-fix.md`）；status.md 记 773/934 | 13/0 |
| RFC-0010 | 会话元数据：分组（文件夹）/ 图标 / 颜色 / 置顶，与 `session/*` 十方法 | proposed | 已实现（core 侧；stage 自述一致） | `session/list`、`session/rename`、`session/pin`、`session/archive`、`session/delete`、`session/group.list`、`session/group.create`、`session/group.update`、`session/group.delete`、`session/group.assign` | `session.updated`、`session.list_changed` | — | C-05 1.3.0；C-06 2.0.0 | `core/vade/session/manager.py`、`core/vade/session/rpc.py`、`core/vade/session/sqlite_store.py`、`core/vade/session/store.py` | 966 passed（`core/vade/session/RESULT-rfc0010.md`：934+32） | 10/0 |
| RFC-0011 | InputBar 拆分为「输入区」与 SendOrb（C-09 组件 11 → 12） | proposed | 客户端仓已实现（`docs/rfc/README.md` 记 vade-client `d667442`）——**本仓未核实** | 无 | 无 | 无登记（C-09 组件清单非 JSON 契约文件） | — | 本仓无落点（vade-client 仓） | 未核实 | 8/0 |
| RFC-0012 | 子代理事件族 R3：`subagent.spawned` / `subagent.status` / `subagent.result` 三事件与 `subagent/list` / `subagent/get` 两方法 | proposed | 已实现（core 侧；stage 自述一致） | `subagent/list`、`subagent/get` | `subagent.spawned`、`subagent.status`、`subagent.result`（顶层 `subagent_states` 新键） | — | C-05 1.4.0；C-06 2.0.0 | `core/vade/subagents/manager.py`、`core/vade/subagents/rpc.py` | 985 passed（`core/vade/subagents/RESULT-rfc0012.md`：966+19） | 10/0 |
| RFC-0013 | 中断留痕落库（R9）：partial 消息进 snapshot，删掉「只能从 turn.delta 重建」的旧注记 | proposed | 已实现（core 侧；stage 自述一致） | 无（adds=[]，仅注记） | 无（adds=[]；`turn.interrupted.notes` 就地改写——显式偏离「不改 notes 字节」惯例） | — | C-05 1.2.0（append 级）；C-06 1.2.0（append 级） | `core/vade/session/queue.py`、`core/vade/session/sqlite_store.py`、`core/vade/session/models.py` | 979 passed（`core/vade/session/RESULT-rfc0013.md`：966+13） | 8/0 |
| RFC-0014 | 会话搜索：`session/search`（标题 + 正文 LIKE 扫描、两档排序、片段硬截断） | proposed | 已实现（core 侧；stage 自述一致） | `session/search` | 无 | — | C-05 1.5.0 | `core/vade/session/search.py`、`core/vade/session/sqlite_store.py`、`core/vade/session/rpc.py`、`core/vade/session/manager.py` | 1065 passed（`core/vade/session/RESULT-rfc0014.md`：1041+24；本 worktree 复跑同数） | 14/0 |

> 「验收」列 = 验收表条目行数 / 已勾选数。**全部 14 份 RFC 的验收表均无勾选列，机械计数 `[x]` 均为 0**——「已勾选」这回事在本仓从不存在过，勿再说「验收已勾完」。
> append 版本是**块级**登记版本（append-per-minor 纪律），与契约**顶部**版本（C-05 现 1.5.0 / C-06 现 2.0.0）是两个东西；RFC-0013 两个块都是 1.2.0 但顶部版本不动，这是设计而非错误，但口头汇报极易混——见 §4-4。

## 3. 机器事实块（`check_ledger.py` 以此为准重算比对；手改本块必须与契约同步）

```json
{
  "aggregate": {
    "rpc_version": "1.5.0",
    "rpc_methods_by_direction": {"module_to_core": 6, "core_to_module": 7, "ui_to_core": 34},
    "rpc_methods_total": 47,
    "events_version": "2.0.0",
    "events_total_incl_turn_state": 34,
    "events_total_excl_turn_state": 33,
    "error_codes": 34,
    "appends_rpc": 10,
    "appends_events": 8,
    "appends_manifest": 3
  },
  "rfcs": {
    "RFC-0001": {"status": "已实施（C3d，待 C2 追认）", "impl_state": "已实现", "methods": [], "events": [], "manifest_fields": ["subscriptions"], "append_versions": {"manifest.schema.json": "1.0.0"}, "impl": ["core/vade/registry/manifest.py"], "acceptance_rows": 0, "acceptance_checked": 0},
    "RFC-0002": {"status": "草案（实施已落，待批准追认）", "impl_state": "已实现", "methods": [], "events": [], "manifest_fields": [], "append_versions": {"events.json": "1.3.0"}, "impl": ["core/vade/loop/engine.py", "core/vade/session/queue.py"], "acceptance_rows": 0, "acceptance_checked": 0},
    "RFC-0003": {"status": "approved", "impl_state": "已实现", "methods": [], "events": ["turn.skill_catalog", "turn.tool_discovered"], "manifest_fields": [], "append_versions": {"events.json": "1.1.0"}, "impl": ["core/vade/loop/prompt.py", "core/vade/skill/loader.py", "core/vade/loop/engine.py"], "acceptance_rows": 13, "acceptance_checked": 0},
    "RFC-0004": {"status": "approved", "impl_state": "已实现", "methods": ["ui/submit.content typed part union", "http_endpoints"], "events": [], "manifest_fields": [], "append_versions": {"rpc.json": "1.1.0"}, "impl": ["core/vade/blob/http.py", "core/vade/session/parts.py", "core/vade/session/models.py", "core/vade/session/sqlite_store.py"], "acceptance_rows": 13, "acceptance_checked": 0},
    "RFC-0005": {"status": "approved", "impl_state": "已实现", "methods": ["model/list", "session/model.get", "session/model.set", "session/usage.get"], "events": ["usage.budget"], "manifest_fields": ["capabilities.models"], "append_versions": {"rpc.json": "1.2.0", "events.json": "2.0.0", "manifest.schema.json": "1.0.0"}, "impl": ["core/vade/llm/catalog.py", "core/vade/llm/usage.py", "core/vade/llm/capabilities.py"], "acceptance_rows": 13, "acceptance_checked": 0},
    "RFC-0006": {"status": "approved", "impl_state": "已实现", "methods": ["turn/regenerate", "message/edit_resubmit"], "events": ["turn.superseded", "message.edited"], "manifest_fields": [], "append_versions": {"rpc.json": "1.1.0", "events.json": "1.1.0"}, "impl": ["core/vade/session/queue.py", "core/vade/session/models.py", "core/vade/loop/engine.py", "core/vade/loop/context.py", "core/vade/session/limits.py"], "acceptance_rows": 15, "acceptance_checked": 0},
    "RFC-0007": {"status": "approved", "impl_state": "已实现", "methods": ["task/list", "task/log_tail", "task/cancel", "subagent/message"], "events": [], "manifest_fields": [], "append_versions": {"rpc.json": "1.1.0"}, "impl": ["core/vade/tasks/models.py", "core/vade/tasks/rpc.py", "core/vade/session/replay.py"], "acceptance_rows": 16, "acceptance_checked": 0},
    "RFC-0008": {"status": "approved", "impl_state": "已实现", "methods": ["turn/input", "turn/input.cancel"], "events": ["turn.input_required", "turn.input_resolved"], "manifest_fields": [], "append_versions": {"rpc.json": "1.1.0", "events.json": "2.0.0"}, "impl": ["core/vade/input/manager.py", "core/vade/loop/builtins.py", "core/vade/session/models.py"], "acceptance_rows": 14, "acceptance_checked": 0},
    "RFC-0009": {"status": "approved", "impl_state": "已实现", "methods": ["ui/page.get", "ui/state.get", "ui/tool.call", "state/get", "page/get"], "events": [], "manifest_fields": ["ui"], "append_versions": {"rpc.json": "1.2.0", "manifest.schema.json": "1.0.0"}, "impl": ["core/vade/registry/uipage.py", "core/vade/modulepage/service.py", "core/vade/modulepage/rpc.py", "core/vade/modulepage/auth.py", "core/vade/registry/manifest.py"], "acceptance_rows": 13, "acceptance_checked": 0},
    "RFC-0010": {"status": "proposed", "impl_state": "已实现", "methods": ["session/list", "session/rename", "session/pin", "session/archive", "session/delete", "session/group.list", "session/group.create", "session/group.update", "session/group.delete", "session/group.assign"], "events": ["session.updated", "session.list_changed"], "manifest_fields": [], "append_versions": {"rpc.json": "1.3.0", "events.json": "2.0.0"}, "impl": ["core/vade/session/manager.py", "core/vade/session/rpc.py", "core/vade/session/sqlite_store.py", "core/vade/session/store.py"], "acceptance_rows": 10, "acceptance_checked": 0},
    "RFC-0011": {"status": "proposed", "impl_state": "客户端仓（未核实）", "methods": [], "events": [], "manifest_fields": [], "append_versions": {}, "impl": [], "acceptance_rows": 8, "acceptance_checked": 0},
    "RFC-0012": {"status": "proposed", "impl_state": "已实现", "methods": ["subagent/list", "subagent/get"], "events": ["subagent.spawned", "subagent.status", "subagent.result"], "manifest_fields": [], "append_versions": {"rpc.json": "1.4.0", "events.json": "2.0.0"}, "impl": ["core/vade/subagents/manager.py", "core/vade/subagents/rpc.py"], "acceptance_rows": 10, "acceptance_checked": 0},
    "RFC-0013": {"status": "proposed", "impl_state": "已实现", "methods": [], "events": [], "manifest_fields": [], "append_versions": {"rpc.json": "1.2.0", "events.json": "1.2.0"}, "impl": ["core/vade/session/queue.py", "core/vade/session/sqlite_store.py", "core/vade/session/models.py"], "acceptance_rows": 8, "acceptance_checked": 0},
    "RFC-0014": {"status": "proposed", "impl_state": "已实现", "methods": ["session/search"], "events": [], "manifest_fields": [], "append_versions": {"rpc.json": "1.5.0"}, "impl": ["core/vade/session/search.py", "core/vade/session/sqlite_store.py", "core/vade/session/rpc.py", "core/vade/session/manager.py"], "acceptance_rows": 14, "acceptance_checked": 0}
  }
}
```

## 4. 本表已知的口径冲突（详单与证据见 `docs/rfc/LEDGER-RESULT.md`）

1. RFC-0003～0009 frontmatter `stage` 均自称「草稿，未实现」，与 status.md「已合并」及契约登记矛盾——stage 字段过时未回填（本仓不许改 RFC 正文，只能在此记录）。
2. 「34 方法」口径：三方向合计 **47**；34 只是 ui_to_core 方向数（与 34 错误码、34 事件撞数，口头汇报必错）。
3. 「events.json 10 条 appends」口径：events.json 实为 **8** 块；10 是 rpc.json 的块数；三文件合计 21 块。
4. append 块级版本 ≠ 契约顶部版本（RFC-0013 块级 1.2.0 / 顶部 1.3.0→1.5.0 共存），引用版本号必须说明是哪一层。
5. RFC-0007 的 4 个 `task.*` 事件与顶层 `task_states` 在 events.json **没有** append 登记块（rpc.json 侧有登记）——append-per-minor 纪律的缺口。
6. RESULT-rfc0007.md 记 601 passed，status.md 记 607 passed——无出处解释的 6 条差异。
7. rpc.json 的 RFC-0009 append 块缺 `contract` 字段；manifest.schema.json 的 RFC-0005 块 `contract` 写 "RFC-0005" 而非契约号——同数组内命名口径不一。
8. RFC-0011 唯一实现证据在客户端仓（vade-client `d667442`），本仓不可验证。
