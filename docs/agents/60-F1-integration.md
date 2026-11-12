# F1 · 联调 + 安全验证 + 打包（G5）

| 项 | 值 |
|---|---|
| Agent 代号 | `f1-release` |
| 波次 | W6（全部批次合并后） |
| 依赖 | C / M / U 全部出口 |
| 目录所有权 | `deploy/`、`tests/e2e/`、`release/` |
| 产出 | 可交付的 core 镜像、模块镜像、三端产物、验收报告 |
| 出口 | **G5**：抓包 0 泄漏，**69 条 AC** 全过 |

## 一、一句话任务

把所有零件装成一台能交付的机器：一条 `docker compose up` 起全套，跨机接入不用改配置，抓包看不到明文，69 条验收标准逐条有证据。

## 二、边界

| 做 | 不做 |
|---|---|
| 部署编排、跨机联调、混沌测试 | 不改业务代码（发现问题提回原 agent） |
| 安全验证（抓包、扫描、渗透用例） | 不做新功能 |
| 打包、版本号、发布说明、回滚 | 不做 L3 部署编排（D32） |
| 69 条 AC 的证据归档 | 不掩盖失败项（失败就写失败） |

**你只改自己的三个目录**。发现别处有 bug：写 `release/ISSUES.md`，列「现象 / 复现 / 影响批次 / 建议」，由编排者派回原 agent。

## 三、必读

- `docs/requirements.md` §5（AC-1～AC-8）、各 AMEND 里的 **AC-9～AC-69**（不要只读到 48）
- `docs/design.md` §10（部署拓扑）、§13（验证）
- 所有批次的交付说明（`docs/status.md` 的链接）

## 四、任务分解

### T1 · 部署编排（2 天）

```
deploy/
├── docker-compose.yml
├── core/
│   ├── Dockerfile
│   └── core.toml
├── modules/            # 每个模块一个 Dockerfile（各模块自带，这里只引用）
├── .env.example        # 所有凭据用占位符
└── README.md           # 一页启动指南
```

要求：
- core 容器：`/data` 卷挂载（密钥、SQLite、审计、模块密钥）；**镜像内不含任何密钥**。
- 模块容器：`CORE_URL`、`MODULE_ID`、`MODULE_KEY_PATH`、`CORE_FINGERPRINT` 注入；密钥目录必须挂卷（否则重启指纹变化被拒）。

  **只有这四个 env 的话，六个模块起来全是空壳**——每个都有必填配置，缺了不是「降级」而是「拒绝服务」：

  | 模块 | 必填 env | 不填的后果 |
  |---|---|---|
  | `fs` | `FS_ROOTS`（冒号分隔的绝对路径，格式以 M1 的 README 为准；读写分离用 `FS_WRITE_ROOTS`） | roots 为空 = 全部拒绝，`read_file` 一律 `PERMISSION_DENIED` |
  | `shell` | `SHELL_ALLOWLIST`（可选，默认空）、`SHELL_WORKDIR` | 白名单空 = 什么都不让跑 |
  | `web` | `WEB_SEARCH_BACKEND`（`none` 可关） | 搜索工具直接不可用。**没有放行私网的开关**——SSRF 防护是硬约束（AC-16），要访问内网请做 MCP 模块 |
  | `memory` | `MEMORY_EMBED_BASE_URL`、`MEMORY_EMBED_MODEL`、`MEMORY_API_KEY` | 启动即报「未配置 embedding」 |
  | `mcp-bridge` | `BRIDGE_UPSTREAM`、对应上游的凭据 env | 无上游 = 0 工具，注册个空模块 |
  | `model` | `MODEL_BASE_URL`、`MODEL_NAME`、`MODEL_API_KEY` | core 切 `provider=module` 时报 `UNAVAILABLE` |

  每个模块的 `README.md` 必须有一节「环境变量」，列出**名字、是否必填、默认值、示例**。F1 的 `compose` 按这张表逐个注入，并在 `.env.example` 里给占位。
- 每个模块容器挂自己的 `/data` 卷（`memory` 的 `memory.db`、`fs` 的缓存、`mcp-bridge` 的 `manifest.json` 都在里面）。不挂卷 = 每次重启丢状态，`memory` 尤其明显。
- TLS：core 证书 SAN 覆盖 `core`（容器名）、`localhost`、宿主 IP；模块用 `CORE_FINGERPRINT` pin。
- `depends_on` 只保证启动顺序，不保证就绪；模块必须能容忍 core 未就绪（本来就要重连）。
- 一键：`docker compose up -d --build`；日志：`docker compose logs -f core`。
- 资源限制：core `cpus: 2 / mem: 1g`；每模块 `cpus: 1 / mem: 512m`（可按需调，写进 README）。

### T2 · 跨机与故障（2 天）

| 场景 | 做法 | 判据 |
|---|---|---|
| 跨机接入 | 宿主机 A 跑 core，宿主机 B（或另一 docker network + 端口映射）跑模块 | core 零改动零重启，工具立即可用（AC-1） |
| 模块崩溃 | `kill -9` 某模块 | 相关调用返回可解释错误，会话不崩（AC-2） |
| core 重启 | 重启 core 容器 | 模块 5s 内重连并重注册（NFR-4）；未决审批作废并通知 UI |
| 网络分区 | `iptables` 丢弃模块→core 流量 30s | UI 显示 degraded/offline；恢复后回绿（AC-18） |
| 50 模块 | 用假模块起 50 个连接 | 稳定 30 分钟，内存/CPU 记录（NFR-1） |
| 慢模块 | 注入 5s 延迟 | 不阻塞其他模块调用（NFR-5） |

### T3 · 安全验证（2 天）

**抓包（AC-3）**：

```bash
# 1) 让客户端写 TLS keylog（仅测试环境），便于解开 TLS 后继续验证应用层
export SSLKEYLOGFILE=/tmp/tls.keys
sudo tcpdump -i any -s 0 -w /tmp/core.pcap 'tcp port 8443'
# 发一条含独特关键字的工具调用，例如 read_file /tmp/canary-7f3a.txt

# 2) 先确认 TLS 层确实加密
strings /tmp/core.pcap | grep -c "canary-7f3a"        # 必须 0

# 3) 再用 keylog 解开 TLS，检查应用层仍看不到明文
tshark -r /tmp/core.pcap -o tls.keylog_file:/tmp/tls.keys -T fields -e tcp.payload \
  | xxd -r -p | grep -c "canary-7f3a"                 # 必须 0
```

第 3 步是关键：**只测第 2 步是假绿**——TLS 本来就会挡住明文，证明不了 Noise 在生效。解 TLS 后仍是密文，才说明应用层加密真的在工作。

另做两条控制实验：
- 单元级：加密一条 canary，断言密文中不含明文（证明 AEAD 生效）；
- 进程自检：`/admin/api/health` 暴露 `e2e_enabled=true`，且 core 启动日志有 `crypto_gateway: noise=IK`。

**其余**：

| 项 | 方法 | 判据 |
|---|---|---|
| 吊销 | 配置页删指纹 | 下次连接失败（AC-4）；已连接立即断 |
| 白名单 | 未登记模块连接 | 拒绝 + `rejected` 记录 |
| 高危审批 | 不确认 60s | 不执行，超时拒绝（AC-5） |
| 路径逃逸 | `fs` 传 `../../etc/passwd` | 拒绝（AC-16） |
| SSRF | `web` 传 `169.254.169.254` | 拒绝（AC-16） |
| Admin 泄漏 | 扫全部 `/admin/api/*` | 密钥模式 0 命中（AC-19） |
| 依赖漏洞 | `pip-audit` / `cargo audit` / `npm audit` | 无 high/critical，或有明确豁免理由 |
| 密钥入库 | `gitleaks detect` | 0 命中 |
| 端口暴露 | `nmap` core 主机 | 只开 8443（数据面）与 8444（管理面）；8444 对外时必须至少绑定 1 台设备 + 强密码，否则 core 应拒绝启动 |

### T4 · UI 组合验收（1.5 天）

```bash
ls ui/shots/miuix/*.png ui/shots/glass/*.png ui/shots/material/*.png
```

- AC-13：3 皮肤 × 3 底栏 = 9 组合（同一构建产物内）；
- AC-15：3 皮肤 × 3 底栏 × 3 平台（Android/Desktop/iOS）= 27，**平台支持的组合**全部有截图；
- iOS 由 U4 提供 4 张；F1 只做汇总与缺失标记，不重拍。
- 汇总表：`release/AC-matrix.md`，每格「通过 / 不适用（原因）/ 未通过」。

### T5 · 打包与发布（2 天）

```
release/
├── AC-matrix.md          # 69 条 AC 的证据索引
├── QUICKSTART.md         # 手机从零到能用的 5 步（AC-55/AC-56 的证据）
├── RUNBOOK.md            # 云端部署、token 获取、故障恢复、**不出现 ssh**
├── SECURITY.md           # 安全模型、已知边界、部署检查清单
├── CHANGELOG.md
├── VERSION               # 语义化版本
└── artifacts/            # 不放二进制进 git，只放校验和与下载说明
```

产物：
- core：`ghcr.io/<org>/vade-core:<version>`（镜像 digest 记录）
- 模块：6 个镜像 + echo
- UI：Android APK（debug 签名即可，注明非商店版）、Desktop 包（dmg/zip）、Web 静态包（由 core 托管）、iOS（需 Apple 开发者签名，交付源码 + 构建说明）
- SDK：`vade-sdk==0.1.x` wheel + `vade-protocol` wheel

发布说明必须含：
1. 版本与 commit；
2. 破坏性变更（应为无）；
3. 已知问题（含未验证项，例如无 macOS 时的 Swift 绑定）；
4. 升级步骤与**回滚步骤**（镜像 tag 回退 + 数据兼容说明）；
5. 安全模型一段话：私钥在 core、浏览器止于 TLS、模块不反向调用。

### T6 · 交付评审（1 天）

按 `docs/agents/71-R2-correctness-review.md` 自检一遍，再请 R1/R2/R3 各跑一次。**不要自己给自己发通过**。

## 五、验收（G5）

| # | 判据 | 证据 |
|---|---|---|
| AC-1 | 跨机模块零改动接入 | 两端日志 + 工具调用结果 |
| AC-2 | 拔模块不崩 | kill 后继续对话的记录 |
| AC-3 | 抓包 0 明文 | pcap + 解 TLS 后的 grep 输出（含单元对照） |
| AC-4 | 吊销即失效 | 重连失败日志 |
| AC-5 | 高危未确认不执行 | 审批超时记录 |
| AC-6 | 模块独立运行 | 每模块 `--standalone` 输出 |
| AC-7 | 换 UI 模块 core 不变 | 切换前后 core 日志 |
| AC-8 | 配置页可管 | 截图 + 操作审计 |
| AC-9 | 裸进程与 Docker 行为一致 | 同工具同参数输出 diff |
| AC-10 | 多端 500ms 同步 | 时间戳 |
| AC-11 | 断网 30s 补齐 | seq 日志 |
| AC-12 | 交替 3 条有序 | 事件序列 |
| AC-13 | 9 组合（Android/Desktop/Web 各 3） | 截图，`ui/shots/material|miuix|glass/` |
| AC-14 | iOS 玻璃 | 真机截图 |
| AC-15 | 27 组合（3 皮肤 × 3 底栏 × 3 平台） | 汇总表；**平台不支持的格必须写理由**，不能空着 |
| AC-16 | 越权拒绝 | 原始报错 |
| AC-17 | 布局持久化 | 刷新前后 |
| AC-18 | 断连 1s 变灰 | 录屏/时间戳 |
| AC-19 | Admin 0 密钥 | 扫描输出 |
| AC-20 | 停用即生效 | 下一轮工具表 diff |
| AC-21 | bridge 上游断链隔离 | `kill -9` 单一上游 → 只有该节点 `offline`，其他 bridge 与模块照常（证据：拓扑截图 + 两次调用日志） |
| AC-22 | skill 注入三种情形 | 正常 / `requires_tools` 缺失 / 正文 12KB → 三种 prompt 快照；**同一输入两次生成逐字节相同**（`sha256` 对比） |
| AC-23 | 跨会话晋升 | 两会话各说一次 → `global` 只 1 条；单会话说过 → 仍在 `staging`，`recall` 查不到 |
| AC-24 | 冲突软失效 | 新条 `supersedes` 旧条；默认 `recall` 只返新条；`include_superseded=true` 能看到链 |
| AC-25 | skill 演进不重复 | 同任务跑两遍 → 第二次是 `patch` 不是 `create`；`pending/` 不参与注入 |
| AC-26 | 关闭即零动作 | `enabled=false` 跑完整个会话 → 无 `events/deliver`、无新记忆、无新提案；日志有 `evolve disabled` |
| AC-27 | 未声明订阅被拒 | 未在 manifest 声明 `subscriptions` 的模块调 `events/subscribe` → `rejected` 含主题名与原因 |
| AC-28 | 跨会话不串记忆 | 两个会话写同一句话 → `memory.db` 里 2 条；global 层同句仍 1 条 |
| AC-29 | risk 降级被拒 | 模块提交 `capabilities.update` 把 `high` 改 `low` → 整份拒绝，旧能力仍可用 |
| AC-30 | 浏览器不能批 high | `browser_session` 调 `approval/decide` 批 high → `APPROVER_INVALID`，审计留痕 |
| AC-31 | 自封 evolve 被拒 | 未进 core `[evolve] allowlist` 的模块声明 `role=evolve` 并订阅 → 拒绝 |
| AC-32 | prompt 可复现 | 同一输入两次 `assemble` → `sha256` 相同；`turn.completed.skills` 与当轮实际激活一致 |
| AC-33 | 管理面跨源隔离 | webui 控制台 `fetch` 管理面被 CORS 拦；伪造 Origin 的 curl → `403 ORIGIN_MISMATCH` |
| AC-34 | 配对码一次性 | 匹配 `^[汉字]{2}-[A-Z2-9]{6}$`；两种写法首次均成功、第二次均失败；码过期后 180s 边界正确 |
| AC-35 | E2E 口径一致 | 拓扑与 `/admin/api/nodes` 的 `e2e` 字段一致：模块/设备 `true`，浏览器与 bridge 上游 `false` |
| AC-36 | 管理面双因素 | 远程四种组合（只密码/只签名/未登记设备/双因素齐）结果正确：只密码 → 200 只读，其余失败同为 `AUTH_FAILED`（AMEND-18 修订） |
| AC-37 | setup 边界 | 回环免 token 成功；非回环无 token `401 SETUP_UNAUTHORIZED`；非回环带对 token 成功；完成后 `GET /admin/setup` 302 `/login`、`POST` 404 |
| AC-38 | rekey 正确 | 旧 key 帧被拒；generation 回退被拒；rekey 前后各 1000 帧无丢失无重复 |
| AC-39 | setup token 生命周期 | `print` 与文件一致；`rotate` 后旧值 401、新值 200；`reset` 后重新进 setup 且 token 变化；env 注入时写回文件 |
| AC-40 | 无凭据不得对外 | 非回环 `0.0.0.0` + 未初始化 + 无可用 token → 拒绝启动；已初始化 + 空 `devices.json` → 拒绝启动 |
| AC-41 | 重启即轮换 | 未初始化 + 无 env，启动两次 token 不同，旧值 401；设 env 时两次相同 |
| AC-42 | 残留清理 | 已初始化后放回 `data/setup.token`，重启后文件消失，`/admin/setup` 仍 302 |
| AC-43 | 单实例 | 同一 `data/` 起第二个 core → 非 0 退出，日志含锁占用 |
| AC-44 | 配置页能力边界 | `/settings` 改密后其它设备 401、当前仍可用；`/skills` approve 后 `rev+1` 且状态转 `active`；`core.toml` 参数无编辑框，只显示 `value + source` |
| AC-45 | 技能安装 | 三来源各成功一次；`../evil` zip 被拒且无越界写入；>5MB 被拒；重名被拒；无 `preview_token` 被拒 |
| AC-46 | 记忆管理 | `staging` 经页面晋升后 `active` 且写审计；归档可恢复；`PATCH /memory/{id}` 返回 404 |
| AC-47 | 客户端入口 | `ui` 代码与 manifest 无 `/admin/api` 转发；点击入口在 App 内 WebView 打开管理页并完成登录（AMEND-17 反转：原为唤起系统浏览器） |
| AC-48 | 非交互初始化 | `core admin init` 后 `/admin/setup` 302 → `/login`、token 文件不存在；运行中执行报错且不改文件 |
| AC-49 | 安装即 pending | install 后 `status=pending`，不在 prompt 工具目录、不能被激活；approve 后 `active`、`rev+1`、`.history/` 有新条目 |
| AC-50 | 安装期不执行脚本 | zip 含 `setup.sh` → `INVALID_ARGUMENT` 且磁盘无该文件；同 zip 改 1 字节后 install → `preview_token` 不匹配被拒 |
| AC-51 | WebView 只加载本地资源 | release 包无 `https://` 的 `loadUrl`；桥方法数 = 3；`onReceivedSslError` 只有 `cancel()` |
| AC-52 | 桥接签名防护 | JS 调 `sign("arbitrary text")` 被拒；正常登录弹原生确认，取消即中止 |
| AC-53 | 浏览器只读 | 非回环与回环各测：`GET` 200、`POST /skills/install` → `403 READ_ONLY_SESSION`、`POST /login` 不被拦 |
| AC-54 | 设备绑定全链路 | 扫码后 `devices.json` 有 `alg=p256`、`hardware_backed=true`；同票第二次 401；改 `fp` 一位本地中止 |
| AC-55 | SSH 已移除 | 用户文档无 `ssh -L` / `ssh-keygen`；`core admin proxy` help 标「开发者工具」 |
| AC-56 | 无电脑可完成 | 仅手机 + 对外 core，从零到「看到拓扑」不碰命令行 |
| AC-57 | setup 门禁 | 非回环未解锁 `GET /admin/setup` 响应体无 `bind_url`/`fp8`/`acs_`；unlock 错 token 401、对 token 200 且 `t` 为 26 字符 base32 |
| AC-58 | 问候签名 | 改 `kex_pub` 或 `challenge` 一位 → App 在 POST 前中止且显示签名无效；core 侧该票未消费 |
| AC-59 | 票不被烧 | 同票先错签名 401，再正确签名成功；并发两请求仅一个成功 |
| AC-60 | 只读按凭据 | 回环浏览器 `POST /admin/api/layout` → 403；同会话 `POST /login` 正常；Bearer 同请求 200 |
| AC-61 | refresh 不出原生层 | WebView 的 `window`/`localStorage`/`sessionStorage` 搜不到 refresh token；无签名刷新 → 401 |
| AC-62 | 注入预算 | 100 轮会话，`session` 层注入 ≤1200 token/轮；`session_close` 日志 `input_kind=rolling`，无全文输入 |
| AC-63 | 轮次复盘 | `nudge_interval=10` 跑 25 轮 → 恰好 2 次 `turn_review`；每次输入 ≤1.5k token；产出 ≤3 条 + ≤1 lesson |
| AC-64 | 短会话不烧钱 | 3 轮寒暄 → 无 LLM 调用，日志 `evolve_skipped_low_signal`，`staging` 为空 |
| AC-65 | 字符硬顶 | 写入至 >4200 字符 → `MEMORY_FULL`；当轮合并后重试成功；前后条数对得上（无静默丢失） |
| AC-66 | 失败也学 | 失败任务 + 有证据 → 1 条 `lesson`（`unverified`，confidence ≤0.6），**无 skill 提案**；`recall` 带 `evidence` |
| AC-67 | skill 四段 | 缺 `Pitfalls` 的 `SKILL.md` → 报 `missing section: Pitfalls`；补齐后加载成功 |
| AC-68 | 三层加载 | 64 个 active skill → L0 ≤3000 token；激活 3 个 → 正文 ≤12KB；`references/` 不在 prompt 里 |
| AC-69 | 配置热改 | 页面改 `nudge_interval` → `overrides.json` 有值、`core.toml` 未变、审计有 `config.changed`、重启后仍生效 |

性能（NFR）：握手 < 50ms、1KB 加解密 < 2ms、重连 < 5s、冷启动 < 3s，各 20 次取 P95。

## 六、禁止

- 禁止修改 `core/`、`modules/`、`ui/`、`admin-ui/`（发现问题写 ISSUES）。
- 禁止在部署文件里写真实凭据（`.env.example` 只能占位）。
- 禁止跳过失败项写"基本通过"。
- 禁止用「TLS 已经加密了」解释 AC-3（必须证明应用层加密在生效）。
- 禁止把二进制产物提交进 git。

## 七、交付格式

按 `00-orchestration.md` §三，额外附：`AC-matrix.md` 全文、pcap 证据路径与命令、镜像 digest、回滚演练记录（真的回滚一次再升回来）。
