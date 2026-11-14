# 快速开始（QUICKSTART）—— 从零起 core，并让客户端真连上

> **本文实跑范围**：所有 **core 侧**命令（起服务、取码、`/healthz`、兑换 cookie）都在
> Windows 11 + git-bash 上按原文复制执行过（2026-11-14）。**客户端 GUI 没有在本轮重新启动验证**，
> 涉及它的结论来自客户端仓源码与其 E2E 用例，已逐条标注路径。
>
> 客户端 UI 自己的编译/打包/测试请读**客户端仓**的 `docs/QUICKSTART.md`；本文只覆盖
> 「core ↔ 客户端」这一段接线，细节与故障排查见本仓 `docs/PAIRING.md`。

## 0. 前提

| 项 | 值 |
| --- | --- |
| 本仓（core） | 就是你现在这个仓库，Python 3.12 venv 在 `.venv/` |
| 客户端仓 | 另一个仓（示例路径 `D:/HermesWorkspace/Vade-client`） |
| 原生绑定 | `protocol/core/python/vade_protocol.dll`（Windows）/ `.so`（Linux）必须存在 |

## 1. 起 core

**Windows（git-bash / MSYS）** —— `PYTHONPATH` 分隔符是 `;`：

```bash
cd /d/HermesWorkspace/Vade/Vade
export PYTHONPATH="core;protocol/core/python"
.venv/Scripts/python.exe -m vade --config core.toml
```

**Linux / macOS** —— 分隔符是 `:`：

```bash
cd ~/Vade
export PYTHONPATH="core:protocol/core/python"
.venv/bin/python -m vade --config core.toml
```

`core.toml` 最小内容（`listen` / `data_dir` 都有默认值，写出来只为明确）：

```toml
[core]
listen = "127.0.0.1:8443"
data_dir = "./data"
```

## 2. 拿配对码

监听端口就绪后，**stdout** 会打印三行（默认 `core.log_level = "info"`）：

```text
{"ts":"…","level":"info","logger":"vade.app","msg":"core identity loaded"}
pair_code=青松-AAAAAA purpose=browser expires_in=600s
vade: first-run pairing — enter this code in the app/browser (…)
```

**码在第 2 行**，形状 `2 个汉字-6 位 base32`，有效期 `600s`，**一次性**。
把 `log_level` 调到 `warning` 会让第 1 行消失、码落到第 1 行 —— 脚本里认 `pair_code=` 前缀，
别写死行号。

## 3. 起客户端，带上四个环境变量

**图形化配对入口尚未实现**（三端都没有输入配对码的首屏，见 `docs/ALPHA.md` 的 A1-2），
所以码只能用环境变量传给客户端进程：

```bash
cd D:/HermesWorkspace/Vade-client

VADE_SOURCE=real \
VADE_HOST=127.0.0.1 \
VADE_PORT=8443 \
VADE_PAIR_CODE='青松-AAAAAA' \
  gradle :composeApp:run
```

- `VADE_SOURCE` 必须正好是 `real`；缺它客户端走假数据，**完全不尝试连接**。
- `VADE_PORT` 要和 `[core].listen` 的端口一致。
- 变量名是 `VADE_PAIR_CODE`（不是 `VADE_PAIRED_CODE` / `VADE_PAIRCODE`）。
- Android 走 intent extras：`vade.source` / `vade.host` / `vade.port` / `vade.pair_code`。

## 4. 怎么确认真的连上了

| 看哪 | 连上 | 没连上 |
| --- | --- | --- |
| core 侧 HTTP | `curl -s http://127.0.0.1:8443/healthz` → `{"ok": true, "version": "0.1.0", "policy_mode": "…"}` | 连不上 → 端口/绑错地址 |
| core 侧请求 | 出现 `POST /ui/session` 与 `/ws` 升级 | 只有 `/healthz` 或什么都没有 |
| 客户端列表 | core 里的真会话；顶部不再有「未连接 core」横幅 | 仍是 6 条 fixtures + 假数据横幅 |

⚠️ **连不上时客户端不会报错**：`sessionsError` / `sessionsPhase` 已算出来，但**没有任何 UI
消费它们**（只有 `SessionsLoader.kt` / `UiState.kt` 定义与测试在读），所以界面是「一排空列表」
而不是错误横幅。判断连没连上要看 core 侧，不要等客户端弹错误。

## 5. 四个常见误解

这几条曾被当成「未实现 / 坏掉了」，实际都已生效；写在这里省得下一个人重踩：

| 误解 | 事实 | 依据 |
| --- | --- | --- |
| `python -m vade` 不铸配对码 | **每次启动都铸并打印**（`data_dir` 里已有身份也一样） | `core/vade/__main__.py` 的 `announce_pair_code`，在监听就绪后被调用 |
| `/healthz` 应该报 `allow_all` | 只有配了 `[dev].allow_all = true` 才是 `allow_all`，否则是 `default`；**两者都不影响配对** | `core/vade/__main__.py`：`policy_mode="allow_all" if app.config.get("dev.allow_all") else "default"` |
| 客户端窗口里全是假数据 → 没接线 | 不带环境变量才是假数据（fixtures 路径）；带 `VADE_SOURCE=real` 走真源 | 客户端 `composeApp/src/desktopMain/kotlin/vade/ui/Main.kt`：`source = sessionSourceFromEnvironment()` |
| 配对应该在客户端有输入框 | 没有；唯一通道是 `VADE_PAIR_CODE` 环境变量 | `docs/ALPHA.md` A1-2；客户端 `…/ui/data/SourceConfig.desktop.kt` |

## 6. 下一步

- 配对机制、码的规则、故障排查 → `docs/PAIRING.md`
- 客户端 UI 现状 → 客户端仓 `docs/QUICKSTART.md` 的「当前状态」表
  （注意：该表里「UI 数据仍是 fixtures」「真连接 UI 未接线」两行、以及其 §6 的
  「`python -m vade` 不铸码」结论**已过期**，以本文 §5 为准）
