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
| U1 | `u1-ui` | 未开工 | — | — | — | — | 0 | **G3**，第 3 天冻结 C-09；含 **T5b 管理页 WebView 宿主 + 设备绑定（+2 天，仅 Android/Desktop）** |
| U2 | `u2-miuix` | 未开工 | — | — | — | — | 0 | 依赖 C-09 |
| U3 | `u3-glass` | 未开工 | — | — | — | — | 0 | 依赖 C-09 |
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
| rfc0004a | 已合并 | PR #15 squash `3163166`，回同步 `fcd28ea` | 安全审查两轮：首轮 fail（3 阻断）→ 返工 → 复审 **pass**（0 阻断 / 1 应修 / 8 建议） | **RFC-0004 前半**：`core/vade/blob/` 六路由、id `^(?:blb|upl)_…`、sha256 推导路径、设备 token + cookie 四道闸、`multipart` 先于鉴权 415、分片/续传/幂等、写盘前配额、**引用计数**跨设备去重、周期 sweeper（ASGI lifespan）、服务端复校 + PNG 有界解压；`[attach]` 17 键；**489 passed**（+105 blob 用例）；未做：`parts[]` / `ui/submit` 定型（0004b 进行中）、`ui/blob.token`、缩略图/扫描管线 |

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
