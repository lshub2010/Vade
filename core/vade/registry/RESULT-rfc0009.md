# C2/C3 · RFC-0009 模块 UI 页面（C-19）· RESULT

| 项 | 值 |
|---|---|
| 分支 | `tmp/rfc0009`（基线 `67c5c49`） |
| RFC | `docs/rfc/RFC-0009-module-ui-pages.md`（approved） |
| 测试 | `pytest core/tests -q` → **710 passed**（基线 607，新增 103；无失败、无 skip） |
| 契约门禁 | `python tools/check_contracts.py` → `contracts: OK` |
| 文档门禁 | `python docs/check_docs.py` → 12 项全通过（C5 仍是「C-01–C-18 共 18 个」） |
| 未 push | 是（按任务要求） |

## 一、交付清单（逐条）

| # | 要求 | 落点 | 状态 |
|---|---|---|---|
| 1 | `ui.pages[]` schema（页面声明 + 节点树 + 限额） | `schema/manifest.schema.json`（`properties.ui`、`$defs.page`、`x-vade.limits`、`x-vade.appends` C-19） | ✅ |
| 2 | 7 白名单组件（只允许 RFC 那七个），4 个宿主组件出现即拒 | `core/vade/registry/uipage.py` `COMPONENTS` / `HOST_COMPONENTS` / `PROP_SPECS` | ✅ |
| 3 | 三类动作 + `open_url`：`navigate` / `tools/call` / `ui/submit` / `open_url` | `uipage.ACTION_KINDS`、`validate_action`、`modulepage/service.route_action` | ✅ |
| 4 | core 侧解析模块自报页面；无 `ui` 声明就无页 | `core/vade/registry/manifest.py`（`Manifest.ui_pages` / `ui_warnings`）、`registry/uipage.py:validate_ui` | ✅ |
| 5 | 只读三根（`module.status` / `page.state` / `item`），越权直接拒 | `uipage.validate_binding_path`（未知根 = 整页拒，不是空值降级） | ✅ |
| 6 | 非法组件 / 未知动作 / 超限 / 环 → fail-closed 拒渲，可观测 | `UIPageError`（`MANIFEST_INVALID` + JSON Pointer）、`Manifest.ui_warnings`、`_node_view.ui_page_warnings`、`ui/page.get` 的 `MANIFEST_INVALID` | ✅ |
| 7 | 动作路由走现有 RPC 分发；`navigate` 站内；`open_url` 仅 http(s) | `modulepage/service.py:route_action` / `tool_call`（`Router.call` 同一条路）、`check_navigate` / `check_open_url` | ✅ |
| 8 | C-18 新配置键 + `check_contracts` 契约断言 | `schema/config.schema.json` `[ui]` 4 键；`tools/check_contracts.py:check_ui` | ✅ |
| 9 | 测试（合法页 / 非法组件 / 未知动作 / 超限 / `open_url` 非 http(s) / 无 `ui` / `navigate` 越界） | `core/tests/registry/test_ui_pages.py`（74）、`core/tests/uipage/`（29） | ✅ |

## 二、变更文件

```text
新增  core/vade/registry/uipage.py       C-19 唯一校验器：节点/组件/props/绑定/动作/限额/脚本键
新增  core/vade/modulepage/__init__.py   组件 install（handler provider；m < s，早于 session）
新增  core/vade/modulepage/service.py    page.get / state.get / tool.call 两阶段 / route_action
新增  core/vade/modulepage/rpc.py        ui/page.get、ui/state.get、ui/tool.call
新增  core/tests/registry/test_ui_pages.py  注册期与 schema 契约（73 项）
新增  core/tests/uipage/{__init__,helpers,test_service,test_tool_call}.py  服务/RPC/动作（28 项）
改    core/vade/registry/manifest.py     Manifest.ui_pages/ui_warnings + 逐页校验（坏页剔除）
改    core/vade/registry/api.py          模块卡片可见 ui_page_count / ui_page_warnings
改    core/vade/policy/approval.py       open_request()（不阻塞铸审批）+ decision_of + reap_expired
改    schema/manifest.schema.json        ui / page / x-vade.limits / x-vade.appends(C-19)
改    schema/rpc.json                    ui_to_core 3 方法、core_to_module 2 方法、1.2.0
改    schema/config.schema.json          [ui] state_cache_s / page_cache_bytes / module_cache_bytes / pending_calls
改    tools/check_contracts.py           check_ui：配置键、C-03 严格子树、组件/动作/限额、§10.1 扫描、C-05 方法
```

**未动**（硬约束核对）：`schema/events.json`（git diff 为空）、`turn_states`、`core/vade/tasks/**`、
`core/vade/blob/**`、`core/vade/input/**`、`docs/agents/01-contracts.md`。

## 三、实现要点

- **三个校验点共用一张表**：注册期（`validate_manifest → validate_ui`，坏页剔除 + warning）、取回期
  （`ui/page.get` 的 ref 页：≤64 KiB → sha256 比对 → 再校验）、渲染期（`ModulePageService._revalidate`
  对「真正要发出去的那份 body」再跑一次）。ref 页面在注册时还不知道内容，所以只钉摘要；取回时摘要不符
  即 `MANIFEST_INVALID`，**不截断、不半渲染**。
- **fail-closed 的观测方式**（不静默）：
  1. 注册期：坏页不进 `ui.pages`，`Manifest.ui_warnings` 带 `/ui/pages/N (id): 原因`；
  2. 管理面：`RegistryAPI.snapshot_for_admin()` 的 `ui_page_warnings` 直接把这句话送到模块卡片；
  3. 取回/渲染期：`VadeError("MANIFEST_INVALID", "<pointer>: <reason>")`，错误卡可直接显示前 3 条；
  4. `check_contracts.py` 静态断言包络：白名单、动作集、限额、`[ui]` 默认值、§10.1 禁用 token。
- **脚本键扫描**：`SCRIPT_KEYS` 先做大小写与 `_`/`-` 归一（`onClick`、`on_click`、`ON-CLICK` 同一条），
  任意层级命中即整页拒；字符串值里的 `<script` 同样拒。`x-*` 是唯一逃生口，且宿主完全忽略。
- **`ui/tool.call` 两阶段**：阶段一只做归属 + `input_schema` 复校 + 帧长 + 风险判定 + 铸审批
  （`ApprovalManager.open_request()`，**不等待**），模块执行计数为 0；阶段二要求 `call_id` + `approval_id` +
  逐字节相同的 `args`（摘要比对），再走 `Router.call`（与 `tools/call` 同一条派发路径、同一套熔断/超时）。
  审批结果由既有 `approval/decide` 写回（设备密钥/loopback），没有新增事件族。
- **无审批口即拒绝**：`approval_port` 缺失时高风险页面调用返回 `UNAVAILABLE`，绝不落入「无需审批」分支。
- **`route_action`**：`tools/call` → 同一 `tool_call`；`ui/submit` → 既有 `ui/submit` handler（队列/限流/
  `READ_ONLY_SESSION`/审计全复用）；`navigate` 用**当前**页集合重新校验（注册后被删的页无法跳转）；
  `open_url` 重新校验协议。后两者只返回目标，由宿主执行，Core 不打开任何 URL。
- **入口空集合**：`ModulePageService.pages()` 在无页面时返回 `()`；`ui.pages` 缺失 → `ui_pages == ()`，
  桌面/手机不出现任何入口（FR-8.79 判据由数据形状保证，不靠 UI 判断）。

## 四、与 RFC 文本的偏差（逐条）

1. **`open_url` 是第 4 类动作**：RFC §1/§6 只冻结 `navigate` / `tools/call` / `ui/submit`；本次实现按任务书
   增加 `open_url`，并收紧为 **http/https**（拒绝 `javascript:` / `file:` / `data:` / `ftp:` / 无 host）。
   它只让宿主浏览器打开链接，不把远程内容载入页面，§10.1「页面无 url/src/href 字段」仍成立——目标键名用
   `to`，且 `url`/`src`/`href` 都在脚本键黑名单里。

2. **页面标识键是 `id` 不是 `page_id`**：RFC §4.2 的页面声明用 `id`，`page_id` 只出现在 `ui/*` 的 RPC 参数与
   `schema.ref.page_id`。实现遵循 RFC；同时提供 `UIPage.page_id` 属性、`UIPage.component_names()` 与
   `UIPage.actions()` 作为「components[] / actions[]」的派生视图（扁平化、保序）。`refresh_s` 作为页面级可选键
   （1..3600）实现，用于客户端刷新提示；core 自身的缓存上限是 `[ui].state_cache_s`。

3. **FR-8.78「未知节点降级为占位」未实现（有意的收窄）**：§7.2 / D109 选了更严的 fail-closed——未知
   `type`/组件/动作/脚本键/超限一律**整页拒渲** + `MANIFEST_INVALID`，不做节点占位。本实现即 §7.2 口径；
   前向兼容由页面 `version` 与入口门控承担（超支持集 → 入口不出现）。这是任务书要求「非法即 fail-closed 拒渲，
   不是降级占位」的直接落点，也是与 FR-8.78 原文唯一冲突处，需 C-19 冻结时追认（RFC Q5）。

4. **A7 的处理比 RFC 更严**：RFC §5.4 对「路径不在三根内」写「空值 + 一次性提示」，但 §7.2 把 `data_ref` 越界
   列为拒渲触发，任务书也要求「越权直接拒」。实现选后者：`session.messages` / `audit.*` / 他人
   `page.state` 在**注册期**就被拒，页面根本到不了渲染器，因此「0 新增 RPC 帧、不回显 core 数据」由构造保证。
   §5.4 的「空值」只保留给合法根但字段缺失/为 `null` 的运行时情形。

5. **页面里的工具名必须是 C-03 合法名（无点号）**：RFC §4.6 示例写 `download.refresh`，但 C-03 的
   `^[a-z][a-z0-9_]{0,63}$` 不允许 `.`，且 `Manifest.tool()` 按裸名查找。实现按 C-03：页面动作写 `refresh`，
   core 以「页面所属模块 + 裸工具名」定位 `module/tool`。

6. **`[ui]` 是新增配置段（4 键）**：RFC §7.3 给的是常量上限，没有点名 core.toml 键。为了让「缓存 ≤5 s /
   每页 ≤64 KiB / 每模块 ≤512 KiB / 待批页面调用上限」可配置且可被门禁断言，按 `[attach]`/`[tasks]` 先例
   新增 `[ui]`（全部 hot，owner C3），默认值与 §7.3 一致。

7. **`policy/approval.py` 只做加法**：新增 `open_request()`（铸审批不等待）、`decision_of()`（只读决策）、
   `reap_expired()`（回收超时的页面审批，无后台任务）；`request()` 改为 `open_request + _await_decision`
   的组合，行为与审计不变（`core/tests/policy`、`core/tests/task` 全绿）。风险判定逻辑（`policy/engine.py`）
   一字未改。

8. **RPC 方法名与 RFC §9.3 一致，但 `ui/tool.call` 结果补了 `args_digest`**：客户端要用它确认「自己发的就是
   审批看到的」；其余字段（`call_id`/`status`/`approval`/`result`/`error`）按 §9.3。

9. **`ui/state.get` 的 `rev` 单调**：模块给低版本号时 core 取 `max(rev, cached.rev + 1)`，宁可用旧值也不让
   客户端看到 rev 回退（§9.3 的「可先用旧值渲染再刷新」）。

10. **`schema/manifest.schema.json` 的 `schema.oneOf` 加了判别键**：RFC §9.2 的两分支（裸 `{type:object}` +
    `required:[ref]`）会让 `{"ref":...}` 同时命中两支，2020-12 的 `oneOf` 因此恒假。实现把内联分支改为
    `required:["type"]` 且 `type` 限定 6 个节点类型，两支互斥。这是对 RFC 片段的必要修正。

## 五、后续（不在本 RFC 内）

- C-19 冻结时登记 `docs/agents/01-contracts.md`（18 → 19）并同步 `docs/README` 的契约计数（本提交不动，
  否则 `check_docs.py` C5 立刻变红）。
- 三端渲染器（Kotlin `core/modulepage/`）与入口/深链/皮肤一致性（A5/A6/A9/A11/A12）不在本仓库 core 范围；
  core 侧已提供它们需要的 `ui/page.get` / `ui/state.get` / `ui/tool.call` 与 `pages()` 入口数据。
