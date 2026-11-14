# RFC-0009 安全审查返工 — c19-fix

基线 `bb92122`（RFC-0009 已合入），修复提交 `5e060dc`，合并 `dev_gingshan`（RFC-0005）后 **`934 passed`**。

## 修了什么

| 项 | 修法 | 文件 |
|---|---|---|
| **B1（HIGH）** 调用方鉴权 | 新增 `modulepage/auth.py`：`Caller`/`caller_of`/`authorize_target`/`require_visible`/`is_read_only`。身份**只**来自 `connection`（`device_id` = whitelist 的 `module_id#instance_id`，或 `ui-…` 浏览器会话），从不读参数。四个入口全部校验目标归属。`rpc.py::page_get/state_get` 现在转发 `connection`（这就是原来的 bug）。 | `modulepage/auth.py`（新）、`service.py`、`rpc.py` |
| **B2（HIGH）** 审批绑会话/设备 | `_PendingCall` 增 `session_id` + `approver_device_id`；第二阶段逐字比对，不一致 → `PERMISSION_DENIED`，`data.field` 指明 `session_id` 还是 `approver`；被拒的兑换不消耗 mint。 | `service.py` |
| **B3（MED）** 只读拦截 | `ui/tool.call` 与 `ui/submit`（后者原本**完全没有**）在铸造/建 turn 前返回 `READ_ONLY_SESSION`。 | `service.py`、`session/rpc.py` |
| **B4（MED）** ref 8 KiB | `_revalidate` 改传 `inline_max_bytes=MAX_REF_BYTES`（64 KiB），ref 机制恢复可用；48 KiB 块上限与 256 节点/8 层仍生效。 | `service.py`、`registry/uipage.py` |
| **B5（MED）** 缓存旧值覆盖 | per-key `asyncio.Lock` + compare-and-swap；`rev` 回退直接丢弃，`rev` 相等返回 `unchanged:true`；**删除** `max(rev, cached.rev+1)` 这种伪造 rev 的写法。 | `service.py` |
| **S3** ref 缓存不按 digest 失效 | 缓存键加 `digest`。 | `service.py` |
| **S4** `open_url` 主机欺骗 | 拒 userinfo（`user:pw@`）、回环/私网字面量（`127.`/`::1`/`10.`/`172.16-31`/`192.168.`/`169.254.`/简写 `127.1`）、`%0a`/`%0d`/`%09`/`%00`、`#javascript:` 片段。 | `registry/uipage.py` |
| **S5** schema 未闭合节点树 | `manifest.schema.json` 新增闭合的 `$defs/node`、`uiProps`、`uiOn`、`uiAction`、`uiBinding`；内联分支 `$ref` 到 `$defs/node`。`js_validate` 现在能独立拦住 `onClick`/未知 prop，不再只靠 `uipage.py`。配套 `_schema_without_ui()` 保证一个坏页不会让整个注册失败（§7.1 语义不变）。 | `schema/manifest.schema.json`、`registry/manifest.py`、`check_contracts.py` |

## 测试

新增 `core/tests/uipage/test_review_c19.py`（57 例，每条发现一节）+ 变异工具 `tools/mutation_check_c19.py`（施加 → 期望红 → 还原 → 期望绿，四态判定）。

## 变异结果：13/14 承重

B1（删鉴权、禁读已禁用模块）、B2（会话绑定、设备绑定）、B3（tool.call、ui_submit）、B4（8 KiB 上限）、B5（伪造 rev、盲写、等 rev 覆盖）、S3（键去掉 digest）、S4（userinfo、私网、编码控制字符）、S5（重开 node、重开 props）—— 全部施加后变红、还原后变绿。

### 一条如实记录：B5 的锁未证明承重

去掉 `async with lock:` 后测试**仍然绿**，因为临界区内没有 `await`，asyncio 无法在其间切换；**真正承重的是 compare-and-swap 守卫**，锁是纵深防御（防止将来有人在临界区里加 `await`）。

补充的对照组（注入 `await asyncio.sleep(0)` 后应红、保留锁应绿）已写好但**未跑**。因此：**不要把锁报成变异已验证**——本报告也不这么写。

## 门禁（合并 dev_gingshan 后）
```
pytest core/tests -q      → 934 passed
tools/check_contracts.py  → OK（34 error codes，RFC-0005 append green）
docs/check_docs.py        → 12 项，失败 0
冲突标记                  → 0
```

## 遗留
- B5 锁的对照组未跑（结论已按实情写）。
- `route_action` 线上仍不可达（`ui/action` 不在 `rpc.json`）。
- `ApprovalManager._resolved` 永不清理（内存）。
- 工具参数字面量无 per-string 上限（受 8 KiB 页上限约束）。
- Android/Wasm 客户端是否把 `check_state` 的值当纯文本渲染，跨仓依赖，本仓无法证明。
