# 开发台账

> 每次派活、收活、审查、合并后更新本表。状态枚举：`未开工 / 进行中 / 待审查 / 返工中 / 已合并 / 阻塞 / 暂缓`（`暂缓` = 用户明确决定不在当前范围，恢复需重新拍板）。
> 阻塞必须写原因与解除条件。

| 批次 | Agent | 状态 | 交付 commit | R1 | R2 | R3 | 返工次数 | 备注 |
|---|---|---|---|---|---|---|---|---|
| C0a | `c0a-uniffi` | 未开工 | — | — | — | — | 0 | 硬时限 3 天 |
| C0b | `c0b-wasm` | 未开工 | — | — | — | — | 0 | 硬时限 3 天，出 ADR-001 |
| C0c | `c0c-miuix` | 进行中 | —（分支 `tmp/c0c-miuix`） | — | — | — | 0 | **Android 侧阻塞**：Miuix 0.9.2～0.9.4 的 AAR 均要求 `minCompileSdk=37`，当前 AGP 8.8.2 无法使用 API 37（无裸 android-37 平台、`compileSdkVersion("android-37.0")` 被拒、目录联接被判 inconsistent）。已验：Desktop 无头 `ImageComposeScene` 渲染通过（8 张 `miuix-desktop-*.png`，**非窗口截图**；Dialog 那张只出触发按钮，真实窗口未跑）、Wasm 只验 `:composeApp:wasmJsBrowserProductionWebpack` 构建通过（**浏览器运行时未跑**；同界面替换口径下 `app.wasm` gzip 936,611 B vs Material 1,013,216 B，小 76,605 B；三皮肤共存增量需 U2 另测）。解除条件：升 AGP 8.13+/9.x + Gradle ≥8.14.4 后复跑 `assembleDebug`。证据 `spike/miuix/RESULT.md` |
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
| 2 | Miuix 三端可行性 | **已验（有保留）：Desktop 无头渲染、Wasm 构建；Android 待升 AGP**（C0c） | `c0c-miuix` |
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
