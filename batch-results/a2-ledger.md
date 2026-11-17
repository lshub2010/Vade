# a2-ledger 批次结果（任务 #6：RFC-0007 `task.*` 的 C-06 append 登记缺口）

分支 `tmp/a2-ledger`（基于 `dev_gingshan`），未 push。

## 改了什么

| 文件 | 内容 |
|---|---|
| `schema/events.json` | `x-vade.appends` 追加第 **9** 块：`contract: C-06`、`version: "1.1.0"`、`owner: C3`、`doc: docs/rfc/RFC-0007-subagents-and-tasks.md`、`adds: [task.started, task.status, task.progress, task.finished]`、`rewrites: "none. …"`，另 4 条 `overrides`（`task_states` 顶层枚举、三状态事件 vs `task.progress` payload-only、`x-vade.coalesce` 无对应 rule、审批归属四可选键）。既有 8 块与 34 个事件定义**逐字节未动**（diff 为 `14 insertions, 0 deletions`）。 |
| `tools/check_contracts.py` | `check_task_events()` 末尾追加该块的机器断言：块存在、`contract == "C-06"`、`version == "1.1.0"`、`adds` 恰为四事件、`rewrites` 以 `none` 开头；`main()` 的 OK 串补 `RFC-0007 task family green`。 |
| `docs/rfc/LEDGER.md` | §1 新增「RFC-0007 events 块」行，`append 登记块` 计数 events 8→**9**、合计 23→**24**；RFC-0007 主表行把「**无登记块**」改成实际四事件 + `C-06 1.1.0（第 9 块）`；§3 事实块 `appends_events` 8→9、RFC-0007 的 `events` / `append_versions` 同步；§4-3 口径更新；§4-5 缺口条目标「已收口」（保留为历史条目）。 |

## 为什么是 1.1.0、为什么算「补登记」

1. **版本对齐**：`schema/rpc.json` 的 RFC-0007 块一直是 `1.1.0`（`404c3f3` 落地时 rpc.json 顶部就是 1.1.0），因此 C-06 侧块也取 1.1.0。契约**顶部** version 仍 **2.0.0**（那是 RFC-0008 的枚举加宽，块级版本 ≠ 顶部版本，见 LEDGER §4-4）。
2. **确实是补登记**：`404c3f3`（`feat(tasks): RFC-0007 task.* 事件族与 task RPC`）时 events.json 顶部 version 还是 `1.0.0`，`x-vade.appends` 不是数组而是字符串（`"RFC-0004, RFC-0007, …"`），四个 `task.*` 事件与 `task_states` 就这么「有定义、无登记」地落了地。原 `x-vade.append_rfc0007` 兄弟键保留作史料（与 `x-vade.rfc0005_append` 同一形态的先例），块里 `rewrites` 明确记为 retrospective。
3. **没有动 rules**：RFC-0007 §9.2 提议追加一条 `x-vade.coalesce` 的重放规则，但 `rules[]` 是冻结文本、当时没追加（现存 7 条里确实没有）。所以 `overrides` 只声明「marker + 本块是权威」，不伪造一条不存在的规则。

## 验证

| 项 | 结果 |
|---|---|
| `python3 tools/check_contracts.py` | **OK**（exit 0），OK 串含 `RFC-0007 task family green` |
| `python3 tools/check_ledger.py` | **OK**（exit 0）`appends 12+9+3` —— 即 `appends_events` **8 → 9** |
| `python3 docs/check_docs.py` | 12 项 / 失败 0 项（exit 0） |
| 34 个事件定义逐字节不变 | `sha256(json.dumps(events, sort_keys=True))` = `51cbebb9…7e3cee`，改前改后**同值**；`events.json` diff = `14 insertions, 0 deletions` |
| 相关测试 | `pytest core/tests/events core/tests/task core/tests/session/test_rfc0013_partial_persistence.py core/tests/session/test_meta.py core/tests/session/test_rfc0014_search.py core/tests/loop/test_engine.py -q` → **197 passed**（这些文件都直接读 `x-vade.appends`，含 `appends[0]` 下标写法） |
| 门禁咬合（变异实测，逐一还原） | ① 块 `version` 改 2.0.0 → `FAIL: …must carry version 1.1.0`；② `adds` 只剩 `task.started` → `FAIL: …must add exactly the four task.* events`；③ 整块删除 → `FAIL: missing the x-vade.appends block for RFC-0007`；还原后三闸全绿 |

## 遗留 / 未做

1. **未跑全量 `core/tests`**（本仓全量一次 ~9 分钟）。本次只动 schema 文档与门禁脚本，实跑的是所有读 `appends` 的测试文件 + `core/tests/task` 全域，197 passed。全量数仍引用 LEDGER §1 的 1203。
2. **`docs/WHERE-ARE-WE.md:36` 仍写「events.json 是 8 块」**——该文件同时还有其他过期口径（§35 还写 `ui/modules` 不在 `appends`、三方向合计 48），不在本棒授权范围，未动，按 LEDGER 惯例只记录。
3. **`docs/rfc/RFC-0016-blob-token.md:418` 的「`appends_events` 仍 8」**是该提案落地时的口径，属 RFC 正文（本仓不许改），未动。
4. RFC-0007 块在 `appends` 里排在 **第 9（末尾）**，不是按 RFC 序号插队——与 RFC-0002/RFC-0003 的「追加即末尾」惯例一致，`test_rfc0013_partial_persistence.py` 对 `appends[0]` 的下标断言因此不受影响。
