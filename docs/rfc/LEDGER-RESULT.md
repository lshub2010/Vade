# LEDGER-RESULT：台账批次报告（不一致清单）

> 批次：`ledger`（status.md 附录行）。基线 `320384a`，交付 commit `dcd0690`（LEDGER + 自检 + 口径页 + status 行）+ 本文件。
> 纪律：本报告只贴原始输出与出处，不写「已通过」；核不出的写「未核实」。未 push、未合并。

## 0. 交付物与验证输出（原始）

```
$ D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe tools/check_ledger.py
ledger: OK (14 RFC rows; rpc 1.5.0 47 methods in 3 directions, events 2.0.0 34 entries,
34 error codes, appends 10+7+3; impl paths verified, statuses verified, acceptance tables recounted)
EXIT=0
```

自检的牙齿验证（篡改测试）：把事实块 `error_codes` 手改为 35 后——

```
ledger: FAIL
  - aggregate.error_codes: LEDGER claims 35, contracts say 34
TAMPER_EXIT=1
```

改回 34 → 恢复 `ledger: OK` / EXIT=0（失败路径与绿路径都真实跑过）。

```
$ pytest core/tests -q（worktree，复制 DLL 后）
1065 passed, 6 warnings in 92.20s (0:01:32)
$ python tools/check_contracts.py
contracts: OK (…34 error codes…RFC-0014 session search green…event gate green)   exit 0
$ python docs/check_docs.py
合计 12 项，失败 0 项。   exit 0
```

环境备注：worktree 首跑全量是 `9 failed, 1040 passed, 16 errors`——原因不是代码，是 worktree 缺
gitignore 的构建产物 `protocol/core/python/vade_protocol.dll`（主仓有，2026-11-13 编译）。失败/错误
全部集中在 transport/integration 的 Noise 握手、活服务器、真实接线用例；从主仓复制该 DLL（未被 git
跟踪，`git status` 不变）后复跑即 1065 passed。**任何新 worktree 直接跑全量都会假红**。

## 1. 不一致清单（本任务最有价值的产出）

### A. RFC 自述 vs 事实
1. **RFC-0003～0009 frontmatter `stage` 全部自称「草稿，未实现」**，但：status.md 记录这 7 个 RFC 全部「已合并」（rfc0003-impl / rfc0004a / rfc0004-parts / rfc0005 / rfc0006 / rfc0007 / rfc0008 / rfc0009 / c19-fix 行），契约 appends 已登记（rpc/events/manifest 三文件），实现文件全部在仓，全量测试过。→ stage 字段过时未回填。本仓不许改 RFC 正文，只能在此记录。
2. **`docs/rfc/README.md` 索引把 RFC-0003～0009 标「approved（已批准，未实施）」**——同一过时口径在索引层重复。
3. RFC-0010/0012/0013/0014 `status: proposed` + stage「已实现」并存：设计词汇（proposed=未批准）不是错误，但汇报时「已实现」与「已批准」必须分开说，否则必然重演「把代码存在当成生效」。

### B. 背景/口头口径 vs 契约文件
4. **「契约 1.5.0 / 34 方法」不准确**：三方向合计 **47** 方法（ui_to_core 34 / core_to_module 7 / module_to_core 6）。34 只是 ui_to_core 方向数——与 34 错误码、34 事件撞数，口头引用极易串。说「全表 34 方法」即错。
5. **「events.json 10 条 appends」不准确**：events.json 是 **7** 块 append；**10 是 rpc.json 的块数**；三文件合计 **20** 块（出处：`x-vade.appends` 数组长度逐一重算）。

### C. 契约文件内部/之间
6. **RFC-0007 的 4 个 `task.*` 事件与顶层 `task_states` 在 events.json 没有 append 登记块**：rpc.json 侧 RFC-0007 块登记了 4 个方法；events.json 实际声明了 `task.started/status/progress/finished` 与 `task_states`（check_contracts 的 check_task_events 门禁在跑、绿）；但 events.json `x-vade.appends` 只有 0003/0005/0006/0008/0010/0012/0013 七块，**没有 RFC-0007 块**——append-per-minor 登记纪律的缺口。
7. **rpc.json 的 RFC-0009 append 块缺 `contract` 键**（同数组其余 9 块均有）。
8. **manifest.schema.json 的 RFC-0005 append 块 `contract` 字段写的是 `"RFC-0005"` 而非契约号**（C-03）；同数组另两块写 C-14 / C-19——同一数组内命名口径不一。

### D. 文档 vs 文档（数字冲突）
9. **RESULT-rfc0007.md 记 601 passed（基线 549+52），status.md rfc0007 行记 607 passed**——差 6 条，无任何出处解释。对照：rfc0006（RESULT 552 vs status 670）、rfc0005（RESULT 530 vs status 860）的差异能用「分支时 vs 合并后全量」口径解释并写明了基线，唯独 601/607 解释不通。

### E. 代码质量（顺带发现，未修——不在本批授权清单）
10. **`core/vade/session/search.py` docstring 两处非法转义**（`\` 后跟反引号）触发 `SyntaxWarning: invalid escape sequence`，每次跑 check_contracts.py 与 pytest 都打印。功能无碍（仅 docstring），但门禁输出长期带噪音，且「门禁输出里出现 warning」本身就是口径污染。

### F. 环境/基础设施
11. **新 worktree 全量测试假红**（见 §0 环境备注）：缺 DLL → 25 条 transport/integration 用例红。这类「环境红」一旦被当成「代码红」记进台账，就是第二次「说错」的原料。

**不一致合计：11 条。**

## 2. 核实不了、需要人确认的条目

| # | 条目 | 卡在哪 | 建议谁定 |
|---|---|---|---|
| 1 | RFC-0011 客户端实现（vade-client `d667442`）与三皮肤测试 | 客户端仓不在本任务可达范围 | 客户端侧贴测试输出 |
| 2 | 601 vs 607 的 6 条差异来源 | 两处文档都无中间记录 | 当时的执行/记录者 |
| 3 | RFC-0003～0009 stage 回填 + README 索引修订 | 本批禁改 RFC 正文 | 各 RFC 作者 |
| 4 | RFC-0007 events.json 登记缺口：补 append 块还是判定豁免 | 改 schema 是契约持有人权限 | 编排者 |
| 5 | RFC-0009 缺 `contract` 键、RFC-0005 manifest 块命名 | 同上 | 契约持有人 |
| 6 | search.py SyntaxWarning 修复 | 本批禁改 core 源码 | RFC-0014 作者（一行转义改动） |
| 7 | worktree 缺 DLL 的假红问题：初始化文档化 or CI 固化复制步骤 | 超出本批授权 | 编排者 |
| 8 | Android / Wasm / 浏览器端运行时验收 | 本仓无对应测试证据 | U1/U2/U3 批次 |

## 3. 数字出处速查

- 契约聚合数字（47/34/34/20、每 RFC adds 与块级版本）：`tools/check_ledger.py` 从 `schema/rpc.json`、`schema/events.json`、`schema/manifest.schema.json`、`schema/errors.json` 重算（输出见 §0）。
- 1065 passed：本 worktree 复跑原始输出（§0）；同数见 `core/vade/session/RESULT-rfc0014.md`。
- 各 RFC 测试数：全部引自 `core/vade/*/RESULT-*.md` 或 `docs/status.md`，逐条标注在 `docs/rfc/LEDGER.md` 主表「测试数（来源）」列；无出处的写「未记录/未核实」，无一编造。
- 验收表行数：`check_ledger.py` 对 14 份 RFC 正文机械复数（首列为 A-id 的表行）；勾选数 `[x]` 全仓 14 份 RFC **均为 0**——验收表从来没有勾选列。
