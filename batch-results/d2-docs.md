# d2-docs 批次结果（任务 #8：文档口径收口）

分支 `tmp/d2-docs`（基于 `dev_gingshan` @ `be16ebe`），未 push。文档改动 commit `39cb48a`，本文件单独一 commit。

## 改了什么

| 文件 | 内容 |
|---|---|
| `docs/rfc/README.md:22` | RFC-0016 状态列：`proposed（**未实施**：本棒只交付设计，schema/** 与 core/** 一行未动）` → `proposed（core 侧已实现 + 契约已登记为 x-vade.appends 第 12 块 / C-05 1.7.0，adds=["ui/blob.token"]；32 + 5 + 1 条新测试，见 shots/s-blob-impl/RESULT.md。客户端三态（A11）在 vade-client，本仓不核实）`。保留 `proposed` 而非改「已实施」，因为 frontmatter `status:` 仍是 `proposed`（本仓不许改 RFC 正文），与本表其余 core-已实现行的写法一致。 |
| `docs/rfc/README.md:21` | 顺带：RFC-0015 行「客户端未接线」→「客户端接线状态本仓不可核实（指向 `docs/ALPHA.md` §七 B1 + LEDGER）」。原文与 ALPHA §七 B1「2026-11-15 已解除 codec/订阅/组合根」冲突，本仓无法裁决，故不站边。 |
| `docs/WHERE-ARE-WE.md` | 契约段：C-05 **1.5.0→1.7.0**、方法 **48→49**（35→36 ui_to_core）、append **10+8+3=21→12+9+3=24**，删掉「另有 x-vade.rfc0015_pending 1 块」；测试 **1089→1203**（并补当前 HEAD 的收集数 1383，注明全量未复跑）；RFC-0015 那条「契约登记仍 pending / impl_state=未实施 / 客户端未接线」整条重写为「core 已实现 + 第 11 块 1.6.0」；新增 RFC-0016 已实现条目；RFC-0002 从「未开始/未实施」移到「已实现但未批准」（events.json 第 8 块 C-06 1.3.0 + 两个实现落点）；口径陷阱三条过期说法（35/48、`ui/modules` 不在 appends、events 8 块、RFC-0007 无登记块）全部改写；RFC-0003～0009「stage 仍是草稿」这条文档债标记为已由 `s-hygiene` 回填；未核实清单去掉已查实的 601/607，新增「客户端两处口径互相矛盾」与「HEAD 全量未复跑」。 |
| `docs/rfc/LEDGER.md` | §4-7 标 **已修**：`s-hygiene`（`a9c7dcc`）给 rpc.json 的 RFC-0009 块补 `"contract": "C-05"`、把 manifest 的 RFC-0005 块 `"RFC-0005"`→`"C-03"`；现 rpc 12 / events 9 / manifest 3 块 `contract` 键齐备。附订正：C-18 无 `appends` 数组，旧句「C-18 append 块缺 contract」不成立（出处 `HYGIENE-RESULT.md` §3-2）。同批收口：§4-1（stage 已回填 `a9c7dcc`）、§4-2（35/48 → 36/49）、§4-4（顶部 1.6.0→1.7.0）、§4-6（601/607 已查实，549/601/555/607 四点实测）；§2 主表说明列「frontmatter stage 已过时」×8、「缺 contract」「写 RFC-0005」三处改为实际状态；§1 测试行补「HEAD 只做 --collect-only = 1383」并改署名「本人复跑（1203 除外）」。表头基线段改为「初版 320384a / 最近复算 tmp/d2-docs」。 |

## 验证（本 worktree 实跑，原文）

```
$ python docs/check_docs.py
[PASS] C1 … C10（12 项全 PASS）
合计 12 项，失败 0 项。                      EXIT=0

$ python tools/check_ledger.py
ledger: OK (16 RFC rows; rpc 1.7.0 49 methods in 3 directions, events 2.0.0 34 entries,
34 error codes, appends 12+9+3; impl paths verified, statuses verified, acceptance tables recounted)
                                             EXIT=0

$ python -m pytest core/tests -q --collect-only
1383 tests collected in 19.11s
```

两道文档门禁改前改后均为绿（改前基线同样 12/12 + ledger OK），改动只落在 markdown 状态/说明列；`git diff --stat` = 3 files, 39 insertions(+), 37 deletions(−)，`schema/**`、`core/**`、`tools/**`、任何 `RFC-*.md` 正文零改动（`git status --short` 只有这三个 md）。

## 判断与取舍

1. **RFC-0016 为什么不写「已实施」**：check_ledger 的 status 列必须与 frontmatter `status:` 逐字相等（`check_ledger.py:120-133`），frontmatter 仍是 `proposed`。实现状态写在括号里，与 RFC-0015/0014/0010 行同构。
2. **测试数 1203 不当作 HEAD 的现状**：1203 是 `s-blob-impl` 基线 `7941b93` 的全量（出处 `shots/s-blob-impl/RESULT.md`），当前 HEAD 收集 **1383** 条（比 1203 多 180 条，未逐批核对是哪些批次新增，仅确认收集数）。全量一次 ~9 分钟，本棒只做 `--collect-only`，故 WHERE-ARE-WE 与 LEDGER 都写「HEAD 全量未复跑」，不给 HEAD 的 passed 数——不拿旧数冒充现状。
3. **客户端口径不裁决**：`docs/status.md` U1 行（1122 tests / APK 22,915,908 B / 「聊天写侧不出网」）与 `docs/ALPHA.md` §三 A6、§七 B1（1467 tests / APK 22,845,270 B / 聊天写侧已解除）互相矛盾，两处都不在本棒授权范围。WHERE-ARE-WE 改为并列两处出处 + 「需编排者裁定」，而不是替客户端仓选一个数。
4. **RFC-0002 移出「未开始」**：events.json 第 8 块（`doc: RFC-0002`、C-06 1.3.0、`adds: []`）+ `core/vade/loop/engine.py`、`queue.py` 落点 + LEDGER `impl_state=已实现` 三条都指向「已实现、待批准」。原句「无契约登记、无实现落点」是假的。
5. **`status.md` 主表 23 批次仍写「未开工」**：这句本身对 status.md 是准确的（表里确实这么写），但与同文件附录的「已合并」行矛盾。本棒只加一句「该主表未随实施回填、引用前先看附录行」，不去改 status.md（不在授权清单）。

## 遗留（不在本棒授权，未动）

1. `docs/ALPHA.md:58` 与 `:128` 引用的 `check_ledger` 原文写 `appends 12+8+3`——实际输出是 **12+9+3**（events 9 块）。ALPHA 的 A6 门禁证据串因此与现状差 1。
2. `docs/ALPHA.md:103` N7 仍写「附件上传端到端仍断 / RFC-0016 已 proposed、实施未排」——RFC-0016 已在 `s-blob-impl` 实现（C-05 1.7.0 第 12 块 + 38 条测试）。
3. `docs/rfc/RFC-0016-blob-token.md` 的 frontmatter `appends_events: 8 → 8`、`stage: 提案（未实现）` 与 §「落地后计数」段（`:418`）仍是落地前口径——RFC 正文/ frontmatter 属禁改区，只能由 RFC 作者回填。
4. `docs/rfc/LEDGER-RESULT.md:48` 记「events.json 7 块 / 合计 20 块」，是 2026-11-14 初版快照的历史记录，已在 LEDGER 表头注明「后者为初版快照，条目沿用原文」。
5. `docs/status.md` 主表 23 批次的「未开工」回填（见上文取舍 5）。
