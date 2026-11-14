# 开发台账

> 每次派活、收活、审查、合并后更新本表。状态枚举：`未开工 / 进行中 / 待审查 / 返工中 / 已合并 / 阻塞 / 暂缓`（`暂缓` = 用户明确决定不在当前范围，恢复需重新拍板）。
> 阻塞必须写原因与解除条件。

| 批次 | Agent | 状态 | 交付 commit | R1 | R2 | R3 | 返工次数 | 备注 |
|---|---|---|---|---|---|---|---|---|
| C0a | `c0a-uniffi` | 未开工 | — | — | — | — | 0 | 硬时限 3 天 |
| C0b | `c0b-wasm` | 未开工 | — | — | — | — | 0 | 硬时限 3 天，出 ADR-001 |
| C0c | `c0c-miuix` | 已合并 | `3c55e50`（merge，含 808189f / ee72a77） | — | R2 **fail→修正**（`../Vade-REVIEW-c0c-r2.md`） | — | 2 | **三端可行**。证据：16 张截图（8 张真实窗口 466×893 + 8 张无头 480×420，无手绘替身）；Android 需 **AGP 9.5.1 + Gradle 9.6.1 + compileSdk 37 + `android.builtInKotlin=false` + `android.newDsl=false`**，出 APK 13,575,772 B（compileSdk 37 / minSdk 24 / targetSdk 36）；Wasm 同界面替换口径 gzip 936,611 B vs Material 1,013,216 B（小 76,605 B，三皮肤共存增量需 U2 另测）；**OverlayDialog 归因已订正**：它只注册 DialogState，绘制由 `MiuixPopupHost` 完成，而 host 只在 Miuix `Scaffold` 内——脱离 Scaffold 时三端都不绘制（不等于 Desktop popup 缺陷）；真实形态是**底部 sheet**。未验：Wasm/Android 弹窗、Android 运行时、浏览器运行时、release 体积 |
| C1 | `c1-protocol` | 未开工 | — | — | — | — | 0 | 冻结 C-01/C-02（**W1 末**）与 **C-15**（W1 第 2 天），C2 只消费 |
| C2 | `c2-registry` | 未开工 | — | — | — | — | 0 | 冻结 C-03/04/05/**C-15**/**C-18**（W2 中，config schema） |
| C3 | `c3-loop` | 未开工 | — | — | — | — | 0 | 冻结 C-06 |
| C3b | `c3b-skill` | 未开工 | — | — | — | — | 0 | 冻结 C-11；可与 C4/C5a/U1 并行 |
| C3d | `c3d-events` | 未开工 | — | — | — | — | 0 | 冻结 C-13/C-14；与 C3b 同批 |
| C4 | `c4-sdk` | 未开工 | — | — | — | — | 0 | **G1**，冻结 C-07 |
| C5a | `c5a-admin` | 未开工 | — | — | — | — | 0 | **G2**，第 1 天冻结 C-08、第 2 天冻结 **C-17**（获准写 `schema/`）；独立 listener :8444；setup token + 密码 + **设备密钥**；+`identity.py`（core 身份键）+`/admin/setup/unlock` +`/admin/bind` +`/settings` `/skills` `/memory`；**11 个子命令** |
| C5b | `c5b-canvas` | 未开工 | — | — | — | — | 0 | 依赖 C-08 |
| M1 | `m1-fs` | 未开工 | — | — | — | — | 0 | |
| M2 | `m2-shell` | 未开工 | — | — | — | — | 0 | |
| M3 | `m3-web` | 未开工 | — | — | — | — | 0 | |
| M4a | `m4-memory` | 未开工 | — | — | — | — | 0 | 冻结 C-12；v1.17 加 `kind/evidence/verified_at` 列、字符闸 `MEMORY_FULL` |
| M4b | `m4b-evolve` | 未开工 | — | — | — | — | 0 | 依赖 M4a + C3b + C3d，不与其他模块并行；v1.17 改五触发器 + `turn_review` + `lesson` |
| M5 | `m5-mcp` | 未开工 | — | — | — | — | 0 | |
| M6 | `m6-model` | 未开工 | — | — | — | — | 0 | |
| U1 | `u1-ui` | 已合并（**客户端仓**） | 见 `vade-client` `main` @ `31f8f9a` | — | — | — | — | **G3**。`ui/` 已于 2026-11-14 迁出为独立仓 `vade-client`，本行不再跟踪单一批次，实际进度见该仓 `docs/QUICKSTART.md` 状态表与主仓 `docs/rfc/LEDGER.md`。**已核实**：368 tests / 0 failures、三目标绿（`desktopTest` + `assembleDebug` + `compileKotlinWasmJs`）、APK 21,878,888 B、Wasm 链接产物 13,681,513 B、MSI 93,312,713 B。**未完成**：UI 数据仍是 fixtures（`w-wire` 接线中）；ask_user 卡片（`c-askuser`）未合并；模块页宿主（`g-module`）未落地。T5b 管理页 WebView 宿主未开工 |
| U2 | `u2-miuix` | 未开工（占位） | — | — | — | — | 0 | `MiuixSkin` 仍是**返回 material token 的占位实现**（`vade-client` `skin/Skins.kt`，带「未安装」角标），未注册真 Miuix 组件。底色板已支持暗色（`MiuixSkinEntry.dark`） |
| U3 | `u3-glass` | 未开工（占位） | — | — | — | — | 0 | `skin/glass/GlassSurface`（haze 24dp + 26%/34% 染色 + `colorEffects` tint）**已存在并被顶栏/底栏调用**，但 glass 皮肤本身仍是占位。⚠️ 用户已两次指出「看不出模糊」，根因是**玻璃背后没有内容经过**（容器内边距把内容裁在栏下），修复在 `g-glass` 进行中 |
| U4 | `u4-ios` | 暂缓 | — | — | — | — | 0 | **暂缓（用户 2026-11-13 决定）**：v1 首批只做 Android / Desktop / Web 三端，**G4 不作当前门禁**；原「G4；含 T6 iOS WebView 宿主 + P-256 设备绑定（+2 天）」保留为历史范围，恢复需用户重新拍板 |
| F1 | `f1-release` | 未开工 | — | — | — | — | 0 | **G5** |

### 附录：非批次变更

> 主表只放 23 个批次（C4 校验口径）；win-fix 是换机环境适配，无对应任务书，单列于此。

| 变更 | 状态 | 交付 commit | 审查 | 备注 |
|---|---|---|---|---|
| win-fix | 已合并 | `35c4dde`（merge，含 98617b8 / ccf79f0） | R1/R2 pass（报告：`../Vade-REVIEW-win-fix.md`） | 2026-11-13 换机 Windows 11 x86_64：uv 建 Python 3.12 venv、重编 `protocol/core/python/vade_protocol.dll`、两条 POSIX 权限位断言加 `os.name != "nt"` 守卫（Linux 仍校验）；全量 `pytest core/tests -q` → **378 passed**；环境见 `docs/dev-windows.md` |
| req-amend | 已合并（文档） | `2fa5470` | 待审查（纯文档，无代码；`docs/check_docs.py` 12 项全绿） | **AMEND-21 客户端功能需求 52 条**（FR-8.26～8.77：M 41 / S 11），追加为 §8.21，不改原文；收敛出 9 族协议缺口（R1–R9，**RFC 编号待编排者分配**）与 8 条待裁定；已在 §8 汇总表登记 AMEND-21 行，`check_docs.py` C3 上限随之上调为 21、C6 增查「AMEND-01～20」，README/OVERVIEW/overview.html/agents 索引的口径同步为「AMEND-01～21」；C1/C2 仍钉 D=86 / AC=69，故本节**未**新增 AC 与 D 编号，待 AC 台账解冻后补 |
| debt-a | 已合并 | `01e3d3d`（merge，含 3ef8a28 / 8fd0131 / a5da445 / 48d29c5 / 1902913 / 33de1a8） | R1+R2 **fail→返工→父验 pass**（`../Vade-REVIEW-debt-a.md`、`../Vade-REVIEW-debt-a-r2.md`） | 中断语义收口：turn 流所有写入统一经 `_turn_emit` 前置守卫（含 `turn.tool_call`）、已执行工具审计保留并标 `aborted:"interrupted"`、`_cancelled_turns` 按 turn 自身 task 释放（含 pre-task 窗口）、中断轮不再审计为 `turn_completed`；`_finalized`/`_cancel_fired` 有界；**384 passed**、contracts OK；RFC-0002 行号与登记方式订正 |
| rfc-inject | 已合并（文档） | `e0a3a44`（merge，含 f77add5） | 父验 pass（check_docs 12/12；`skill_load` 未占用、`llm_name` 为 `{prefix}__{tool}` 两点已复核） | **RFC-0003 提案 406 行**（status=proposed）：技能目录改为会话内持久 user 消息（digest 变则整条重发、空目录发退休条）+ 正文 `skill_load` 按需；工具热常驻/冷走 `tools/search`（每轮冻结快照、step 边界扩张）；system prompt 拆 stable/context + 中央 `SECTION_ORDERS`。6 条契约变更（C-11 追加 `injection`、C3b 行为破坏性、C-05 保留名、C-06 追加 `turn.tool_discovered`、C-18 开关、C-06 条件条）；7 条未决；成本估算首轮 +4% / 稳定期 −45% / 装技能后 −83%；默认 `prompt.injection=legacy` |
| rfc-attach | 已合并（文档） | `9ed1ec7`（merge，含 3ee5c9c） | 父验 pass（check_docs 12/12；`rpc.json:66` 帧上限与 `manager.py:323-325` 纯图被拒两点已复核） | **RFC-0004 提案 449 行**（status=proposed）：附件走数据面 `POST /ui/blob` 六路由（begin/part/complete/abort + Range 断点 + 按设备 sha256 去重）；压缩到长边 2048 / ≤5MB 单 `file_id`（D96）；消息改 content parts（`content` 保留为只读属性、旧会话读时合成）；契约 8 项（C-05 追加 `http_endpoints`/`ui/blob.token`、`ui/submit.content` 定型，C-18 新增 `[attach]` 17 键，C-06 **不**加 `attachment.*`）；安全四道 CSRF 闸 + 拒 multipart 强制预检 + 路径只由 sha256 推导；验收 A1–A14、未决 Q1–Q9 |
| rfc-model | 已合并（文档） | `d294d1f`（merge，含 891a75d / eadf8d2） | 父验 pass（check_docs 12/12；`turn.completed.usage` 现为未定型 `object` 已核实） | **RFC-0005 445 行**：`model/list` 能力字段（`input_modalities`/`max_image_bytes`/`max_image_px`/`max_images_per_turn`/`context_window`/`supports_tools`/`priority`）、会话级 `session/model.get` / `set`（会话显式 > `[llm].default_model` > `manifest.priority`）、用量两级口径（`session/usage.get` 累计 + 审计 `llm.call` 按模块归因）、预算**默认真阻断**（对齐 FR-8.49，新增 `BUDGET_EXCEEDED` 提案，usage 不可信时降级告警）；9 行契约变更、12 条验收、7 条未决 |
| rfc-turn | 已合并（文档） | `ad8f8bd`（merge，含 c1d02a4） | 父验 pass（`turn.interrupted` 仅三字段、`_finalize` 不写 messages 两点已核实） | **RFC-0006 424 行**：中断留痕选**落库** `partial=true` assistant 消息（`turn.interrupted` 只带指针）、工具卡追加 `args_preview`（按 risk 脱敏 ≤512B）/`started_at`/`batch_id`/`duration_ms`/`truncated`/`result_ref`、重生成/编辑重发 = 新 turn + 旧 turn `superseded_by`（v1 不做树/分叉）、`turn.delta` 追加 `part_index`/`kind`、单条 32k 上限；C-06 只追加、C-05 两个新方法、C-04 不新增码；7 条未决 |
| rfc-task | 已合并（文档） | `7192828`（merge，含 d047bb8） | 父验 pass（`turn.approval_required` 缺 `session_id` 但 `approval.py:156` 在发，漂移属实） | **RFC-0007 449 行**：统一 `task.*`，子代理 = `kind:"subagent"`；`task.started/status/progress/finished`（`parent_id`/`depth`/`action`/`summary≤500`）、进度 ≤2/s 合并 + `x-vade.coalesce`；`task/list`/`log_tail`（≤200 行 & ≤64KiB）/`cancel`（3s 硬上限）/`subagent/message`（默认 UNSUPPORTED）；审批复用 `turn.approval_required` 并补 `task_id`/`task_name`/`depth` 与漂移的 `session_id`；通知按 `task_id` 去重；C-18 新增 `[tasks]` 13 键；A1–A16、Q1–Q8 |
| rfc-ask | 已合并（文档） | `2f33ab5`（merge，含 21aebd7） | 父验 pass（`waiting_approval` 在全仓只有 models.py 里的枚举、无生产者；`docs/check_docs.py` 12/12） | **RFC-0008 450 行**：新增 `waiting_input` 与 `turn.input_required`/`turn.input_resolved` 事件对、`turn/input` + `turn/input.cancel` RPC；四类控件（free_text / single_choice / multi_choice / confirm）；超时收 `CANCELLED`（不用 `TIMEOUT`，避免 retriable 诱发自动重问）；多端同弹 + 幂等矩阵；安全上“回答永不能满足审批、审计只存 value_digest”；C-06 枚举拓宽、C-05 两方法、C-18 `[ask_user]` 6 键；A1–A14、Q1–Q7 |
| rfc-module | 已合并（文档） | `0652717`（merge，含 8dffd28） | 父验 pass（C-03 实为 `additionalProperties:true`、`ui_to_core` 只有 6 方法且 `tools/call` 仅在 `core_to_module`，两点已核） | **RFC-0009 449 行（C-19 提案）**：`ui.pages[]`（id/title/icon/entry/schema/version）；节点 = column/row/tabs/list + text + C-09 组件子集（7 白名单，4 个宿主组件出现即拒渲）；只读绑定三根；动作仅 navigate / `tools/call` / `ui/submit`，并提案 `ui/tool.call` 补 UI→core 缺口；未知 type/脚本 → 整页拒渲 + `MANIFEST_INVALID`；深度 8 / 节点 256 / 内联 8 KiB；入口（桌面右面板 / 手机能力 tab / 无页面无入口）；A1–A13、Q1–Q7 |
| rfc-approve | 已批准 | 见 `approved` 字段 | 用户逐条裁定 5 条（记录：`research/rfc-approvals.md`） | RFC-0003～0009 **全部批准**，实施顺序 **0004 → 0006 → 0005 → 0007/0008 → 0009 → 0003**；三项契约扩充获批：C-04 `BUDGET_EXCEEDED`（33→34）、C-05 `ui/tool.call`（UI→core 6→7）、C-06 `turn_states` 升 2.0.0 加 `waiting_input`；RFC-0009 的 fail-closed 覆盖 FR-8.78“降级占位”原句 |
| req-amend23 | 已合并（文档） | （见本提交） | 待审查（纯文档，无代码；`docs/check_docs.py` 12 项全绿） | **AMEND-23 七份 RFC 批准与五条裁定**：C-04 `BUDGET_EXCEEDED`（33→34）、C-05 `ui/tool.call`（`ui_to_core` 6→7）、C-06 `turn_states` 加 `waiting_input` 并升 2.0.0、追认 RFC-0009 fail-closed（覆盖 FR-8.78「降级为占位」）、实施顺序 0004→0006→0005→0007/0008→0009→0003；新增 D106–D110。**契约文件本节不动**，随各 RFC 实施时改 |
| client-split | 已迁出 | （见提交） | 用户裁定：客户端也算模块、submodule 取契约、**admin-ui 是 AgentCore 附属产物不算模块** | `ui/` 迁至 `vade-client` 独立仓（43 文件，保留 UI 相关历史）；主仓 `ui/` 只留指针 README；`00-orchestration`/`01-contracts`/`50-52` 任务书加路径口径说明；`admin-ui/` 不动 |
| rfc0004-parts | 已合并 | PR #16 squash `c4e81ac`，回同步 `58e9e66` | 规格审查 pass（5 应修已修）+ 复核变异承重 | **RFC-0004 后半**：`session/parts.py` 的 §6.4 校验阶梯、`Message.parts` + PRAGMA 可重入迁移、`ui/submit.content` 定型 `parts[]`（1–20）、`rpc.json` 1.1.0、顶层 `http_endpoints`；**part 元数据以 BlobRecord 为准**、`kind`↔`mime` 一致、无设备身份时 `file_id` fail-closed；**549 passed** |
| device-id | 已合并 | `24bd131` | 反向验证：删接线后 3 条主用例红 | **设备身份接线**：cookie 会话取 `/ui/session` 的 `ui-…`；Noise 握手后按 `remote_fingerprint` 在 whitelist 反查 `module_id#instance_id`；查不到保持 `None`（fail-closed）；含真 Noise 握手与 uvicorn 真 listener 用例；**555 passed** |
| rfc0007 | 已合并 | `67c5c49` | 待复审（两轮审查未走） | **RFC-0007 子代理/任务**：`task.*` 四事件 + 7 态、进度 ≤2/s 合并（`x-vade.coalesce`，重放不补）、`task/list|log_tail|cancel` + `subagent/message`(UNSUPPORTED)、审批归属四字段、`[tasks]` 13 键；`RingLog` 双上限 + 不透明游标；**607 passed**；601/607 差异已查实（s-hygiene 卫生批次复核）：601=分支 `tmp/rfc0007`（基线 `94f5995`=549+本批 52）上的全量，见 `core/vade/tasks/RESULT-rfc0007.md`；607=合并点全量——分叉后 dev 侧经 `32eb6c4`（设备身份接线，`tmp/device-id`）新增 6 条 transport 测试（`test_device_identity.py` 5 条 + `test_live_server.py` 1 条），基线 549→555，555+52=607。`pytest --collect-only` 四点实测：`94f5995`=549、`404c3f3`=601、`ebbc150`=555、`67c5c49`=607。与 rfc0005/0006 同为「分支时 vs 合并后全量」口径，LEDGER §4-6 的「无出处差异」由此结案 |
| rfc0006 | 已合并 | `46f1de6`（合并 `28dcada`） | 复审 pass（B1 + S1–S5 全关，四个变异承重） | **RFC-0006 turn 语义**：`Message.message_ref()` 统一 `msg:<hex>`；墓碑早返回不落幽灵消息；`edit_resubmit` 沿 `superseded_by` 取 root head；非 text part 的 turn 拒绝 regenerate（`UNSUPPORTED`）；工具卡密钥名归一化脱敏 + base64 ≥32；`turns.user_parts` 迁移；**670 passed** |
| rfc0009 | 已合并 | `c39885b` | 待复审（安全审查未走） | **RFC-0009 模块 UI 页面（C-19）**：`manifest.ui.pages[]` ≤8 + `$defs.page`（`additionalProperties:false`，唯一严格子树）；4 原语 + 7 白名单组件（4 个宿主组件出现即拒渲）；只读绑定三根；动作三类 + `open_url` 仅 http/https；限额 256 节点 / 深度 8 / 内联 8 KiB / ref 64 KiB / 块 48 KiB / state 8 KiB·64 键；环检测；`ui/page.get`/`ui/state.get`/`ui/tool.call` + 模块侧 `state/get`/`page/get`（rpc 1.2.0）；C-18 `[ui]` 4 键；`ui_warnings` 拒渲观测；**773 passed** |
| c19-fix | 已合并 | `1fa35c5` | 安全审查 FAIL → 八条修完；13/14 变异承重 | **RFC-0009 安全返工**：B1 调用方鉴权（`modulepage/auth.py`，身份只来自 connection）、B2 审批绑 `session_id`+`approver_device_id`、B3 只读拦截（含 `ui/submit`）、B4 ref 用 64KiB 上限、B5 CAS 不伪造 rev；S3 ref 缓存按 digest、S4 `open_url` 拒 userinfo/私网/编码控制字符、S5 `$defs/node` 闭合；**934 passed**；**B5 的锁是纵深防御（CAS 承重），不报变异已验证** |
| rfc0008 | 已合并 | `905a2dc` | 待复审 | **RFC-0008 ask-user**：`waiting_input` 入 `turn_states`（**C-06 升 2.0.0**，8 值）；`turn.input_required/resolved` 两事件（resolved 先于终态）；`turn/input` 幂等矩阵 + `turn/input.cancel`；四类控件值编码与闭合校验；超时收 `CANCELLED`（不给 `default`）；审计只存 `value_digest`/`value_chars`；`[ask_user]` 6 键；**829 passed**；**缺口：`ask_user` 未注册为 engine 内置工具** |
| rfc0005 | 已合并 | `69107a7`（合并 `ab10efe`） | 待复审 | **RFC-0005 模型与用量**：`model/list`（11 字段）、`session/model.get|set`、`session/usage.get`、预算 `enforce=block` 真阻断 + `BUDGET_EXCEEDED`（错误码 **34**）、`source="none"` 降级 `degraded=true`；`[llm]` 4 键 + `[usage]` 6 键；模块自报 `capabilities.models[]`（≤16）；**860 passed**；**限制：`Registry` 跨模块同名工具判 `TOOL_CONFLICT`，两个模型模块不能同时在线 → 跨模块 priority 不可达** |
| rfc0004a | 已合并 | PR #15 squash `3163166`，回同步 `fcd28ea` | 安全审查两轮：首轮 fail（3 阻断）→ 返工 → 复审 **pass**（0 阻断 / 1 应修 / 8 建议） | **RFC-0004 前半**：`core/vade/blob/` 六路由、id `^(?:blb|upl)_…`、sha256 推导路径、设备 token + cookie 四道闸、`multipart` 先于鉴权 415、分片/续传/幂等、写盘前配额、**引用计数**跨设备去重、周期 sweeper（ASGI lifespan）、服务端复校 + PNG 有界解压；`[attach]` 17 键；**489 passed**（+105 blob 用例）；未做：`parts[]` / `ui/submit` 定型（0004b 进行中）、`ui/blob.token`、缩略图/扫描管线 |

| rfc0010 | 已合并 | `e200772`（含 6a708c1） | 待审查（R1/R2 未走；合并前父验 966 passed + 两个变异承重） | **RFC-0010 会话元数据**：`session/*` 新增十方法（`group.create/update/delete/list/assign` + `session/list/rename/pin/archive/delete`），C-05 **1.3.0**；排序 `(pinned_at IS NULL) ASC, updated_at DESC, created_at DESC, id ASC`；`updated_at` 只随消息/turn 落库推进、attach 不推进；图标白名单 39 个 Tabler 名；删组不解除会话关联（置 `group_id=NULL`）；删会话拒活跃 turn；组不允许嵌套（`nesting_forbidden`）；12 files +2033/−14；**966 passed**、contracts OK、docs 12/12 |
| rfc0012 | 已合并 | `18dea95`（merge，含 c5efe27 / 6323839） | 待审查 | **RFC-0012 子代理事件族 R3**：`subagent.spawned/status/result` 三事件 + 顶层 `subagent_states` 5 值（spawned/running/completed/failed/cancelled）；`subagent/list|get` 两方法，**C-05 1.3.0 → 1.4.0**；`subagent_runs` 表（PRAGMA 可重入）；父 turn 终态前收口（断言 `subagent.result` seq < `turn.completed` seq）；**脱敏**：只发 `result_digest`(sha256) + `result_chars`，全文仅在子会话消息流；状态机闸非法转移 `INVALID_ARGUMENT` 且零写入；19 测试；**985 passed**（分支）；变异①删状态机闸→2 红、变异②digest 改全文→脱敏测试红 |
| rfc0013 | 已合并 | `4be7424`（含 eebb276） | 待审查 | **RFC-0013 中断留痕落库 R9**：`partial` 消息 + 指针进 `ui/attach` snapshot（复用 RFC-0006 §4 既有 `_finalize_interrupted` 单事务写入，`session/` 零代码改动）；C-06 `turn.interrupted` 旧注记「partial 只能从 turn.delta 重建」就地改写（明示偏离「不改 notes 字节」惯例，理由与审计写入 RFC §2.2/§6.1）；**FR-8.35「另一端 attach 可见已生成文本」自此可标通过**（A1 用全新 `SessionLog` + `attach(last_seq=None)` 零 delta 重放复现）；13 测试；**979 passed**（分支）；变异①不写 partial→7 红、变异②partial 恒 false→7 红 |
| rfc0003-impl | 已合并 | `daa9323`（含 7f595dc） | 待审查 | **RFC-0003 注入分层实现**：`prompt.injection`（默认 `legacy`，仅 legacy/layered）、`tools.hot`（默认 `[]`）、`tools.hot_max`（1..64，默认 12）三配置键；技能目录改为会话内持久 user 消息（digest 变则整条重发、空目录发退休条）；工具热常驻 / 冷走 `tools/search`（每轮冻结快照、step 边界扩张）；system prompt 拆 stable/context；C-06 追加 `turn.skill_catalog` + `turn.tool_discovered`（顶层 version 仍 2.0.0，走 `x-vade.appends`）；**顺带修真 bug**：no-capability 门原在 core 工具 graft 之前，无模块的 layered core 永远拿不到目录、`skill_load` 不可达（违反 §4.2.4）——graft 上移到门之前，legacy 路径不动；**967 passed**（分支）；变异①目录注入 no-op→5 红、变异②legacy 改道 layered→1 红（诚实备注：engine 级 golden 抓不到 M2，由装配级测试兜住） |
| askuser-tool | 已合并 | `bf2080b`（含 f3fb7cb） | 待审查 | **`ask_user` 注册为 engine 内置工具（RFC-0008 §14）**：`loop/builtins.py` `BuiltinRegistry`（lookup / LLM schemas / prompt block）+ `core/ask_user` 执行器；内置工具**不需要模块**——无能力门改成 `not snapshot and not self.builtins`；工具表 = 快照 ∪ 分层精选 ∪ `builtins.schemas()`；分派 `builtin` 走 `_call_builtin`（`core_tool=None`，不入 snapshot/discovery）；**无 UI 连接时工具仍在表里但调用立即返回 `UNAVAILABLE`**（§8.4「没人可问就现在说，别烧超时」）；6 files +1037；10 测试；**1041 passed**；合并时与 RFC-0003 在 `engine.py` 撞 6 处，全部按「两侧特性共存」解 |
| ledger | 已合并（文档） | （见本提交） | 待审查（纯文档，无代码；`tools/check_ledger.py` 自检 exit 0） | **RFC↔契约↔实现↔验收 总账**：`docs/rfc/LEDGER.md`（14 条 RFC 全覆盖；契约列以 `x-vade.appends` 为准，不以 RFC 自述为准）+ 可重跑自检 `tools/check_ledger.py` + 口径页 `docs/WHERE-ARE-WE.md` + 不一致清单 `docs/rfc/LEDGER-RESULT.md`；契约真值：C-05 1.5.0（三方向合计 **47** 方法）/ C-06 2.0.0（34 事件）/ C-04 34 码 / appends 10+7+3=20 块；全量 **1065 passed**（本 worktree 复跑） |
| s-modules-rfc | 待审查（纯文档 + `schema/rpc.json` 声明；无 core / 无客户端代码） | （见本提交） | — | **A3 模块清单通道补契约（RFC-0015，proposed）**：`ui_to_core` 追加只读方法 `ui/modules`——`params:{}`、`result:{revision:int, modules:[{module_id, instance_id, display_name, version, online, pages:[{id, title, icon, entry, version, requires, refresh_s}]}]}`（字段逐字对齐 C-03 manifest / `modulepage/service.py:202 pages()` / 客户端 `ModuleDeclaration`；**不用** `page_id`，三层都叫 `id`）；变化通知**不新增事件**——复用已有 `module.status` 作失效边 + `revision` 去重（`_publish_status` 与 `_bump` 同点，§5.2）；错误只用现有 `PERMISSION_DENIED`/`UNAVAILABLE`/`PAYLOAD_TOO_LARGE`（C-04 仍 34）；C-06 / C-03 / C-18 **不动**；**方法数 ui_to_core 35 / 合计 48**；登记块在 `x-vade.rfc0015_pending`（不是 append → LEDGER 记 `未实施`，不谎报已实现）；**C-05 顶部仍 1.5.0**（把 1.6.0 实测三处版本钉会红，`tools/check_contracts.py` 不许改；1.6.0 作为落地版本记在 pending 块）；门禁：`check_contracts` OK / `check_ledger` OK（15 行、48 方法）/ `check_docs` 12/12 / `pytest core/tests -q` 1089 |
| s-modules-impl | 待审查（core 实现 + 测试；无客户端改动） | （见本提交） | — | **A3 `ui/modules` core 落地（RFC-0015）**：`ModulePageService.list_modules()`（`core/vade/modulepage/service.py`，与 `pages()` 共用同一个 `_page_rows()` 投影）+ `ui/modules` handler（`modulepage/rpc.py`，注册进 `handlers()` → `install()` 发布到 `app.session_handlers`）；错误只用 `PERMISSION_DENIED`/`UNAVAILABLE`/`PAYLOAD_TOO_LARGE`（C-04 仍 34），`events.json` 未动；**顺带修两个真 bug**：① C-03 `display_name` 一直被 `registry/manifest.py` 的 `Manifest` dataclass 丢掉（schema 有、dataclass 无），本棒补字段+解析；② `modulepage.install()` 原先只在 `session_handlers` 不存在时挂 provider，而更早的组件已建好该 dict、`session.install()` 又整体**替换**它 → 真 core（`startup()`+`install_components()`）上 `ui/page.get`/`ui/state.get`/`ui/tool.call` 一律回 `unknown method`（单元测试全绿也照不到），现改为两条路径都发布（`setdefault` 幂等），由新的真 WS e2e 承重；**版本号未升**：实测顶部 1.6.0 仍红 `check_contracts` 三处版本钉（`check_session_meta:1356` / `check_subagent_events:1096` / `check_session_search:1480`），`tools/check_*.py` 本棒不许改，故保留 `x-vade.rfc0015_pending`（文本改为「core 已实现、契约登记 pending」）、C-05 顶部仍 1.5.0、LEDGER 行 `impl_state` 按门禁口径仍 `未实施`（正文写明不是没写 core）；**测试 32 条新增**（空 registry / 逐字段 / 顺序= snapshot 顺序 / online-offline / display_name=null / revision 递增 / 未鉴权 / 模块只见自己 / 禁用对浏览器不可见 / 未知参数容忍 / 与 `pages()` 逐键一致 / 无正文泄漏 / 超大 → `PAYLOAD_TOO_LARGE` / 真 WS 端到端）；门禁：`pytest core/tests -q` **1164 passed**（基线 1132 + 32，本 worktree 复跑 678.90s）、`check_contracts` OK、`check_docs` 12/12、`check_ledger` OK（15 行 / 48 方法） |

## 里程碑

| 里程碑 | 状态 | 证据链接 |
|---|---|---|
| G1 端到端 + SDK 冻结 | 未达成 | — |
| G2 审批/审计/Admin 闭环 | 未达成 | — |
| G3 三端聊天 | 未达成 | — |
| G4 iOS 真机 | 暂缓（用户 2026-11-13 决定） | — |
| G5 抓包 0 泄漏、可交付 | 未达成 | — |

## 待定 / 风险

| # | 事项 | 状态 | 负责人 |
|---|---|---|---|
| 1 | W-A/W-B 裁决 | 待 C0b | `c0b-wasm` |
| 2 | Miuix 三端可行性 | **已验：Desktop 真实窗口 + Wasm 构建 + Android APK（升 AGP 9.5.1 矩阵）**；弹窗需 Miuix `Scaffold`（C0c） | `c0c-miuix` |
| 3 | iOS 路径 A/B 裁决 | **暂缓**（U4 押后，用户 2026-11-13 决定） | `u4-ios` |
| 4 | Web 端 glass 是否能用 CSS backdrop-filter | 待 U3 | `u3-glass` |
| 5 | 知识库走哪条路（MCP vs 自建模块） | **已定：MCP + mcp-bridge**（D37） | 编排者 |
| 6 | skill 是否进 v1 | **已定：进，C3b**（D38）；不含执行通道 | 编排者 |
| 7 | 自进化是否进 v1 | **已定：进，M4b**（D41）；默认关闭，需显式开启 | 编排者 |
| 8 | 记忆层数与命名 | **已定：session / global 两层**（D40） | 编排者 |
| 9 | 浏览器能否批 high | **已定：不能**，只读；只有 `device_key` 与 loopback 管理员可批（D48） | 编排者 |
| 10 | `/admin` 与 `/app` 同源 | **已定：必须分离**，三面不同源（D51）；C2 不再 mount admin | 编排者 |
| 11 | 配对码长度与形态 | **已定：8 位中英混合**（D52），180s，消费即删；影响 6 处（含 R3 清单） | 编排者 |
| 12 | 管理面鉴权方式 | **已定：密码 + 远程公钥**（D54），废弃登录用 token；setup 准入见 D57 | 编排者 |
| 13 | 管理面能否对外 | **已定：允许**（D55），非回环写操作强制双因素；**SSH 路径已删除**（D70），浏览器降为只读 | 编排者 |
| 14 | 加密轮换口径 | **已定：rekey 24h/1GiB/2^20 帧 + generation**（D56）；24h 另起重握手 | 编排者 |
| 15 | setup 期公网准入 | **已定：回环免 token / 非回环凭 `setup_token`**（D57），取消 30min 硬超时 | 编排者 |
| 16 | setup token 形态 | **已定：`acs_` + 32B base64url，存 `data/setup.token`，消费即删**（D58） | 编排者 |
| 17 | token 有效期 | **已定：每次启动未初始化就换新**（D59）；env 固定则不换 | 编排者 |
| 18 | 配置页能改什么 | **已定：模块治理 + 技能 + 记忆 + 布局**（§7.9），`core.toml` 只读展示 | 编排者 |
| 19 | 配置页能否从客户端打开 | **已定（v1.15 反转）：App 内 WebView 为主**（D65），系统浏览器只读；仍禁模块代理 | 编排者 |
| 20 | 公钥能否 Web 上传 | **已定：能，但需已登录 + step-up 重输密码**（D62），上限 5 台；主路径改为 App 扫码绑定（D67） | 编排者 |
| 21 | 能否跳过 setup 页面 | **已定：`core admin init --password-stdin`**（D63） | 编排者 |
| 22 | 技能安装是否立即生效 | **已定（v1.15）：不生效**，先进 `pending/`，人工批准才 `active`（D64） | 编排者 |
| 23 | 设备密钥算法 | **已定：P-256**（Secure Enclave 只支持 P-256，Ed25519 在 iOS 上不可用）；Ed25519 仅软件密钥（D68） | 编排者 |
| 24 | 配置页在 WebView 里是否安全 | **已定：只加载 App 内置资源 + core 只返回 JSON**；加载远程 HTML + JS 桥是明确禁止项（D66/§7.11） | 编排者 |
| 25 | setup 页二维码是否可公开 | **已定（v1.16）：不行**，非回环必须先 `unlock`；二维码里是 26 字符一次性票（D73） | 编排者 |
| 26 | 绑定信封是否防主动 MITM | **已定（v1.16）：靠 `hello_sig`**，覆盖 `kex_pub`/`challenge`/`ticket_hash`/host（D74） | 编排者 |
| 27 | 票被错签名烧掉怎么办 | **已定（v1.16）：先验后删**，失败 ROLLBACK 不消费（D75） | 编排者 |
| 28 | 回环浏览器能否写 | **已定（v1.16）：不能**，cookie 一律只读，Bearer 才可写（D76） | 编排者 |
| 29 | 自进化多久跑一次 | **已定（v1.17）：每 10 轮 `turn_review`**（D80），会话末兜底；`flush_min_turns=6` 挡短会话 | 编排者 |
| 30 | 记忆按条数还是 token 限容 | **已定（v1.17）：字符硬顶 4200（可配）+ 条数双闸**（D79）；写超 `MEMORY_FULL` | 编排者 |
| 31 | 失败/自纠能不能学到东西 | **已定（v1.17）：能，但只产 `lesson`**（D81），带证据、标 unverified；不产 skill | 编排者 |
| 32 | 配置项散落没有 schema | **已定（v1.17）：C-18 全量 schema**，未知键拒绝启动（D85） | 编排者 |
| 33 | 产品叫什么 | **已定：Vade Agent Runtime / 简称 Vade**（原名 Vademecum，现更名），中文「偕行」；技术标识一律小写 `vade`，配置文件名沿用 `core.toml` | 用户 |
