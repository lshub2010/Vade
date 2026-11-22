# 现在到哪一步了（WHERE-ARE-WE）

> 可直接贴给人的口径页。基线：`tmp/f15-contract`（基于 `fcf638e`，2026-11-22）；契约数字按 `schema/*.json` 重算，`tools/check_ledger.py` 复算通过。
> 纪律：每一行带证据来源；核不出的写「未核实」；**不以「代码里存在 X」推断「X 生效」，不以计划充当现状**。
> 完整对应关系见 `docs/rfc/LEDGER.md`（每条 RFC 一行，`tools/check_ledger.py` 可重跑自检）。

## 已完成（本仓可核实）

- 契约：C-05 `rpc.json` **1.7.0**，方法 **49**（ui_to_core 36 / core_to_module 7 / module_to_core 6）；C-06 `events.json` **2.0.0**，事件 **34**（含 `turn.state` 枚举条目，不含则 33）；C-04 **34** 错误码；append 登记 **12+9+3=24** 块，**无** `x-vade.rfc0015_pending` 之类占位块（出处：四份 schema 重算；`tools/check_ledger.py` → `ledger: OK (16 RFC rows; rpc 1.7.0 49 methods …, appends 12+9+3)`）。
- 服务端测试：**1557 passed**（f15 worktree `tmp/f15-contract`，2026-11-22：`PYTHONPATH=core:protocol/core/python pytest core/tests -q` → `1557 passed, 14 warnings in 481.82s`；未本机构建 Rust，复用同源预编译 `libvade_protocol.so`）。历史口径：**1203 passed**（`shots/s-blob-impl/RESULT.md`，基线 `7941b93`）；**1383 collected**（旧 HEAD `be16ebe` 仅收集计数）；1165（`s-rfc0015-register`）、1089（2026-11-14）。
- 契约门禁：`tools/check_contracts.py` → exit 0（含 `RFC-0016 blob token green`；出处 `docs/rfc/LEDGER.md` §1、`shots/s-blob-impl/RESULT.md`）。
- docs 门禁：`docs/check_docs.py` → exit 0（12 项，失败 0 项；本 worktree 2026-11-22 复跑）。
- 契约登记全部在案：RFC-0001～0009 各有 `x-vade.appends` 块；RFC-0015 第 **11** 块（C-05 1.6.0）、RFC-0016 第 **12** 块（C-05 1.7.0）；f15 不登记 append 块（保持 C-05 1.7.0，`parts`/`user_text` 保留）；RFC-0007 的 4 个 `task.*` 事件补登记为 events.json 第 **9** 块（C-06 1.1.0，追溯登记，随块保留原 `x-vade.append_rfc0007` 作史料）。
- RFC-0001～0009：已批准（frontmatter `status: approved`；0001/0002 为正文状态表）且已实现合并；frontmatter `stage` 已由 `s-hygiene`（`a9c7dcc`）回填为「已实现并合并」，`docs/rfc/README.md` 状态列同批同步（见 `docs/rfc/HYGIENE-RESULT.md` §1-4）。
- RFC-0010 / 0012 / 0013 / 0014 / 0015 / 0016：core 侧已实现（各自 `core/vade/*/RESULT-rfc*.md`；契约块 1.3.0 / 1.4.0 / 1.2.0 / 1.5.0 / 1.6.0 / 1.7.0 均在案）；f15 不登记契约块：保持 1.7.0，snapshot 计数键只写进 `ui/attach.notes`（additive，无字段删除）。
- RFC-0004 数据面 `/ui/blob` 六路由 + content parts 定型（`core/vade/blob/http.py`、`core/vade/session/parts.py`；RESULT-parts.md 539 passed）。
- RFC-0016 `ui/blob.token`：`BlobTokenRPC`（`core/vade/blob/rpc.py`，严格参数 / 拒 cookie / 绑 `connection.device_id`）经 `handler_providers` 双路发布；解绑设备即清 token（`Whitelist.revoke` → `BlobService.revoke_device`）；新增 32 + 5 + 1 条测试（含真 socket 的 铸 token → HTTP 上传 → `ui/submit` → 新连接 `ui/attach` 取回）。

## 在飞 / 有保留的「完成」

- **「已实现但未批准」**：RFC-0010 / 0012 / 0013 / 0014 / 0015 / 0016 frontmatter `status: proposed` —— 代码与契约已落地，批准流程未走（出处：各 RFC frontmatter；`docs/rfc/README.md`）。
- **RFC-0002（`turn.completed.skills`）**：正文状态「草案」，但**已实现**——`turn.completed` 追加可选字段（`core/vade/loop/engine.py`、`core/vade/session/queue.py`），events.json 第 8 块（C-06 1.3.0，`adds=[]`、`rewrites: none`）。缺的是批准追认，不是实现（旧口径「未实施 / 无契约登记」已过期）。
- **RFC-0011（InputBar 拆分）**：README 记客户端仓 vade-client `d667442` 已实现——**本仓无证据，未核实**。
- **客户端三端**：`ui/` 已迁出为独立仓 `vade-client`，**本仓不可达**。本仓两处口径互相矛盾，引用前先裁定：`docs/status.md` U1 行记 **1122 tests / 0 failures / 1 skipped**、APK **22,915,908 B**、MSI 93,312,713 B（2026-11-15 复核，`shots/g-module-page/RESULT.md`），且仍写「聊天写侧不出网」；`docs/ALPHA.md` §三 A6 记 **1467 tests（154 files）/ 0 failures / 1 skipped**、APK **22,845,270 B**（`tmp/s-alpha-truth2`），§七 B1 记「聊天写侧已解除」。**哪一行是新口径需编排者裁定**，本页不替客户端仓下结论。
- `ask_user` 内置工具（RFC-0008 §14）：已注册（`core/vade/loop/builtins.py`；RESULT-askuser-tool.md 944 passed）。

## 未开始 / 未达成

- 里程碑 G1～G5：全部「未达成」；G4（iOS）用户已裁定暂缓（出处：status.md 里程碑表与主表 U4 行）。
- status.md 主表 23 批次中 C0a/C0b/C1/C2/C3/C3b/C3d/C4/C5a/C5b/M1–M6/F1 仍标「未开工」——该主表**未随实施回填**，与同文件附录里大量「已合并」行矛盾；引用「未开工」前先看附录行，本页不再据此断言「全部未开工」（出处：status.md 主表 vs 附录行）。

## 口径陷阱（已说错过的几件事）

- 「代码里有 GlassSurface」≠「glass 皮肤生效」；「字符图标存在」≠「已清零」——一切以 LEDGER + 测试输出为准。
- 「36 方法」是 ui_to_core 一个方向的数；三方向合计 **49**（把 36 当全表就是错的）。`ui/modules`（第 11 块）与 `ui/blob.token`（第 12 块）现在**既在** `directions` 里、**也在** `x-vade.appends` 里、**且有 core 实现**——旧说法「`ui/modules` 只是提案声明、core 未实现」已过期。
- 「events.json 10 条 appends」错：events.json 是 **9** 块、rpc.json 是 **12** 块，三文件合计 **24** 块（引用时务必写清是哪个文件；「10」「8」都是旧快照）。
- RFC-0007 的 4 个 `task.*` 事件**已有** events.json 第 9 块（C-06 1.1.0，追溯登记）——旧说法「无 append 登记块、纪律缺口」已过期，缺口在 `a2-ledger` 补完。
- append **块级**版本 ≠ 契约**顶部**版本（例：RFC-0013 块级 1.2.0，顶部 C-06 2.0.0）；引用版本号必须说明是哪一层。

## 未核实清单

- RFC-0011 的客户端实现与测试（vade-client 仓，本仓不可达）。
- 客户端测试数 / 产物体积 / 聊天写侧是否出网：`docs/status.md` U1 行与 `docs/ALPHA.md` §三 A6、§七 B1 互相矛盾（见上「客户端三端」条），本仓无法裁决。
- 当前 HEAD `be16ebe` 的服务端全量 passed 数（只做了 `--collect-only` = 1383；1203 是 `s-blob-impl` 基线 `7941b93` 的记录值）。
- Android / Wasm / 浏览器端运行时行为（本仓无对应测试证据）。
