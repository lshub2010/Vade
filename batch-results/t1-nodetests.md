# t1-nodetests 批次结果（w1 三处修复的回归测试）

分支 `tmp/t1-nodetests`（基于 `dev_gingshan`，已含 `f961862` 的实现但无测试）。
本批**只加测试，未改生产代码**；三处修复均被新测试钉住（含反向验证，见下）。

## 改动

| 文件 | 内容 |
|---|---|
| `core/tests/admin/test_node_contracts.py`（新，16 个测试） | layout rev 冲突 / 畸形 rev、`capabilities` 与 `tool_names` 拆分、SSE `node.upsert` 全量视图与心跳恢复。复用 `conftest.py` 的 `make_context` / `client_for` / `new_device` / `login_with_device`，与 `test_admin_api.py` 同一写法（device token 才有写权限）。 |
| `batch-results/t1-nodetests.md`（本文件） | 批次结果。 |

## 覆盖点

### 1. `PATCH /admin/api/layout` 的 rev 语义

| 场景 | 断言 |
|---|---|
| 首次写（无 rev） | 200，`rev` 0 → 1，坐标落库 |
| 陈旧 rev | **409** `TOOL_CONFLICT`，message 含 `current rev is 1`；`GET /layout` 证明被拒的写没有落库（rev 与 nodes 原样） |
| 用 409 里给的 rev 重试 | 200，rev 2（说明 message 里的值是可直接用的，不是摆设） |
| 同一请求再发一次 | 409，message 跟着变成 `current rev is 2` |
| 畸形 rev：`"one"` / `"1.0"` / `[]` / `{"rev":1}` | 400 `INVALID_ARGUMENT`，message 为 `rev must be an integer`，layout 保持 rev 0（`str` 走 ValueError、`list`/`dict` 走 TypeError 两条分支都覆盖） |

### 2. `capabilities` 是种类，不是工具名

| 场景 | 断言 |
|---|---|
| 两个工具 `read_file`/`list_dir` | `capabilities == ["tools"]`，**不含任一工具名**；`tool_names` 保持调用方顺序、`tool_count == 2`；`set(capabilities) <= CAPABILITY_KINDS` |
| 同时声明 prompts/events/models | `capabilities == ["tools","prompts","events","model"]`（`CAPABILITY_KINDS` 顺序稳定，两次轮询可直接 diff；`models` 发布为单数 `model`） |
| `module_id` 约定 | `com.example.memory` → `+memory`；`com.example.mcp`、`com.example.bridge-service` → `+bridge` |
| 无 manifest 的 rejected 记录 | `capabilities == []`、`tool_names == []`、`tool_count == 0`（不因缺 manifest 抛异常） |
| HTTP 出口 `GET /admin/api/nodes/{id}` | 字段集 == `NODE_FIELDS`；`capabilities == ["tools","model"]` 而 `tool_names == ["read_file"]`——证明拆分真的过了 `to_public_view` 这个唯一出口，不是只改了单测里的辅助函数 |

### 3. SSE `node.upsert`

| 场景 | 断言 |
|---|---|
| L1 改名（`{label}`） | 恰 1 个 `node.upsert`；`data == {id, node, changes}`；`node` 字段集 == `NODE_FIELDS` 且 `node.state == "online"`；`node` 与 `GET /admin/api/nodes/{id}` **逐字段相等**；`format()` 出来的 `data:` 行去掉 `type` 后就是同一对象 |
| 停用（`{enabled:false}`） | 健康态同时翻 offline，listener 与显式 publish 会各发一帧：断言**每一帧都是完整视图**、只有一帧带 `changes`、该帧 `node.state == "offline"`（不断言帧数，见「注意」） |
| 心跳恢复 | `mark_disconnected` → 1 帧 offline；`touch` → **恰 1 帧** online，`node.state` 存在；已 online 再 `touch` → **0 帧**（不会每帧心跳刷一次画布） |
| 线路格式 | 直接消费 `GET /admin/api/stream` 包装的那个生成器（`ctx.stream.events("0")`），首块以 `id: <seq>` 开头、`data:` 里的 JSON 有 `type/id/node`，`node` 是 18 字段全量视图 |

## 验证

- `python -m pytest core/tests/admin/test_node_contracts.py -q` → **16 passed**（新增）
- `python -m pytest core/tests/admin core/tests/registry -q` → **218 passed**（基线 202 + 16）
- `python tools/check_contracts.py` → **contracts: OK**（未新增事件/错误码/契约字段，纯测试）
- **反向验证（mutation check）**：逐条把 w1 的四处改动改回旧行为，跑对应测试，四次全部 FAIL：
  | 回退的代码 | 命中的测试 |
  |---|---|
  | `TOOL_CONFLICT` → `INVALID_ARGUMENT`（409 变 400） | `test_layout_stale_rev_is_409_conflict_with_the_current_rev` |
  | `"capabilities": capability_kinds(record)` → `names` | `test_capabilities_are_kinds_and_tool_names_stay_separate` |
  | `_publish_node` 的 `node` 换成 `changes` | `test_patch_node_publishes_one_full_view_not_a_sparse_change` |
  | `touch()` 去掉 `_publish_status(updated)` | `test_heartbeat_restore_publishes_exactly_one_upsert` |
  验证后 `git checkout -- core` 还原，工作树只剩本批新增的测试文件。

## 注意 / 后续（测试中发现，未在本批修改）

1. **一次停用会发两帧 `node.upsert`**：`Registry.set_enabled()` 自己 `_publish_status()`（listener 发一帧，不带 `changes`），`patch_node` 随后又显式 `_publish_node(..., changes=...)` 发一帧。两帧的 `node` 完全相同，`node.upsert` 是 replace 语义，客户端幂等，故未按 bug 处理；但一次编辑两帧是无谓流量，若要收敛，应在 `patch_node` 里对同一次变更只发一帧（或让 listener 在这条路径上让位），而不是让测试去断言 2。测试因此只断言「每帧完整、恰一帧带 changes」。
2. **停用后的节点 `tool_names` 变空**：`_view_for` 从 `registry.tools()`（活的绑定索引）取名字，`set_enabled(False)` 会把该实例移出索引，所以面板上「工具」区显示「当前无工具」，而同一响应里的 `impact.tool_count` 是 1。这是 w1 之前就有的行为（`_view_for` 只是把旧 `_views` 的内联逻辑提出来），不属于本批三处修复，未动。
3. **SSE 路由本身无法用 httpx 的 `ASGITransport` 测**：该 transport 会把响应读到 `more_body=False` 才返回，而事件流是无限的——直接请求会挂死。测试改为消费 `ctx.stream.events()`（路由 `StreamingResponse` 包的就是它）+ `ctx.stream.format()`，覆盖的是同一段线路代码；连接/断线重连属于 `EventStream` 自身职责，不在本批范围。
4. 测试全程无网络、无真实 TLS，沿用 `core/tests/admin/conftest.py` 的 `make_context`/`client_for`，与现有 admin 测试同构。
