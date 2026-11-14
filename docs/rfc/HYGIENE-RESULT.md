# HYGIENE-RESULT：文档/契约卫生批次（台账 4 条）

> 批次：`s-hygiene`。worktree `D:/HermesWorkspace/Vade-wt/s-hygiene`，分支 `tmp/s-hygiene`，基线 `dev_gingshan @ 54032ef`（DLL 已预置）。
> 交付 commit：**`a9c7dcc`**（11 文件，+17/−16）+ 本文件。
> 纪律核对：未 push、未合并；`tools/check_contracts.py` / `tools/check_ledger.py` / `docs/check_docs.py` / 任何测试一字未动（`git diff 54032ef..a9c7dcc -- tools/` 为空）；`schema/events.json` 未碰；601 与 607 两个数字都保留（只在 status.md 行尾追加备注）；本报告只贴原始输出，不写「已通过」。

## 0. 四道门禁（改动全部完成后的原始输出）

```
$ D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe -m pytest core/tests -q
1078 passed, 6 warnings in 67.34s (0:01:07)
```
（基线 1078，同数）

```
$ D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete,
RFC-0005 append green, RFC-0010 session metadata green, RFC-0012 subagent family green,
RFC-0014 session search green, RFC-0003 layers green, event gate green)
EXIT=0
```

```
$ D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe docs/check_docs.py
合计 12 项，失败 0 项。
EXIT=0
```

```
$ D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe tools/check_ledger.py
ledger: OK (14 RFC rows; rpc 1.5.0 47 methods in 3 directions, events 2.0.0 34 entries,
34 error codes, appends 10+7+3; impl paths verified, statuses verified, acceptance tables recounted)
EXIT=0
```

改动前基线亦先跑过：contracts / ledger / docs 三道均 OK / EXIT=0（2026-11-14，`54032ef`）。

## 1. 逐条处置

### 1）`schema/rpc.json` RFC-0009 append 块补 `contract` 键 ✅ 已修

```diff
       {
+        "contract": "C-05",
         "version": "1.2.0",
         "owner": "C2",
         "doc": "docs/rfc/RFC-0009-module-ui-pages.md",
```

取值依据：同数组其余 9 块**全部**写 `"C-05"`——该数组登记的是各 RFC 对 C-05 方法表的追加，块级 `contract` = 追加落在哪个契约（manifest.schema.json 里的三块写 C-14/C-19/C-03，语义是「承接该字段的契约」，两文件口径本就不同）。照邻块写法填 `C-05`，数组内 10 块从此一致。

改后立即跑（原文）：
```
contracts: OK (…)   EXIT=0
ledger: OK (…appends 10+7+3…)   EXIT=0
```
（appends 计数 10 不变：加的是**键**不是块，check_ledger 的计数口径不受影响。）

### 2）`schema/manifest.schema.json` RFC-0005 块 `contract`：`"RFC-0005"` → `"C-03"` ✅ 已修

```diff
-        "contract": "RFC-0005",
+        "contract": "C-03",
         "version": "1.0.0",
         "fields": ["capabilities.models"],
```

依据：任务书核实值 **C-03**（`capabilities.models` 由 C-03 的 `Manifest` 承接，LEDGER-RESULT §1-8 同一判定）；同数组另两块为 C-14 / C-19，命名口径从此统一。

改后立即跑（原文）：
```
contracts: OK (…)   EXIT=0
```

### 3）601 vs 607 差异查证 ✅ 已解释，写入 status.md 备注（两个数字都没删）

出处定位：
- `core/vade/tasks/RESULT-rfc0007.md`：601 passed（基线 549，新增 52）——分支 `tmp/rfc0007` 顶端 `404c3f3` 上记录；
- `docs/status.md` rfc0007 行：607 passed——由登记提交 `8ff8a46`（merge `67c5c49` 之后）写入。

查证过程（不猜数字）：建临时 probe worktree，按台账批次同一办法复制 `protocol/core/python/vade_protocol.dll`，对四个关键提交各跑 `pytest --collect-only -q` 实测收集数：

| 提交 | 位置 | 实测收集数 |
|---|---|---|
| `94f5995` | 分支基点（AMEND-23） | **549** |
| `404c3f3` | `tmp/rfc0007` 顶端 | **601** |
| `ebbc150` | 合并的 dev 侧父 | **555** |
| `67c5c49` | 合并点（status.md 记的批次） | **607** |

差异来源：`git log 94f5995..ebbc150 -- core/tests` 唯一相关提交 `32eb6c4`（设备身份接线，`tmp/device-id`，合入 `24bd131`）——`test_device_identity.py` 5 条 + `test_live_server.py` 1 条 = **恰好 6 条**，549+6=555。

旁证：`docs/status.md` device-id 行本来就记 **555 passed**（`24bd131`），与本实测一致。

结论：**601=分支时全量、607=合并点全量**，与 rfc0005（530→860）、rfc0006（552→670）同为「分支时 vs 合并后全量」口径，只是 0007 分叉后 dev 侧恰好长了 6 条。非测试增删、两个数字都没有错。

处置：`docs/status.md` rfc0007 行尾追加查实备注（四点实测数字 + 来源提交），行内原内容与两个数字一字未删；`RESULT-rfc0007.md` 一字未动。probe worktree 已删（`git worktree remove`）。

### 4）RFC-0003～0009 `stage` 回填 + README 索引同步 ✅ 已修

- 7 份 RFC frontmatter：`stage: 草稿，未实现；…` → **`stage: 已实现并合并`**（只动 frontmatter 第 11 行，正文零改动，diff 可验）。
- `docs/rfc/README.md` 索引 7 行状态列：`approved（已批准，未实施）` → **`approved（已实现并合并）`**；只改状态列文字，未增删行，其余 7 行（0001/0002/0010/0011/0012/0013/0014）未动。

**决策说明（需编排者知悉）**：回填时把 stage 行尾缀的草稿期护栏子句一并去掉了（如 0003 的「默认开关 legacy…」、0007/0008/0009 的「不改 `schema/`」）。原因：0007/0008/0009 三批实际都向 `schema/` 追加了内容（各自 RESULT 的变更清单可查），「不改 schema/」在合并后是**假命题**，留在回填后的真相字段里等于制造新的自相矛盾。若要保留历史子句，git 历史 `54032ef` 里原文俱在，可由编排者裁定恢复。

复核：check_ledger 对台账状态列的机器比对用的是 frontmatter `status:`（本条未动 `status:`，只动 `stage:`），实测 EXIT=0；check_docs 的 12 项与本条无关，实测失败 0 项。

## 2. 门禁执行记录（按改动时序）

| 步骤 | 立即执行的门禁 | 结果 |
|---|---|---|
| 基线（改前） | contracts / ledger / docs | 各 OK，EXIT=0 ×3 |
| 改 `schema/manifest.schema.json` | check_contracts | OK，EXIT=0 |
| 改 `schema/rpc.json` | check_contracts + check_ledger | OK，EXIT=0 ×2 |
| 改 `docs/status.md` | check_contracts | OK，EXIT=0 |
| 7×frontmatter + README（批量） | contracts + ledger + docs 三道全跑 | OK / OK / 12 项 0 失败 |
| 收尾全量 | pytest + 三道脚本 | 1078 passed；EXIT=0 ×3 |

**全程零非零退出，无任何回滚。**

## 3. 未做项 / 移交

1. **LEDGER.md 正文未动**。事实块无需重算：appends 计数 10+7+3 不变（本批加键不加块）、状态列口径未动，check_ledger 实测 OK。但 §4-7 与主表 RFC-0005/RFC-0009 行的描述文字仍写「缺 `contract`/写 `"RFC-0005"`」——修复后这些句子变成过时记录，建议台账后续批次把 §4-7 标注「已在 s-hygiene（`a9c7dcc`）修复」，避免下次对账重开。
2. **LEDGER 主表 RFC-0009 行的一处指引本身不准确**（顺带发现，未修——不在授权清单）：该行写「C-18 `[ui]` 4 键（append 块缺 `contract` 字段→§4-7）」，但脚本核实 `schema/config.schema.json` 全文**没有** `x-vade.appends` 数组——C-18 四键是叶子级 `x-vade` 元数据，由 `check_contracts.py` 的 `check_ui` 直接钉住；「append 块缺 `contract`」只发生在 rpc.json（本批已修）。
3. RFC-0007 的 events.json 登记缺口（LEDGER-RESULT #6 / §4-5）不归本批（本批禁碰 `schema/events.json`）。
4. stage 尾缀子句的去留见 §1-4 决策说明，如需恢复由编排者裁定。
5. 未 push、未合并。
