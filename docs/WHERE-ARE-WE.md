# 现在到哪一步了（WHERE-ARE-WE）

> 可直接贴给人的口径页。基线：`tmp/w-ledger` @ `320384a`，2026-11-14。
> 纪律：每一行带证据来源；核不出的写「未核实」；**不以「代码里存在 X」推断「X 生效」，不以计划充当现状**。
> 完整对应关系见 `docs/rfc/LEDGER.md`（每条 RFC 一行，`tools/check_ledger.py` 可重跑自检）。

## 已完成（本仓可核实）

- 契约：C-05 `rpc.json` **1.5.0**，方法 **47**（ui_to_core 34 / core_to_module 7 / module_to_core 6）；C-06 `events.json` **2.0.0**，事件 **34**；C-04 **34** 错误码；append 登记 **10+7+3=20** 块（出处：三份 schema 文件重算，`tools/check_ledger.py` 比对通过）。
- 服务端测试：**1065 passed**（`pytest core/tests -q`，本 worktree 2026-11-14 复跑 `1065 passed, 6 warnings in 92.20s`；`core/vade/session/RESULT-rfc0014.md` 同数）。
- 契约门禁：`tools/check_contracts.py` → exit 0（`contracts: OK … RFC-0014 session search green …`）。
- docs 门禁：`docs/check_docs.py` → exit 0（12 项，失败 0 项）。
- RFC-0001～0009：已批准（frontmatter `approved`）且已实现合并（`docs/status.md` 各「已合并」行 + 契约 appends 登记在案 + 实现文件在仓）。
- RFC-0010 / 0012 / 0013 / 0014：core 侧已实现（各自 `core/vade/*/RESULT-rfc*.md` + C-05 1.3.0/1.4.0/1.5.0 与 C-06 登记块）。
- RFC-0004 数据面 `/ui/blob` 六路由 + content parts 定型（`core/vade/blob/http.py`、`core/vade/session/parts.py`；RESULT-parts.md 539 passed）。

## 在飞 / 有保留的「完成」

- **「已实现但未批准」**：RFC-0010/0012/0013/0014 frontmatter `status: proposed` —— 代码与契约已落地，批准流程未走（出处：各 RFC frontmatter；`docs/rfc/README.md`）。
- **文档债**：RFC-0003～0009 frontmatter `stage` 仍写「草稿，未实现」，与 status.md「已合并」矛盾——字段过时未回填（本仓不许改 RFC 正文，只能记录）。
- **RFC-0011（InputBar 拆分）**：README 记客户端仓 vade-client `d667442` 已实现——**本仓无证据，未核实**。
- 客户端三端（U1/U2/U3 批次）：status.md 主表均「未开工」，仅 C0c（Miuix 三端可行性）已合并为可行性结论，不是产品功能。
- `ask_user` 内置工具（RFC-0008 §14）：已注册（`core/vade/loop/builtins.py`；RESULT-askuser-tool.md 944 passed）。

## 未开始 / 未达成

- RFC-0002（`turn.completed.skills`）：**未实施**（正文状态「草案」，无契约登记、无实现落点）。
- 里程碑 G1～G5：全部「未达成」；G4（iOS）用户已裁定暂缓（出处：status.md 里程碑表与主表 U4 行）。
- status.md 主表 23 批次中 C0a/C0b/C1/C2/C3/C3b/C3d/C4/C5a/C5b/M1–M6/U1–U3/F1 均「未开工」（出处：status.md 主表）。

## 口径陷阱（已说错过的两件事 + 本次新发现）

- 「代码里有 GlassSurface」≠「glass 皮肤生效」；「字符图标存在」≠「已清零」——一切以 LEDGER + 测试输出为准。
- 「34 方法」是 ui_to_core 一个方向的数；三方向合计 **47**（说全表 34 就是错的）。
- 「events.json 10 条 appends」错：events.json 是 **7** 块；10 是 rpc.json 的块数。
- RFC-0007 的 4 个 `task.*` 事件在 events.json **无 append 登记块**（rpc.json 侧有）——纪律缺口，详见 `docs/rfc/LEDGER-RESULT.md`。

## 未核实清单

- RFC-0011 的客户端实现与测试（vade-client 仓，本仓不可达）。
- status.md rfc0007 行 607 passed vs RESULT-rfc0007.md 601 passed 的 6 条差异来源。
- Android/Wasm/浏览器端运行时行为（本仓无对应测试证据）。
