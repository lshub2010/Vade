# s-modules-impl · `ui/modules` from declaration to a served method

分支 `tmp/s-modules-impl`（基于 `dev_gingshan` @ `84ab577`）。
实现提交 **`963210b`**（本报告单独一提交）。规格：`docs/rfc/RFC-0015-ui-modules.md` + `schema/rpc.json` 的 `directions.ui_to_core["ui/modules"]`。

## 1. 结论

`ui/modules` 现在**真的被 core 服务**：一次拉全量 `{revision, modules[]}`，页面行与 `ModulePageService.pages()` 同一段拼装，鉴权只认连接身份，错误只用 C-04 既有的三个码。

**契约登记仍是 pending**：C-05 顶部版本没升（升了会红，见 §4），`x-vade.rfc0015_pending` 保留，LEDGER 行的 `impl_state` 按门禁口径仍是 `未实施`——**这条不是「没写 core」**，正文已写明原因。

## 2. 落点

| 项 | 内容 |
|---|---|
| `list_modules()` | `core/vade/modulepage/service.py:242` — `def list_modules(self, connection: Any) -> dict[str, Any]`（同步方法：纯内存投影，无 await；handler 是 async 包装） |
| 页面行投影 | `ModulePageService._page_rows(record)`（`service.py:216`，`@staticmethod`）——`pages()` 与 `list_modules()` 共用，RFC §4.2 尾注要求的「不要第二次手写」 |
| RPC handler | `core/vade/modulepage/rpc.py:42` — `ModulePageRPC.modules(connection, params)`，`params` 完全不读（`{}`、未知键、`None` 都接受） |
| 注册方式 | `ModulePageRPC.handlers()` 里加 `"ui/modules": self.modules`；`modulepage/install()` 把它发布进 `app.session_handlers`，也就是 `app.make_rpc_handler` 唯一读的那张表 |
| 鉴权 | `modulepage/auth.py` 的 `require_caller(connection)`：浏览器（`ui-<hex>`）只见 `enabled` 模块；模块 peer 只见自己那一行；未知身份 `PERMISSION_DENIED`。**params 不参与身份判定** |
| 错误 | 只用 `PERMISSION_DENIED` / `UNAVAILABLE`（registry 未装配）/ `PAYLOAD_TOO_LARGE`（`data={field:"modules", limit:65519, actual:N}`），C-04 仍 34 码 |
| `events.json` | **一行未动**（变化通知继续用 `module.status` 作失效边 + `revision` 去重） |
| `revision` | `Registry.revision()`（`registry/table.py:135`），未新造计数器 |
| `online` | `record.health is HealthState.ONLINE and record.connection_id is not None`——与 `module.status` 同一式子 |

顺序（写进测试，不靠猜）：模块 = `Registry.snapshot()` 顺序（即 `(module_id, instance_id)` 排序，`table.py:380`；RFC 括号里写的「首次注册顺序」与代码不符，以 `snapshot()` 这条规范项为准）；页面 = `manifest.ui.pages[]` 原序。

## 3. 路上发现的两个真 bug（都修了，都在本棒可改范围内）

1. **C-03 的 `display_name` 一直被丢掉。** `manifest.schema.json` 有它、RFC §4.2 要求透传、`Manifest` dataclass **没有这个字段**（`validate_manifest` 也不解析）——`grep display_name core/vade/registry/` 零命中。RFC 正文说「`Manifest.display_name` 在 dataclass 上」是**错的**。修法：`Manifest` 末尾加 `display_name: str | None = None` + 解析一行（`core/vade/registry/manifest.py`）。**这是超出任务书「可改」清单的一处改动**（清单里没有 `registry/manifest.py`，但也没把它列进「不可改」；不补这个字段，`display_name` 只能是永远 `null` 的假实现）。
2. **真 core 上 `ui/page.get` / `ui/state.get` / `ui/tool.call` 全部不可达（先于本棒存在）。** `modulepage.install()` 只在 `app.session_handlers` **不存在**时挂 `handler_providers`；而更早安装的组件已把该 dict 建好，接着 `session.install()` 又把它**整体替换**（`session/__init__.py:232`），于是 modulepage 的三个 handler 被丢掉。本棒的 WS e2e 第一次跑就撞上：`{'code': 'NOT_FOUND', 'message': "unknown method 'ui/modules'"}`（换成 `ui/page.get` 同样红）。修法：`modulepage/install()` 两条路径都发布（`setdefault` 幂等）。`core/vade/session/**` **未改**。

## 4. 版本号：实测仍红，按纪律退回 1.5.0

先实现、后升版，`schema/rpc.json` 顶部 `1.5.0 → 1.6.0`（pending 块原样），`check_contracts` 原文：

```
contracts: FAIL
  - rpc.json: version must be 1.3.0 (RFC-0010), 1.4.0 (RFC-0012) or 1.5.0 (RFC-0014, the current head) (got '1.6.0')
  - rpc.json: version must be 1.5.0 after the RFC-0014 append (got '1.6.0')
  - rpc.json: version must be 1.4.0 (RFC-0012) or 1.5.0 (RFC-0014, the current head) (got '1.6.0')
EXIT=1
```

三处都是 `tools/check_contracts.py` 里的**硬版本钉**（`check_session_meta:1356` / `check_subagent_events:1096` / `check_session_search:1480`），本棒**不许改门禁**，所以版本号已退回 **1.5.0**（`git diff schema/rpc.json` 里没有任何版本行），pending 块保留，只把它的文字改成事实：status = `implemented in core — still NOT an append registration (header pinned at 1.5.0)`，notes 写明实现落点与「升 1.6.0 仍红三处」。方法 `notes` 的开头也从 `PROPOSED — not served by any core yet` 改成 `SERVED …；CONTRACT registration is still pending`（技术内容一字未动）。

**还差什么才能升 1.6.0**（三件事必须同批，缺一件 gate 就红）：
1. 抬 `tools/check_contracts.py` 的三处版本钉（1356/1096/1480）——需要有权限改门禁的那一棒；
2. `x-vade.rfc0015_pending` → `x-vade.appends` 末尾（appends 10 → 11），块 `version: 1.6.0`；
3. LEDGER：`impl_state` `未实施 → 已实现`、`impl` 填本报告 §2 的路径、`append_versions {"rpc.json":"1.6.0"}`、aggregate `appends_rpc 10 → 11`。

## 5. 台账最终状态

| 文件 | 状态 |
|---|---|
| `schema/rpc.json` | 顶部 **1.5.0**；`ui/modules` 在 `directions.ui_to_core`；`x-vade.rfc0015_pending` 保留（1 块，文本已改为「core 已实现、契约登记 pending」）；`appends` 仍 **10** 块 |
| `docs/rfc/LEDGER.md` | RFC-0015 行：实现状态 = **core 已实现，契约登记仍 pending**；实现落点列已填；测试列 = 32 passed / 全量 1164；`impl_state` 仍 `未实施`（门禁口径，正文解释了为什么）；facts 块加了一个不参与校验的 `note` 说明同一件事 |
| `docs/status.md` | 新增 `s-modules-impl` 行（待审查） |
| `docs/WHERE-ARE-WE.md` | RFC-0015 行改为「core 已实现，契约登记仍 pending；客户端未接线 → A3 仍缺数据源」 |

## 6. 测试（32 条新增，全绿）

`core/tests/uipage/test_list_modules.py`（17）：
空 registry（真 `Registry`，`revision==0`、`modules==[]`）/ 单模块逐字段 / 无 `ui.pages` → `pages: []` / **与 `pages()` 逐键相等** / 顺序 = `snapshot()` 顺序（真 registry，含同模块双实例）/ 两次读取相同 / 离线仍列出且带 pages / `online` 需 health+connection_id / `display_name` 缺失 → `null` 且不冒用 `module_id` / C-03 拒显式 `null`（只能省略）/ `Manifest` 真的带上 `display_name` / 真 registry 注册与断连各 +1 / 两次读不变 + `capabilities.update` +1 且页面更新 / **A9 无正文**（递归查 `schema`/`children`/`$defs`）/ handler 表含四个名字 / `install()` 两条路径都发布 / `session` 替换 handler 表后仍存活（bug #2 回归）。

`core/tests/uipage/test_list_modules_auth.py`（8）：
未知身份 → `PERMISSION_DENIED` / 浏览器只见 enabled / 模块只见自己 / 模块即使被禁用也见自己 / 未注册的模块身份 → `[]` 而非别人的行 / `manifest is None` 的记录不列出 / 只读会话正常服务 / params 里写别人的 `module_id` 不改变结果（B1）。

`core/tests/uipage/test_list_modules_limits.py`（6）：
registry 未装配 → `UNAVAILABLE` / 8 页模块 ≤ 8 KiB / 120 模块 → `PAYLOAD_TOO_LARGE` + `data{field,limit,actual}`（不截断）/ 未知 params（含 `session_id`、`None`）与 `{}` 结果相同 / 声明的错误码恰好三个且都在 `errors.json`（34 码不变）/ `app.modulepage` 就是带 `list_modules` 的那个 service。

`core/tests/e2e/test_ui_modules_ws.py`（1）：
真 uvicorn + TCP + WS：`module/register` 走 Noise 注册（含 `display_name` 与两页）→ `POST /ui/session` 拿 cookie → 该 socket 上 `ui/modules` 拿到整份结构（逐字段、`online:true`、`requires`/`refresh_s`/`entry` 对）→ 第二次带未知参数拉取结果完全相同 → 响应里搜不到 `"schema"`/`"children"`/`"$defs"` → `ui/modules` 在 `app.session_handlers` 里。

已知抖动 `core/tests/task/test_progress.py::test_three_rapid_updates_become_at_most_two_events`：本棒两次全量都没红（未改、未 skip）。

## 7. 四道门禁（本 worktree，最终树）

| 门禁 | 结果 |
|---|---|
| `pytest core/tests -q` | **1164 passed**（基线 1132 + 32；8 warnings，678.90s）。另两次全量复跑（最终树）各 **1163 passed + 1 failed**，红的都是**已知抖动** `core/tests/task/test_progress.py::test_three_rapid_updates_become_at_most_two_events`——**未改、未 skip**；单独重跑该用例 3 红 1 绿；在**基线 `84ab577` 的干净 worktree**（无本棒任何改动）里同一用例 **5 红 1 绿**，证明是既有抖动而不是本棒引入 |
| `tools/check_contracts.py` | `contracts: OK (… 34 error codes … RFC-0014 session search green …)`，exit 0 |
| `docs/check_docs.py` | `合计 12 项，失败 0 项`，exit 0 |
| `tools/check_ledger.py` | `ledger: OK (15 RFC rows; rpc 1.5.0 48 methods in 3 directions, … appends 10+8+3; …)`，exit 0 |

## 8. 未做 / 留给下一棒

- 客户端三处接线（RFC §8）：`RealSessionSource` 失效分支、`RealModulePageSource` 两个 override、`CapabilitiesScreen` 的可观察 `revision`。**本仓不可达**，A3 在用户眼里仍不会出现入口。
- 版本 1.6.0 + pending → appends + LEDGER `已实现`：见 §4 的三件事，需要能改门禁的提交。
- `list_modules` 只服务、不缓存：`revision` 已够客户端去重，加缓存只会多一份要失效的状态。
