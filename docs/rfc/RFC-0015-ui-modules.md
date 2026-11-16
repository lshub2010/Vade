---
rfc: RFC-0015
title: 模块清单通道：`ui/modules`（一次拉全量模块与页面入口、`revision` 单调、`module.status` 作失效边）
status: proposed
date: 2026-11-14
proposer: 编排（依据 A3 缺口：客户端 `RealModulePageSource.declarations()` 只能返回空列表；core 内 `modulepage/service.py:202 pages()` 已有数据却没有任何 wire 出口；`ui/attach` 快照与 34 个 `ui_to_core` 方法均无模块清单）
affects: C-05 `schema/rpc.json` 的 `ui_to_core` **追加 1 个只读方法** `ui/modules`（声明在 `directions` 内；登记块已由 `s-rfc0015-register` 转正进 `x-vade.appends` 第 11 块、块版本 **1.6.0**，顶部 version 1.5.0 → **1.6.0**，见 §9.6）；落地时要动 `core/vade/modulepage/service.py`（加 `list_modules()`）与 `core/vade/modulepage/rpc.py`（加一个 handler）；**不改** C-06（无新事件）、**不改** C-04（无新错误码，仍 34）、**不改** C-03（`manifest.schema.json` 一行不动：`display_name` 与 `ui.pages[]` 早在 RFC-0001/0009 里就齐了）
related: RFC-0009（C-19 的 `ui.pages[]` 声明、`ui/page.get` / `ui/state.get` 的三坐标、§9.4「页面审批不新增事件」的同一条纪律）；RFC-0005（`model/list` 的 `revision` 语义、`if_revision` 的 `not_modified` 坑「客户端不得清缓存」）；RFC-0010（`session/list` 的「轻行 + 拉取式」形态：列表归列表、正文归正文）；RFC-0012（required-with-null 的自描述写法，本 RFC 的 `display_name` / `version` 沿用）
type: C-05 追加（1 个只读方法）；不改 C-04 错误码、不改 C-06 事件族、不改 C-03 manifest、无破坏性改名、无客户端强制升级
stage: 提案（core 与契约登记**均已落地**：`s-modules-impl` 实现 core，`s-rfc0015-register` 登记契约；客户端未接线，见 §9.6）
---

# RFC-0015 · 模块清单通道

## 1. 摘要

`ui/page.get` 要 `(module_id, instance_id, page_id)` 三个坐标，才能取回一页的节点树。可客户端**今天没有任何办法知道有哪几个模块、每个模块有哪几页**——不知道坐标，三坐标方法等于不可调用。A3（模块页入口）因此一路卡在「客户端只能返回空列表」。

本提案补的是**一条只读的清单通道**：`ui/modules` 一次返回整份清单（模块 → 页面入口），字段**逐字复用** `ModulePageService.pages()` 与 C-03 manifest 的既有键名，页面**正文不过线**；`revision` 单调，让重复拉取可以是 no-op。**不新增事件、不新增错误码、不改 manifest schema**：变化通知复用已经存在的 `module.status` 作失效边（§5）。

| # | 决定 | 理由 |
|---|---|---|
| 1 | 新增 `ui/modules`（`ui_to_core`），**一次返回全量**，不设分页 | 入口列表要「一上车就能渲染」，分页会把「有没有页面」变成一串串行往返；单模块投影 ≈ 1 KB，10 模块 ≈ 10 KB，远低于 65 519 B 帧上限（§4.6） |
| 2 | **params 为 `{}`，永不带 `session_id`** | 模块集合是**进程级**事实（`Registry.snapshot()` 不接会话）；鉴权只来自 connection（`modulepage/auth.py` 的 B1 教训：参数是攻击者能控制的东西）；「这页要不要会话」已由页面级 `requires:["session"]` 表达（§4.5） |
| 3 | 页面行键名 = `ModulePageService.pages()` **原样**（`id` 而不是 `page_id`） | 三层已经对齐：C-03 `$defs.page.id`、`pages()` 的 `"id"`、客户端 `PageDeclaration(pageId=…)` 从 `id` 读。新造 `page_id` 会立刻制造两套拼写（§4.3，**这是对任务书建议形状的一处显式偏差**） |
| 4 | 页面**正文**不过线，`ui/page.get` 仍是唯一正文通道 | 清单要能一次给全（含离线模块），正文要按需、可校验（ref 页的 sha256 在 manifest 里钉住）。混在一起等于把 48 KiB `ui` 块塞进一个列表响应 |
| 5 | 变化通知 = **已有 `module.status` 作失效边 + `revision` 作去重**，**不新增事件** | `_publish_status` 的 fire 点集合恰好等于「清单可能变化的集合」（§5.2 逐个列出）；新事件要付整套 C-06 生命周期，而本轮**连声明都过不了门禁**（§9.3 实测） |
| 6 | 只复用 C-04 现有码：`PERMISSION_DENIED` / `UNAVAILABLE` / `PAYLOAD_TOO_LARGE` | 新增错误码会打破「34 码」台账（`tools/check_contracts.py:1540` 硬钉 `len(codes)==34`），收益为零（§6） |
| 7 | `display_name` / `version` **required-with-null** | C-03 里 `display_name` **不是** required（required 只有 `module_id`/`instance_id`/`version`/`protocol`/`capabilities`/`limits`），硬要求非空会拒掉合法模块；RFC-0012 §5.2 的 required-with-null 是既有写法 |
| 8 | 客户端必须显式处理 `-32601` 降级 | 旧 core 的 dispatch 表里没有这个方法，它回的是 JSON-RPC `-32601`，**不是** `UNSUPPORTED`（RFC-0006 §6.5 的同一先例）。不降级 = 能力页弹错误（§7） |

## 2. 动机与证据

### 2.1 `ui/attach` 的快照不带模块清单

`schema/rpc.json:421-425` 的 `ui/attach` 把快照里有什么写得很全：`snapshot.session`（含 `model`）、`snapshot.messages[]`（含 `parts` / `partial`）、`snapshot.tasks` / `tasks_truncated`、`snapshot.pending_inputs[]`、`pending_approvals[].task_id`——**没有 modules**。core 侧拼装点 `core/vade/session/manager.py:767-785` 的字典键也逐个对得上：`from_seq` / `session` / `messages` / `turns` / `queue` / `pending_approvals` / `pending_inputs`（`tasks` 在 `:793` 条件追加）。**快照是会话视图，模块注册表不是会话的一部分**——这不是遗漏，是分层：`ui/attach` 的参数是 `session_id`，而模块集合与任何会话无关。

### 2.2 34 个 `ui_to_core` 方法里没有任何清单拉取

`python -c` 从 `schema/rpc.json` 重算：`module_to_core` 6 / `core_to_module` 7 / `ui_to_core` **34**，合计 **47**。34 个方法里最接近的是 `ui/page.get` / `ui/state.get` / `ui/tool.call`——**三个都要求先知道 `module_id` + `instance_id` + `page_id`**（`rpc.json:883-965`）。也就是说：协议里有一条「怎么读一页」的路，却没有一条「有哪些页」的路，前者的前置条件在后者的缺席下不可满足。

### 2.3 数据在 core 内部是齐的，缺的只是出口

- `core/vade/modulepage/service.py:202-221` 的 `pages(module_id, instance_id)` 已经把一页的入口信息拼好了：`id` / `title` / `icon` / `entry` / `version` / `requires` / `refresh_s`（`requires` 只在 `requires_session` 为真时给 `["session"]`）。它甚至写明了 FR-8.79 的判据（空元组 = 不显示入口）。
- `core/vade/modulepage/rpc.py:31-37` 的 `handlers()` 只注册 **3** 个名字（`ui/page.get` / `ui/state.get` / `ui/tool.call`）。`pages()` 因此**没有调用者**。
- 管理面**故意**不透传：`core/vade/registry/api.py:23-24` 的 docstring 是「Whitelist only. The manifest body is never forwarded」，`_node_view` 只给 `ui_page_count`（`:44`）与 `ui_page_warnings`（`:45`）；连 `display_name` 都不在里面。admin 面不是这条通道的替代品，也不该是。
- `Manifest.display_name`（C-03 顶层，`minLength:1, maxLength:64`）与 `Manifest.ui_pages` 都在 `core/vade/registry/manifest.py` 的 dataclass 上——**两样数据都在手里，只差一个 `dict`**。

### 2.4 `module.register` 是 RPC 不是事件；`module.status` 只有 4 个字段

`rpc.json:221-236` 的 `module/register` 在 **`module_to_core`** 方向，`core/vade/app.py:229` 是它的 handler（`if method == "module/register": return app.register_module(...)`）。它**不是** core→UI 的事件，UI 从来看不到它。

core→UI 侧关于模块的**唯一**事件是 `module.status`（`events.json`，`payload` = `module_id` / `instance_id` / `online` / `version`，`required` = 前三个）。它的生产者 `core/vade/session/status.py:1-13, 55-61` 把自己的定位写死了：「`module.status` **is a trigger for the admin surface to re-pull**」——**触发信号，不是数据通道**。`registry/table.py:143-149` 的 `_record_status` 也只给那 4 个字段。

> 客户端 `RealModulePageSource.kt:47-53` 的 KDoc 把这件事记成了「声明只随 `module.register` 事件到达」——**这个表述是错的**（没有那个事件），但它的结论（「没有 RPC、没有可解码的事件，任何非空返回都只能是臆造」）与本节的四条证据一致。本 RFC 顺手把这个误记订正掉：不是「事件没解码」，是**根本没有那条事件**。

### 2.5 客户端三处现状（A3 的现场）

| 位置 | 现状 | 后果 |
|---|---|---|
| `RealModulePageSource.kt:57` | `declarations(): List<ModuleDeclaration> = emptyList()` | 入口集合恒为空 |
| `RealModulePageSource.kt:68` | `status(moduleId) = ModuleStatus(online=false, version=null)` | 恒显示「模块离线」 |
| `CapabilitiesScreen.kt:47` | `remember(pageSource) { ModulePageEntryRules.build(pageSource.declarations()) }` | **即使数据到了也不会重组**：`pageSource` 是稳定对象、`declarations()` 是普通函数，Compose 没有任何理由重算（§8.3） |

客户端已经写好的零件恰好是缺口的另一半：`vade.ui.data.ModuleRegistry` 有 `declare(manifest)`（吃 C-03 manifest 原文）与 `applyStatus(moduleId, instanceId, online, version)`，`declarations()` / `status()` 与 `ModulePageSource` 的接口**逐字段一致**，其 KDoc 原话是「等 RFC 补上通道（`ui/modules` 或 `snapshot.modules[]`），调用方把 manifest 原样喂进来即可，本类与入口规则一行都不用改」。本 RFC 选 `ui/modules` 而不是 `snapshot.modules[]`（理由见 §11）。

### 2.6 结论

A3 **不可能靠客户端修**：缺的是协议面上的一条读取通道。三坐标方法已经有了，缺的是坐标来源；数据已经在 core 内存里，缺的是一个 wire 出口。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 一条只读 RPC 一次给出「哪些模块、每个模块哪些页」，足够客户端首屏渲染入口（模块显示名 › 页面标题） |
| G2 | 字段名与 C-03 manifest、`ModulePageService.pages()`、客户端 `ModuleDeclaration` **三方逐字一致**，客户端映射不改类型 |
| G3 | 页面**正文**不过线；清单里不出现任何节点树、脚本或 `schema` 键 |
| G4 | 变化可被发现：客户端在 `module.status` 到达后重拉一次即可看到新增/消失的模块与页面，**不需要重启**；未变化时重复拉取不产生副作用（`revision` 不变） |
| G5 | 旧 core 上**优雅降级**：`-32601` → 入口区块不渲染（今天的形态），不弹错误、不重试风暴、不假装有数据 |
| N1 | **不做**按会话裁剪的清单（模块集合与会话无关，§4.5） |
| N2 | **不做**清单内带页面正文/`sha256`（正文仍走 `ui/page.get`，ref 页的摘要校验留在那里） |
| N3 | **不做**分页/游标（一次全量；到 60 模块级再谈，§4.6） |
| N4 | **不做**模块启停、角色、权限、工具计数等 admin 字段（那是 `registry/api.py` 的管理面，鉴权与裁剪规则不同） |
| N5 | **不新增**事件、错误码、manifest 字段；**不改** `manifest.schema.json` |

## 4. 方法与字段表

### 4.1 签名

```
method : ui/modules          方向: ui_to_core       只读、无副作用
params : {}                  （空对象；未知键忽略，为前向兼容）
result : {revision:int, modules:[module]}
errors : PERMISSION_DENIED | UNAVAILABLE | PAYLOAD_TOO_LARGE
```

```json
{
  "revision": 42,
  "modules": [
    {
      "module_id": "download",
      "instance_id": "dl-1",
      "display_name": "下载管理",
      "version": "0.3.1",
      "online": true,
      "pages": [
        {"id": "queue", "title": "下载队列", "icon": "list", "entry": "auto",
         "version": 1, "requires": [], "refresh_s": 0},
        {"id": "settings", "title": "下载设置", "icon": "settings", "entry": "auto",
         "version": 1, "requires": ["session"], "refresh_s": 0}
      ]
    },
    {"module_id": "notes", "instance_id": "no-1", "display_name": "笔记",
     "version": "0.1.0", "online": true, "pages": []}
  ]
}
```

### 4.2 字段表

**模块行**（顺序 = 注册表 `snapshot()` 顺序 = 首次注册顺序）

| wire 键 | 类型 | 必填 | core 来源 | 客户端接收 |
|---|---|---|---|---|
| `module_id` | string | 是 | `NodeRecord.module_id` = C-03 `manifest.module_id` | `ModuleDeclaration.moduleId` |
| `instance_id` | string | 是 | `NodeRecord.instance_id` = C-03 `manifest.instance_id` | `ModuleDeclaration.instanceId` |
| `display_name` | string\|null | 是（可 null） | C-03 `manifest.display_name`（**C-03 里可选**，故此处可空） | `ModuleDeclaration.moduleTitle`（null → 该节点不进入口，`ModuleRegistry.isDeclared`） |
| `version` | string\|null | 是（可 null） | C-03 `manifest.version`（SemVer 2.0.0）；`manifest is None` 的记录不列出（§4.5） | `ModuleDeclaration.moduleVersion` |
| `online` | bool | 是 | `record.health is HealthState.ONLINE and record.connection_id is not None`（`registry/table.py:143-149`，**与 `module.status` 同一式子**） | `ModuleDeclaration.online` / `ModuleStatus.online` |
| `pages` | array | 是（可为空） | `ModulePageService.pages(module_id, instance_id)`（`service.py:202-221`） | `ModuleDeclaration.pages` |

**页面行**（顺序 = manifest `ui.pages[]` 顺序 = 注册期通过 C-19 校验的顺序）

| wire 键 | 类型 | 必填 | core 来源 | 客户端接收 |
|---|---|---|---|---|
| `id` | string | 是 | `UIPage.page_id`（C-03 `$defs.page.id`，`^[a-z][a-z0-9_-]{0,31}$`） | `PageDeclaration.pageId` |
| `title` | string | 是 | `UIPage.title`（1–40 字符） | `PageDeclaration.title` |
| `icon` | string | 是 | `UIPage.icon`，`list\|chart\|settings\|tool\|file\|none`；C-19 默认 `none`，**未知值已在注册期回落**，这里不再有未知值 | `PageDeclaration.icon`（`PageIcon`） |
| `entry` | string | 是 | `UIPage.entry`，`auto\|panel\|capabilities`，默认 `auto` | 未消费（v1 按 manifest 顺序渲染；`entry` 的桌面/手机差异是宿主规则，Q4） |
| `version` | integer | 是 | `UIPage.version`（≥1） | `PageDeclaration.version`（`> SUPPORTED_PAGE_VERSION` → 入口不出现，§8.2） |
| `requires` | array | 是 | `["session"]` 当且仅当 `UIPage.requires_session`，否则 `[]` | `PageDeclaration.requiresSession`（`any { it == "session" }`） |
| `refresh_s` | integer | 是 | `UIPage.refresh_s`（≥0，默认 0） | 未消费（v1 不自动轮询页面状态，Q3） |

字段级硬约束：`pages[]` **必须**是 `pages()` 的逐字投影（键名、枚举、默认值）。落地时 `list_modules()` 直接 `return {"id": page.page_id, ...}` 复用同一段拼装，不要第二次手写——两份拼装迟早漂移（RFC-0009 §9.2 的「schema 有、`Manifest` 丢」就是这个坑的上一代）。

### 4.3 为什么是 `id` 而不是 `page_id`（对任务书建议形状的显式偏差）

任务书的建议结果是 `pages:[{page_id, title, icon?, kind?}]`。落地前查了三处，**三处都用 `id`**：

| 层 | 键 | 位置 |
|---|---|---|
| 契约（C-03 / C-19） | `id` | `schema/manifest.schema.json` `$defs.page.required = [id, title, version, schema]` |
| core 投影 | `id` | `core/vade/modulepage/service.py:212` `{"id": page.page_id, ...}` |
| 客户端解码 | `id` | `ModuleRegistry.kt:174-178` `val id = page.mustStr("id")` → `PageDeclaration(pageId = id, …)` |

`kind` 在 C-19 里没有对应物（节点才有 `type`；页面级只有 `icon` / `entry`），`icon` 在 `pages()` 里**恒有值**（`UIPage.icon` 默认 `"none"`），所以不是可选字段。按「不要自创名字」这条纪律，本 RFC 用 `id` / `entry` / `refresh_s`，**不引入 `page_id` / `kind`**。客户端不需要任何改动：它本来就从 `id` 读。

### 4.4 两个 `version` 不要混

- 模块行 `version` = **模块**的 SemVer（C-03 `manifest.version`，`0.3.1`），来自注册帧，用于模块卡片展示与「模块换了版本」的观测。
- 页面行 `version` = **页面 schema** 的 C-19 修订号（整数，≥1），是入口门控的判据（`> SUPPORTED_PAGE_VERSION` → 入口不出现 + 一行说明，RFC-0009 §8.2 / A13）。

两者同名不同义是 C-03/C-19 的既有事实，本 RFC 不改名（改名会同时破坏 manifest 与 `pages()`），只在 schema 的 `notes` 里点明。

### 4.5 顺序、多实例、离线和裁剪

| 维度 | 规则 | 依据 |
|---|---|---|
| 模块顺序 | `Registry.snapshot()` 顺序（首次注册顺序，稳定） | `registry/table.py:380`；客户端 `ModuleRegistry` 的节点顺序也是首次出现顺序（`ModuleRegistry.kt:95-99`） |
| 页面顺序 | `manifest.ui.pages[]` 原样（注册期已按 C-19 逐页校验、坏页剔除） | RFC-0009 §7.1「单页剔除」 |
| 多实例 | 每个 `(module_id, instance_id)` **一行**，不按 `module_id` 合并 | `module.status` 每帧都带 `instance_id`；`ui/page.get` 的三坐标里也有它；客户端 `ModuleRegistry` 的身份键就是二元组 |
| `manifest is None` 的记录 | **不列出**（没有声明可展示；`pages()` 对此也抛 `NOT_FOUND`） | `service.py:186-188` |
| `enabled = false` | 浏览器调用者**看不到**（与 `require_visible` 对 enabled 的要求一致）；模块自身调用者能看到自己 | `modulepage/auth.py`（browser 只能读 **enabled** 模块） |
| 离线模块 | **照常列出**，`online=false`，`pages` 保留 | FR-8.79 要求离线是卡片/横幅，不是入口消失（RFC-0009 §5.4） |
| 无 `ui.pages` 的模块 | 列出，`pages: []` | 空数组 = 无入口（客户端 `ModulePageEntryRules` 因此不产生分组，**不是**空列表占位） |

### 4.6 大小与帧上限

单模块投影 ≈ 1 KB（8 页 × ~100 B + 行头），10 模块 ≈ 10 KB，远低于 65 519 B（C-01 §7.5）。规则：

- `pages` 每模块 ≤ 8（C-19 `maxItems`，已冻结），页面行不含正文，故无「一页撑爆一帧」的路径。
- 极端情况（>约 60 个模块实例，白名单手工配对下不可达）core **必须**回 `PAYLOAD_TOO_LARGE`（C-04 既有码，`data = {field:"modules", limit:65519, actual:N}`，沿用 RFC-0006 §8 的 `data` 形态），**不静默截断**——截断会让「某模块没入口」无从解释。
- 分页/游标是**未决**（Q1）：真到那个量级再按 `task/list` 的 keyset 形态追加，不作为 v1 的形状。

## 5. 变化通知：三个方案与代价

问题：模块会注册、会 `capabilities.update`（改 `ui.pages`）、会断连重连。客户端怎么知道该重拉？

### 5.1 候选与代价

| 方案 | 做法 | 代价 | 判断 |
|---|---|---|---|
| **(a) 新增事件 `module.declared`** | C-06 加一个 topic，core 在注册/更新/注销时发 `{revision, module_id}` | ① C-06 要开一个新 topic：`events/subscribe` 的 `EVOLVE_TOPICS`、replay/seq、`module/event` 白名单、快照语义全要定；② 模块清单是**进程级**事实，事件却按会话扇出（`session/status.py:55-61` 每个 attached session 一份），等于把全局事实做 N 份副本；③ **本轮无法落地**：`tools/check_contracts.py:1618-1619` 的 `declared events nobody emits` 扫描要求每个声明的事件都有 emit 点，core 不实现就必红（§9.3 有实测）；④ 客户端要再加一个 collector 与一个事件解码分支 | 否（留 Q2） |
| **(b) 纯轮询 `revision`** | 客户端每 30 s 拉一次 `ui/modules` 比 `revision` | ① 不变化时也在打帧；② 首个变化的最坏延迟 = 轮询周期；③ 移动端/省电策略会把它掐掉，静默过期 | 否（作为降级路径保留，§7） |
| **(c) 扩 `module.status` payload** | 在现有事件里加 `display_name` + `pages[]` | ① 一个「在线态翻转」的事件携带整份页面清单，健康检查抖动时会把清单反复写进每会话 seq 日志；② C-06 的 `required` 已冻（3 字段），加可选键意味着客户端**不能**假定「有 status 就有清单」，仍要回落到拉取；③ 与生产者的自我定位背离——`session/status.py` 明写它是 **trigger to re-pull**，把它变 snapshot 就要新限额、新分页 | 否 |
| **(d) 选定：已有 `module.status` 作失效边 + `revision` 去重** | 收到 `module.status` → 拉一次 `ui/modules`；`revision` 未变则丢弃结果（不重组） | 见 §5.3 | **是** |

### 5.2 为什么 (d) 在语义上就是对的

`module.status` 的 fire 点集合**恰好**等于「清单可能变化的集合」。`registry/table.py` 里每次 `_bump()`（revision +1）旁边就是 `_publish_status(...)`：

| 位置 | 触发 |
|---|---|
| `:214/:216` | `register()` —— 新模块（可能带 `ui.pages`） |
| `:338/:341` | `capabilities.update()` —— 重提交 manifest（`ui.pages` 可能变） |
| `:404/:405` | 注销/断连 |
| `:424/:425` | 健康态翻转（`online` 变） |
| `:448/:450`、`:502/:504` | 批量 drain |

所以「revision 动了」⇔「发过 `module.status`」，两者是同一集合的两种观察。既然失效边已经有了、也已经在 C-06 里冻着、客户端也已经有 collector（`RealSessionSource.kt:144-158` 的 `created.events.collect`），**再开一个 topic 只是把同一条边说第二遍**。

### 5.3 选 (d) 的代价（必须写清，别当成免费）

1. **噪声失效**：一次只改工具的 `capabilities.update` 也会触发重拉（每模块每次 1 帧，≈1 KB）。可接受；要省就得给事件加「改的是不是 ui」的位，那才是真的加字段。
2. **同字段重复**：`module.status` 的 4 个字段可能一字未变（断连后立刻重连、`version` 不变）。客户端**不能**按「字段有没有变」diff，必须**收到就重拉**——这是选 (d) 唯一的纪律成本，写进 §8 的接线要求。
3. **无 sink 时不落盘**：`session/status.py:9-13` 明写「Sessions without sinks are skipped」，所以**首次 attach 必须主动拉一次** `ui/modules`，不能等事件（事件只覆盖「连上之后的变化」）。
4. **`revision` 只在同一 core 进程内可比**：它是内存计数器（`table.py:122` 初始化 0），core 重启归零。客户端**必须**在重连后无条件接受第一次拉取的 `revision` 作新基线；看到比本地小的值只能判定「换了 core」，重拉而不是丢弃。
5. **事件与拉取的顺序**：先到达的 `module.status` 可能对应一次已经过去的变更；`revision` 让乱序无害（拉到的 revision ≤ 本地缓存 → 丢弃，或按 §5.3-4 处理重启）。

### 5.4 不给 `module.status` 加任何字段

加字段的诱因是「省一次重拉」。代价是 C-06 的 required 冻结被打开一道口子，且 `_publish_status` 的 payload 会从「状态 4 元组」变成「状态 + 可变长清单」——一个每设备每次健康抖动都要过一遍 seq 日志的负载。**不做。**

## 6. 错误处理（只用现有 34 个码，不新增）

| 情况 | 返回 | 客户端表现 |
|---|---|---|
| 调用者身份无法解析（`device_id` 空/未配对/形状不识别） | `PERMISSION_DENIED` | 不显示入口区块；连接态本身已在别处报错（`modulepage/auth.py` 的 `require_caller`） |
| registry 未装配（core 无注册表组件） | `UNAVAILABLE` | 「模块清单不可用」+ 重试；**不白屏**（与 RFC-0009 §5.4 同一姿态） |
| 模块实例数超帧上限（理论） | `PAYLOAD_TOO_LARGE`（`data={field,limit,actual}`） | 入口区块显示不可用 + 重试；**不静默截断** |
| 旧 core 没有这个方法 | JSON-RPC **`-32601`**（不是 C-04 码） | **必须降级**：等同「没有清单通道」→ 入口区块不渲染（今天 `declarations()=emptyList()` 的形态），**不弹错误、不进重试循环**，下次重连再试一次（§7.1） |
| 只读会话 | **正常返回** | `is_read_only` 只拦写；本方法是读，不该被 `READ_ONLY_SESSION` 拦（与 `session/list` 一致） |

`params` 是 `{}`，未知键忽略（前向兼容，与 `session/group.list` 同姿态），因此**不需要** `INVALID_ARGUMENT` 这条路径。

## 7. 兼容性

### 7.1 旧 core + 新客户端

dispatch 表里没有 `ui/modules` → `-32601`。客户端处理：

```
第一次拉取失败且 code == -32601 → 记「无清单通道」（进程内一次）
  · declarations() 回落空列表 → 入口区块不渲染（FR-8.79 的正确降级，与今天等价）
  · 不重试、不 toast、不显示错误卡（这是「core 较旧」，不是故障）
  · 重连（新的 connection epoch）后清掉标记、再试一次
```

RFC-0006 §6.5 的先例原文：「clients get JSON-RPC -32601, not UNSUPPORTED」——同一个处理口径。

### 7.2 新 core + 旧客户端

旧客户端从不调用它，行为不变（多一个方法对它不可观测）。C-05 的追加不构成强制升级。

### 7.3 新 core + 新客户端

- 模块无 `ui.pages` → `pages: []` → 无入口，**不出现空 tab / 空列表 / 灰行**（FR-8.79 判据不变）。
- 页面 `version` 高于客户端支持集 → 入口不出现 + 模块卡片一行说明（RFC-0009 §8.2，判定仍在客户端，本方法只给 `version`）。
- 模块离线 → 入口保留、点开显示「模块离线」、动作禁用（RFC-0009 §5.4）。

## 8. 客户端接线（三处，逐行可核）

> 客户端仓 `vade-client` 不在本 worktree 的可改范围；本节是**接线要求**，不是已落地的代码。

### 8.1 `RealSessionSource.kt:144-158`：事件 collector 加一条失效分支

现状：两个 collector（`created.state` / `created.events`）在 `connect()` 之前起，`mapEvent(event)?.let { _events.emit(it) }`。要求：

- 在**同一个** `created.events.collect { }` 分支里（`mapEvent` 之前或之后皆可），若事件是 `module.status` → 置一个 `invalidate` 标志（`MutableStateFlow<Int>` 自增或 `Channel(CONFLATED)`），由**一个**串行消费协程去拉 `ui/modules`（不要每个事件一个请求；`CONFLATED` 保证抖动只留最后一次）。
- 首拉时机：`state` 到达 `Connected` **之后**、与 `ui/attach` 并行（不要串行等 attach——入口列表不依赖会话）。
- 拉到结果后：对每个模块行 `ModuleRegistry.applyStatus(module_id, instance_id, online, version)`，并（当 `display_name != null` 时）`ModuleRegistry.declare({module_id, instance_id, display_name, version, ui:{pages:[…]}})`。
  - 为什么还要 `declare` 一层映射：`ModuleRegistry.declare()` 吃的是 **C-03 manifest 原文**（读 `ui.pages`），而 wire 给的是**投影**（顶层 `pages`）。映射是 6 行字面量，`declare()` 的严格解码与 `isDeclared` 规则**一行不用改**（其余键忽略，C-03 `additionalProperties: true`）。
  - 顺序：先 `declare` 后 `applyStatus`。`declare()` 对 `version` 是「已有值优先」（`ModuleRegistry.kt:148-157`），只有后写的 `applyStatus` 能让清单里的 `online`/`version` 生效。
  - `display_name == null` 时**跳过 `declare`**：`declare()` 用 `mustStr("display_name")`，null 会抛 `PayloadDecodeException`；而「没显示名就没有入口标题」（`ModuleRegistry.kt:68-69` 的 `isDeclared`）本来就是对的，不要替模块起名。
- 失败处理：`-32601` → §7.1；`UNAVAILABLE` / `PAYLOAD_TOO_LARGE` → 保留上一份快照，界面显示「清单不可用」，`release` 后按退避重试（≤1 次/30 s）。

### 8.2 `RealModulePageSource.kt:57/68`：两个 override 换数据源

```kotlin
override fun declarations(): List<ModuleDeclaration> = registry.declarations()   // 原 emptyList()
override fun status(moduleId: String): ModuleStatus = registry.status(moduleId)  // 原在线态恒 false
```

`ModuleRegistry.declarations()`（`ModuleRegistry.kt:208-227`）与 `status()`（`:232-243`）的返回类型与 `ModulePageSource` **逐字段一致**，它的 KDoc 就写着「接线就是一行」。注意 `status()` 按 `module_id` 聚合（任一实例在线即在线）——因为接口只有一个参数，这不是新语义。

### 8.3 `CapabilitiesScreen.kt:47`：**必须能观察 `revision`**，否则事件到了也不重组

```kotlin
// 现状：pageSource 是稳定对象，declarations() 是普通函数，Compose 没有重算理由
val entryIndex = remember(pageSource) { ModulePageEntryRules.build(pageSource.declarations()) }
```

要求（两种形态都接受，v1 建议前者）：

1. 新增一个窄接口 `ContainerRevisionSource { val revision: StateFlow<Int> }`（或给 `ModulePageSource` 加一个 `val revision: StateFlow<Int>`，接口变更需客户端拍板），屏幕里
   `val revision by sourceRevision.collectAsState()` + `remember(pageSource, revision) { build(...) }`；
2. 或由宿主把 `revision: Int` 作为参数传进 `CapabilitiesScreen`，`remember(pageSource, revision)` 同效。

不加这一步的后果：`ui/modules` 通了、`ModuleRegistry` 填了、能力页仍然显示「无模块页面区块」——A3 会以「数据到了但 UI 不动」的形态复发一次。**这是本 RFC 唯一一处「协议对了但必须改 UI 才成立」的耦合，写进验收 A7。**

## 9. 契约变更

### 9.1 本 worktree 落了什么

| # | 契约 | 变更 | 类型 |
|---|---|---|---|
| 1 | C-05 `ui_to_core` | 追加 `ui/modules`（`params`/`result`/`errors`/`notes` 四件齐全） | 追加 |
| 2 | C-05 `x-vade.rfc0015_pending` | 登记块（`contract`/`version 1.6.0`/`adds`/`notes`/`rewrites`/`landing`） | **pending，不是 append**（**已被 `s-rfc0015-register` 取代：块转正进 `x-vade.appends` 第 11 块，见 §9.6**） |
| 3 | C-05 顶部 `version` | **保持 1.5.0**（理由见 §9.2） | 不动（**已被 `s-rfc0015-register` 取代：1.5.0 → 1.6.0，见 §9.6**） |
| 4 | C-06 | **不动**（无新事件，事件数仍 34） | — |
| 5 | C-04 | **不动**（错误码仍 34） | — |
| 6 | C-03 `manifest.schema.json` | **不动**（`display_name` 与 `ui.pages[]` 已存在） | — |
| 7 | C-18 `config.schema.json` | **不动**（无新配置键） | — |

### 9.2 为什么这次不写进 `x-vade.appends`、也不升顶部版本

两条都是**门禁逼出来的**，不是偏好：

1. **`tools/check_ledger.py:203-205`**：`if has_appends: check(impl_state == "已实现")`；且 `:217-223` 要求 impl 路径在仓里真实存在。把 `ui/modules` 写进 `appends` 就等于在总账里**声明 core 已实现**，而这一棒明确不写 core。**实测**（把一个最小的 append 块临时塞进 `appends` 末尾，随后回滚）：

```
ledger: FAIL
  - aggregate.appends_rpc: LEDGER claims 10, contracts say 11
  - RFC-0015: contracts register appends for it, so impl_state must be 已实现, got '未实施'
  - RFC-0015.methods: LEDGER [] != contracts ['ui/modules']
  - RFC-0015.append_versions: LEDGER {} != contracts {'rpc.json': '1.6.0'}
```

**我不会为了让门禁变绿去填一个假的「已实现」。** 所以登记块放在 `x-vade.rfc0015_pending`（与既有的 `x-vade.rfc0005_append` 同一形态的自定义键），`appends` 仍是 10 块，LEDGER 里 RFC-0015 的 `impl_state` 写 **未实施**——这是真话。
2. **`tools/check_contracts.py` 三处版本钉**：`check_session_meta:1356`、`check_subagent_events:1096`、`check_session_search:1480`（`== "1.5.0"`）。**实测**（把顶部临时改成 1.6.0 后跑一次，随后回滚）：

```
contracts: FAIL
  - rpc.json: version must be 1.3.0 (RFC-0010), 1.4.0 (RFC-0012) or 1.5.0 (RFC-0014, the current head) (got '1.6.0')
  - rpc.json: version must be 1.5.0 after the RFC-0014 append (got '1.6.0')
  - rpc.json: version must be 1.4.0 (RFC-0012) or 1.5.0 (RFC-0014, the current head) (got '1.6.0')
```

本 RFC **不许改脚本**，所以顶部保持 1.5.0，1.6.0 作为 **pending 块的目标版本**记录。同一条纪律的既有先例：RFC-0005 的 append 块原文「The top-level `version` is NOT bumped by this RFC … an appender may not make a frozen file claim a header it does not own」。

### 9.3 为什么「新增事件」这一轮连声明都过不了

`tools/check_contracts.py:1618-1619`：

```
missing_handlers = schema_events - emitted_event_names() - pushed - {"turn.approval_required"}
check(not missing_handlers, "events.json: declared events nobody emits: …")
```

`emitted_event_names()` 扫 `core/vade/**/*.py` 的 `emit(...)` / `emit_batch(...)` 字面量。不写 core ⇒ 没有 emit 点 ⇒ 声明 `module.declared` 必红。这不是「门禁碍事」，它恰好证明了 C-06 的纪律：**事件表是「已经在发什么」的记录，不是愿望清单**。所以新增事件必须与 core 实现同批，而本 RFC 只做提案。

### 9.4 落地清单（实现那一棒照做，含门禁同步）

```
① core/vade/modulepage/service.py: 加 list_modules(connection) -> dict
     - 复用 pages()，不要第二次手写页面行（§4.2 尾注）
     - 裁剪按 modulepage/auth.py：browser 只见 enabled；module peer 只见自己
     - revision 取 registry.revision（table.py:135-141），不要新造计数器
② core/vade/modulepage/rpc.py: handlers() 加 "ui/modules": self.modules
③ schema/rpc.json: 删除 x-vade.rfc0015_pending；把同内容追加进 x-vade.appends 末尾
   （数组追加，既有 10 块原样）；顶部 version 1.5.0 → 1.6.0
④ tools/check_contracts.py: 三处版本钉随顶部版本上调（1356 / 1096 / 1480），
   并把 ui/modules 纳入某个既有 check_* 的断言（例如与 ui/page.get 同组）
⑤ docs/rfc/LEDGER.md: RFC-0015 行 impl_state 未实施 → 已实现，填 impl 路径与测试数；
   §1 表与 JSON 事实块同步（appends_rpc 10 → 11）
⑥ 客户端三处接线（§8）
```

### 9.5 不动 `manifest.schema.json`

`display_name`（`minLength:1,maxLength:64`，可选）与 `ui.pages[]`（≤8，`$defs.page`）都在 C-03 里，`Manifest` dataclass 也承接了它们。本 RFC 只是**读**，不需要任何 schema 追加——**再加一个字段只会制造第二个真相源**。

### 9.6 登记已落地（`s-rfc0015-register`）

§9.1 第 2/3 行、§9.2、§9.4 ③④⑤ 描述的是**提案那一棒**的状态（core 未写、门禁不许动，故 1.6.0 只能记在 `x-vade.rfc0015_pending` 里）。本节记录收口事实，上述各节保留原文作为历史：

| 动作 | 结果 |
|---|---|
| `x-vade.rfc0015_pending` → `x-vade.appends` | 块已转正为 `appends` **第 11 块**（`contract: C-05`、`version: 1.6.0`、`owner: C3`、`doc` 指向本文件、`adds: ["ui/modules"]`、`notes` 4 条、`rewrites: none`）；既有 10 块**逐字未动**；占位键已删除 |
| C-05 顶部 `version` | 1.5.0 → **1.6.0** |
| `tools/check_contracts.py` 三处版本钉 | **追加** 1.6.0（`check_subagent_events` / `check_session_meta` / `check_session_search`）；1.3.0/1.4.0/1.5.0 **全部保留**。咬合证据：把顶部临时改成 **1.7.0** 后 `check_contracts` 仍 FAIL 三条（原文见 `shots/s-rfc0015-register/RESULT.md` §5） |
| `docs/rfc/LEDGER.md` | RFC-0015 行 `impl_state` 未实施 → **已实现**；填 `ui/modules`、`append_versions={"rpc.json":"1.6.0"}`、impl 路径；`aggregate.appends_rpc` 10 → **11**；三方向方法数仍 **48**（`ui/modules` 早已计入 `directions`） |
| `core/tests` 头部版本断言 | 三处（`test_meta.py` / `test_rfc0013_partial_persistence.py` / `test_rfc0014_search.py`）随顶部版本同步为 `1.6.0`，与 RFC-0014 的 `6b568b9` 同一做法：只改版本号与相邻「谁拥有 C-05 head」注释，断言仍是**精确相等**（不是放宽成集合） |
| `schema/events.json` | **逐字节未变**（本 RFC 无事件） |

## 10. 安全与授权

| 面 | 规则 | 依据 |
|---|---|---|
| 身份 | 只来自 connection（Noise peer 的 `module_id#instance_id`，或浏览器 `ui-<hex>`），**永不来自 params** | `modulepage/auth.py`（B1 的教训：参数是攻击者能控制的） |
| 浏览器调用者 | 只能看到 `enabled` 的模块；看不到被禁用/被拒的节点 | `require_visible` 的既有语义 |
| 模块调用者 | 只看到自己那一行（枚举他人模块没有收益，且是信息泄露面） | 同 B1 |
| 未知调用者 | `PERMISSION_DENIED`，fail-closed | `require_caller` |
| 泄露面 | 只出 `module_id` / `instance_id` / 显示名 / SemVer / 在线位 / 页面 id·标题·图标·入口·版本·requires·refresh | 全部是「已声明的元数据」；**不含**副本身份、工具清单、`input_schema`、页面正文 |
| 注入面 | 零：**没有**新增输入（`params` 空），响应是纯数据（标题由客户端按 `text` 转义渲染，不解释 Markdown/HTML） | RFC-0009 §10.3 同一口径 |
| 枚举防滥用 | 一次全量、无分页 ⇒ 无法用游标做「探测存在性」的放大；单次响应 ≤ 帧上限 | §4.6 |

不是新的信任边界：它给浏览器的信息不超过「这个模块在能力页上露个脸」所需。**它确实**把「模块存在」从 admin 面移到了 UI 面——评审时请不要假设 UI 面本来就看不到模块列表（今天它确实看不到，这正是 A3）。

## 11. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| A. `ui/attach.result.snapshot.modules[]` | 模块集合与**会话**无关，塞进快照等于：① 每个会话一份副本；② 无会话时（手机首屏还没开会话）依然拿不到；③ `ui/attach` 的 params/result 是 C-05 里最热的契约，为一条不相干的数据动它不划算。客户端 KDoc 把它列为备选，本 RFC 明确否掉 |
| B. 复用 `registry/api.py` 的管理面（`snapshot_for_admin`） | 它**故意**只给 `ui_page_count` / `ui_page_warnings`（`api.py:23-24` 的 docstring：「The manifest body is never forwarded」），且鉴权是 admin 面（loopback/管理员），与浏览器会话不是一套 |
| C. 客户端自己维护一份模块清单（从 `module.status` 加本地配置） | 配置里没有 `ui.pages`，客户端只能编。`ModuleRegistry.kt:30-36` 已经写明「没有任何代码会凭空构造 manifest」 |
| D. 一个模块一个 RPC（`ui/module.get`） | 入口首屏就要 N 次往返，且 N 未知（要先把清单告诉客户端才能逐个取——回到原问题） |
| E. 事件推送整份清单（`module.list_changed` 带全量） | 事件体量与帧上限、seq 日志、重放成本全压在最热的通道上；且见 §9.3（声明不过门禁） |

## 12. 验收

A1–A12 要求真实执行；截图与手写记录不算证据（沿用 RFC-0004 口径）。落点为计划路径。

| # | 断言 | 落点 |
|---|---|---|
| A1 | 3 个模块（其中一个无 `ui.pages`）→ `ui/modules` 返回 3 行、顺序 = 注册顺序、无页面的那个 `pages: []`；入口规则推出 2 个分组、没有空分组 | `core/tests/modulepage/test_list_modules.py` / `modulepage/EntryTest.kt` |
| A2 | 页面行与 `ModulePageService.pages()` **逐键相等**（把 `id` 改名成 `page_id` 的变异必须红） | `core/tests/modulepage/test_list_modules.py` |
| A3 | 模块无 `display_name` → `display_name: null`（不抛、不填 `module_id` 冒充）；客户端该模块无入口 | 同上 + `ModuleRegistryTest.kt` |
| A4 | 浏览器调用者看不到 `enabled=false` 的模块；模块 peer 只看到自己那一行；未知调用者 `PERMISSION_DENIED` | `core/tests/modulepage/test_list_modules_auth.py` |
| A5 | 模块离线 → 仍列出、`online:false`、`pages` 保留；重连后 `online:true` | `core/tests/modulepage/test_list_modules.py` |
| A6 | `revision` 单调：register / `capabilities.update`（改 `ui.pages`）/ 断连各 +1；无变更的两次调用 revision 相同 | 同上 |
| A7 | 接线后：attach 之后注册的模块，**不重启**出现在能力页（≤1 s，一次 `module.status` 内），且能力页确实重组（可观察的 `revision` 生效） | `modulepage/EntryLiveTest.kt` |
| A8 | 旧 core（无该方法）→ 客户端收到 `-32601`，入口区块不渲染、**0 弹窗 / 0 重试循环**；重连后重试一次 | `modulepage/DegradeTest.kt` |
| A9 | 清单里**没有任何**页面正文：响应 JSON 全文搜不到 `children` / `$defs` / `"schema"` 键（A9 是「正文不过线」的机器判据） | `core/tests/modulepage/test_list_modules.py` |
| A10 | `events.json` 仍 34 条、`module.status` payload 仍 4 键（有人把清单塞进去必须红） | `tools/check_contracts.py`（既有断言）+ 本 RFC 落的用例 |
| A11 | 1 模块 8 页的响应 ≤ 8 KiB（实测）；60 模块的合成注册表 → `PAYLOAD_TOO_LARGE` 且 `data={field,limit,actual}` | `core/tests/modulepage/test_list_modules_limits.py` |
| A12 | `tools/check_ledger.py` 绿：`ui_to_core` 35 / 合计 48；RFC-0015 行 `impl_state=未实施`（落地那一棒改成「已实现」并把块转进 `appends`） | `tools/check_ledger.py` |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿（新文件不参与 C1–C10 计数）。

## 13. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **要不要分页/游标？** | 本 RFC 一次全量（`≤8 页/模块`，10 模块 ≈ 10 KB，帧上限 65 519 B）。若真出现 >60 模块，按 `task/list` 的 keyset 形态追加 `cursor`；倾向**永不**做，而是届时把 `ui/modules` 拆成「模块行」与「页面行」两个方法 |
| Q2 | **要不要 `module.declared` 事件？** | 本 RFC 用 `module.status` 作失效边（§5）。若实测发现「同字段重复 → 重拉过量」不可接受，再加 `{revision, module_id}` 轻事件（**不带清单本体**），并且必须与 core 实现同批（§9.3）。倾向：先不做，用 `revision` 把重拉做成 no-op |
| Q3 | **`refresh_s` 要不要驱动自动刷新？** | 清单里给了 `refresh_s`（`UIPage` 已有），v1 客户端**不**消费（页面状态仍在进页/`online` 转真/用户重试时取，RFC-0009 §5.1）。倾向 v1 不消费，字段先给出去（省一次契约追加） |
| Q4 | **`entry` 的桌面/手机差异由谁执行？** | C-19 §4.2 说 `entry` 决定入口落在桌面右面板还是手机能力 tab；本 RFC 只透传，不判定。倾向：宿主按 `entry` 过滤（与 C-19 一致），但 v1 客户端先按 manifest 顺序全渲染（差异留 U2） |
| Q5 | **`revision` 要不要像 `model/list` 那样支持 `if_revision`？** | 本 RFC **不**加：清单小、客户端自己比 `revision` 就够，而 `not_modified` 会引入「`modules:null` 表示未变化」的第二种失败模式（RFC-0005 已经教过客户端「不得在 `not_modified` 时清缓存」）。要加时按 `model/list` 的形状追加 |
| Q6 | **多实例要不要合并成一行（带 `instances[]`）？** | 本 RFC 一行一实例（与 `module.status` / `ui/page.get` 的坐标一致）。合并会引入「哪个实例的页面算数」的新语义（pool 模式下各实例声明的 pages 应当相同，但契约没规定）。倾向不合并 |

## 14. 影响面与后续

**本 worktree 落了**：`docs/rfc/RFC-0015-ui-modules.md`（本文件）、`schema/rpc.json`（`ui/modules` + `x-vade.rfc0015_pending`）、`docs/status.md` / `docs/rfc/LEDGER.md` / `docs/WHERE-ARE-WE.md` 三份台账的口径同步。

**实现阶段要动**（不在本棒）：`core/vade/modulepage/service.py`（`list_modules()`）、`core/vade/modulepage/rpc.py`（一个 handler，共 4 个）、`schema/rpc.json`（pending → `appends`，顶部 1.6.0）、`tools/check_contracts.py`（三处版本钉 + 一个断言组）、客户端三处（§8）。

**明确不动**：`schema/manifest.schema.json`、`schema/events.json`、`schema/errors.json`、`schema/config.schema.json`、`core/vade/session/**`（快照不加 modules）、`core/vade/registry/api.py`（admin 面仍只给计数与告警）。

**后续**：A3 的入口列表在 §8 三处接线后才会真的出现模块；`ui/page.get` / `ui/state.get` 的调用方（`RealModulePageSource.fetchPage/fetchState`）已经在，无需改动；模块页的 `navigate module:<page_id>` 校验需要 `declaredPageIds`（`RealModulePageSource.kt:106`），这份集合正来自本方法的 `pages[].id`——**接线后顺带把 `declaredPageIds` 补上**，否则带页内导航的页面会被 fail-closed 拒渲。

**参考**：`schema/rpc.json:421-425`（`ui/attach` 快照声明）、`core/vade/session/manager.py:767-785`（快照键）、`core/vade/modulepage/service.py:202-221`（`pages()`）、`core/vade/modulepage/rpc.py:31-37`（3 个 handler）、`core/vade/registry/table.py:135-162`（revision + `_publish_status`）、`core/vade/session/status.py:1-13`（`module.status` 的定位）、`core/vade/registry/api.py:20-45`（admin 面白名单）、客户端 `RealModulePageSource.kt:45-68`、`ModuleRegistry.kt:30-36/139-158/208-243`、`CapabilitiesScreen.kt:47`、RFC-0009 §9.3/§9.4、RFC-0005 §7.1、RFC-0006 §6.5、RFC-0012 §5.2。
