---
rfc: RFC-0009
title: 模块 UI 页面：原生声明式 schema（新契约 C-19）、只读数据绑定与三类动作
status: approved
approved: 2026-11-14
date: 2026-11-14
proposer: 编排（依据 `docs/requirements.md` §8.22.1 决策 #18 / #19、§8.22.3 FR-8.78 / FR-8.79、§8.22.4（C-09 7 → 11）、§8.22.8 #3；`docs/agents/50-U1-ui-skeleton.md` §五 U1-h 与 §七 T5c；`research/ui-decisions.md` 在本 worktree 缺失，19 条裁定以 requirements §8.22.1 表格为准）
affects: C-19（新，待冻结）；C-03 `schema/manifest.schema.json` 追加 `ui.pages[]`（含 `core/vade/registry/manifest.py` 的 `Manifest` 承接，否则静默丢弃）；C-05 `schema/rpc.json` 的 `ui_to_core` 追加 `ui/tool.call` / `ui/state.get` / `ui/page.get`，`core_to_module` 追加 `state/get` / `page/get`
related: RFC-0001（C-03 追加的正确写法，以及「`additionalProperties: true` 放行但 `Manifest` 丢弃」这个已踩过的坑）；RFC-0004 / RFC-0007（章节风格与 C-05 追加惯例来源；本文件复用 `ui/submit` 与 `approval/decide`）；AMEND-22 §8.22.3 / §8.22.6 #4（C-19 冻结时才登记进 01-contracts 契约表）
type: 新契约（C-19）+ C-03 / C-05 追加；不改 C-04 错误码、不改 C-06 事件族、不改 C-09 组件签名、无破坏性改名
stage: 草稿，未实现；不改 `schema/`、不改 `core/`、不改 `docs/agents/01-contracts.md`（登记会让 check_docs 的 C5「18 个契约」口径变红）
---

# RFC-0009 · 模块 UI 页面

## 1. 摘要

模块可以带页面，但页面不能是「模块自带的一份网页」。本提案给模块一条**声明式**的表达通道：在 manifest 的 `ui.pages[]` 里声明页面，页面体是由 4 个布局原语（`column` / `row` / `tabs` / `list`）、一个文本叶（`text`）与 C-09 组件子集组成的节点树；数据只能**只读**绑定模块自己的状态；交互只有 3 类动作（`navigate` / `tools/call` / `ui/submit`）。客户端用原生组件渲染，**没有脚本、没有表达式求值器、没有 WebView、没有远程 HTML**。

| # | 决定 | 理由 |
|---|---|---|
| 1 | `ui.pages[]` 挂 manifest **顶层**，不在 `capabilities` 内 | 页面是 UI 表面，`capabilities` 是能力（tools / prompts / events）；混进去等于让 `capabilities.update` 变成「能改 UI」（§4.2） |
| 2 | 节点 = 4 原语 + `text` 叶 + `component` 引用 | 4 原语表达不了一段文字，而 C-09 里**没有** `Text` 工厂（`design.md` §9.2 只有 7 个组件）：加一个叶比加第 12 个组件便宜 |
| 3 | 绑定 = 路径 + 格式，**没有表达式** | 不求值就没有注入面；「条件显示」由模块在 `ui/state.get` 里算成布尔字段（§5.2） |
| 4 | `$input` 是**动作参数**，不是状态 | 满足「不做表单双向绑定」，又不把搜索类页面判死：输入值只在客户端内存活到动作发出那一刻（§5.3） |
| 5 | `tools/call` 两阶段：先铸审批、再执行 | 审批要绑 `args` 摘要，且不能让 ws 请求阻塞 60s 等人工；两阶段同时解决（§6.2） |
| 6 | 结构性未知 **fail-closed**，装饰性未知可降级 | 未知 `type` / 组件 / 动作 / 脚本键 = 页面不可信，整页拒渲；`icon` 拼错只回落默认图标（§7.2） |
| 7 | `schema` 可内联（≤8 KiB）或 `ref`（sha256 钉住，≤64 KiB） | 注册帧上限 65519 B（C-01 §7.5）装不下大页面；ref + 摘要同时防「注册后换页」（§4.2 / §9.2） |
| 8 | C-19 的 `ui` 子树是 C-03 里**唯一** `additionalProperties:false` 的区域 | 未知键在页面里就是潜在脚本钩子；C-03 其余部分的 `true` 是为前向兼容，页面这块不能沿用（§9.2） |

## 2. 动机与证据

### 2.1 今天模块没有任何「自己的页面」

`schema/manifest.schema.json` 的顶层属性只有 `module_id` / `instance_id` / `version` / `protocol` / `display_name` / `role` / `concurrency` / `priority` / `capabilities` / `limits` / `injects` / `subscriptions` / `extensions`——**没有 `ui`**；`core/vade/registry/manifest.py:96-115` 的 `Manifest` dataclass 同样没有。模块想在宿主里放界面，唯一形态是 `webui`（`design.md` §10.1 的 8080 端口那行）——**另一个浏览器窗口**，用户得自己记住地址。`50-U1-ui-skeleton.md` T5c 把这条缺口写成了流程阻塞：C-19 未冻结时 U1-h **只做壳**，「不要自己发明 schema 字段——发明出来的字段冻结时一定对不上」。一个切片靠一句「等契约」停着，说明缺的不是实现，是**字段级的形状**。

### 2.2 WebView 渲染模块页的代价是三条

**样式分裂**：模块自带 HTML/CSS 不认识 `Skin`。三皮肤下模块页要么统一长成「网页样」，要么作者写三套 CSS——而 U2/U3 的 AC-13 验收不覆盖模块页，三皮肤承诺在模块页上失效。**XSS 与桥**：宿主 WebView 注入了 `ACBridge`（T5b：`getDeviceInfo` / `sign` / `secureStore`）；`sign()` 虽校验 payload 模板，但一个能执行脚本的页面仍是「拿到设备身份的最近路径」，`design.md` §7.11 的「不允许 `innerHTML` 插值」是给 **admin 自己的代码**定的纪律，管不到第三方 HTML。**信任域混淆**：admin 配置页跑的是 core 自己的前端产物（D99），模块页跑的是第三方内容；都塞进 WebView，等于让第三方内容与 admin 前端共享桥、cookie 与 CSP 豁免。

### 2.3 现有协议也不支持「页面」这件事

- `schema/rpc.json` 的 `ui_to_core` 只有 6 个方法（`ui/attach` / `ui/detach` / `ui/submit` / `ui/interrupt` / `ui/queue.cancel` / `approval/decide`）：**没有**读模块状态、取页面、调工具的方法。
- `tools/call` 只存在于 **`core_to_module`** 方向：工具只能由 core（模型循环里的 router）发起，**UI 没有发起入口**。FR-8.78 写「经 core 路由」在语义上成立、在报文上不成立——UI→core 这一跳是空的（§9.3 补 `ui/tool.call`）。另外两点也不支持：审批不用伪造 turn（`approval/decide` 只吃 `{request_id, decision, scope}`，没有 `turn_id`，所以页面审批不必改 `turn.approval_required` 的 `required`）；状态只有 `module.status`（`module_id/instance_id/online/version`），而页面要展示的是模块**业务**状态（队列长度、剩余额度），协议里没有只读通道。

**结论**：页面必须声明式（否则三皮肤与 XSS 承诺同时破产）；声明必须进 manifest（否则 FR-8.79 的「无页面不显示入口」没有判据）；声明必须可静态校验（否则「含 `<script>` → 拒渲」只能运行时试探）。三条合起来就是 C-19。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 模块能声明 1–8 个页面，三端用**原生组件**渲染，零脚本、零远程资源（FR-8.78） |
| G2 | 同一 schema 在 `material` / `miuix` / `glass` 下**结构一致、仅样式不同**（走 `Skin.*`，页面不接触 material3） |
| G3 | 只读绑定可取「模块在线状态」与「模块自报页面状态」；不得取会话正文 / 审计 / 其他模块状态 |
| G4 | 三类动作可用；`risk=high` 必审批，未批不执行，审批绑定参数摘要 |
| G5 | 非法 schema（脚本字段 / 未知结构）在**注册期与渲染期双拒**，不存在「部分执行」；入口按 FR-8.79，无页面无入口，离线与超时是卡片不是白屏 |
| N1 | **不做任意脚本**：无 JS、无 `eval`、无模板引擎、无内联表达式、无求值器（连 `a > 0` 都不支持） |
| N2 | **不做表单双向绑定**：输入不回写模块状态；`$input` 只作动作参数（D98 的「真相只存 core」不破例） |
| N3 | **不做模块页面写 core 状态**：没有 `state.set`、没有直接消息写入；写路径只有 `ui/submit`（用户可见的一条消息）与 `tools/call`（工具自己的副作用） |
| N4 | 不做跨模块页面组合与嵌套（只能引用本模块状态与工具）；不做页面内附件上传（`AttachmentChip` 只读，Q4） |
| N5 | 不做页面级路由表 / 多级导航栈（只有 `navigate` + 面包屑，§8.3）、不做页面热更新（以 `version` + `capabilities.update` 生效） |

## 4. C-19：结构

### 4.1 三层与三个校验点

```
manifest.ui.pages[i]   # 声明（≤8 项，C-03 追加）        注册期校验
  └─ schema            # 内联 或 {ref:{page_id, sha256}} 注册期 / 取回期
       └─ node         # 4 原语 / text 叶 / component    渲染期再校验
            props · children · data_ref · on.<event> → action（3 类之一）
```

### 4.2 页面声明

| 字段 | 类型 | 必填 | 约束与语义 |
|---|---|---|---|
| `id` | string | 是 | `^[a-z][a-z0-9_-]{0,31}$`，模块内唯一；深链与路由的稳定键（改了等于换页面） |
| `title` | string | 是 | 1–40 字符；入口项与面包屑显示；v1 单语（Q7） |
| `icon` | string | 否 | 枚举 `list` / `chart` / `settings` / `tool` / `file` / `none`；**未知值回落默认图标 + 一条告警**，不拒渲（装饰性，§7.2） |
| `entry` | string | 否 | `auto`（默认）= 桌面右面板 + 手机「能力」tab（FR-8.79 默认）；`panel` = 仅桌面右面板（手机不出现、不留空行）；`capabilities` = 两端都进「能力」列表，桌面另在右面板顶部列一项 |
| `version` | integer | 是 | ≥1，页面 schema 的 C-19 修订号；超出客户端支持集 → **入口不出现**（§8.2），不降级渲染 |
| `requires` | array | 否 | 只允许 `["session"]`：页面有 `ui/submit` 动作、或工具需要 `ctx.session_id` 时声明；无活跃会话 → 入口可点但页顶提示「需要先打开会话」、动作禁用 |
| `schema` | object | 是 | 内联＝节点树根（≤8 KiB 序列化后）；ref＝`{"ref":{"page_id":"...","sha256":"<64 hex>"}}`，由 `ui/page.get` 取回（≤64 KiB），客户端**必须**校验摘要，不符即 `MANIFEST_INVALID` |

`ui` 块整体 ≤48 KiB（8 页 × 平均 6 KiB），给 `capabilities` 留出注册帧余量。

### 4.3 节点类型

| `type` | 类别 | `children` | `data_ref` 指向 | `props` |
|---|---|---|---|---|
| `column` | 布局原语 | 是（≤32） | — | `gap` ∈ `none\|s\|m\|l` |
| `row` | 布局原语 | 是（≤32） | — | `gap` / `align` ∈ `start\|center\|end` / `wrap` bool |
| `tabs` | 布局原语 | 是（每 child 带 `props.tab_title`） | — | `selected`（0-based，可绑定） |
| `list` | 布局原语 | 是（**恰好 1 个**模板 child） | 数组（≤512 项） | `empty_text` / `item_fields`（字段白名单）；渲染窗口 ≤50 项/屏（Q3） |
| `text` | C-19 叶 | 否 | 标量（字符串化） | `style` ∈ `title\|subtitle\|body\|caption\|mono` / `max_lines` 1–8 / `format` |
| `component` | C-09 引用 | 仅 `Card` | 见 §4.5「主值」列 | `component`（白名单名）+ `props` |

原语只 4 个是有意的：纵向 / 横向 / 分栏 / 列表覆盖绝大多数模块页；再多的原语（`grid` / `stack` / `spacer` / `scroll`）会诱使模块把页面当画布，而画布一旦存在，三皮肤就无法保证一致。`tabs` 是页内分栏，**不是路由**——路由只有页面级（§8.3）。

### 4.4 通用属性

| 属性 | 类型 | 说明 |
|---|---|---|
| `id` | string，页内唯一，`^[a-z][a-z0-9_-]{0,31}$` | 无障碍 id 与测试锚点；不参与路由 |
| `visible` | bool \| `{"bind": path}` | **只做真值判定**（falsy / 空数组 / 空串 / `false` / `null` 为假）；不支持比较与布尔组合（§5.2） |
| `data_ref` | path | 节点**主值**的只读绑定：`text` 的文本、`list` 的数组、组件的 `props.value` |
| `props` | object | **未知键 → 拒渲**（`additionalProperties:false`）；值可字面量或 `{"bind": path}` |
| `children` / `on` | array / object | 仅容器可有 `children`（叶节点带 → 拒渲）；`on` 的事件名只能取 §4.5 允许值，未知 → 拒渲 |

### 4.5 C-09 子集白名单（rev 1）

只有这 7 个 `component` 名可用；`props` 键是**封闭集**，与 C-09 签名一一对应（C-19 只固定 JSON 键名，签名本体仍归 C-09）。

| 组件 | 主值 | 允许的 `props` | 事件 | 只读形态的硬约束 |
|---|---|---|---|---|
| `Button` | — | `text` / `style` ∈ `primary\|secondary\|danger\|text` / `enabled` | `click` | 请求未返回前禁用（防连点）；`danger` 只用于 destructive |
| `TextField` | — | `label` / `placeholder` / `value`（只接受 `$input` 绑定）/ `multiline` / `max_length` ≤4096 | `change`（可选） | **只写本地 `$input`**，永不回写模块状态（N2） |
| `Card` | — | `title` / `subtitle` / `elevated` | `click`（可选） | 唯一允许带 `children` 的组件 |
| `ListItem` | — | `title` / `subtitle` / `leading_icon` / `trailing_text` | `click` | 叶节点；`list` 模板里最常用 |
| `Switch` | `checked` | `label` / `enabled` | `change` | **宿主不本地翻转**：`checked` 只来自绑定，翻转只发动作，状态由下一次 `ui/state.get` 反映 |
| `ToolCallCard` | — | `tool` / `state` ∈ `running\|ok\|failed\|waiting_approval` / `args_preview` ≤512 / `result_summary` | `click`（可选） | 只读：不能批准 / 重试 / 取消 |
| `AttachmentChip` | — | `file_id` / `mime` / `name` / `state` | `click`（可选） | 只读：不能上传；`click` 只能 `navigate`（Q4） |

**明确不得出现**（出现即整页拒渲，不是忽略）：`MessageBubble` / `InputBar` / `ApprovalDialog` / `BottomBar`。这四个是**宿主**组件——一出现，模块页面就能伪装成会话记录、输入区、审批弹窗或一级导航，这是钓鱼面而非样式问题（D93 的 11 个组件是宿主集合，不是页面集合）。

### 4.6 示例（列表 + 绑定 + 两类动作）

```json
{"type": "column", "props": {"gap": "m"}, "children": [
  {"type": "text", "data_ref": "page.state.queue_len", "props": {"style": "title", "format": "int"}},
  {"type": "text", "props": {"text": "在线"}, "visible": {"bind": "module.status.online"}},
  {"type": "list", "data_ref": "page.state.items", "props": {"empty_text": "队列为空"},
   "children": [{"type": "component", "component": "ListItem",
                 "props": {"title": {"bind": "item.name"},
                           "subtitle": {"bind": "item.progress", "format": "percent"}}}]},
  {"type": "component", "component": "Button", "props": {"text": "刷新", "style": "primary"},
   "on": {"click": {"kind": "tools/call", "tool": "download.refresh", "args": {}}}}
]}
```

`item.*` 只在 `list` 模板内合法，字段集由 `props.item_fields`（省略时取首项所有键）界定，越界按 §5.4 处理。

## 5. 数据绑定（只读）

### 5.1 三个根

| 根 | 来源 | 字段 | 刷新 |
|---|---|---|---|
| `module.status.*` | C-06 `module.status`（已存在） | `online` / `version` / `instance_id` | 事件到达即刷新 |
| `page.state.*` | C-05 新增 `ui/state.get`（core 代理模块 `state/get`） | 模块自报扁平对象，值只能 `string\|number\|bool\|null` | 进入页面、`online` 变真、用户重试 |
| `item.*` | `list` 当前项 | 见 §4.6 | 随列表 |

**没有第四个根。** 会话正文、消息、审计、审批、其他模块的 `page.state` 一律不可绑定：越界按 §5.4 显示空值，不回落、不带 `session_id` 去 core 取（否则「只读模块自身状态」就成了「只读 core」）。

### 5.2 表达式边界

绑定值就是一个**路径字符串** + 可选的同级 `format`：

- 允许：取字段（点号分隔，≤4 段，每段 `[a-z0-9_]{1,32}`）+ 格式化（`text` / `int` / `decimal` / `percent` / `bytes` / `datetime` / `relative`）；不允许：运算、比较、逻辑、三元、函数调用、字符串拼接、取下标、通配、管道。
- 「条件显示」不靠表达式：模块在 `state` 里直接给布尔字段（`{"has_items": true}`），页面写 `"visible": {"bind": "page.state.has_items"}`；「条件文本」同理，`state` 直接给格式化好的字符串（`{"eta_text": "约 3 分钟"}`）。

代价是模块多算一点；收益是客户端**没有求值器**——没有求值器就没有 `eval` 的等价物，FR-8.78 的「不执行任意脚本」才不是靠代码审查守着的口头承诺。

### 5.3 `$input`：动作参数，不是状态

`TextField` 的 `value` 只能写成 `{"input": "<input_id>"}`。语义：

- 存在**客户端内存**，随页面销毁而丢；不进快照、不同步、不落盘（D98 的「可丢弃派生状态」）。
- 只能作为动作参数被读（`{"input": "q"}`），**不能**出现在 `data_ref` 或 `props` 的其他位置、不能参与 `visible`。
- `Switch` 的 `checked` 不接受 `$input`（否则就是双向绑定）：开关必须绑模块状态，翻转走动作。
- 一律按**字符串**提交；需要数字时写 `{"input": "n", "as": "int"}`，严格解析（`^[0-9]{1,9}$`），失败 → 动作不发出、就地提示。

### 5.4 越界、缺失与离线

| 情况 | 表现 |
|---|---|
| 路径不在三个根内（如 `session.messages`） | **空值** + 一次性提示「此页面引用了不可用数据」；**不发任何 RPC**、不回显任何 core 数据（FR-8.78 判据） |
| 路径合法但字段缺失 / `null` | 空值，无提示（正常状态，如 `page.state` 首次返回前） |
| `page.state` 取不到 / `online == false` | 前者：页顶 banner「模块状态不可用」+ 重试，结构照常渲染、绑定值为空；后者：banner「模块离线」，保留最后一次成功渲染的**结构**（仅内存）、绑定值置空、**全部动作禁用**。两者都不白屏 |

## 6. 动作（3 类）

动作对象形如 `{"kind": ..., ...}`；`kind` 不在下面三个之内 → **整页拒渲**（§7.2）。每节点 ≤4 个动作，每页 ≤64 个。

### 6.1 `navigate`

```json
{"kind": "navigate", "to": "module:stats" | "session:current" | "session:<session_id>" | "settings:appearance"}
```

| `to` | 落到 | 条件 |
|---|---|---|
| `module:<page_id>` | 同模块另一页（页内路由） | 必须是本模块声明的页面，否则拒渲 |
| `session:current` / `session:<id>` | 会话详情 | 需要活跃会话；`<id>` 不存在 → toast，不改路由 |
| `settings:<tab>` | 设置子页（`appearance` / `models` / `devices` / `about`） | 枚举固定 |

`navigate` 不离开宿主、不开浏览器、不开 WebView；`module:` 目标不进深链（深链只到页面级，§8.3）。

### 6.2 `tools/call`（两阶段）

```json
{"kind": "tools/call", "tool": "download.pause_all",
 "args": {"all": true, "note": {"input": "why"}}, "confirm_text": "暂停全部下载？"}
```

**归属**：`tool` 必须是**本模块** `capabilities.tools` 里声明的名字；跨模块调用 v1 不做（N4）。core 以「页面所属模块」定位工具，客户端自报的 `module_id` 不参与授权（§10.2）。

```
客户端                                 core                      模块
  │ ui/tool.call {tool, args, client_call_id, session_id?}
  │───────────────────────────────────▶│ ① 归属 + args vs input_schema；② >65263 B → PAYLOAD_TOO_LARGE
  │                                    │ ③ risk = max(manifest.risk, policy.tool_risk)
  │                                    │ ④ 需审批 → 铸 request_id + args_digest
  │◀── {call_id, status:"approval_required", approval:{request_id, tool, args, risk, expires_at}}
  │   宿主弹 C-09 ApprovalDialog → approval/decide {request_id, "approve", "once"}
  │ ui/tool.call {call_id, approval_id, ...完全相同的 args}
  │───────────────────────────────────▶│ ⑤ 摘要不符 / 过期 → PERMISSION_DENIED；⑥ tools/call ──▶│
  │◀── {status:"ok", result} | {status:"failed", error}
```

- 第一阶段**只校验并铸审批，不执行任何工具**（否则「未批不执行」在实现上就漏了）；第二阶段必须重发**完全相同**的 `args`，core 用摘要比对，防「审批看的是 A、执行发的是 B」。
- `risk` 由 core 判定，`manifest.risk` 只能被 `[policy.tool_risk]` **上调**（C-04 `RISK_DOWNGRADE_FORBIDDEN` 保证 `capabilities.update` 不能下调）；`low` 是否也审批由 policy 定，C-19 不写死。
- 审批继承 FR-8.53：`deny` 与超时（默认 60s，`expires_at`）都是拒绝（fail-closed）；`scope:"session"` 只对同 `(module, tool)` 生效，不跨工具、不跨模块；审批人不合规 → `APPROVER_INVALID`。
- 结果**不进模型上下文、不产生消息**：页面动作是用户行为，不是 turn。
- 审计：`origin:"module_page"` + `module_id` + `page_id` + `tool` + `args_digest` + `approval_id` + `decision`，按 FR-10.3 归因到模块。

### 6.3 `ui/submit`

```json
{"kind": "ui/submit", "content": [{"type": "text", "text": {"input": "q"}},
                                  {"type": "text", "text": "（来自模块页面）"}]}
```

- 映射到**现有** `ui/submit`（`session_id` 由宿主填当前会话，`client_msg_id` 由客户端生成 uuid）。part 只允许 `type:"text"`（附件见 N5 / Q4），`text` 可字面量 / `{"bind"}` / `{"input"}`。
- 必须声明 `requires: ["session"]`；无会话 → 动作禁用并提示，不静默丢弃、不弹错误。
- 与用户手打消息**同一条**写路径：队列、限流、`READ_ONLY_SESSION`、审计都不变；显式只读会话下返回 `READ_ONLY_SESSION`，页面显示提示而不是崩。

### 6.4 参数来源

| 来源 | 写法 | 解析方 | 备注 |
|---|---|---|---|
| 字面量 | `true` / `"x"` / `3` | — | 只有 `string` / `number` / `bool` / `null` 及其数组 |
| 绑定值 | `{"bind": "page.state.target"}` | 客户端 | 解析后按字面量发送；解析不到 → 空串，core 按 `input_schema` 报 `INVALID_ARGUMENT` |
| 本地输入 | `{"input": "q"}` / `{"input": "n", "as": "int"}` | 客户端 | 只允许字符串与严格整数；**不做嵌套对象** |

其余来源键（`{"bind": "$env"}`、`{"eval": ...}`、`$input` 嵌进对象）→ 拒渲。参数在客户端解析成具体值后发送，core 再按 `input_schema` 复校：客户端解析错最坏是 `INVALID_ARGUMENT`；core 不复校才是问题，所以两道都要。

## 7. 渲染与校验

### 7.1 三个校验点

| 阶段 | 执行者 | 触发 | 失败后果 |
|---|---|---|---|
| 注册期 | core（`registry/manifest.py`，C-19 校验器） | `module/register` / `capabilities.update` | **单页剔除**：该页不进 `ui.pages`，模块其余能力照常注册（一个 UI 拼写不该打死整个模块，同 C-14「未知 topic 逐条拒绝」）；留告警，模块卡片显示「页面 X 未通过校验」 |
| 取回期 | core（`ui/page.get`） | ref 页面首次打开 | 摘要不符 / >64 KiB / 不可解析 → `MANIFEST_INVALID`，入口置灰并说明 |
| 渲染期 | 客户端（同一份规则表） | 打开页面 | **整页拒渲** + 错误卡（模块名、页面标题、`MANIFEST_INVALID`、最多 3 条定位），不渲染任何节点 |

注册期拒绝是「不让坏页面进系统」；渲染期拒绝是纵深防御（ref 页面在注册之后才取回，客户端版本也可能比 core 旧）。

### 7.2 拒渲规则（fail-closed）

**整页拒渲 + `MANIFEST_INVALID`**：

| 触发 | 例子 |
|---|---|
| 任何脚本键（大小写不敏感、任意层级） | `script` / `on_load` / `onclick` / `eval` / `js` / `html` / `innerHTML` / `template` 含 `${}` / `expression` / `lambda` |
| 未知 `type` / `component` | `grid` / `stack` / `iframe` / `webview`；`MessageBubble` / `InputBar` / `ApprovalDialog` / `BottomBar` / 任意非白名单名 |
| 未知动作 `kind` / 未知键 | `http.request` / `shell` / `state.set`；`props.onclick`、动作里的 `"then"` |
| 结构性越界 | 叶节点带 `children`、`list` 模板 child ≠ 1、`id` 重复、`props` 类型不符、`data_ref` 越界、`navigate` 到未声明页面 |
| 超限（§7.3） | 深度 9 / 节点 257 / 内联 9 KiB |

**允许降级（不拒渲，一条告警）**：`icon` 未知 → 默认图标；只读展示类 `format` 不认识（`currency`）→ 按 `text` 输出原值。分界是「这字段能不能改变行为」：能改变行为的未知 = 拒；纯展示的未知 = 降级。

> **与 FR-8.78 的差异（显式记录）**：FR-8.78 的验收写「未知原语或组件 → 该节点降级为占位并告警，页面其余部分仍可渲染」，T5c 同句。C-19 选**更严**：未知 `type` / 组件 / 动作一律整页拒渲。理由三条：① 未知原语破坏的是**骨架**（不认识的 `row` 意味着布局语义不可知，占位符的位置本身就是错的）；② 「部分渲染」给模块留了一条「先让我这半显示出来」的谈判路径，收益只是观感；③ 前向兼容由 `version` + 入口门控承担（§8.2），比半渲染诚实也比半渲染可控。**这需要需求方在 C-19 冻结时追认**（Q5），否则冻结文本与 FR-8.78 判据不一致。

### 7.3 限额（防渲染炸弹）

| 限 | 值 | 依据 |
|---|---|---|
| 单页 `schema` / `ui` 块 | 内联 ≤8 KiB / ref ≤64 KiB / 整块 ≤48 KiB | 注册帧 65519 B（`manifest.schema.json`）；ref 走独立请求 |
| 节点总数 / 嵌套深度 | ≤256 / ≤8 | 渲染成本与递归栈；超过即拒渲 |
| 单节点 `children` / 单 `text` | ≤32 / ≤4096 字符 | 宽度失控；文本比 C-03 的 `description_max_len: 512` 宽 |
| `list` 数据 | ≤512 项，渲染窗口 ≤50 项/屏 | 与 Q3 分页配套 |
| 绑定路径 / `page.state` | 路径 ≤4 段（每段 ≤32 字符）/ state ≤8 KiB、≤64 键 | 防 path 爆炸；响应上限 |
| 动作 / core 页面缓存 | 每节点 ≤4、每页 ≤64 / ≤64 KiB 每页、≤512 KiB 每模块 | 防「按钮墙」式钓鱼；`ui/page.get` 不做内存放大器 |

超限一律 `MANIFEST_INVALID`（注册期剔除 / 渲染期拒渲），**不做截断后渲染**——截断会把校验失败伪装成正常页面。

### 7.4 超时与重试

| 情况 | 表现 |
|---|---|
| 打开后 5s 首次渲染未完成 | 可重试卡片「加载超时」+ 返回；宿主不卡死、不阻塞导航（FR-8.79） |
| 模块离线 | 「模块离线」banner + 保留结构 + 动作禁用（§5.4）；`online` 变真时自动重取 `page.state` |
| `ui/page.get` 失败 / 页面被注册期剔除 | 卡片 + 重试，连续 3 次失败 → 本次进程内停止自动重试（保留手动）；被剔除的页面入口不出现，模块卡片一行说明（不让用户对着空白猜） |

## 8. 入口规则

### 8.1 桌面 / 手机

**桌面**：右面板顶部列本模块入口（`panel` / `auto`），一项 = 模块显示名 › 页面标题；右面板收起时不占位、不留空行，`⌘\` / `Ctrl+\` 展开。面板样式（宽度档位、承载组件）仍按 R3 自定义——**入口是面板的内容，不是面板的样式**，改样式不影响入口集合。

**手机**：「能力」tab → 「模块页面」列表项（`auto` / `capabilities`），每项 = 模块 + 页面标题。一级目的地仍是 4 个（FR-8.77 不变）：这里没有第 5 个 tab，只有能力页里的一个入口。

### 8.2 无页面 / 无入口 / 版本门控

`ui.pages` 缺失或为空 → **不显示任何入口**：不出现空 tab、空列表、灰掉的占位行。这条是 FR-8.79 的判据，也最容易被「先做个壳」破坏（壳一旦存在，卸载模块后就留一块空白）。`version` 高于客户端支持集 → 同样不出现入口，模块卡片一行「此页面需要更新的客户端」——这是 §7.2 选 fail-closed 后的配套：宁可让用户看不到，也不给结构残缺的页面（协商细节见 Q2）。

### 8.3 页内导航、面包屑与深链

- 深度 1 = 页面标题（顶栏）；`navigate` 到同模块另一页 → 深度 2，面包屑「模块名 › 页 A › 页 B」；上限 3 层，第 4 次 `navigate` **替换**栈顶而不是入栈（防止模块把宿主变成浏览器历史）。
- 返回（手势 / `Esc` / 顶栏返回）逐级回退，退到底回入口；手机上落回「能力」tab 且**列表滚动位置不变**。退出页面时 `$input` 全丢、`page.state` 缓存丢弃（下次进页重取）。
- 深链形态不在 C-19 内冻结（C-19 只保证 `module_id` + `page_id` 稳定）。建议 `vade://module/<module_id>/<page_id>`；未命中 → 回落模块入口 + toast，不崩、不开浏览器。

## 9. 契约变更

本 RFC **不改**任何冻结文件（`schema/`、`core/`、`docs/agents/01-contracts.md`）；下面是实施时要落的改动。

### 9.1 汇总

| # | 契约 | 变更 | 类型 | 风险 |
|---|---|---|---|---|
| 1 | **C-19（新）** | 页面声明 + 节点树 + 绑定 + 3 动作 + 拒渲规则 + 限额 | 新契约，待冻结 | 新，无兼容包袱 |
| 2 | C-03 | 追加顶层 `ui`（`pages[]`，≤8） | 追加 | **静默丢弃**（9.2），不是校验失败 |
| 3 | C-05 `ui_to_core` | `ui/tool.call` / `ui/state.get` / `ui/page.get` | 追加 | 新能力面：客户端可直调工具（§10.2） |
| 4 | C-05 `core_to_module` | `state/get` / `page/get` | 追加 | 模块 SDK 要能应答；不应答 → `UNSUPPORTED`（绑定空值 / ref 页不可用） |
| 5 | C-06 | **不改** | — | 页面审批走 `approval/decide` 两阶段，不新增事件、不动 `turn.approval_required` 的 `required` |
| 6 | C-09 | **不改** | — | C-19 只引用签名；子集白名单在 C-19 冻结时定稿（§8.22.8 #3 要求别在本节写死） |
| 7 | C-04 | **不改** | — | 复用 `MANIFEST_INVALID` / `INVALID_ARGUMENT` / `NOT_FOUND` / `PERMISSION_DENIED` / `PAYLOAD_TOO_LARGE` / `UNSUPPORTED` / `TIMEOUT` / `OVERLOADED` / `UNAVAILABLE` / `READ_ONLY_SESSION` / `APPROVER_INVALID` |

### 9.2 C-03 追加（`ui.pages[]`）

**现状核对（与常见假设相反）**：`schema/manifest.schema.json` 的 `additionalProperties` 在顶层与各子对象上**都是 `true`**，文件 description 写明「Forward compatible: unknown fields are ignored」。所以追加 `ui` 时 JSON Schema **会放行**——风险不是校验失败，而是 `core/vade/registry/manifest.py` 的 `Manifest` 只承接已知键，不认识的字段被**静默丢掉**。RFC-0001 的 `subscriptions` 就是同一个坑（其 `x-vade.appends` 注记原文：「`additionalProperties: true` let a module send it, but `Manifest` dropped it」）。这次追加是三件套：

```json
"ui": {"type": "object", "additionalProperties": false, "patternProperties": {"^x-": {}},
       "description": "C-19 append (RFC-0009). Strict: an unknown key inside a page is a script hook.",
       "properties": {"pages": {"type": "array", "maxItems": 8, "items": {"$ref": "#/$defs/page"}}}},
"page": {"type": "object", "additionalProperties": false, "patternProperties": {"^x-": {}},
         "required": ["id", "title", "version", "schema"],
         "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,31}$"},
                        "title": {"type": "string", "minLength": 1, "maxLength": 40},
                        "icon": {"enum": ["list", "chart", "settings", "tool", "file", "none"]},
                        "entry": {"enum": ["auto", "panel", "capabilities"], "default": "auto"},
                        "version": {"type": "integer", "minimum": 1},
                        "requires": {"type": "array", "maxItems": 1, "items": {"enum": ["session"]}},
                        "schema": {"oneOf": [
                          {"type": "object", "description": "Inline node tree, <=8 KiB."},
                          {"type": "object", "required": ["ref"], "additionalProperties": false,
                           "properties": {"ref": {"type": "object", "required": ["page_id", "sha256"],
                                                  "properties": {"page_id": {"type": "string"},
                                                                 "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}}}}]}}}}
```

三件缺一不可：① 上面的 schema 块（`ui` 进顶层 `properties`，`page` 进 `$defs`）；② `Manifest` 增加 `ui` 并在 `from_dict` 里解析（保序、逐页校验、坏页剔除）；③ `x-vade.appends` 追加 `{"contract": "C-19", "version": "1.0.0", "fields": ["ui"], "doc": "docs/rfc/RFC-0009-module-ui-pages.md", "why": "..."}`。

**为什么这块是 `additionalProperties:false`**：C-03 其余部分的 `true` 是为「旧 core 读新模块」；页面这块恰好相反——**未知键就是攻击面**（`on_click` / `script` 只要有一次被实现者「顺手支持」就破功）。代价是加字段必须走 C-19 修订 + core 更新，因此留页面级 `x-` 前缀作唯一逃生口（**宿主必须完全忽略 `x-*`**：不渲染、不求值、不转发给皮肤）。旧端行为：不认识 `ui` → 忽略 → 没有入口（这是唯一可接受的降级，崩溃与空白页都不可接受）。

### 9.3 C-05 追加

| 方法 | 方向 | 参数 | 结果 | 错误 |
|---|---|---|---|---|
| `ui/tool.call` | ui→core | `{module_id, instance_id, page_id, tool, args, client_call_id, session_id?, approval_id?}` | 阶段一 `{call_id, status:"approval_required"\|"denied", approval?}`；阶段二 `{call_id, status:"ok"\|"failed", result?, error?}` | `INVALID_ARGUMENT` / `NOT_FOUND` / `PERMISSION_DENIED` / `PAYLOAD_TOO_LARGE` / `TIMEOUT` / `OVERLOADED` / `UNAVAILABLE` / `READ_ONLY_SESSION` |
| `ui/state.get` | ui→core | `{module_id, instance_id, page_id}` | `{rev:int, state:object, fetched_at:int}` | `NOT_FOUND` / `UNSUPPORTED` / `UNAVAILABLE` / `TIMEOUT` |
| `ui/page.get` | ui→core | `{module_id, instance_id, page_id}` | `{schema:object, sha256:string}` | `NOT_FOUND` / `MANIFEST_INVALID` / `UNSUPPORTED` / `UNAVAILABLE` |
| `state/get` | core→module | `{page_id}` | `{rev:int, state:object}` | `INVALID_ARGUMENT` / `NOT_FOUND` / `UNSUPPORTED` |
| `page/get` | core→module | `{page_id}` | `{schema:object}` | `INVALID_ARGUMENT` / `NOT_FOUND` / `UNSUPPORTED` |

三个 UI 侧方法沿用 C-01/C-02 的设备鉴权与会话上下文，不新增鉴权面。`ui/state.get` 在 core 侧按 `(module, instance, page)` 缓存 ≤5s，`rev` 单调（客户端可先用旧值渲染再刷新）。模块侧 `state/get` / `page/get` 未实现 → core 回 `UNSUPPORTED`，客户端按 §5.4 / §7.4 显示，不弹错误。命名遵循 `core_to_module` 既有风格（`prompts/get`、`tools/call`）：`ui/` 前缀只属于 UI 面。

### 9.4 为什么不动 C-06，以及登记时点

页面触发的审批不需要事件：请求本身是同步 RPC，审批结果由 `approval/decide` 写回，第二阶段带回终局。新增 `ui.call.*` 事件族要为「另一台设备也能看到这个审批」付出整套生命周期（去重、重连补发、快照），而页面动作的审批**天然只有发起端在做**（用户正看着这个页面点按钮）。跨端可见留作 Q6。

`docs/agents/01-contracts.md` 的契约表在 C-19 **冻结时**才加行（18 → 19）；本 RFC 不动它，否则 `docs/check_docs.py` 的 C5（`契约 C-01–C-18 共 18 个`）立刻变红。这是 §8.22.6 #4 已写好的口径。

## 10. 安全

### 10.1 三条否定式约束（可被 CI 扫描）

- **不执行 JS / 不求值**：模块页面渲染路径（`core/modulepage/`）搜不到 `eval` / `Function(` / 模板引擎 / `WebView` / `loadUrl`（沿用 R3 对 admin WebView 的同一份清单，范围扩到 `modulepage/`）。
- **不加载远程资源**：页面没有 `url` / `src` / `href` 字段（`props` 是封闭集，§4.5）；图片只经 `AttachmentChip` 的 `file_id` 走既有 blob 取回。
- **不引入 WebView**：WebView 只存在于 `adminweb/`（T5b）；模块页不得 import 它，两条渲染路径彼此不可达。客户端也不直连模块：归属校验、`risk` 判定、审批、限流、审计都在 core——客户端能做的最坏的事是「发一个 core 会拒绝的请求」。

### 10.2 `ui/tool.call` 的边界（本提案唯一实质扩大的攻击面）

此前工具只能由模型在 turn 里发起。四条收口：①**归属**——只允许调本模块声明的工具，core 以「页面所属模块」定位，客户端自报的 `module_id` 不参与授权（只用于定位已注册页面，且必须与 `page_id` 同时命中）；②**风险不可降**——`risk` 只由 core 判定，`manifest.risk` 只能被 policy 上调；③**审批绑摘要**——`args_digest` 绑定，第二阶段逐字节相同（§6.2），`scope:"session"` 不跨工具、不跨模块；④**留痕**——`origin:"module_page"` + `page_id`，审计能回答「这条调用是模型发起还是页面按钮发起」。不声称的部分：这不是新的信任边界——没给设备超出「用户自己点按钮」的权力；但它确实去掉了「工具调用必伴随一个 turn」这个隐含前提，评审时不要假设它成立。

### 10.3 参数、数据与皮肤

- `args` 只允许字面量 / 绑定 / `$input` 三种来源（§6.4）：不做客户端拼 JSON、不做对象展开、不做 `$ref` 解引用。
- `$input` 一律按字符串或严格整数提交，永不解析成嵌套结构（否则就是一个迷你 JSON 解析器，且是注入点）。
- `page.state` 由模块提供，**不可信**：按 `format` 转义输出（纯文本，不解释 Markdown / HTML），长度截断到 §7.3 上限。
- 皮肤只接收组件树，**不读 schema**（换皮肤不改行为）；`ref` 页面的 `sha256` 在 manifest 里钉住，注册后换页 → `MANIFEST_INVALID`。

## 11. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| A. 模块自带可执行内容：WebView + HTML/CSS/JS，或下发 wasm / dex / class | 三皮肤要重写两套、XSS 直通设备密钥与桥（§2.2）；下发编译产物等于任意代码执行，与 FR-8.78 正面冲突。R3/T5c 已明令 WebView 只给 admin 配置页 |
| B. 远程 JSON-RPC UI 树（core 每帧下发 diff） | 渲染循环要 core 参与，断连即不可用，与「模块离线显示离线」冲突；每帧都要过加密与 seq，成本远高于「页面 + 只读状态」 |
| C. 只给 C-09 组件、不给布局原语，或反过来开放全部 11 个组件 | 前者表达不了「左标签右值」「两列按钮」，模块会持续要求「再加一个原语」，更快腐蚀契约；后者让 `MessageBubble` / `InputBar` / `ApprovalDialog` / `BottomBar` 进入页面，模块页就能伪装会话与审批（钓鱼面） |
| D. 表达式 DSL（模板 + 条件 + 运算） | 求值器 = 新注入面 + 新兼容面；v1 的真实需求（展示 + 触发）用「模块算好字段」即可覆盖（§5.2） |

## 12. 验收

A1–A13 要求真实执行；截图与手写记录不算证据（沿用 RFC-0004 口径）。落点为计划路径。

| # | 断言 | 落点 |
|---|---|---|
| A1 | 页面含 `"script"` 或 `"on_click"` → 注册期该页被剔除（模块其余能力仍在）、渲染期整页拒渲、卡片 `MANIFEST_INVALID`，且**无任何代码执行**（写文件探针验证文件不存在） | `core/tests/registry/test_ui_pages.py` / `modulepage/RejectTest.kt` |
| A2 | 未知 `type`（`grid`）/ 未知 `component`（构造的 `Foo` 与四个禁用组件逐个）/ 未知动作（`http.request`）/ `navigate` 到未声明页 → 全部整页拒渲，错误卡给出节点定位 | 同上 |
| A3 | `risk=high`：第一阶段返回 `approval_required` 且模块侧执行计数 0；`approve` 后第二阶段执行、计数 1；`deny` 后仍 0 且返回 `denied`；`scope:"session"` 只对同工具生效 | `core/tests/ui/test_tool_call.py` |
| A4 | 审批后第二阶段改 `args` → `PERMISSION_DENIED`，工具未执行（摘要绑定） | 同上 |
| A5 | 无 `ui.pages` → 桌面无入口、手机能力 tab 无「模块页面」项，且**无空行 / 空 tab / 占位页**；2 页 → 桌面 2 入口、手机 2 项 | `modulepage/EntryTest.kt`（三端截图对照） |
| A6 | 卸载或换成无页面模块 → 入口消失、已开页面关闭回入口，无残留空白页 | 同上 |
| A7 | 绑定 `session.messages` → 空值 + 一次性提示、**0 新增 RPC 帧**、不回显会话正文 | `core/tests/ui/test_bindings.py` |
| A8 | 深度 9 / 节点 257 / 内联 9 KiB → 拒渲；恰好 8 / 256 / 8 KiB → 正常渲染（两侧都验） | `modulepage/LimitsTest.kt` |
| A9 | 模块断连 → 「模块离线」、动作禁用、不白屏，重连后 `page.state` 自动刷新；渲染 >5s → 可重试卡片、宿主可返回、其他页面可开 | `modulepage/OfflineTest.kt` |
| A10 | `ui/submit` → 走既有 `ui/submit`，消息与手打消息同队列同审计；显式只读会话 → `READ_ONLY_SESSION` 提示、不崩 | `core/tests/ui/test_submit.py` |
| A11 | 同一 schema 在 `material` / `miuix` / `glass` 下结构一致（节点数、顺序、文案逐项相等），仅视觉不同；页面代码 `material3` import 数为 0 | `modulepage/SkinParityTest.kt` + lint |
| A12 | 深链三端落同一模块同一页面；未命中 → 回落入口 + toast；手机返回落回「能力」tab 且滚动位置不变 | `modulepage/RouteTest.kt` |
| A13 | `version: 2`（客户端支持 {1}）→ 入口不出现 + 模块卡片一行说明；`ui` 缺失 → 旧客户端忽略、新客户端无入口 | `core/tests/registry/test_ui_pages.py` |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿（写本文件时已实测；新文件不参与 C1–C10 计数）。

## 13. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **内联 schema vs ref？** | 本 RFC 双轨（内联 ≤8 KiB / ref ≤64 KiB），倾向**默认内联**（页面小、无额外 RTT、无摘要路径）。若实现发现 8 KiB 装不下真实页面（40+ 节点就容易超），应把默认改成 ref 并把 `ui/page.get` 提前成必选 |
| Q2 | **版本协商放哪？** | 本 RFC 只定义「超支持集 → 入口不出现」。落地三选：①`ui/attach` 上报支持集（C-05 加可选字段）；②core 注册期按自身支持集裁剪 `ui.pages`（多端版本不一致时会误伤）；③纯客户端判定（现状）。倾向 ① |
| Q3 | **列表分页** | 本 RFC 只给「≤512 项 + 窗口 ≤50」。三选：①模块自己在 `state` 里给窗口（最简单，但模块要管滚动位置）；②`ui/state.get` 加分页参数（C-05 追加）；③`list` 加 `page_size` + 客户端按需再取。倾向 ① 先跑，等真实页面项数分布再定 |
| Q4 | **模块页面能否嵌附件组件** | 本 RFC：`AttachmentChip` 只读（能显示 `file_id`，不能上传、不能发 blob 请求）。若允许选文件上传，要接 `POST /ui/blob` 的设备密钥 + 分片 + 会话上下文（RFC-0004），且页面没有输入区承载进度。倾向 v1 只读，需要上传的模块 `navigate` 到会话输入区 |
| Q5 | **FR-8.78 的「未知节点降级为占位」保留吗？** | 本 RFC 选 fail-closed（§7.2）并记为对 FR-8.78 的**收窄**。若需求方坚持原句，退路是「节点级占位 + 页顶 banner + 动作全禁用」，但要同时接受「用户看到半张页面」，且要定义占位符高度（否则布局会跳） |
| Q6 | **页面审批要不要跨端可见？** | 本 RFC 只给发起端（同步 RPC + `approval/decide`）。要求「另一台设备也能批」就得上 `ui.call.*` 事件族 + 快照（§9.4 的成本），并解 `turn_id` 缺失时的归属显示。倾向不做 |
| Q7 | **多语言** | `title` / `text` 现在都是单语字符串。要 i18n 的最小改法是 `title: {"default": "...", "zh-CN": "..."}`，但那会让 `title` 变联合类型，冻结后再改是破坏性的。倾向 v1 明确单语，并写进 C-19 的「v1 明确不做」 |

## 14. 影响面与后续

**实现阶段要动**：`schema/manifest.schema.json`（§9.2 的 `ui` / `page` 块 + `x-vade.appends`）、`core/vade/registry/manifest.py`（`Manifest.ui` + 逐页校验 + 坏页剔除）、`core/vade/rpc/handlers/ui*.py`（三个新方法；`ui/tool.call` 接 router 与 approval store）、`core/vade/router/dispatch.py`（页面发起的 `tools/call` 复用同一入口，不新建第二条派发路径）、`schema/rpc.json`（§9.3 五行）、模块 SDK（`state/get` / `page/get` 的默认 `UNSUPPORTED` 应答）、`ui/composeApp/src/commonMain/kotlin/core/modulepage/`（解析器 + 校验器 + 4 原语 + 组件映射 + 入口）、`ui/.../core/destinations/`（右面板顶部入口、能力 tab 列表项）。

**明确不动**：`schema/events.json`（§9.4）、`schema/errors.json`（§9.1 #7）、`core/vade/policy/*`（风险判定沿用）、`core/vade/session/*`（`ui/submit` 复用现路径）、`docs/agents/01-contracts.md`（C-19 冻结时登记）、`design.md` §9.2（C-09 签名不动，C-19 只引用）。

**方案层待同步**（§8.22.6 #2 已列，本文件不代改）：`design.md` 的 UI 章节要补「模块页面」这一层（`core/modulepage/` 的定位、入口位置、与右面板的关系），否则方案里 U1 的目录结构仍只有 `core/screens` 与 `shell/`。

**后续（不在本 RFC）**：C-09 的 11 组件冻结（D93，U1 W4 D3 前，必须先于 C-19，因为白名单引用它的签名）；C-19 冻结后登记 01-contracts（18 → 19）并同步 `docs/README` 的契约计数口径；`ToolCallCard` 的 `waiting_approval` 态与 RFC-0008 的 `waiting_input` 在两处都要一致；页面可访问性（TalkBack 顺序、200% 字号）挂 U1-g。

**参考**：`docs/requirements.md` §8.22.1（D104 / D105）、§8.22.3、§8.22.4、§8.22.6 #2 / #4、§8.22.8 #3；`docs/agents/50-U1-ui-skeleton.md` §五 U1-h、§七 T5c；`schema/manifest.schema.json`（C-03 现状）；`schema/rpc.json`（`ui_to_core` 6 方法 / `core_to_module` 5 方法）；`docs/rfc/RFC-0001-manifest-subscriptions.md`（C-03 追加的写法与教训）；RFC-0004 / RFC-0007（章节与验收风格）。
